#!/bin/sh -ex

PRODUCT=${1}
RELEASE=${2}

gosu couchbase sh -c "git config --global user.name \"${git_user_name}\" && \
                      git config --global user.email \"${git_user_email}\" && \
                      git config --global color.ui auto && \
                      /app/jenkins/run_missing_commit_check.sh ${PRODUCT} ${RELEASE}"
