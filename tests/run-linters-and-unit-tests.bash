#!/usr/bin/env bash
# ---------------------------------------------------------------------------- #

set -o errexit -o pipefail -o nounset

# ---------------------------------------------------------------------------- #
# check usage

(( $# == 0 )) || {
    >&2 echo "Usage: $0"
    exit 2
}

# ---------------------------------------------------------------------------- #
# build 'test' target of the PaV image

cd "$( dirname "$0" )"
docker image build --target=test ../image

# ---------------------------------------------------------------------------- #
