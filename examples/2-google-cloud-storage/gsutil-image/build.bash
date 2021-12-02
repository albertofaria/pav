#!/usr/bin/env bash
# ---------------------------------------------------------------------------- #

set -o errexit -o pipefail -o nounset -o xtrace

version=5.2

docker build \
    --build-arg "version=${version}" \
    --tag "gsutil:${version}" \
    "$( dirname "$0" )"

# ---------------------------------------------------------------------------- #
