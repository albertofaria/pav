#!/usr/bin/env bash
# ---------------------------------------------------------------------------- #

set -o errexit -o pipefail -o nounset -o xtrace

# check usage and change to script directory

(( $# == 0 ))
cd "$( dirname "$0" )"

# create temporary directory

venv="$( mktemp -d )"
trap 'rm -fr "${venv}"' EXIT

# create and activate python venv

python3 -m venv "${venv}"
# shellcheck disable=SC1091
source "${venv}/bin/activate"

# install dependencies

python -m pip install grpcio-tools==1.41.0 mypy-protobuf==3.0.0

# download CSI proto file

mkdir -p "${venv}/pav/csi/spec"
curl -Lo "${venv}/pav/csi/spec/csi.proto" \
    https://github.com/container-storage-interface/spec/raw/v1.5.0/csi.proto

# generate bindings

python -m grpc_tools.protoc -I"${venv}" "${venv}/pav/csi/spec/csi.proto" \
    --python_out=. --grpc_python_out=. --mypy_out=. --mypy_grpc_out=.

# adjust gRPC typing stubs to have async servicers

function subst() { perl -i -p0e "$@" pav/csi/spec/csi_pb2_grpc.pyi; }

subst 's/import grpc/import grpc.aio  # type: ignore/'
subst 's/@abc[.]abstractmethod\n    def/@abc.abstractmethod\n    async def/g'
subst 's/grpc[.]ServicerContext/grpc.aio.ServicerContext/g'

# ---------------------------------------------------------------------------- #
