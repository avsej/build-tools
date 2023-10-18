#!/bin/bash -e

function usage() {
    echo
    echo "$0 -r <release> -v <version> -b <build-number>"
    echo "   [-t <product>] [-s <suffix> ] [-c <private | public | only>]"
    echo "   [-p <platforms>] [-l]"
    echo "where:"
    echo "  -r: release code name (defaults to <version>)"
    echo "  -v: version number; eg. 7.0.0"
    echo "  -b: build number to release"
    echo "  -t: product; defaults to couchbase-server"
    echo "  -s: version suffix, eg. 'MP1' or 'beta' [optional]"
    echo "  -c: how to handle CE builds [optional]. Legal values are:"
    echo "        private: to make it non-downloadable (default for server)"
    echo "        public: CE builds are downloadable (default except server)"
    echo "        only: only upload CE (implies public)"
    echo "        none: do NOT upload CE [optional]"
    echo "  -p: specific platforms to upload. By default uploads all platforms."
    echo "      Pass -p multiple times for multiple platforms [optional]"
    echo "  -l: Push it to live (production) s3. Default is to push to staging [optional]"
    echo
}

LIVE=false

while getopts "r:v:V:b:t:s:c:p:lh?" opt; do
    case $opt in
        r) RELEASE=$OPTARG;;
        v) VERSION=$OPTARG;;
        b) BLD_NUM=$OPTARG;;
        t) PRODUCT=$OPTARG;;
        s) SUFFIX=$OPTARG;;
        c) COMMUNITY=$OPTARG;;
        p) PLATFORMS+=("$OPTARG");;
        l) LIVE=true;;
        h|?) usage
           exit 0;;
        *) echo "Invalid argument $opt"
           usage
           exit 1;;
    esac
done

if [ "x${PRODUCT}" = "x" ]; then
    echo "Product not set"
    usage
    exit 2
fi

if [ "x${COMMUNITY}" = "x" ]; then
    if [ "${PRODUCT}" = "couchbase-server" ]; then
        COMMUNITY=private
    else
        # Set to "public" which means "all files" - appropriate for majority of
        # products that don't have EE/CE split. Also only Server has the
        # "sometimes we release CE, sometimes we don't" thing.
        COMMUNITY=public
    fi
fi

if [ ${#PLATFORMS[@]} -eq 0 ]; then
    if [ "${PRODUCT}" = "couchbase-server" ]; then
        # For Server 7.2.4 and later, only release generic linux, macos, windows
        if [ "7.2.4" = $(printf "7.2.4\n${VERSION}" | sort -n | head -1) ]; then
            PLATFORMS=(linux macos windows)
        else
            PLATFORMS=(ubuntu amzn2 centos debian rhel macos oel suse windows linux)
        fi
    else
        # This is a "no-op" platform wildcard, since all filenames will have
        # at least one - in them.
        PLATFORMS=(-)
    fi
fi

if [ "x${VERSION}" = "x" ]; then
    echo "Version not set"
    usage
    exit 2
fi

if [ "x${RELEASE}" = "x" ]; then
    RELEASE=${VERSION}
fi

if [ "x${BLD_NUM}" = "x" ]; then
    echo "Build number not set"
    usage
    exit 2
fi

if ! [[ $VERSION =~ ^[0-9]*\.[0-9]*\.[0-9]*$ ]]; then
    echo "Version number format incorrect. Correct format is A.B.C where A, B and C are integers."
    exit 3
fi

if ! [[ $BLD_NUM =~ ^[0-9]*$ ]]; then
    echo "Build number must be an integer"
    exit 3
fi

RELEASES_MOUNT=/releases
if [ ! -e ${RELEASES_MOUNT} ]; then
    echo "'releases' directory is not mounted"
    exit 4
fi

LB_MOUNT=/latestbuilds
if [ ! -e ${LB_MOUNT} ]; then
    echo "'latestbuilds' directory is not mounted"
    exit 4
fi


# Compute target filename components
if [ -z "$SUFFIX" ]; then
    RELEASE_DIRNAME=$VERSION
    FILENAME_VER=$VERSION
else
    RELEASE_DIRNAME=$VERSION-$SUFFIX
    FILENAME_VER=$VERSION-$SUFFIX
fi

# Note: bizarrely, "mkdir" on zz-lightweight, when creating directories
# on the NFS-mounted /releases, will create them with either permissions
# 770 or 755, seemingly at random. We use "mkdir -m 755" throughout here
# to ensure the desired permissions. Also, "mkdir -p -m 755" only
# ensures that the final directory component is 755; any intermediate
# directories it creates get a random permission. Therefore we create
# any intermediate directories explicitly here. We still use "mkdir -p"
# just to avoid needing to check whether they exist first.

# Compute root destination directories, creating them as necessary.
if [[ "$LIVE" = "true" ]]; then
    S3_ROOT=s3://packages.couchbase.com/releases
    RELEASE_ROOT=${RELEASES_MOUNT}
else
    S3_ROOT=s3://packages-staging.couchbase.com/releases
    RELEASE_ROOT=${RELEASES_MOUNT}/staging
    mkdir -p -m 755 ${RELEASE_ROOT}
fi

# Add product super-directory, if not couchbase-server. Create if
# necessary.
if [[ "${PRODUCT}" != "couchbase-server" ]]; then
    RELEASE_DIRNAME=${PRODUCT}/${RELEASE_DIRNAME}
    mkdir -p -m 755 ${RELEASE_ROOT}/${PRODUCT}
fi

# Create destination directory
RELEASE_DIR=${RELEASE_ROOT}/${RELEASE_DIRNAME}
mkdir -p -m 755 $RELEASE_DIR
S3_DIR=${S3_ROOT}/${RELEASE_DIRNAME}

upload()
{
    echo ::::::::::::::::::::::::::::::::::::::

    if [[ "$COMMUNITY" == "private" ]]; then
        echo Uploading ${RELEASE_DIRNAME} ...
        echo CE are uploaded PRIVATELY ...
        perm_arg="private"
        aws s3 sync ${UPLOAD_TMP_DIR} ${S3_DIR}/ --acl private --exclude "*" --include "*community*"
        aws s3 sync ${UPLOAD_TMP_DIR} ${S3_DIR}/ --acl public-read --exclude "*community*"
    else
        echo Uploading ${RELEASE_DIRNAME} ...
        aws s3 sync ${UPLOAD_TMP_DIR} ${S3_DIR}/ --acl public-read
    fi

    echo Copying ${UPLOAD_TMP_DIR} to ${RELEASE_DIR} ...
    rsync -a ${UPLOAD_TMP_DIR}/* ${RELEASE_DIR}/
}

OPWD=`pwd`
finish() {
    cd $OPWD
    exit
}
trap finish EXIT

if [ ! -e ${LB_MOUNT}/${PRODUCT}/$RELEASE/$BLD_NUM ]; then
    echo "Given build doesn't exist: ${LB_MOUNT}/${PRODUCT}/$RELEASE/$BLD_NUM"
    exit 5
fi

# Prepare files to be uploaded
UPLOAD_TMP_DIR=/tmp/${PRODUCT}-${RELEASE}-${BLD_NUM}
rm -rf ${UPLOAD_TMP_DIR} && mkdir -p ${UPLOAD_TMP_DIR}

cd ${LB_MOUNT}/${PRODUCT}/$RELEASE/$BLD_NUM

# Copy manifest and notices.txt to release directory
cp ${PRODUCT}-${VERSION}-${BLD_NUM}-manifest.xml ${UPLOAD_TMP_DIR}/${PRODUCT}-${VERSION}-manifest.xml
NOTICES_FILE=blackduck/${PRODUCT}-${VERSION}-${BLD_NUM}-notices.txt
if [ -e ${NOTICES_FILE} ]; then
    cp ${NOTICES_FILE} ${UPLOAD_TMP_DIR}/${PRODUCT}-${VERSION}-notices.txt
fi

for platform in ${PLATFORMS[@]}
do
    # Have to disable bash's filename expansion here (with "set -f") - doesn't
    # seem to be any other way to pass EXTRA_FIND_ARGS to find without bash
    # expanding the glob wildcards first. (EXTRA_FIND_ARGS isn't used anymore,
    # but leaving it in case we need it again in future.)
    for file in $( \
        set -f; \
        find . -maxdepth 1 \( \
            -name *${PRODUCT}*${platform}* \
            -not -name *unsigned* \
            -not -name *unnotarized* \
            -not -name *asan* \
            -not -name *.md5 \
            -not -name *.sha26 \
            -not -name *.properties \
            -not -name *properties.json \
            -not -name *-manifest.xml \
            -not -name *-source.tar.gz \
            ${EXTRA_FIND_ARGS} \
        \) )
    do
        # Remove leading "./" from find results
        file=${file/.\//}

        # "artifact" is the filename with the build number stripped out
        artifact=${file/$VERSION-$BLD_NUM/$FILENAME_VER}

        # Handle various options for CE artifacts
        if [[ "$COMMUNITY" == "none" && "${artifact}" =~ "community" ]]; then
            echo "COMMUNITY=none set, skipping ${artifact}"
            continue
        fi
        if [[ "$COMMUNITY" == "only" && ! "${artifact}" =~ "community" ]]; then
            echo "COMMUNITY=only set, skipping ${artifact}"
            continue
        fi

        # Copy artifact to release mirror and create checksum file
        echo Copying ${artifact}
        cp $file ${UPLOAD_TMP_DIR}/${artifact}
        echo Creating fresh sha256sum file for ${artifact}
        sha256sum ${UPLOAD_TMP_DIR}/${artifact} | cut -c1-64 > ${UPLOAD_TMP_DIR}/${artifact}.sha256
    done
done

upload
rm -rf ${UPLOAD_TMP_DIR}
