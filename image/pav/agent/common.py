# ---------------------------------------------------------------------------- #

from __future__ import annotations

import re
from asyncio import CancelledError, create_task, sleep
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import Path
from traceback import format_exc
from types import SimpleNamespace
from typing import Any, Optional

from kubernetes_asyncio.client import (  # type: ignore
    ApiClient,
    CoreV1Api,
    V1Node,
    V1PersistentVolume,
    V1PersistentVolumeClaim,
    V1Pod,
    V1StorageClass,
)

from pav.shared.config import AGENT_HANDLER_RETRY_DELAY, DOMAIN
from pav.shared.kubernetes import (
    atomically_modify_persistent_volume_claim,
    atomically_modify_pod,
    watch_all_persistent_volume_claims,
    watch_all_pods,
)
from pav.shared.provisioner import (
    Provisioner,
    VolumeCreationConfig,
    VolumeDeletionConfig,
    VolumeStagingConfig,
    VolumeUnstagingConfig,
    VolumeValidationConfig,
)
from pav.shared.states import (
    VolumeProvisioningState,
    VolumeProvisioningStates,
    VolumeStagingState,
    VolumeStagingStates,
)
from pav.shared.util import log

# ---------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VolumeProvisioningContext:

    api_client: ApiClient

    provisioner: Provisioner
    pvc: V1PersistentVolumeClaim
    sc: V1StorageClass

    @staticmethod
    async def from_pvc(
        api_client: ApiClient, pvc_name: str, pvc_namespace: str
    ) -> VolumeProvisioningContext:

        pvc = await CoreV1Api(
            api_client
        ).read_namespaced_persistent_volume_claim(
            name=pvc_name, namespace=pvc_namespace
        )

        # SC may already have been deleted, so we retrieve the version stored in
        # the PVC annotations

        sc = api_client.deserialize(
            response=SimpleNamespace(
                data=pvc.metadata.annotations[f"{DOMAIN}/storage-class"]
            ),
            response_type="V1StorageClass",
        )

        assert type(sc) is V1StorageClass

        provisioner = await Provisioner.get(
            api_client=api_client, provisioner_name=sc.provisioner
        )

        return VolumeProvisioningContext(
            api_client=api_client,
            provisioner=provisioner,
            pvc=pvc,
            sc=sc,
        )

    async def eval_dynamic_validation_config(self) -> VolumeValidationConfig:
        return await self.provisioner.eval_dynamic_validation_config(
            storage_class=self.sc, persistent_volume_claim=self.pvc
        )

    async def eval_creation_config(self) -> VolumeCreationConfig:
        return await self.provisioner.eval_creation_config(
            storage_class=self.sc, persistent_volume_claim=self.pvc
        )

    async def eval_deletion_config(self) -> VolumeDeletionConfig:
        return await self.provisioner.eval_deletion_config(
            storage_class=self.sc, persistent_volume_claim=self.pvc
        )

    async def set_state(
        self,
        state: VolumeProvisioningState,
        *,
        handler_node_name: Optional[str] = None,
    ) -> None:
        def modifier(pvc: V1PersistentVolumeClaim) -> None:

            deletion_requested = (
                f"{DOMAIN}/deletion-requested" in pvc.metadata.annotations
            )

            new_state = state

            if isinstance(new_state, VolumeProvisioningStates.Created):

                if deletion_requested:
                    new_state = VolumeProvisioningStates.LaunchDeletionPod()

            elif isinstance(new_state, VolumeProvisioningStates.CreationFailed):

                pvc.metadata.finalizers.remove(f"{DOMAIN}/delete-volume")

                if deletion_requested:
                    new_state = VolumeProvisioningStates.Deleted()

            elif isinstance(new_state, VolumeProvisioningStates.Deleted):

                pvc.metadata.finalizers.remove(f"{DOMAIN}/delete-volume")

            pvc.metadata.annotations |= {f"{DOMAIN}/state": new_state.to_json()}

            if pvc.metadata.labels is None:
                pvc.metadata.labels = {}

            if handler_node_name is None:
                pvc.metadata.labels.pop(f"{DOMAIN}/handler-node", None)
            else:
                pvc.metadata.labels |= {
                    f"{DOMAIN}/handler-node": handler_node_name
                }

        await atomically_modify_persistent_volume_claim(
            api_client=self.api_client,
            name=self.pvc.metadata.name,
            namespace=self.pvc.metadata.namespace,
            modifier=modifier,
        )


VolumeProvisioningHandler = Callable[
    [VolumeProvisioningContext, Any], Coroutine[Any, Any, None]
]


async def handle_volume_provisioning(
    api_client: ApiClient,
    handlers: Mapping[type[VolumeProvisioningState], VolumeProvisioningHandler],
    *,
    handler_node_name: Optional[str] = None,
) -> None:

    while True:

        try:
            await _handle_volume_provisioning(
                api_client, handler_node_name, handlers
            )
        except CancelledError:
            pass
        except:
            log(format_exc())
            await sleep(AGENT_HANDLER_RETRY_DELAY.total_seconds())


async def _handle_volume_provisioning(
    api_client: ApiClient,
    handler_node_name: Optional[str],
    handlers: Mapping[type[VolumeProvisioningState], VolumeProvisioningHandler],
) -> None:

    # PVC UID --> PVC
    latest_state: dict[str, V1PersistentVolumeClaim] = {}

    # UIDs of PVCs for which there is a managing task
    has_task: set[str] = set()

    async def callback(pvc: V1PersistentVolumeClaim, exists: bool) -> None:

        if exists:

            latest_state[pvc.metadata.uid] = pvc

            if pvc.metadata.uid not in has_task:
                has_task.add(pvc.metadata.uid)
                create_task(manage_pvc(pvc.metadata.uid))

        else:

            del latest_state[pvc.metadata.uid]

    async def manage_pvc(pvc_uid: str) -> None:

        prev_state: Optional[VolumeProvisioningState] = None

        while True:

            try:

                pvc = latest_state.get(pvc_uid)

                if pvc is None:
                    break  # PVC no longer exists

                state = VolumeProvisioningState.from_json(
                    pvc.metadata.annotations[f"{DOMAIN}/state"]
                )

                if state == prev_state:
                    break  # state hasn't changed

                handler = handlers.get(type(state))

                if handler is None:
                    break  # no handler for current state

                log(f"Running handler for state {state} of PVC {pvc_uid}...")

                context = await VolumeProvisioningContext.from_pvc(
                    api_client=api_client,
                    pvc_name=pvc.metadata.name,
                    pvc_namespace=pvc.metadata.namespace,
                )

                await handler(context, state)

                prev_state = state

            except CancelledError:

                break  # cancelled

            except:

                # something failed, retry after a delay

                log(f"Error while managing PVC {pvc_uid}:\n{format_exc()}")
                await sleep(AGENT_HANDLER_RETRY_DELAY.total_seconds())

        has_task.remove(pvc_uid)

    label_selector = f"{DOMAIN}/provisioner"

    if handler_node_name is not None:
        label_selector += f",{DOMAIN}/handler-node={handler_node_name}"

    await watch_all_persistent_volume_claims(
        api_client=api_client, label_selector=label_selector, callback=callback
    )


# ---------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VolumeStagingContext:

    api_client: ApiClient

    provisioner: Provisioner
    pvc: V1PersistentVolumeClaim
    pv: V1PersistentVolume
    node: V1Node
    client_pod: V1Pod

    target_path_in_host: Path
    read_only: bool

    @staticmethod
    async def from_client_pod(
        api_client: ApiClient, client_pod: V1Pod, pvc_uid: str, node_name: str
    ) -> VolumeStagingContext:

        api = CoreV1Api(api_client)
        prefix = f"{DOMAIN}/{pvc_uid}"

        pvc = await api.read_namespaced_persistent_volume_claim(
            name=client_pod.metadata.annotations[f"{prefix}-pvc-name"],
            namespace=client_pod.metadata.annotations[
                f"{prefix}-pvc-namespace"
            ],
        )

        pv = await api.read_persistent_volume(name=pvc.spec.volume_name)

        provisioner = await Provisioner.get(
            api_client=api_client, provisioner_name=pv.spec.csi.driver
        )

        node = await api.read_node(name=node_name)

        target_path_in_host = Path(
            client_pod.metadata.annotations[f"{prefix}-target-path-in-host"]
        )

        read_only = {"true": True, "false": False}[
            client_pod.metadata.annotations[f"{prefix}-read-only"]
        ]

        return VolumeStagingContext(
            api_client=api_client,
            provisioner=provisioner,
            pvc=pvc,
            pv=pv,
            node=node,
            client_pod=client_pod,
            target_path_in_host=target_path_in_host,
            read_only=read_only,
        )

    async def eval_staging_config(self) -> VolumeStagingConfig:
        return await self.provisioner.eval_staging_config(
            persistent_volume_claim=self.pvc,
            persistent_volume=self.pv,
            node=self.node,
            read_only=self.read_only,
        )

    async def eval_unstaging_config(self) -> VolumeUnstagingConfig:
        return await self.provisioner.eval_unstaging_config(
            persistent_volume_claim=self.pvc,
            persistent_volume=self.pv,
            node=self.node,
            read_only=self.read_only,
        )

    async def set_state(self, state: VolumeStagingState) -> None:
        def modifier(client_pod: V1Pod) -> None:

            prefix = f"{DOMAIN}/{self.pvc.metadata.uid}"

            unstaging_requested = (
                f"{prefix}-unstaging-requested"
                in client_pod.metadata.annotations
            )

            new_state = state

            if isinstance(new_state, VolumeStagingStates.Staged):

                if unstaging_requested:
                    new_state = VolumeStagingStates.RemoveStagingPod(
                        staging_pod_namespace=new_state.staging_pod_namespace
                    )

            elif isinstance(new_state, VolumeStagingStates.StagingFailed):

                client_pod.metadata.finalizers.remove(
                    f"{prefix}-unstage-volume"
                )

                if unstaging_requested:
                    new_state = VolumeStagingStates.Unstaged()

            elif isinstance(new_state, VolumeStagingStates.Unstaged):

                client_pod.metadata.finalizers.remove(
                    f"{prefix}-unstage-volume"
                )

            client_pod.metadata.annotations |= {
                f"{prefix}-state": new_state.to_json()
            }

        await atomically_modify_pod(
            api_client=self.api_client,
            name=self.client_pod.metadata.name,
            namespace=self.client_pod.metadata.namespace,
            modifier=modifier,
        )


VolumeStagingHandler = Callable[
    [VolumeStagingContext, Any], Coroutine[Any, Any, None]
]


async def handle_volume_staging(
    api_client: ApiClient,
    handler_node_name: str,
    handlers: Mapping[type[VolumeStagingState], VolumeStagingHandler],
) -> None:

    while True:

        try:
            await _handle_volume_staging(
                api_client, handler_node_name, handlers
            )
        except CancelledError:
            pass
        except:
            log(format_exc())
            await sleep(AGENT_HANDLER_RETRY_DELAY.total_seconds())


async def _handle_volume_staging(
    api_client: ApiClient,
    handler_node_name: str,
    handlers: Mapping[type[VolumeStagingState], VolumeStagingHandler],
) -> None:

    pvc_uid_pattern = re.compile(
        fr"^{DOMAIN}/(\w{{8}}-\w{{4}}-\w{{4}}-\w{{4}}-\w{{12}})-"
    )

    # (pod UID, PVC UID) --> Pod
    latest_state: dict[tuple[str, str], V1Pod] = {}

    # (pod UID, PVC UID) pairs for which there is a managing task
    has_task: set[tuple[str, str]] = set()

    async def callback(pod: V1Pod, exists: bool) -> None:

        pvc_uid_list = {
            m.group(1)
            for key in (pod.metadata.annotations or {})
            if (m := pvc_uid_pattern.match(key))
        }

        for pvc_uid in pvc_uid_list:

            key = (pod.metadata.uid, pvc_uid)

            if exists:

                latest_state[key] = pod

                if key not in has_task:
                    has_task.add(key)
                    create_task(manage_pod_and_pvc(pod.metadata.uid, pvc_uid))

            else:

                del latest_state[key]

    async def manage_pod_and_pvc(pod_uid: str, pvc_uid: str) -> None:

        prev_state: Optional[VolumeStagingState] = None

        while True:

            try:

                pod = latest_state.get((pod_uid, pvc_uid))

                if pod is None:
                    break  # Pod no longer exists

                state = VolumeStagingState.from_json(
                    pod.metadata.annotations[f"{DOMAIN}/{pvc_uid}-state"]
                )

                if state == prev_state:
                    break  # state hasn't changed

                handler = handlers.get(type(state))

                if handler is None:
                    break  # no handler for current state

                log(
                    f"Running handler for state {state} of mount of PVC"
                    f" {pvc_uid} on Pod {pod_uid}..."
                )

                context = await VolumeStagingContext.from_client_pod(
                    api_client=api_client,
                    client_pod=pod,
                    pvc_uid=pvc_uid,
                    node_name=handler_node_name,
                )

                await handler(context, state)

                prev_state = state

            except CancelledError:

                break  # cancelled

            except:

                # something failed, retry after a delay

                log(
                    f"Error while managing mount of PVC {pvc_uid} on Pod"
                    f" {pod_uid}:\n{format_exc()}"
                )

                await sleep(AGENT_HANDLER_RETRY_DELAY.total_seconds())

        has_task.remove((pod_uid, pvc_uid))

    await watch_all_pods(
        api_client=api_client,
        label_selector=f"{DOMAIN}/uses-volumes",
        field_selector=f"spec.nodeName={handler_node_name}",
        callback=callback,
    )


# ---------------------------------------------------------------------------- #
