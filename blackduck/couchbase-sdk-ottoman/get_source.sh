#!/bin/bash -ex

PRODUCT=$1
RELEASE=$2
VERSION=$3
BLD_NUM=$4

TAG=v$VERSION
git clone ssh://git@github.com/couchbaselabs/node-ottoman.git
pushd node-ottoman
if git rev-parse --verify --quiet $TAG >& /dev/null
then
    echo "Tag $TAG exists, checking it out"
    git checkout $TAG
else
    echo "No tag $TAG, assuming master"
fi

# remove directory that is not part of the released product
rm -rf docusaurus

popd
