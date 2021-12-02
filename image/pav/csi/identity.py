# ---------------------------------------------------------------------------- #

from __future__ import annotations

from google.protobuf.wrappers_pb2 import BoolValue
from grpc.aio import ServicerContext  # type: ignore

from pav.csi.common import log_grpc
from pav.csi.spec.csi_pb2 import (
    GetPluginCapabilitiesRequest,
    GetPluginCapabilitiesResponse,
    GetPluginInfoRequest,
    GetPluginInfoResponse,
    PluginCapability,
    ProbeRequest,
    ProbeResponse,
)
from pav.csi.spec.csi_pb2_grpc import IdentityServicer

# ---------------------------------------------------------------------------- #


class Identity(IdentityServicer):

    provisioner_name: str

    def __init__(self, provisioner_name: str) -> None:
        super().__init__()
        self.provisioner_name = provisioner_name

    @log_grpc
    async def GetPluginInfo(
        self, request: GetPluginInfoRequest, context: ServicerContext
    ) -> GetPluginInfoResponse:

        return GetPluginInfoResponse(
            name=self.provisioner_name, vendor_version="0.0.0"
        )

    @log_grpc
    async def GetPluginCapabilities(
        self, request: GetPluginCapabilitiesRequest, context: ServicerContext
    ) -> GetPluginCapabilitiesResponse:

        return GetPluginCapabilitiesResponse(
            capabilities=[
                PluginCapability(
                    service=PluginCapability.Service(
                        type=PluginCapability.Service.CONTROLLER_SERVICE
                    )
                ),
            ]
        )

    @log_grpc
    async def Probe(
        self, request: ProbeRequest, context: ServicerContext
    ) -> ProbeResponse:

        return ProbeResponse(ready=BoolValue(value=True))


# ---------------------------------------------------------------------------- #
