#!/bin/sh -e

PRODUCT=$1
shift
RELEASE=$1
shift

if [ "${DEBUG}" = "true" ]; then
    DEBUG="--debug"
fi

if [ "${PROJECT}" != "" ]; then
    PROJECT="--project ${PROJECT}"
fi

reporef_dir=/data/reporef
metadata_dir=/data/metadata

# Update reporef. Note: This script requires /home/couchbase/reporef
# to exist in two places, with that exact path:
#  - The Docker host (currently mega3), so it's persistent
#  - Mounted in the Jenkins agent container, so this script can be run
#    to update it
# It is then mounted into the container running this script as
# /data/reporef Remember that when passing -v arguments to "docker run"
# from within a container (like the Jenkins agent), the path is
# interpreted by the Docker daemon, so the path must exist on the
# Docker *host*.
if [ -z "$(ls -A $reporef_dir)" ]
then
  echo "reporef dir is empty"
  exit 1
fi

cd "${reporef_dir}"

if [ ! -e .repo ]; then
    # This only pre-populates the reporef for Server git code. Might be able
    # to do better in future.
    repo init -u ssh://git@github.com/couchbase/manifest -g all -m branch-master.xml
fi
repo sync --jobs=6 > /dev/null

cd "${metadata_dir}"

# This script also expects a /home/couchbase/check_missing_commits to be
# available on the Docker host, and mounted into the Jenkins agent container
# at /data/metadata, for basically the same reasons as above.
# Note: I tried initially to use a named Docker volume for this
# to avoid needing to create the directory on the host; however, Docker kept
# changing the ownership of the mounted directory to root in that case.

rm -rf product-metadata
git clone ssh://git@github.com/couchbase/product-metadata > /dev/null

release_dir=product-metadata/${PRODUCT}/missing_commits/${RELEASE}
if [ ! -e "${release_dir}" ]; then
    echo "Cannot run check for unknown release ${RELEASE}!"
    exit 1
fi

# Sync Gateway annoyingly has a different layout and repository for manifests
# compared to the rest of the company. In particular they re-use "default.xml"
# changing the release name, which is hard for us to track. Therefore we just
# hard-code default.xml here. It would take more effort to handle checking for
# missing commits in earlier releases.
if [ "x${PRODUCT}" = "xsync_gateway" ]; then
    manifest_repo=ssh://git@github.com/couchbase/sync_gateway
    current_manifest=manifest/default.xml
    echo
    echo
    echo @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
    echo "ALERT: product is sync_gateway, so forcing manifest to default.xml"
    echo @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
    echo
else
    manifest_repo=ssh://git@github.com/couchbase/manifest
    current_manifest=${PRODUCT}/${RELEASE}.xml
fi

echo
echo "Checking for missing commits in release ${RELEASE}...."
echo

cd ${release_dir}

set +ex
failed=0

for previous_manifest in $(cat previous-manifests.txt); do
    echo "Checking ${previous_manifest}"
    PYTHONUNBUFFERED=1 find_missing_commits \
        $DEBUG \
        $PROJECT \
        --manifest_repo ${manifest_repo} \
        --reporef_dir ${reporef_dir} \
        -i ok-missing-commits.txt \
        -m merge-projects.txt \
        ${PRODUCT} \
        ${previous_manifest} \
        ${current_manifest}
    failed=$(($failed + $?))
done

exit $failed
