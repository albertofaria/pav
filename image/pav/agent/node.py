# ---------------------------------------------------------------------------- #

from __future__ import annotations

from asyncio import create_task
from collections.abc import Callable, Mapping
from typing import Any, Optional, Union

import kopf
from grpc import StatusCode  # type: ignore
from kubernetes_asyncio.client import ApiClient  # type: ignore

from pav.agent.common import (
    VolumeProvisioningContext,
    VolumeProvisioningHandler,
    VolumeStagingContext,
    VolumeStagingHandler,
    handle_volume_provisioning,
    handle_volume_staging,
)
from pav.shared.config import KOPF_FINALIZER
from pav.shared.kubernetes import parse_and_round_quantity
from pav.shared.pods import Pod
from pav.shared.states import (
    VolumeProvisioningState,
    VolumeProvisioningStates,
    VolumeStagingState,
    VolumeStagingStates,
)
from pav.shared.util import get_block_device_size

# ---------------------------------------------------------------------------- #


def run(node_name: str) -> None:

    # create Kubernetes API client object

    api_client = ApiClient()

    # define handlers

    registry = kopf.OperatorRegistry()

    _define_operator_handlers(registry, api_client, node_name)

    # run kopf

    kopf.configure()
    kopf.run(registry=registry, standalone=True, clusterwide=True)


# ---------------------------------------------------------------------------- #
# Operator lifecycle


def _define_operator_handlers(
    registry: kopf.OperatorRegistry, api_client: ApiClient, node_name: str
) -> None:
    @kopf.on.login(registry=registry)
    async def on_login(**kwargs: Any) -> Optional[kopf.ConnectionInfo]:
        return kopf.login_via_client(**kwargs)

    @kopf.on.startup(registry=registry)
    async def on_startup(
        settings: kopf.OperatorSettings, logger: kopf.Logger, **_: object
    ) -> None:

        # use custom finalizer

        settings.persistence.finalizer = KOPF_FINALIZER

        # don't create events

        settings.posting.enabled = False

        # launch task that watches PVCs

        provisioning_handlers = _define_volume_provisioning_handlers(node_name)

        provisioning_coroutine = handle_volume_provisioning(
            api_client, provisioning_handlers, handler_node_name=node_name
        )

        create_task(provisioning_coroutine)

        # launch task that watches client pods

        staging_coroutine = handle_volume_staging(
            api_client, node_name, _staging_handlers
        )

        create_task(staging_coroutine)


# ---------------------------------------------------------------------------- #
# Volume validation, creation, and deletion


def _define_volume_provisioning_handlers(
    node_name: str,
) -> Mapping[type[VolumeProvisioningState], VolumeProvisioningHandler]:

    handlers: dict[
        type[VolumeProvisioningState], VolumeProvisioningHandler
    ] = {}

    def add_handler(
        state_type: type[VolumeProvisioningState],
    ) -> Callable[[VolumeProvisioningHandler], VolumeProvisioningHandler]:
        def decorator(
            handler: VolumeProvisioningHandler,
        ) -> VolumeProvisioningHandler:
            nonlocal handlers
            handlers[state_type] = handler
            return handler

        return decorator

    @add_handler(VolumeProvisioningStates.AwaitValidationPod)
    async def handle_await_validation_pod(
        context: VolumeProvisioningContext,
        state: VolumeProvisioningStates.AwaitValidationPod,
    ) -> None:

        # get validation pod and corresponding /pav volume

        validation_pod = Pod(
            api_client=context.api_client,
            name=f"pav-volume-validation-pod-{context.pvc.metadata.uid}",
            namespace=state.validation_pod_namespace,
        )

        # wait until validation pod terminates

        if await validation_pod.wait_until_terminated():

            await context.set_state(
                VolumeProvisioningStates.RemoveValidationPod(
                    validation_pod_namespace=state.validation_pod_namespace
                ),
                handler_node_name=node_name,
            )

        else:

            error_message = (
                validation_pod.read_file_in_pav_volume("error") or ""
            ).strip()

            await context.set_state(
                VolumeProvisioningStates.RemoveValidationPodAfterFailure(
                    validation_pod_namespace=state.validation_pod_namespace,
                    error_code=StatusCode.INVALID_ARGUMENT,
                    error_details=f"Validation pod failed: {error_message}",
                ),
                handler_node_name=node_name,
            )

    @add_handler(VolumeProvisioningStates.RemoveValidationPod)
    @add_handler(VolumeProvisioningStates.RemoveValidationPodAfterFailure)
    async def handle_remove_validation_pod(
        context: VolumeProvisioningContext,
        state: Union[
            VolumeProvisioningStates.RemoveValidationPod,
            VolumeProvisioningStates.RemoveValidationPodAfterFailure,
        ],
    ) -> None:

        # get validation pod and corresponding /pav volume

        validation_pod = Pod(
            api_client=context.api_client,
            name=f"pav-volume-validation-pod-{context.pvc.metadata.uid}",
            namespace=state.validation_pod_namespace,
        )

        # delete validation pod and corresponding /pav volume

        await validation_pod.delete()

        # advance state

        if isinstance(state, VolumeProvisioningStates.RemoveValidationPod):
            await context.set_state(
                VolumeProvisioningStates.LaunchCreationPod()
            )
        else:
            await context.set_state(
                VolumeProvisioningStates.CreationFailed(
                    error_code=state.error_code,
                    error_details=state.error_details,
                )
            )

    @add_handler(VolumeProvisioningStates.AwaitCreationPod)
    async def handle_await_creation_pod(
        context: VolumeProvisioningContext,
        state: VolumeProvisioningStates.AwaitCreationPod,
    ) -> None:
        async def error(message: str) -> None:

            await context.set_state(
                VolumeProvisioningStates.RemoveCreationPodAfterFailure(
                    creation_pod_namespace=state.creation_pod_namespace,
                    error_code=StatusCode.INVALID_ARGUMENT,
                    error_details=f"Creation pod failed: {message.strip()}",
                ),
                handler_node_name=node_name,
            )

        # get creation pod and corresponding /pav volume

        creation_pod = Pod(
            api_client=context.api_client,
            name=f"pav-volume-creation-pod-{context.pvc.metadata.uid}",
            namespace=state.creation_pod_namespace,
        )

        # wait until validation pod terminates

        if not await creation_pod.wait_until_terminated():
            await error(creation_pod.read_file_in_pav_volume("error") or "")
            return

        # get volume handle

        handle_from_file = creation_pod.read_file_in_pav_volume("handle")

        if handle_from_file is not None:
            handle = handle_from_file
            if not handle:
                await error("Specified empty handle in file /pav/handle")
                return
        elif state.handle is not None:
            handle = state.handle
        else:
            handle = f"pvc-{context.pvc.metadata.uid}"

        # get volume capacity

        capacity_from_file = creation_pod.read_file_in_pav_volume("capacity")

        if capacity_from_file is not None:
            try:
                capacity = parse_and_round_quantity(capacity_from_file)
            except Exception as e:
                await error(
                    f"Specified invalid capacity in file /pav/capacity:"
                    f" {str(e)}"
                )
                return
        elif state.capacity is not None:
            capacity = state.capacity
        else:
            await error(
                "Creation pod didn't specify volume capacity in file"
                " /pav/capacity"
            )
            return

        # advance state

        await context.set_state(
            VolumeProvisioningStates.RemoveCreationPod(
                creation_pod_namespace=state.creation_pod_namespace,
                handle=handle,
                capacity=capacity,
            ),
            handler_node_name=node_name,
        )

    @add_handler(VolumeProvisioningStates.RemoveCreationPod)
    @add_handler(VolumeProvisioningStates.RemoveCreationPodAfterFailure)
    async def handle_remove_creation_pod(
        context: VolumeProvisioningContext,
        state: Union[
            VolumeProvisioningStates.RemoveCreationPod,
            VolumeProvisioningStates.RemoveCreationPodAfterFailure,
        ],
    ) -> None:

        # get creation pod and corresponding /pav volume

        creation_pod = Pod(
            api_client=context.api_client,
            name=f"pav-volume-creation-pod-{context.pvc.metadata.uid}",
            namespace=state.creation_pod_namespace,
        )

        # delete creation pod and corresponding /pav volume

        await creation_pod.delete()

        # advance state

        if isinstance(state, VolumeProvisioningStates.RemoveCreationPod):
            await context.set_state(
                VolumeProvisioningStates.Created(
                    handle=state.handle, capacity=state.capacity
                )
            )
        else:
            await context.set_state(
                VolumeProvisioningStates.LaunchDeletionPodAfterFailure(
                    error_code=state.error_code,
                    error_details=state.error_details,
                )
            )

    @add_handler(VolumeProvisioningStates.AwaitDeletionPod)
    @add_handler(VolumeProvisioningStates.AwaitDeletionPodAfterFailure)
    async def handle_await_deletion_pod(
        context: VolumeProvisioningContext,
        state: Union[
            VolumeProvisioningStates.AwaitDeletionPod,
            VolumeProvisioningStates.AwaitDeletionPodAfterFailure,
        ],
    ) -> None:

        # get deletion pod and corresponding /pav volume

        deletion_pod = Pod(
            api_client=context.api_client,
            name=f"pav-volume-deletion-pod-{context.pvc.metadata.uid}",
            namespace=state.deletion_pod_namespace,
        )

        # wait until deletion pod terminates

        if not await deletion_pod.wait_until_terminated():

            error_message = (
                deletion_pod.read_file_in_pav_volume("error") or ""
            ).strip()

            await context.set_state(
                VolumeProvisioningStates.UnrecoverableFailure(
                    error_code=StatusCode.INVALID_ARGUMENT,
                    error_details=f"Deletion pod failed: {error_message}",
                )
            )

            return

        # deletion pod terminated successfully, advance state

        if isinstance(state, VolumeProvisioningStates.AwaitDeletionPod):

            await context.set_state(
                VolumeProvisioningStates.RemoveDeletionPod(
                    deletion_pod_namespace=state.deletion_pod_namespace
                ),
                handler_node_name=node_name,
            )

        else:

            await context.set_state(
                VolumeProvisioningStates.RemoveDeletionPodAfterFailure(
                    deletion_pod_namespace=state.deletion_pod_namespace,
                    error_code=state.error_code,
                    error_details=state.error_details,
                ),
                handler_node_name=node_name,
            )

    @add_handler(VolumeProvisioningStates.RemoveDeletionPod)
    @add_handler(VolumeProvisioningStates.RemoveDeletionPodAfterFailure)
    async def handle_remove_deletion_pod(
        context: VolumeProvisioningContext,
        state: Union[
            VolumeProvisioningStates.RemoveDeletionPod,
            VolumeProvisioningStates.RemoveDeletionPodAfterFailure,
        ],
    ) -> None:

        # get deletion pod and corresponding /pav volume

        deletion_pod = Pod(
            api_client=context.api_client,
            name=f"pav-volume-deletion-pod-{context.pvc.metadata.uid}",
            namespace=state.deletion_pod_namespace,
        )

        # delete deletion pod and corresponding /pav volume

        await deletion_pod.delete()

        # advance state

        if isinstance(state, VolumeProvisioningStates.RemoveDeletionPod):
            await context.set_state(VolumeProvisioningStates.Deleted())
        else:
            await context.set_state(
                VolumeProvisioningStates.CreationFailed(
                    error_code=state.error_code,
                    error_details=state.error_details,
                )
            )

    return handlers


# ---------------------------------------------------------------------------- #
# Volume staging and unstaging

_staging_handlers: dict[type[VolumeStagingState], VolumeStagingHandler] = {}


def _add_staging_handler(
    state_type: type[VolumeStagingState],
) -> Callable[[VolumeStagingHandler], VolumeStagingHandler]:
    def decorator(handler: VolumeStagingHandler) -> VolumeStagingHandler:
        _staging_handlers[state_type] = handler
        return handler

    return decorator


@_add_staging_handler(VolumeStagingStates.LaunchStagingPod)
async def _handle_launch_staging_pod(
    context: VolumeStagingContext,
    state: VolumeStagingStates.LaunchStagingPod,
) -> None:

    # get staging config

    try:
        staging_config = await context.eval_staging_config()
    except Exception as e:
        await context.set_state(
            VolumeStagingStates.StagingFailed(
                error_code=StatusCode.INVALID_ARGUMENT, error_details=str(e)
            )
        )
        return

    # create staging pod

    try:
        staging_pod = await staging_config.pod_template.create(
            pod_name=(
                f"pav-volume-staging-pod-{context.pvc.metadata.uid}"
                f"-{context.client_pod.metadata.uid}"
            ),
            node_name=context.node.metadata.name,
            pav_volume_bidirectional_mount_propagation=True,
            pav_volume_name=(
                f"pav-volume-stage-{context.pvc.metadata.uid}"
                f"-{context.client_pod.metadata.uid}"
            ),
        )
    except Exception as e:
        await context.set_state(
            VolumeStagingStates.RemoveStagingPodAfterFailure(
                staging_pod_namespace=staging_config.pod_template.namespace,
                error_code=StatusCode.INVALID_ARGUMENT,
                error_details=str(e),
            )
        )
        return

    # advance state

    await context.set_state(
        VolumeStagingStates.AwaitStagingPod(
            staging_pod_namespace=staging_pod.namespace
        )
    )


@_add_staging_handler(VolumeStagingStates.AwaitStagingPod)
async def _handle_await_staging_pod(
    context: VolumeStagingContext,
    state: VolumeStagingStates.AwaitStagingPod,
) -> None:
    async def error(message: str) -> None:

        await context.set_state(
            VolumeStagingStates.RemoveStagingPodAfterFailure(
                staging_pod_namespace=state.staging_pod_namespace,
                error_code=StatusCode.INVALID_ARGUMENT,
                error_details=f"Staging pod failed: {message.strip()}",
            )
        )

    # get staging pod

    staging_pod = Pod(
        api_client=context.api_client,
        name=(
            f"pav-volume-staging-pod-{context.pvc.metadata.uid}"
            f"-{context.client_pod.metadata.uid}"
        ),
        namespace=state.staging_pod_namespace,
        pav_volume_name=(
            f"pav-volume-stage-{context.pvc.metadata.uid}"
            f"-{context.client_pod.metadata.uid}"
        ),
    )

    # wait until staging pod terminates or is ready

    if not await staging_pod.wait_until_terminated_or_ready():
        await error(staging_pod.read_file_in_pav_volume("error") or "")
        return

    # resolve and validate /pav/volume

    try:
        volume_path_in_host = (
            staging_pod.pav_volume_path_in_host / "volume"
        ).resolve(strict=True)
    except Exception as e:
        await error(f"Error resolving /pav/volume: {str(e)}")
        return

    if staging_pod.pav_volume_path_in_host not in volume_path_in_host.parents:
        await error("/pav/volume resolves to a path outside /pav")
        return

    # validate volume mode

    if (
        context.pv.spec.volume_mode == "Filesystem"
        and not volume_path_in_host.is_dir()
    ):
        await error("/pav/volume must resolve to a regular file")
        return

    if (
        context.pv.spec.volume_mode == "Block"
        and not volume_path_in_host.is_block_device()
    ):
        await error("/pav/volume must resolve to a block special file")
        return

    # validate volume capacity

    if volume_path_in_host.is_block_device():

        expected_capacity = parse_and_round_quantity(
            context.pv.spec.capacity["storage"]
        )

        actual_capacity = get_block_device_size(volume_path_in_host)

        if actual_capacity != expected_capacity:
            await error(
                f"Block device at /pav/volume has size {actual_capacity},"
                f" should be {expected_capacity}"
            )

    # create symlink to volume where Kubernetes expects it

    context.target_path_in_host.symlink_to(volume_path_in_host)

    # advance state

    await context.set_state(
        VolumeStagingStates.Staged(
            staging_pod_namespace=state.staging_pod_namespace
        )
    )


@_add_staging_handler(VolumeStagingStates.RemoveStagingPod)
@_add_staging_handler(VolumeStagingStates.RemoveStagingPodAfterFailure)
async def _handle_remove_staging_pod(
    context: VolumeStagingContext,
    state: Union[
        VolumeStagingStates.RemoveStagingPod,
        VolumeStagingStates.RemoveStagingPodAfterFailure,
    ],
) -> None:

    # get staging pod

    staging_pod = Pod(
        api_client=context.api_client,
        name=(
            f"pav-volume-staging-pod-{context.pvc.metadata.uid}"
            f"-{context.client_pod.metadata.uid}"
        ),
        namespace=state.staging_pod_namespace,
        pav_volume_name=(
            f"pav-volume-stage-{context.pvc.metadata.uid}"
            f"-{context.client_pod.metadata.uid}"
        ),
    )

    # delete staging pod

    await staging_pod.delete()

    # remove symlink

    context.target_path_in_host.unlink(missing_ok=True)

    # advance state

    if isinstance(state, VolumeStagingStates.RemoveStagingPod):
        await context.set_state(VolumeStagingStates.LaunchUnstagingPod())
    else:
        await context.set_state(
            VolumeStagingStates.LaunchUnstagingPodAfterFailure(
                error_code=state.error_code, error_details=state.error_details
            )
        )


@_add_staging_handler(VolumeStagingStates.LaunchUnstagingPod)
@_add_staging_handler(VolumeStagingStates.LaunchUnstagingPodAfterFailure)
async def _handle_launch_unstaging_pod(
    context: VolumeStagingContext,
    state: Union[
        VolumeStagingStates.LaunchUnstagingPod,
        VolumeStagingStates.LaunchUnstagingPodAfterFailure,
    ],
) -> None:

    # get unstaging config

    try:
        unstaging_config = await context.eval_unstaging_config()
    except Exception as e:
        await context.set_state(
            VolumeStagingStates.UnrecoverableFailure(
                error_code=StatusCode.INVALID_ARGUMENT, error_details=str(e)
            )
        )
        return

    # create unstaging pod

    if unstaging_config.pod_template is None:

        if isinstance(state, VolumeStagingStates.LaunchUnstagingPod):
            await context.set_state(VolumeStagingStates.Unstaged())
        else:
            await context.set_state(
                VolumeStagingStates.StagingFailed(
                    error_code=state.error_code,
                    error_details=state.error_details,
                )
            )

        return

    try:
        unstaging_pod = await unstaging_config.pod_template.create(
            pod_name=(
                f"pav-volume-unstaging-pod-{context.pvc.metadata.uid}"
                f"-{context.client_pod.metadata.uid}"
            ),
            node_name=context.node.metadata.name,
            pav_volume_bidirectional_mount_propagation=True,
            pav_volume_name=(
                f"pav-volume-stage-{context.pvc.metadata.uid}"
                f"-{context.client_pod.metadata.uid}"
            ),
        )
    except Exception as e:
        await context.set_state(
            VolumeStagingStates.UnrecoverableFailure(
                error_code=StatusCode.INVALID_ARGUMENT, error_details=str(e)
            )
        )
        return

    # advance state

    if isinstance(state, VolumeStagingStates.LaunchUnstagingPod):
        await context.set_state(
            VolumeStagingStates.AwaitUnstagingPod(
                unstaging_pod_namespace=unstaging_pod.namespace
            )
        )
    else:
        await context.set_state(
            VolumeStagingStates.AwaitUnstagingPodAfterFailure(
                unstaging_pod_namespace=unstaging_pod.namespace,
                error_code=state.error_code,
                error_details=state.error_details,
            )
        )


@_add_staging_handler(VolumeStagingStates.AwaitUnstagingPod)
@_add_staging_handler(VolumeStagingStates.AwaitUnstagingPodAfterFailure)
async def _handle_await_unstaging_pod(
    context: VolumeStagingContext,
    state: Union[
        VolumeStagingStates.AwaitUnstagingPod,
        VolumeStagingStates.AwaitUnstagingPodAfterFailure,
    ],
) -> None:

    # get unstaging pod

    unstaging_pod = Pod(
        api_client=context.api_client,
        name=(
            f"pav-volume-unstaging-pod-{context.pvc.metadata.uid}"
            f"-{context.client_pod.metadata.uid}"
        ),
        namespace=state.unstaging_pod_namespace,
        pav_volume_name=(
            f"pav-volume-stage-{context.pvc.metadata.uid}"
            f"-{context.client_pod.metadata.uid}"
        ),
    )

    # wait until unstaging pod terminates

    if not await unstaging_pod.wait_until_terminated():

        error_message = (
            unstaging_pod.read_file_in_pav_volume("error") or ""
        ).strip()

        await context.set_state(
            VolumeStagingStates.UnrecoverableFailure(
                error_code=StatusCode.INVALID_ARGUMENT,
                error_details=f"Unstaging pod failed: {error_message}",
            )
        )

        return

    # advance state

    if isinstance(state, VolumeStagingStates.AwaitUnstagingPod):
        await context.set_state(
            VolumeStagingStates.RemoveUnstagingPod(
                unstaging_pod_namespace=unstaging_pod.namespace
            )
        )
    else:
        await context.set_state(
            VolumeStagingStates.RemoveUnstagingPodAfterFailure(
                unstaging_pod_namespace=unstaging_pod.namespace,
                error_code=state.error_code,
                error_details=state.error_details,
            )
        )


@_add_staging_handler(VolumeStagingStates.RemoveUnstagingPod)
@_add_staging_handler(VolumeStagingStates.RemoveUnstagingPodAfterFailure)
async def _handle_remove_unstaging_pod(
    context: VolumeStagingContext,
    state: Union[
        VolumeStagingStates.RemoveUnstagingPod,
        VolumeStagingStates.RemoveUnstagingPodAfterFailure,
    ],
) -> None:

    # get unstaging pod

    unstaging_pod = Pod(
        api_client=context.api_client,
        name=(
            f"pav-volume-unstaging-pod-{context.pvc.metadata.uid}"
            f"-{context.client_pod.metadata.uid}"
        ),
        namespace=state.unstaging_pod_namespace,
        pav_volume_name=(
            f"pav-volume-stage-{context.pvc.metadata.uid}"
            f"-{context.client_pod.metadata.uid}"
        ),
    )

    # delete staging pod

    await unstaging_pod.delete()

    # advance state

    if isinstance(state, VolumeStagingStates.RemoveUnstagingPod):
        await context.set_state(VolumeStagingStates.Unstaged())
    else:
        await context.set_state(
            VolumeStagingStates.StagingFailed(
                error_code=state.error_code, error_details=state.error_details
            )
        )


# ---------------------------------------------------------------------------- #
