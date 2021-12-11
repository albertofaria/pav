# ---------------------------------------------------------------------------- #

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from grpc import StatusCode  # type: ignore
from grpc.aio import ServicerContext  # type: ignore
from kubernetes_asyncio.client import (  # type: ignore
    ApiClient,
    CoreV1Api,
    V1PersistentVolume,
    V1PersistentVolumeClaim,
    V1Pod,
)

from pav.csi.common import (
    ensure,
    ensure_provisioner_is_not_being_deleted,
    log_grpc,
)
from pav.csi.spec.csi_pb2 import (
    NodeGetCapabilitiesRequest,
    NodeGetCapabilitiesResponse,
    NodeGetInfoRequest,
    NodeGetInfoResponse,
    NodePublishVolumeRequest,
    NodePublishVolumeResponse,
    NodeUnpublishVolumeRequest,
    NodeUnpublishVolumeResponse,
    VolumeCapability,
)
from pav.csi.spec.csi_pb2_grpc import NodeServicer
from pav.shared.config import DOMAIN
from pav.shared.kubernetes import (
    ClusterObjectRef,
    ObjectRef,
    atomically_modify_pod,
    watch_pod,
)
from pav.shared.states import (
    VolumeStagingState,
    VolumeStagingStateAfterStaged,
    VolumeStagingStates,
)
from pav.shared.util import ensure_empty_or_singleton, ensure_singleton

# ---------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VolumeStageRef:
    client_pod: ObjectRef
    pvc: ObjectRef


# ---------------------------------------------------------------------------- #


class Node(NodeServicer):

    api_client: ApiClient
    provisioner_ref: ClusterObjectRef
    node_name: str

    def __init__(
        self,
        api_client: ApiClient,
        provisioner_ref: ClusterObjectRef,
        node_name: str,
    ) -> None:

        self.api_client = api_client
        self.provisioner_ref = provisioner_ref
        self.node_name = node_name

    @log_grpc
    async def NodeGetInfo(
        self, request: NodeGetInfoRequest, context: ServicerContext
    ) -> NodeGetInfoResponse:

        return NodeGetInfoResponse(node_id=self.node_name)

    @log_grpc
    async def NodeGetCapabilities(
        self, request: NodeGetCapabilitiesRequest, context: ServicerContext
    ) -> NodeGetCapabilitiesResponse:

        return NodeGetCapabilitiesResponse(capabilities=[])

    @log_grpc
    async def NodePublishVolume(
        self, request: NodePublishVolumeRequest, context: ServicerContext
    ) -> NodePublishVolumeResponse:

        await ensure_provisioner_is_not_being_deleted(
            context, self.api_client, self.provisioner_ref
        )

        client_pod_ref = ObjectRef(
            name=request.volume_context["csi.storage.k8s.io/pod.name"],
            namespace=request.volume_context[
                "csi.storage.k8s.io/pod.namespace"
            ],
            uid=request.volume_context["csi.storage.k8s.io/pod.uid"],
        )

        pv = await self.get_pv(volume_id=request.volume_id)

        pvc = await CoreV1Api(
            self.api_client
        ).read_namespaced_persistent_volume_claim(
            name=pv.spec.claim_ref.name, namespace=pv.spec.claim_ref.namespace
        )

        assert pvc.metadata.uid == pv.spec.claim_ref.uid

        pvc_ref = ObjectRef(
            name=pv.spec.claim_ref.name,
            namespace=pv.spec.claim_ref.namespace,
            uid=pv.spec.claim_ref.uid,
        )

        await self.assert_publish_volume_request_matches_pv(
            request=request, pv=pv, pvc=pvc
        )

        await self.validate_publish_volume_request(
            context=context, request=request
        )

        await self.delegate_volume_staging_to_agent(
            context=context,
            client_pod_ref=client_pod_ref,
            pvc_ref=pvc_ref,
            target_path_in_host=request.target_path,
            read_only=request.readonly,
        )

        await self.wait_for_agent_to_stage_volume(
            context=context, client_pod_ref=client_pod_ref, pvc_ref=pvc_ref
        )

        return NodePublishVolumeResponse()

    async def get_pv(self, volume_id: str) -> V1PersistentVolume:

        # Unfortunately, the only field selectors valid for PVs are
        # metadata.name and metadata.namespace.

        api = CoreV1Api(self.api_client)

        persistent_volumes = await api.list_persistent_volume()
        assert not persistent_volumes.metadata._continue

        return ensure_singleton(
            pv
            for pv in persistent_volumes.items
            if pv.spec.csi.driver == self.provisioner_ref.name
            and pv.spec.csi.volume_handle == volume_id
        )

    async def assert_publish_volume_request_matches_pv(
        self,
        request: NodePublishVolumeRequest,
        pv: V1PersistentVolume,
        pvc: V1PersistentVolumeClaim,
    ) -> None:
        """
        Ensure that data in the request matches what is specified in the
        corresponding PV and PVC, which should always be the case.

        The agent will reconstruct the data in the request from the PV and PVC.
        """

        # check provisioner name

        assert self.provisioner_ref.name == pv.spec.csi.driver

        # check requested volume mode

        volume_mode = (
            "Filesystem"
            if request.volume_capability.WhichOneof("access_type") == "mount"
            else "Block"
        )

        assert volume_mode == pvc.spec.volume_mode

        # check requested access modes

        access_mode_strings = {
            VolumeCapability.AccessMode.SINGLE_NODE_WRITER: "ReadWriteOnce",
            VolumeCapability.AccessMode.MULTI_NODE_READER_ONLY: "ReadOnlyMany",
            VolumeCapability.AccessMode.MULTI_NODE_MULTI_WRITER: "ReadWriteMany",
        }

        access_mode = access_mode_strings[
            request.volume_capability.access_mode.mode
        ]

        assert access_mode in pvc.spec.access_modes

    async def validate_publish_volume_request(
        self, context: ServicerContext, request: NodePublishVolumeRequest
    ) -> None:

        if request.volume_capability.WhichOneof("access_type") == "mount":

            # We assume that these checks can only fail for
            # statically-provisioned volumes (since for dynamically-provisioned
            # volumes they would otherwise have failed during volume creation),
            # so the error messages only talk about the PV.

            await ensure(
                condition=(not request.volume_capability.mount.fs_type),
                context=context,
                code=StatusCode.INVALID_ARGUMENT,
                details=f"Must not specify 'PersistentVolume.spec.csi.fsType'",
            )

            await ensure(
                condition=(not request.volume_capability.mount.mount_flags),
                context=context,
                code=StatusCode.INVALID_ARGUMENT,
                details=(
                    f"Must not specify 'PersistentVolume.spec.mountOptions'"
                ),
            )

    async def delegate_volume_staging_to_agent(
        self,
        context: ServicerContext,
        client_pod_ref: ObjectRef,
        pvc_ref: ObjectRef,
        target_path_in_host: str,
        read_only: bool,
    ) -> None:
        async def modifier(client_pod: V1Pod) -> None:

            await ensure(
                condition=(client_pod.metadata.uid == client_pod_ref.uid),
                context=context,
                code=StatusCode.FAILED_PRECONDITION,
                details="Pod object was replaced",
            )

            if client_pod.metadata.annotations is None:
                client_pod.metadata.annotations = {}

            prefix = f"{DOMAIN}/{pvc_ref.uid}"

            state_json = client_pod.metadata.annotations.get(f"{prefix}-state")
            state = (
                VolumeStagingState.from_json(state_json) if state_json else None
            )

            unstaging_requested = (
                f"{prefix}-unstaging-requested"
                in client_pod.metadata.annotations
            )

            if state is None or isinstance(
                state, VolumeStagingStates.StagingFailed
            ):

                if client_pod.metadata.labels is None:
                    client_pod.metadata.labels = {}

                client_pod.metadata.labels |= {
                    f"{DOMAIN}/uses-provisioner-{self.provisioner_ref.uid}": "",
                    f"{DOMAIN}/uses-volume-{pvc_ref.uid}": "",
                    f"{DOMAIN}/uses-volumes": "",
                }

                if not unstaging_requested:

                    if client_pod.metadata.finalizers is None:
                        client_pod.metadata.finalizers = []

                    client_pod.metadata.finalizers.append(
                        f"{prefix}-unstage-volume"
                    )

                    client_pod.metadata.annotations |= {
                        f"{prefix}-state": (
                            VolumeStagingStates.LaunchStagingPod().to_json()
                        ),
                        f"{prefix}-pvc-name": pvc_ref.name,
                        f"{prefix}-pvc-namespace": pvc_ref.namespace,
                        f"{prefix}-target-path-in-host": target_path_in_host,
                        f"{prefix}-read-only": str(read_only).lower(),
                    }

        await atomically_modify_pod(
            api_client=self.api_client,
            name=client_pod_ref.name,
            namespace=client_pod_ref.namespace,
            modifier=modifier,
        )

    async def wait_for_agent_to_stage_volume(
        self,
        context: ServicerContext,
        client_pod_ref: ObjectRef,
        pvc_ref: ObjectRef,
    ) -> None:
        async def callback(client_pod: V1Pod) -> Optional[tuple[()]]:

            await ensure(
                condition=(client_pod.metadata.uid == client_pod_ref.uid),
                context=context,
                code=StatusCode.FAILED_PRECONDITION,
                details="Pod object was replaced",
            )

            prefix = f"{DOMAIN}/{pvc_ref.uid}"

            state = VolumeStagingState.from_json(
                client_pod.metadata.annotations[f"{prefix}-state"]
            )

            if isinstance(
                state,
                (
                    VolumeStagingStates.StagingFailed,
                    VolumeStagingStates.UnrecoverableFailure,
                ),
            ):

                await context.abort(
                    code=state.error_code, details=state.error_details
                )

            elif isinstance(state, VolumeStagingStates.Staged):

                return ()

            elif isinstance(state, VolumeStagingStateAfterStaged):

                # volume already started being unstaged after being staged
                await context.abort(code=StatusCode.ABORTED)

            return None

        await watch_pod(
            api_client=self.api_client,
            name=client_pod_ref.name,
            namespace=client_pod_ref.namespace,
            callback=callback,
        )

    @log_grpc
    async def NodeUnpublishVolume(
        self, request: NodeUnpublishVolumeRequest, context: ServicerContext
    ) -> NodeUnpublishVolumeResponse:

        # The target path includes the UIDs of the client pod and PVC, so it is
        # globally unique.

        stage_ref = await self.get_volume_stage_ref(request.target_path)

        if stage_ref is not None:
            await self.delegate_volume_unstaging_to_agent(context, stage_ref)
            await self.wait_for_agent_to_unstage_volume(context, stage_ref)

        return NodeUnpublishVolumeResponse()

    async def get_volume_stage_ref(
        self, target_path_in_host: str
    ) -> Optional[VolumeStageRef]:

        # get all pods on this node

        api = CoreV1Api(self.api_client)

        pods_in_node = await api.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={self.node_name}"
        )

        assert not pods_in_node.metadata._continue

        # find desired pod

        annotation_pattern = re.compile(
            fr"^{DOMAIN}/(\w{{8}}-\w{{4}}-\w{{4}}-\w{{4}}-\w{{12}})"
            fr"-target-path-in-host$"
        )

        def get_ref(pod: V1Pod) -> Optional[VolumeStageRef]:

            pvc_uid = ensure_empty_or_singleton(
                m.group(1)
                for (key, value) in (pod.metadata.annotations or {}).items()
                if (m := annotation_pattern.match(key))
                and value == target_path_in_host
            )

            if pvc_uid is None:
                return None

            return VolumeStageRef(
                client_pod=ObjectRef(
                    name=pod.metadata.name,
                    namespace=pod.metadata.namespace,
                    uid=pod.metadata.uid,
                ),
                pvc=ObjectRef(
                    name=pod.metadata.annotations[
                        f"{DOMAIN}/{pvc_uid}-pvc-name"
                    ],
                    namespace=pod.metadata.annotations[
                        f"{DOMAIN}/{pvc_uid}-pvc-namespace"
                    ],
                    uid=pvc_uid,
                ),
            )

        return ensure_empty_or_singleton(
            ref for pod in pods_in_node.items if (ref := get_ref(pod))
        )

    async def delegate_volume_unstaging_to_agent(
        self, context: ServicerContext, stage_ref: VolumeStageRef
    ) -> None:
        async def modifier(client_pod: V1Pod) -> None:

            await ensure(
                condition=(client_pod.metadata.uid == stage_ref.client_pod.uid),
                context=context,
                code=StatusCode.FAILED_PRECONDITION,
                details="Pod object was replaced",
            )

            prefix = f"{DOMAIN}/{stage_ref.pvc.uid}"

            state = VolumeStagingState.from_json(
                client_pod.metadata.annotations[f"{prefix}-state"]
            )

            client_pod.metadata.annotations |= {
                f"{prefix}-unstaging-requested": ""
            }

            if isinstance(state, VolumeStagingStates.Staged):

                client_pod.metadata.annotations |= {
                    f"{prefix}-state": VolumeStagingStates.RemoveStagingPod(
                        staging_pod_namespace=state.staging_pod_namespace
                    ).to_json()
                }

        await atomically_modify_pod(
            api_client=self.api_client,
            name=stage_ref.client_pod.name,
            namespace=stage_ref.client_pod.namespace,
            modifier=modifier,
        )

    async def wait_for_agent_to_unstage_volume(
        self, context: ServicerContext, stage_ref: VolumeStageRef
    ) -> None:
        async def callback(client_pod: V1Pod) -> Optional[tuple[()]]:

            await ensure(
                condition=(client_pod.metadata.uid == stage_ref.client_pod.uid),
                context=context,
                code=StatusCode.FAILED_PRECONDITION,
                details="Pod object was replaced",
            )

            state = VolumeStagingState.from_json(
                client_pod.metadata.annotations[
                    f"{DOMAIN}/{stage_ref.pvc.uid}-state"
                ]
            )

            if isinstance(
                state,
                (
                    VolumeStagingStates.Unstaged,
                    VolumeStagingStates.StagingFailed,
                    VolumeStagingStates.UnrecoverableFailure,
                ),
            ):

                return ()

            else:

                return None

        await watch_pod(
            api_client=self.api_client,
            name=stage_ref.client_pod.name,
            namespace=stage_ref.client_pod.namespace,
            callback=callback,
        )

    # We do not need to implement the remaining methods, but they are marked
    # abstract, so we do these assignments to keep mypy happy.

    NodeStageVolume = NodeServicer.NodeStageVolume
    NodeUnstageVolume = NodeServicer.NodeUnstageVolume
    NodeGetVolumeStats = NodeServicer.NodeGetVolumeStats
    NodeExpandVolume = NodeServicer.NodeExpandVolume


# ---------------------------------------------------------------------------- #
