#!/usr/bin/env bash
# ---------------------------------------------------------------------------- #

set -o errexit -o pipefail -o nounset -o xtrace

version=0.36.0

temp_dir="$( mktemp -d )"
trap 'rm -fr "${temp_dir}"' EXIT

git clone \
    --branch "v${version}" \
    --config advice.detachedHead=false \
    --depth 1 \
    https://github.com/GoogleCloudPlatform/gcsfuse.git \
    "${temp_dir}"

echo '
ENTRYPOINT [ "gcsfuse" ]' >> "${temp_dir}/Dockerfile"

docker build \
    --build-arg "GCSFUSE_VERSION=v${version}" \
    --tag "gcsfuse:${version}" \
    "${temp_dir}"

# ---------------------------------------------------------------------------- #
