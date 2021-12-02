#!/usr/bin/env bash
# ---------------------------------------------------------------------------- #

set -o errexit -o pipefail -o nounset -o xtrace

version=2.4.1

docker build \
    --build-arg "version=v${version}" \
    --tag "cryptsetup:${version}" \
    "$( dirname "$0" )"

# ---------------------------------------------------------------------------- #
