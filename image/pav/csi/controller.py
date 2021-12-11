# ---------------------------------------------------------------------------- #

from __future__ import annotations

import json
from typing import Optional

from grpc import StatusCode  # type: ignore
from grpc.aio import ServicerContext  # type: ignore
from kubernetes_asyncio.client import (  # type: ignore
    ApiClient,
    CoreV1Api,
    StorageV1Api,
    V1PersistentVolumeClaim,
    V1StorageClass,
)

from pav.csi.common import (
    ensure,
    ensure_provisioner_is_not_being_deleted,
    log_grpc,
)
from pav.csi.spec.csi_pb2 import (
    ControllerGetCapabilitiesRequest,
    ControllerGetCapabilitiesResponse,
    ControllerServiceCapability,
    CreateVolumeRequest,
    CreateVolumeResponse,
    DeleteVolumeRequest,
    DeleteVolumeResponse,
    Volume,
    VolumeCapability,
)
from pav.csi.spec.csi_pb2_grpc import ControllerServicer
from pav.shared.config import DOMAIN
from pav.shared.kubernetes import (
    ClusterObjectRef,
    ObjectRef,
    atomically_modify_persistent_volume_claim,
    parse_and_round_quantity,
    watch_persistent_volume_claim,
)
from pav.shared.states import (
    VolumeProvisioningState,
    VolumeProvisioningStateAfterCreated,
    VolumeProvisioningStates,
)

# ---------------------------------------------------------------------------- #


class Controller(ControllerServicer):

    api_client: ApiClient
    provisioner_ref: ClusterObjectRef

    def __init__(
        self, api_client: ApiClient, provisioner_ref: ClusterObjectRef
    ) -> None:
        self.api_client = api_client
        self.provisioner_ref = provisioner_ref

    @log_grpc
    async def ControllerGetCapabilities(
        self,
        request: ControllerGetCapabilitiesRequest,
        context: ServicerContext,
    ) -> ControllerGetCapabilitiesResponse:

        return ControllerGetCapabilitiesResponse(
            capabilities=[
                ControllerServiceCapability(
                    rpc=ControllerServiceCapability.RPC(
                        type=(
                            ControllerServiceCapability.RPC.CREATE_DELETE_VOLUME
                        )
                    )
                )
            ]
        )

    @log_grpc
    async def CreateVolume(
        self, request: CreateVolumeRequest, context: ServicerContext
    ) -> CreateVolumeResponse:

        pv_name = request.parameters["csi.storage.k8s.io/pv/name"]
        pvc_name = request.parameters["csi.storage.k8s.io/pvc/name"]
        pvc_namespace = request.parameters["csi.storage.k8s.io/pvc/namespace"]

        await ensure_provisioner_is_not_being_deleted(
            context, self.api_client, self.provisioner_ref
        )

        pvc = await CoreV1Api(
            self.api_client
        ).read_namespaced_persistent_volume_claim(
            name=pvc_name, namespace=pvc_namespace
        )

        sc = await StorageV1Api(self.api_client).read_storage_class(
            name=pvc.spec.storage_class_name
        )

        await self.assert_create_volume_request_matches_pvc_and_sc(
            request=request, pvc=pvc, sc=sc
        )

        await self.validate_create_volume_request(
            context=context, request=request
        )

        pvc_ref = ObjectRef(
            name=pvc_name, namespace=pvc_namespace, uid=pvc.metadata.uid
        )

        await self.delegate_volume_creation_to_agent(
            context=context, pvc_ref=pvc_ref, sc=sc
        )

        state = await self.wait_for_agent_to_create_volume(
            context=context, pvc_ref=pvc_ref
        )

        return CreateVolumeResponse(
            volume=Volume(
                volume_id=state.handle,
                capacity_bytes=state.capacity,
                volume_context=sc.parameters or {},  # to copy parameters to PV
            )
        )

    async def assert_create_volume_request_matches_pvc_and_sc(
        self,
        request: CreateVolumeRequest,
        pvc: V1PersistentVolumeClaim,
        sc: V1StorageClass,
    ) -> None:
        """
        Ensure that data in the request matches what is specified in the
        corresponding PVC and SC, which should always be the case.

        The agent will reconstruct the data in the request from the PVC and SC.
        """

        # check provisioner name

        assert self.provisioner_ref.name == sc.provisioner

        # check requested volume mode

        [volume_mode] = {
            (
                "Filesystem"
                if cap.WhichOneof("access_type") == "mount"
                else "Block"
            )
            for cap in request.volume_capabilities
        }

        assert volume_mode == pvc.spec.volume_mode

        # check requested access modes

        access_mode_strings = {
            VolumeCapability.AccessMode.SINGLE_NODE_WRITER: "ReadWriteOnce",
            VolumeCapability.AccessMode.MULTI_NODE_READER_ONLY: "ReadOnlyMany",
            VolumeCapability.AccessMode.MULTI_NODE_MULTI_WRITER: "ReadWriteMany",
        }

        access_modes = {
            access_mode_strings[cap.access_mode.mode]
            for cap in request.volume_capabilities
        }

        assert access_modes == set(pvc.spec.access_modes)

        # check requested minimum and maximum capacity

        min_capacity = parse_and_round_quantity(
            pvc.spec.resources.requests["storage"]
        )

        max_capacity = parse_and_round_quantity(
            (pvc.spec.resources.limits or {}).get("storage", 0)
        )

        assert request.capacity_range.required_bytes == min_capacity
        assert request.capacity_range.limit_bytes == max_capacity

        # check parameters

        request_params = request.parameters.items()
        sc_params = (sc.parameters or {}).items()

        assert set(request_params).issuperset(sc_params)

    async def validate_create_volume_request(
        self, context: ServicerContext, request: CreateVolumeRequest
    ) -> None:

        for capability in request.volume_capabilities:

            if capability.WhichOneof("access_type") == "mount":

                await ensure(
                    condition=(not capability.mount.fs_type),
                    context=context,
                    code=StatusCode.INVALID_ARGUMENT,
                    details=(
                        f"Must not specify 'StorageClass.paramters["
                        f'"csi.storage.k8s.io/fstype"]\''
                    ),
                )

                await ensure(
                    condition=(not capability.mount.mount_flags),
                    context=context,
                    code=StatusCode.INVALID_ARGUMENT,
                    details=f"Must not specify 'StorageClass.mountOptions'",
                )

    async def delegate_volume_creation_to_agent(
        self, context: ServicerContext, pvc_ref: ObjectRef, sc: V1StorageClass
    ) -> None:

        sc_json = json.dumps(self.api_client.sanitize_for_serialization(sc))

        async def modifier(pvc: V1PersistentVolumeClaim) -> None:

            await ensure(
                condition=(pvc.metadata.uid == pvc_ref.uid),
                context=context,
                code=StatusCode.FAILED_PRECONDITION,
                details="PersistentVolumeClaim object was replaced",
            )

            if pvc.metadata.annotations is None:
                pvc.metadata.annotations = {}

            # must store the StorageClass as it can be deleted prior to the PVC
            pvc.metadata.annotations[f"{DOMAIN}/storage-class"] = sc_json

            state_json = pvc.metadata.annotations.get(f"{DOMAIN}/state")
            state = (
                VolumeProvisioningState.from_json(state_json)
                if state_json
                else None
            )

            deletion_requested = (
                f"{DOMAIN}/deletion-requested" in pvc.metadata.annotations
            )

            if state is None or isinstance(
                state, VolumeProvisioningStates.CreationFailed
            ):

                if pvc.metadata.labels is None:
                    pvc.metadata.labels = {}

                pvc.metadata.labels |= {
                    f"{DOMAIN}/provisioner": self.provisioner_ref.name
                }

                if not deletion_requested:

                    if pvc.metadata.finalizers is None:
                        pvc.metadata.finalizers = []

                    pvc.metadata.finalizers.append(f"{DOMAIN}/delete-volume")

                    pvc.metadata.annotations |= {
                        f"{DOMAIN}/state": (
                            VolumeProvisioningStates.LaunchValidationPod().to_json()
                        )
                    }

        await atomically_modify_persistent_volume_claim(
            api_client=self.api_client,
            name=pvc_ref.name,
            namespace=pvc_ref.namespace,
            modifier=modifier,
        )

    async def wait_for_agent_to_create_volume(
        self, context: ServicerContext, pvc_ref: ObjectRef
    ) -> VolumeProvisioningStates.Created:
        async def callback(
            pvc: V1PersistentVolumeClaim,
        ) -> Optional[VolumeProvisioningStates.Created]:

            await ensure(
                condition=(pvc.metadata.uid == pvc_ref.uid),
                context=context,
                code=StatusCode.FAILED_PRECONDITION,
                details="PersistentVolumeClaim object was replaced",
            )

            state = VolumeProvisioningState.from_json(
                pvc.metadata.annotations[f"{DOMAIN}/state"]
            )

            if isinstance(
                state,
                (
                    VolumeProvisioningStates.CreationFailed,
                    VolumeProvisioningStates.UnrecoverableFailure,
                ),
            ):

                await context.abort(
                    code=state.error_code, details=state.error_details
                )

            elif isinstance(state, VolumeProvisioningStates.Created):

                return state

            elif isinstance(state, VolumeProvisioningStateAfterCreated):

                # volume already started being deleted after being created
                await context.abort(code=StatusCode.ABORTED)

            return None

        return await watch_persistent_volume_claim(
            api_client=self.api_client,
            name=pvc_ref.name,
            namespace=pvc_ref.namespace,
            callback=callback,
        )

    @log_grpc
    async def DeleteVolume(
        self, request: DeleteVolumeRequest, context: ServicerContext
    ) -> DeleteVolumeResponse:

        # This RPC is only invoked _after_ the PVC is fully deleted, which only
        # happens after our finalizer is removed from it, which in turn only
        # happens after the controller agent fully deleted the volume. The
        # volume is thus already deleted, and we can return immediately here.

        return DeleteVolumeResponse()

    # We do not need to implement the remaining methods, but they are marked
    # abstract, so we do these assignments to keep mypy happy.

    # Note that Kubernetes never calls ValidateVolumeCapabilities().

    ControllerPublishVolume = ControllerServicer.ControllerPublishVolume
    ControllerUnpublishVolume = ControllerServicer.ControllerUnpublishVolume
    ValidateVolumeCapabilities = ControllerServicer.ValidateVolumeCapabilities
    ListVolumes = ControllerServicer.ListVolumes
    ControllerGetVolume = ControllerServicer.ControllerGetVolume
    GetCapacity = ControllerServicer.GetCapacity
    CreateSnapshot = ControllerServicer.CreateSnapshot
    DeleteSnapshot = ControllerServicer.DeleteSnapshot
    ListSnapshots = ControllerServicer.ListSnapshots
    ControllerExpandVolume = ControllerServicer.ControllerExpandVolume


# ---------------------------------------------------------------------------- #
