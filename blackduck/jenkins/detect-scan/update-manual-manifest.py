#!/usr/bin/env python3

import argparse
import collections
import dictdiffer
import json
import logging
import os
import pathlib
import pprint
import re
import requests
import shutil
import sys
import yaml

from abc import ABC, abstractmethod
from blackduck.HubRestApi import HubInstance
from enum import Enum
from urllib.parse import urlparse, quote


class ManifestWalker(ABC):
    """
    Walks a directory tree for black-duck-manifest files, calling abstract
    member functions for each
    """

    def __init__(self, project, version, dryrun):
        self.dryrun = dryrun
        self.name = project
        self.version = version


    def set_source_root(self, src_root):
        """
        Specifies the source root to search for manifests under.
        """

        self.src_root = src_root


    def start_manifest(self, manifest_file, yaml_manifest):
        """
        Called when each manifest is first loaded.
        """
        pass


    def end_manifest(self, manifest_file, yaml_manifest):
        """
        Called at the end of processing for a manifest.
        """
        pass


    def done_loading_manifests(self):
        """
        Called at the end of all manifest loading.
        """
        pass


    @abstractmethod
    def add_component(self, comp_name, comp_data):
        """
        Called for each component in any manifest. comp_data is the YAML
        representation of the component.
        """
        pass


    @abstractmethod
    def perform(self):
        """
        Perform the operation.
        """
        pass


    def add_manifests(self, for_project = None):
        """
        Searches the source root directory recurisvely for manifests named
        ${for_project}-black-duck-manifest.yaml, and loads them in.
        "for_project" will default to the project name of the ManifestWalker
        instance if not specified.
        """

        project = self.name if for_project is None else for_project
        logging.info(f"Searching for manifests for project {project}")

        manifest_filename = f"{project}-black-duck-manifest.yaml"
        manifest_count = 0
        for root, _, files in os.walk(self.src_root):
            if manifest_filename in files:
                manifest_count += 1
                self.add_manifest(os.path.join(root, manifest_filename))

        if manifest_count == 0:
            logging.warning(f"Loaded zero manifests for {project}!")
        else:
            logging.info(f"Loaded {manifest_count} manifests for {project}")

        # Only log the "done" message when we reach the bottom of this
        # routine for the default project
        if for_project is None:
            self.done_loading_manifests()


    def add_manifest(self, manifest_file):
        """
        Given the YAML file manifest, process any top-level keys.
        """

        manifest_file = pathlib.Path(manifest_file)
        logging.info(f"Loading input manifest {manifest_file}")
        with manifest_file.open() as m:
            manifest = yaml.safe_load(m)
        self.start_manifest(manifest_file, manifest)

        # Iterate through each top-level object in the manifest
        for key, value in manifest.items():
            match key:
                case "include-projects":
                    # include-projects object: recursively call back to
                    # add_manifests()
                    for other_project in value:
                        self.add_manifests(other_project)

                case "meta":
                    # No action to take here, just allow the field to exist.
                    pass

                case "components":
                    # Add each component to ongoing manifest
                    for (comp_name, comp_data) in value.items():
                        # If the value is an empty list, skip it (probably just
                        # there to keep the drift-detector happy).
                        if isinstance(comp_data, list) and len(comp_data) == 0:
                            continue

                        self.add_component(comp_name, comp_data)

                case _:
                    logging.error(
                        f"Unrecognized manifest key '{key}' "
                        f"in manifest file {manifest_file.name}!"
                    )
                    sys.exit(5)

        self.end_manifest(manifest_file, manifest)


class PruneSourceDirs(ManifestWalker):
    """
    Given a set of BD manifests, prunes any source directories referenced by
    those manifests.
    """

    def __init__(self, project, version, dryrun):

        super().__init__(project, version, dryrun)

        # Stack of manifest root directories
        self.mani_rootdir = None
        self.mani_rootdir_stack = []

        # Set of directories to prune, keyed by component name
        self.src_dirs = {}


    def start_manifest(self, manifest_file, yaml_manifest):
        """
        Store a YAML manifest on the stack, and compute the corresponding
        source root directory
        """

        # Push the current mani_rootdir (if any) onto stack
        self.mani_rootdir_stack.append(self.mani_rootdir)

        # Compute new root directory
        try:
            root_dir_str = yaml_manifest['meta']['src-root-dir']
        except KeyError:
            root_dir_str = '.'
        self.mani_rootdir = manifest_file.parent / root_dir_str


    def end_manifest(self, manifest_file, yaml_manifest):
        """
        Pop the mani_rootdir stack
        """

        self.mani_rootdir = self.mani_rootdir_stack.pop()


    def add_component(self, comp_name, comp_data):
        """
        Given a component name and YAML data, prepare to prune any related
        source directories as requested by 'src-path' values
        """

        src_path = comp_data.get("src-path", None)
        if src_path is None:
            return
        self.src_dirs[comp_name] = \
            (self.mani_rootdir / src_path).resolve()


    def perform(self):
        """
        Actually delete all the stored source directories
        """

        logging.info(f"Pruning {len(self.src_dirs)} source directories")

        # Sort the paths by number of directories, so we delete depth-first
        for component, src_path in sorted(
            self.src_dirs.items(),
            key=lambda x: 0 - len(x[1].parents)
        ):
            path = pathlib.Path(src_path)
            if not path.exists():
                logging.fatal(f"Source path {src_path} does not exist!")
                sys.exit(3)
            logging.info(f"Pruning {src_path} for component {component}")
            if self.dryrun:
                logging.info(f"(skipping {src_path} due to dryrun)")
            else:
                shutil.rmtree(path)


class UpdateComponents(ManifestWalker):
    """
    Loads the current set of manually-added components. Given a set of BD
    manifests, applies changes to Black Duck to make the set of manually-added
    components match the manifests.
    """

    # Match a version number that maybe starts with a "v", followed by only
    # digits and dots.
    v_re = re.compile(r"^(v?)([.0-9]+)$")

    # Match a version number that looks like a date
    date_re = re.compile(r"^([0-9]{4})\.([0-9]{1,2})\.([0-9]{1,2})$")

    # There are two important data structures in this class: bom_comp_map and
    # manifest. bom_comp_map represents the current state in Black Duck (when
    # the program is first run), while manifest represents the desired state as
    # specified by the BD manifests. Both of these structures are in
    # "canonicalized" form, which is a dict.
    #
    # The keys of the dict are Black Duck component IDs; for example,
    # eae20828-18b8-478f-83b3-4a058748a28b is the ID for "fmtlib/fmt".
    #
    #  - for bom_comp_map, these keys will be directly from Black Duck.
    #  - for manifest, these keys will be from the "bd-id" field in the manifest
    #
    # The values of the dict are dicts with the following entries:
    #    "versions": a Python set of strings, eg. {"7.1.3", "7.1.4"}. These will
    #        always be in a canonical form, as defined by canonicalize_version.
    #    "bd-name": the lowercased name for the component. This is only used for
    #        human-readable logging output.
    #    "license-approved": True or False, meaning whether the component's
    #        license is approved for distribution. For bom_comp_map, this is
    #        based on the "reviewState" field in the BOM. For manifest, this is
    #        based on the "license-approved" key in the manifest, and may be
    #        None if this key is not specified; in this case, the current value
    #        from the BOM is left unchanged.
    #
    #  - for bom_comp_map, "bd-name" will always be the componentName directly
    #    from Black Duck (lowercased)
    #  - for manifest, "bd-name" will be the "bd-name" field from the manifest
    #    (lowercased) or, if that doesn't exist, the key of the component
    #    itself.

    def __init__(self, credentials_file, project, version, dryrun):

        super().__init__(project, version, dryrun)

        logging.info(f"Preparing to update components for {project} {version}")

        # Connect to Black Duck
        if credentials_file is None:
            logging.error("Must provide --credentials for operation=update!")
            sys.exit(3)
        if not os.path.exists(credentials_file):
            logging.error(f"Credentials file {credentials_file} does not exist!")
            sys.exit(3)
        with open(credentials_file, "r") as c:
            creds = json.load(c)
        self.hub = HubInstance(
            creds['url'],
            creds['username'],
            creds['password'],
            insecure=True
        )
        self.comp_base = self.hub.get_apibase() + "/components/"

        # Save Black Duck's data about the project-version
        logging.debug(f"Looking up project {project}")
        self.project = self.hub.get_project_by_name(project)
        logging.debug(f"Looking up project version {version}")
        self.project_version = self.hub.get_version_by_name(self.project, version)

        # Initialize bom_comp_map and manifest schema
        self.manifest = collections.defaultdict(lambda: { "versions": set() })
        self.bom_comp_map = collections.defaultdict(
            lambda: { "versions": set(), "license-approved": False }
        )

        # Other globally-used structures, populated or initialized in
        # _load_manual_components() or _load_bd_aliases()
        self.bom_comp_ids = dict()
        self.bom_components = None
        self.bd_comp_id_aliases = {}

        # "Fallback versions" are those we experimentally determined to be
        # necessary and saved in bd_aliases.yaml. These should be 1:1.
        self.bd_comp_version_fallbacks = collections.defaultdict(str)

        # "Alt-canonical versions" are alternate spellings of canonical version
        # names that are sometimes used by the Knowledgebase. By definition,
        # passing any of these version names to canonicalize_version() should
        # return the canonical version name.
        self.bd_alt_canonical_versions = collections.defaultdict(set)

        # Load BD component alias list
        self._load_bd_aliases()

        # Load all manually-added components currently in Black Duck
        self._load_manual_bom_components()


    def _load_bd_aliases(self):
        """
        Annoyingly, Black Duck component IDs do not seem to be constant.
        Sometimes without warning they can change, or be aliased to another. In
        some cases we can keep track of what it used to be and what it reports
        now, so we can translate the IDs we get from Black Duck to the ID in our
        manifests. It's important that we use the canonical ID when calling the
        REST API, because things such as search filters don't work if you use
        the aliased component ID.

        Also, sometimes we want to use a new component-version that isn't in the
        Black Duck Knowledgebase yet. In that case, we need to keep a list of
        "fallback" versions, which we will use in place of those new versions
        until the new version appears in the Knowledgebase.
        """

        script_dir = pathlib.Path(__file__).resolve().parent
        alias_filepath = script_dir / "bd_aliases.yaml"
        if not alias_filepath.exists():
            return
        with open(alias_filepath) as aliases_file:
            aliases = yaml.safe_load(aliases_file)

        for comp_id, comp_data in aliases.items():
            comp_name = comp_data.get("bd-name", "<unknown component name>")

            # Create reverse mapping from alias IDs to canonical ID
            for alias_id in comp_data.get("bd-id-aliases", []):
                self.bd_comp_id_aliases[alias_id] = comp_id

            # Create mapping from <component_id>::<version> to
            # the specified fallback version
            for canon_ver, fallback_ver in comp_data.get(
                "fallback-versions", {}
            ).items():
                canon_ver = self.canonicalize_version(
                    comp_name, comp_id, canon_ver
                )
                self.bd_comp_version_fallbacks[
                    f"{comp_id}::{canon_ver}"
                ] = fallback_ver

        logging.debug(
            f"Loaded {len(self.bd_comp_id_aliases)} "
            "Black Duck component aliases and "
            f"{len(self.bd_comp_version_fallbacks)} "
            "component fallback versions"
        )


    def _get_manual_components(self):
        """
        Returns the current "Manually Added" components for the current project-
        version.

        Adapted from hub-rest-api-python/blackduck/Projects.py so we can add
        filters.
        """

        url = self.hub.get_link(self.project_version, "components")
        headers = self.hub.get_headers()
        headers['Accept'] = 'application/vnd.blackducksoftware.bill-of-materials-6+json'
        filter_opts = "bomMatchType:manually_added"
        response = requests.get(
            url,
            headers = headers,
            params = { "limit": 1000, "filter": filter_opts },
            verify = not self.hub.config['insecure']
        )
        jsondata = response.json()
        return jsondata.get("items", [])


    def _load_manual_bom_components(self):
        """
        Reads in set of current manually-added components for project-version
        and store in self.bom_components. Then convert to canonicalized form in
        self.bom_comp_map. Also populate self.bom_comp_ids.
        """

        # Initialize self.bom_components with report from Black Duck
        logging.info(
            f"Retrieving current manual BOM for {self.name} {self.version}"
        )
        self.bom_components = self._get_manual_components()
        logging.debug(f"Found {len(self.bom_components)} manual components")

        # Canonicalize component list into bom_comp_map, and populate
        # bom_comp_ids. See top-level comment for discussion of schema
        # of bom_comp_map.
        for comp in self.bom_components:
            comp_url = comp['component']
            comp_id = urlparse(comp_url).path.rsplit('/', 1)[1]
            comp_name = comp['componentName'].lower()
            comp_version_name = self.canonicalize_version(
                comp_name, comp_id, comp.get('componentVersionName', "")
            )
            reviewed = comp['reviewStatus'] == "REVIEWED"

            self.bom_comp_map[comp_id]["bd-name"] = comp_name
            self.bom_comp_map[comp_id]["versions"].add(comp_version_name)

            # We use |= here so that if *any* version of the component is
            # "reviewed", the whole component will be shown as "reviewed". This
            # is a bit of a cheat since technically different versions of a
            # component could have different licenses, but handling that would
            # make this program far more complex. This "license-approved"
            # feature is only for those relatively few components which have
            # suspect licenses but we ship anyway; it feels unlikely that we'll
            # have a product that depends on two different versions of such a
            # component, AND that those versions will have licenses that differ
            # in a way we care about.
            self.bom_comp_map[comp_id]["license-approved"] |= reviewed

            # Also canonicalize self.bom_components version names - might be
            # referenced by remove_component() later.
            comp['componentVersionName'] = comp_version_name

            # We also keep a map of lowercased full component names to
            # component ID.
            self.bom_comp_ids[comp_name] = comp_id

        logging.debug(f"Final bom_comp_map: {pprint.pformat(self.bom_comp_map)}")


    def _find_component_version(self, comp_name, comp_id, version):
        """
        Looks up a component-version in the Knowledgebase via the BD REST API,
        and returns a component_version_url. Returns None if not found.
        """

        component_url = self.comp_base + comp_id

        # Sadly the BD search API doesn't like some legit characters like +, but
        # we can use _ as a single-character wildcard
        safe_version = re.sub(r'[+]', '_', version)
        versions_url = f"{component_url}/versions?q=versionName:{quote(safe_version)}&limit=100"
        logging.debug(f"Searching for version {version} of {comp_name}: {versions_url}")
        versions = self.hub.execute_get(versions_url).json().get('items', [])
        logging.debug(f"Found {len(versions)} items")

        # Ensure one of those found versions is an exact match.
        for ver_entry in versions:
            if ver_entry['versionName'] == version:
                return ver_entry['_meta']['href']

        logging.debug(f"Found no matching version!")
        return None


    def add_component(self, comp_name, comp_data):
        """
        Given a component name and YAML data, store the important information
        internally in canonicalized form. See top-level comment for discussion
        of schema of manifest.
        """

        # Values are dicts with possible keys 'versions', 'bd-id',
        # 'bd-name', and 'license-approved'.
        versions = comp_data.get('versions', [])
        license_approved = comp_data.get('license-approved')
        comp_name = comp_data.get('bd-name', comp_name).lower()
        comp_id = comp_data.get('bd-id', None)
        # Translate our manifest component ID to BD alias if necessary
        comp_id = self.bd_comp_id_aliases.get(comp_id, comp_id)

        # Canonicalize version names and ensure they're strings (YAML might
        # read them as floats). Pass save_alts=True here, since these are
        # the manifest versions for which we might need to find slightly
        # different spellings in the Knowledgebase later. Also, handle
        # fallback versions now, so that DictDiffer doesn't attempt to
        # handle them later.
        versions = [
            self.fallback_version_if_necessary(
                comp_name, comp_id, self.canonicalize_version(
                    comp_name, comp_id, str(v), save_alts=True
                )
            )
            for v in versions
        ]

        logging.debug(
            f"Adding component {comp_name} ({comp_id}) with "
            f"versions {versions} to manifest"
        )
        self.manifest[comp_id]["bd-name"] = comp_name
        self.manifest[comp_id]["versions"].update(versions)
        self.manifest[comp_id]["license-approved"] = license_approved


    def _add_alt_canonical_version(self, comp_id, canon_ver, alt_ver):
        """
        For a specified canonical version name, add an alternate canonical
        version name to be checked in the Knowledgebase when a requested
        canonical version doesn't exist in the Knowledgebase
        """

        key = f"{comp_id}::{canon_ver}"
        logging.log(5,
            f"Saving '{alt_ver}' as a alt-canonical version for "
            f"'{canon_ver}' of component {comp_id}"
        )
        self.bd_alt_canonical_versions[key].add(alt_ver)


    def canonicalize_version(
        self, comp_name, comp_id, version, save_alts=False
    ):
        """
        Given a version name for a specified component name,
        canonicalize that version name. Normally this is just the
        version name unchanged, but a few components have inconsistent
        version naming in the Knowledgebase which leads to false
        matches/misses.

        Most of the canonicalizations are specific to certain
        components. For instance, many "certifi" packages
        (ca-certificates, etc.) use dates for versions, but
        Knowledgebase is inconsistent about using YYYY.MM.DD or YYYY.M.D
        for dates that have single-digit months or days. Our canonical
        form uses the 0-padded two-digit form.

        For all components, we also strip a leading "v" because a number
        of components in the Knowledgebase are inconsistent about this.

        This will also save alternate canonical versions to
        self.bd_alt_canonical_versions if "save_alts" is True. These are
        used by find_canonical_component_version() when looking up
        component-versions in the Knowledgebase.
        """

        # Strip any leading "v" before any other possible heuristics
        v_match = self.v_re.match(version)
        if v_match:
            # Canonical version has the "v" stripped. We'll add the v-version
            # as an alt later.
            version = v_match[2]

        # For several heuristics, compute both the canonical version name and
        # (optionally) a likely alternative name which can be used as an alt
        canon_ver = version
        alt_ver = version
        if comp_name.startswith("erlang"):
            # Strip any leading "OTP-"
            canon_ver = version[4:] if version.startswith("OTP-") else version
        elif comp_name.startswith("go programming language"):
            # Strip any leading "go"
            canon_ver = version[2:] if version.startswith("go") else version
        elif "certifi" in comp_name:
            match = self.date_re.match(version)
            if match:
                # Choose to have zero-padded month/day values, eg. "2023.05.07"
                # vs. "2023.5.7". Use the non-zero-padded version as an alt.
                # Certifi seems to use zero-padding; Conda tends to report those
                # versions without zero-padding; and Black Duck randomly has one
                # or the other.
                canon_ver = f"{match[1]:>04}.{match[2]:>02}.{match[3]:>02}"
                alt_ver = f"{int(match[1])}.{int(match[2])}.{int(match[3])}"

        # Ok, we have the canonical form of the version. That will allow the
        # diffing process to ignore any irrelevant changes (eg., manifest says
        # "certifi 2023.05.07" while BOM says "certifi 2023.5.7"). However it
        # still can't handle situations where the manifest says one thing, the
        # BOM says *nothing* (ie, this is a new component-version), and the
        # Knowledgebase has something other than the canonical version name. So
        # here we add a variety of possible alt-canonical versions name, when
        # requested to do so. These will ONLY be looked up in the Knowledgebase
        # if the canonical diffing fails, which should only happen when a new
        # component-version is being added, which is rare, so it's OK to include
        # several alterative options.
        if save_alts:
            for alt in (alt_ver, version):
                # If this originally was a potential "vX.Y.Z" version, add the
                # version with a "v" prefix as an alt
                if v_match:
                    self._add_alt_canonical_version(
                        comp_id, canon_ver, f"v{alt}"
                    )
                # If we heuristically generated an alt version different than
                # the canonical, add that also
                if canon_ver != alt:
                    self._add_alt_canonical_version(
                        comp_id, canon_ver, alt
                    )

        return canon_ver


    def fallback_version_if_necessary(
        self, comp_name, comp_id, version
    ):
        """
        Given a canonical version name for a specified component, return
        the version number that should be included in the input manifest
        - either that canonical version name, or the canonicalized
        version of the fallback version name, if none of the canonical
        options exist in the Knowledgebase.
        """

        fallback_version = self.bd_comp_version_fallbacks.get(
            f"{comp_id}::{version}"
        )
        if fallback_version is None:
            # No fallback versions available; do nothing
            return version

        # Figure out whether the BOM currently references the canonical version
        # name.
        bom_has_canonical = False
        comp_data = self.bom_comp_map.get(comp_id)
        if comp_data is not None:
            bom_has_canonical = version in comp_data["versions"]

        # If the BOM already has the canonical version, we don't need the fallback version.
        if bom_has_canonical:
            return version

        # Now we know that the BOM is not referencing the canonical version. The
        # BOM might have no information for this component at all (if this is an
        # entirely new component, or the first run of this script for a new
        # product-version), or it might be referencing the fallback version, or
        # it might only have other versions for this component. In any of those
        # cases, now we have to hit the Knowledgebase to see if the canonical
        # version name or any alt-canonical versions names have appeared since
        # the last time this script was run.
        component_version_url = self.find_canonical_component_version(
            comp_name, comp_id, version
        )
        if component_version_url is None:
            # Knowledgebase still doesn't know about the canonical version, so
            # the best we can do is pretend the manifest requested the fallback
            # version. But let's make sure the fallback version exists first.
            logging.debug(f"Canonicalizing fallback version {fallback_version}")
            canon_fallback_ver = self.canonicalize_version(
                comp_name, comp_id, fallback_version, save_alts=True
            )
            component_version_url = self.find_canonical_component_version(
                comp_name, comp_id, canon_fallback_ver
            )
            if component_version_url is None:
                logging.error(
                    f"Tried fallback version {fallback_version} for "
                    f"component {comp_name}, but it didn't exist either!!"
                )
                sys.exit(2)

            # OK, the fallback version DOES exist. It's possible it
            # exists as a different spelling; however, we always want to
            # store the *canonical* version in manfists and BOMs, so
            # comparisons work right. Return that canonical spelling.
            logging.info(
                f"Using canonical fallback version {canon_fallback_ver} "
                f"for component {comp_name}"
            )
            return canon_fallback_ver

        # If we got here, that means the BOM isn't referencing the canonical
        # version but that canonical version has appeared in the Knowledgebase!
        # Return the canonical version.
        return version


    def find_canonical_component_version(
        self, comp_name, comp_id, version
    ):
        """
        Given a canonical version name for a specified component (which
        is presumed not to exist in the Knowledgebase), see whether the
        Knowledgebase knows about it or any alternate canonical version
        names. This will require hitting the Knowledgebase REST API
        repeatedly, so it should only be called when strictly necessary.

        Returns the discovered component_version_url, or None if nothing
        was found.
        """

        # Try the canonical version first
        component_version_url = self._find_component_version(
            comp_name, comp_id, version
        )
        if component_version_url is not None:
            return component_version_url

        # Does this version have any potential alts?
        key = f"{comp_id}::{version}"
        alt_vers = self.bd_alt_canonical_versions.get(key, None)
        if alt_vers is None:
            # No known alt-canonical versions
            return None

        # See if any of the alt canonical versions are in the Knowledgebase.
        logging.debug(
            f"Version {version} of component {comp_name} "
            f"NOT in Knowledgebase; checking alt-canonical version names..."
        )
        for alt_ver in alt_vers:
            component_version_url = self._find_component_version(
                comp_name, comp_id, alt_ver
            )
            if component_version_url is None:
                logging.debug(f"Didn't find alt-canonical version {alt_ver}")
            else:
                logging.info(f"Found alt-canonical version {alt_ver}!")
                return component_version_url

        return None


    def add_component_version(self, comp_name, comp_id, version):
        """
        Adds a component-version to this project-version, which is presumed to
        not already exist in the BOM. If neither the component-version nor any
        applicable alt-canonical versions exist in the Knowledgebase, raises an
        error.
        """

        logging.info(
            f"Adding component to Black Duck: {comp_name} ({comp_id}) "
            f"version {version}")

        # First, try to find the canonical version name or any alt-canonical
        # version names in the Knowledgebase
        component_version_url = self.find_canonical_component_version(
            comp_name, comp_id, version
        )

        if component_version_url is None:
            logging.fatal(
                f"Could not find version {version} for {comp_name} (or any "
                "alt-canonical version names) in Knowledgebase!")
            sys.exit(3)
        logging.debug(f"Component version URL is {component_version_url}")

        # Sanity check: Did the component-version URL come back with a
        # different component ID? If so, likely a case where Black Duck
        # changed the canonical component ID. It's too late to correct
        # the situation now, and we need to keep a permanent historical
        # record in bd_aliases.yaml because those old component IDs may
        # not last forever in the Knowledgebase but will be in our git
        # history forever. But we can at least raise a useful error
        # message.
        if not comp_id in component_version_url:
            logging.fatal(
                f"ERROR! Component ID mismatch\n\n\n\n\n"
                f"Our black-duck-manifest references component ID {comp_id} "
                f"for component {comp_name}; however searching for version "
                f"{version} returned the URL {component_version_url} which "
                f"references a different component ID. This probably means "
                f"that the Black Duck Knowledgebase has changed the ID. "
                f"Please add a new entry in bd_aliases.yaml that maps this "
                f"new canonical ID to the older ID {comp_id}.\n\n\n\n"
            )

        # OK, finally add the component-version to the project-version
        # (unless dryrun is set).
        pv_components_url = self.hub.get_link(self.project_version, "components")
        if self.dryrun:
            logging.info("DRYRUN: not updating Black Duck")
        else:
            post_data = {'component': component_version_url}
            custom_headers = {
                'Content-Type': 'application/vnd.blackducksoftware.bomcomponent-1+json',
                'Accept': '*/*'
            }
            response = self.hub.execute_post(
                pv_components_url, post_data, custom_headers=custom_headers
            )
            response.raise_for_status()
            logging.debug(f"{comp_id} version {version} added successfully")


    def remove_component_version(self, comp_name, comp_id, version):
        """
        Removes a component-version from this project-version
        """

        logging.info(f"Removing component: {comp_name} ({comp_id}) version {version}")
        # We need the full URL of "this component-version in this
        # project-version". Conveniently enough those are in the information we
        # initially gathered from Black Duck in self.bom_components, so hunt it
        # down there. It should always be there since by definition we can't be
        # deleting something that didn't exist in the first place. This happens
        # rarely so a simple linear search is fine.
        comp_url = self.comp_base + comp_id
        for component in self.bom_components:
            if (component['component'] == comp_url and
                component.get('componentVersionName', "") == version):
                if self.dryrun:
                    logging.info("DRYRUN: found comp-version but not updating Black Duck")
                else:
                    response = self.hub.execute_delete(component['_meta']['href'])
                    response.raise_for_status()
                    logging.debug(f"{comp_id} version {version} deleted successfully")
                return

        logging.fatal(f"Failed to find component {comp_id} {version} to delete!!")
        sys.exit(1)


    def change_component_version_license_approved(self, target, approved):
        """
        Sets the 'reviewStatus' of all versions of the component in the project-
        version to "REVIEWED" / "NOT_REVIEWED" based on the value of "approved".
        """

        # "target" starts with the component_id.
        (comp_id, _) = target.split('.', maxsplit=1)
        comp_url = self.comp_base + comp_id

        # Have to re-read the current BOM to get all the entries for this
        # component. This is expensive; however it should only be executed when
        # there is an actual change to be made, due to DictDiffer.
        curr_items = self._get_manual_components()
        for item in curr_items:
            # Skip any components other than the comp_id we're looking for
            if item["component"] != comp_url:
                continue
            item["reviewStatus"] = "REVIEWED" if approved else "NOT_REVIEWED"
            logging.info(
                f"Setting {item['componentName']} "
                f"version {item['componentVersionName']} "
                f"to reviewStatus {item['reviewStatus']}"
            )
            if self.dryrun:
                logging.info("DRYRUN: not updating Black Duck")
            else:
                custom_headers = {
                    'Content-Type': 'application/vnd.blackducksoftware.bill-of-materials-6+json',
                    'Accept': 'application/vnd.blackducksoftware.bill-of-materials-6+json'
                }
                response = self.hub.execute_put(
                    item['_meta']['href'], item, custom_headers=custom_headers
                )
                response.raise_for_status()


    def done_loading_manifests(self):
        logging.debug(
            f"Final input manifest: {pprint.pformat(self.manifest)}"
        )


    def perform(self):
        """
        Compute the actions to make self.bom_comp_map look like added manifests,
        then execute each action
        """

        logging.debug("Computing actions")
        diff = dictdiffer.diff(self.bom_comp_map, self.manifest)

        func_map = {
            "add": self.add_component_version,
            "remove": self.remove_component_version
        }
        actions_taken = 0

        # dictdiffer gives us a list of diff actions, in a somewhat strange
        # bespoke format. Here we decode them and invoke the corresponding
        # actions.
        for (action, target, value) in diff:
            # "change" actions are kinda noisy - we'll log explicit messages
            # for them later if they're applicable
            if action != "change":
                logging.debug(f"Executing '{action}' '{target}' '{value}'")

            if action == "remove" or action == "add":
                if target == '':
                    # Adding or removing entire components. In this case we
                    # iteratively add/remove each component-version.
                    for (comp_id, data) in value:
                        for version in data["versions"]:
                            func_map[action](data["bd-name"], comp_id, version)
                            actions_taken += 1
                else:
                    # Adding or removing versions from an existing component
                    (comp_id, field) = target.split('.', maxsplit=1)
                    if field != "versions":
                        logging.error(f"Unknown field {field}!")
                        sys.exit(2)
                    # "value" is an array containing 1 tuple.
                    if len(value) != 1:
                        logging.error(f"Too many tuples in {value}!")
                        sys.exit(2)

                    # In this case, comp_id is in both bom_comp_map and manifest,
                    # so we can look up comp_name in either place.
                    comp_name = self.manifest[comp_id]["bd-name"]

                    # The first element in the tuple is always 0 (not
                    # sure why). The second element will be the set of
                    # versions to add/remove.
                    for version in value[0][1]:
                        func_map[action](comp_name, comp_id, version)
                        actions_taken += 1

            elif action == "change":
                if target.endswith(".bd-name"):
                    # Don't care if the bd-name from the manifest doesn't match
                    # Black Duck at this point - log a trace message
                    logging.log(5, f"Ignoring unnecessary change to bd-name")
                elif target.endswith(".license-approved"):
                    # "value" is a tuple of (old-value, new-value). We only
                    # care about new-value. If it's None, do nothing; otherwise
                    # change the BOM.
                    if value[1] is None:
                        # If "license-approved" is None, do nothing - this means
                        # that only black-duck-manifest.yaml files with explicit
                        # "license-approved" fields will change anything in
                        # Black Duck.
                        logging.log(5, f"Ignoring 'None' change to license-approved")
                    else:
                        self.change_component_version_license_approved(
                            target, value[1]
                        )
                        actions_taken +=1
                else:
                    logging.fatal(f"Unknown change field {target}!")

            else:
                logging.fatal(f"Unknown dictdiffer action {action}!")
                sys.exit(6)

        if actions_taken == 0:
            logging.info("Current components match manifest - no updates needed!")
        else:
            logging.info(f"Updated {actions_taken} components")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Get components from hub"
    )
    parser.add_argument('-d', '--debug', action='store_true',
        help="Produce debugging output")
    parser.add_argument('-c', '--credentials', type=str,
        help="Black Duck Hub credentials JSON file")
    parser.add_argument('-p', '--project', required=True,
        help="project from Black Duck server")
    parser.add_argument('-v', '--version', required=True,
        help="Version of <project>")
    parser.add_argument('-s', '--src-root', required=True, type=str,
        help="Root directory of source code (for finding manifests)")
    parser.add_argument('-o', '--operation', required=True,
        choices=['prune', 'update'],
        help="Whether to prune source dirs or update Black Duck")
    parser.add_argument('-n', '--dryrun', action='store_true',
        help="Dry run - don't update Black Duck, just report actions")
    args = parser.parse_args()

    if args.debug:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logging.basicConfig(
        stream=sys.stderr,
        format='%(threadName)s: %(asctime)s: %(levelname)s: %(message)s',
        level=log_level
    )
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    src_root = args.src_root
    if not os.path.isdir(src_root):
        logging.error(f"{src_root} is not a directory!")
        sys.exit(2)

    if args.operation == "prune":
        actor = PruneSourceDirs(args.project, args.version, args.dryrun)
    elif args.operation == "update":
        actor = UpdateComponents(
            args.credentials,
            args.project,
            args.version,
            args.dryrun
        )

    actor.set_source_root(src_root)
    actor.add_manifests()
    actor.perform()
