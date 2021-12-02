# ---------------------------------------------------------------------------- #

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from http import HTTPStatus
from typing import Any, Optional, TypeVar

from kubernetes.utils import parse_quantity  # type: ignore
from kubernetes_asyncio.client import (  # type: ignore
    ApiClient,
    ApiException,
    CoreV1Api,
    StorageV1Api,
    V1PersistentVolumeClaim,
    V1Pod,
)
from kubernetes_asyncio.watch import Watch  # type: ignore

# ---------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ObjectRef:
    name: str
    namespace: str
    uid: str


def parse_and_round_quantity(
    quantity: object, *, rounding_mode: str = ROUND_HALF_EVEN
) -> int:

    parsed = parse_quantity(quantity)
    assert isinstance(parsed, Decimal)

    return int(parsed.to_integral_value(rounding=rounding_mode))


# ---------------------------------------------------------------------------- #


async def get_all_persistent_volume_claims(
    api_client: ApiClient,
    *,
    label_selector: Optional[str] = None,
    field_selector: Optional[str] = None,
) -> list[V1PersistentVolumeClaim]:

    return await _get_all_objects(
        list_fn=CoreV1Api(
            api_client
        ).list_persistent_volume_claim_for_all_namespaces,
        label_selector=label_selector,
        field_selector=field_selector,
    )


async def get_all_pods(
    api_client: ApiClient,
    *,
    label_selector: Optional[str] = None,
    field_selector: Optional[str] = None,
) -> list[V1Pod]:

    return await _get_all_objects(
        list_fn=CoreV1Api(api_client).list_pod_for_all_namespaces,
        label_selector=label_selector,
        field_selector=field_selector,
    )


async def _get_all_objects(
    list_fn: Callable[..., Coroutine[Any, Any, Any]],
    *,
    label_selector: Optional[str] = None,
    field_selector: Optional[str] = None,
) -> list[Any]:

    objects = await list_fn(
        label_selector=label_selector, field_selector=field_selector
    )

    assert not objects.metadata._continue
    assert type(objects.items) is list

    return objects.items


# ---------------------------------------------------------------------------- #


async def synchronously_delete_csi_driver(
    api_client: ApiClient, name: str
) -> None:

    await _synchronously_delete_object(
        delete_fn=StorageV1Api(api_client).delete_csi_driver,
        list_fn=StorageV1Api(api_client).list_csi_driver,
        name=name,
        namespace=None,
    )


async def synchronously_delete_persistent_volume_claim(
    api_client: ApiClient, name: str, namespace: str
) -> None:

    await _synchronously_delete_object(
        delete_fn=CoreV1Api(
            api_client
        ).delete_namespaced_persistent_volume_claim,
        list_fn=CoreV1Api(
            api_client
        ).list_persistent_volume_claim_for_all_namespaces,
        name=name,
        namespace=namespace,
    )


async def synchronously_delete_pod(
    api_client: ApiClient, name: str, namespace: str
) -> None:

    await _synchronously_delete_object(
        delete_fn=CoreV1Api(api_client).delete_namespaced_pod,
        list_fn=CoreV1Api(api_client).list_pod_for_all_namespaces,
        name=name,
        namespace=namespace,
    )


async def _synchronously_delete_object(
    delete_fn: Callable[..., Coroutine[Any, Any, Any]],
    list_fn: Callable[..., Coroutine[Any, Any, Any]],
    name: str,
    namespace: Optional[str],
) -> None:
    """
    Requires RBAC permissions for verbs 'delete', 'list', and 'watch'.

    Uses "foreground cascading deletion".
    """

    # request object deletion

    kwargs = {"name": name, "propagation_policy": "Foreground"}

    if namespace is not None:
        kwargs |= {"namespace": namespace}

    try:
        await delete_fn(**kwargs)
    except ApiException as e:
        if e.status == HTTPStatus.NOT_FOUND:
            return  # object doesn't exist, success
        else:
            raise  # some other error occurred, reraise exception

    # wait until object is deleted

    field_selector = f"metadata.name={name}"

    if namespace is not None:
        field_selector += f",metadata.namespace={namespace}"

    async def callback(obj: Any, exists: bool) -> None:
        if not exists:
            raise StopAsyncIteration

    await _watch_all_objects(
        list_fn=list_fn,
        field_selector=field_selector,
        return_if_no_matches=True,
        callback=callback,
    )


# ---------------------------------------------------------------------------- #

T = TypeVar("T")
U = TypeVar("U")

WatchCallback = Callable[[T], Coroutine[Any, Any, Optional[U]]]


async def watch_persistent_volume_claim(
    api_client: ApiClient,
    name: str,
    namespace: str,
    callback: WatchCallback[V1PersistentVolumeClaim, T],
) -> T:

    return await _watch_object(
        list_fn=CoreV1Api(
            api_client
        ).list_persistent_volume_claim_for_all_namespaces,
        name=name,
        namespace=namespace,
        callback=callback,
    )


async def watch_pod(
    api_client: ApiClient,
    name: str,
    namespace: str,
    callback: WatchCallback[V1Pod, T],
) -> T:

    return await _watch_object(
        list_fn=CoreV1Api(api_client).list_pod_for_all_namespaces,
        name=name,
        namespace=namespace,
        callback=callback,
    )


async def _watch_object(
    list_fn: Callable[..., Coroutine[Any, Any, Any]],
    name: str,
    namespace: Optional[str],
    callback: WatchCallback[Any, T],
) -> T:
    """
    The callback must be idempotent, as the object may be read several times
    before it starts being watched, and also after it starts being watched.

    Nevertheless, this function can still miss intermediate updates, although it
    will always eventually invoke the callback with the latest object version.

    This function will simply not invoke the callback if no such object exists,
    and will fail with an exception if the object being watched is suddenly
    deleted.
    """

    # It seems we can't use watches with read_... functions because they don't
    # accept resource_version arguments.

    field_selector = f"metadata.name={name}"

    if namespace is not None:
        field_selector += f",metadata.namespace={namespace}"

    uid: Optional[str] = None
    result: Optional[T] = None

    async def inner_callback(obj: Any, exists: bool) -> None:

        nonlocal uid, result

        # ensure that we only get events for a single object

        if uid is None:
            uid = obj.metadata.uid
        elif obj.metadata.uid != uid:
            raise RuntimeError("Received events for more than one object")

        # ensure that object still exists

        if not exists:
            raise RuntimeError("The object was deleted")

        # invoke callback

        result = await callback(obj)

        # stop watching if callback returned result

        if result is not None:
            raise StopAsyncIteration

    await _watch_all_objects(
        list_fn=list_fn, field_selector=field_selector, callback=inner_callback
    )

    assert result is not None
    return result


# ---------------------------------------------------------------------------- #

WatchAllCallback = Callable[[T, bool], Coroutine[Any, Any, None]]


async def watch_all_persistent_volume_claims(
    api_client: ApiClient,
    callback: WatchAllCallback[V1PersistentVolumeClaim],
    *,
    label_selector: Optional[str] = None,
    field_selector: Optional[str] = None,
) -> None:

    return await _watch_all_objects(
        list_fn=CoreV1Api(
            api_client
        ).list_persistent_volume_claim_for_all_namespaces,
        callback=callback,
        label_selector=label_selector,
        field_selector=field_selector,
    )


async def watch_all_pods(
    api_client: ApiClient,
    callback: WatchAllCallback[V1Pod],
    *,
    label_selector: Optional[str] = None,
    field_selector: Optional[str] = None,
) -> None:

    return await _watch_all_objects(
        list_fn=CoreV1Api(api_client).list_pod_for_all_namespaces,
        callback=callback,
        label_selector=label_selector,
        field_selector=field_selector,
    )


async def _watch_all_objects(
    list_fn: Callable[..., Coroutine[Any, Any, Any]],
    callback: Callable[[Any, bool], Coroutine[Any, Any, None]],
    *,
    label_selector: Optional[str] = None,
    field_selector: Optional[str] = None,
    return_if_no_matches: bool = False,
) -> None:
    """
    The callback must be idempotent, as all objects may be listed several times
    before they start being watched, and also after they start being watched.

    Nevertheless, this function can still miss intermediate updates, although it
    will always eventually invoke the callback with the latest object version.

    The callback can raise `StopAsyncIteration` to cause this function to
    return.

    If `return_if_no_matches` is `True`, then this function returns immediately
    if no matching object is initially found.
    """

    while True:

        # list

        obj_list = await list_fn(
            label_selector=label_selector, field_selector=field_selector
        )

        if not obj_list.items and return_if_no_matches:
            return

        for obj in obj_list.items:

            try:
                await callback(obj, True)
            except StopAsyncIteration:
                return  # callback requested stop, return

        # watch

        is_callback_api_exception = False

        async with Watch() as watch:

            try:

                stream = watch.stream(
                    list_fn,
                    label_selector=label_selector,
                    field_selector=field_selector,
                    resource_version=obj_list.metadata.resource_version,
                )

                async for event in stream:

                    obj = event["object"]
                    exists = event["type"] != "DELETED"

                    try:
                        await callback(obj, exists)
                    except StopAsyncIteration:
                        return  # callback requested stop, return
                    except ApiException:
                        is_callback_api_exception = True
                        raise

                else:

                    raise RuntimeError("The watch stopped watching")

            except ApiException as e:

                if (
                    not is_callback_api_exception
                    and e.status == HTTPStatus.GONE
                ):
                    pass  # took too long to start watching after listing, retry
                else:
                    raise  # some other error occurred, fail


# ---------------------------------------------------------------------------- #

Modifier = Callable[[T], Any]


async def atomically_modify_persistent_volume_claim(
    api_client: ApiClient,
    name: str,
    namespace: str,
    modifier: Modifier[V1PersistentVolumeClaim],
) -> V1PersistentVolumeClaim:

    api = CoreV1Api(api_client)

    await _atomically_modify_object(
        read_fn=api.read_namespaced_persistent_volume_claim,
        replace_fn=api.replace_namespaced_persistent_volume_claim,
        name=name,
        namespace=namespace,
        modifier=modifier,
    )


async def atomically_modify_pod(
    api_client: ApiClient, name: str, namespace: str, modifier: Modifier[V1Pod]
) -> V1Pod:

    api = CoreV1Api(api_client)

    await _atomically_modify_object(
        read_fn=api.read_namespaced_pod,
        replace_fn=api.replace_namespaced_pod,
        name=name,
        namespace=namespace,
        modifier=modifier,
    )


async def _atomically_modify_object(
    read_fn: Callable[..., Coroutine[Any, Any, Any]],
    replace_fn: Callable[..., Coroutine[Any, Any, Any]],
    name: str,
    namespace: Optional[str],
    modifier: Modifier[Any],
) -> Any:
    """Atomically applies arbitrary modifications to objects. Use when patching
    is insufficient. Works by reading and replacing the object, retrying if it
    was modified in between. Returns the resulting object."""

    kwargs = {"name": name}

    if namespace is not None:
        kwargs["namespace"] = namespace

    while True:

        # retrieve object

        obj = await read_fn(**kwargs)

        # adjust object

        original_obj_dict = obj.to_dict()

        result = modifier(obj)

        if hasattr(result, "__await__"):
            await result

        if obj.to_dict() == original_obj_dict:
            break  # no changes necessary

        # replace object

        try:

            return await replace_fn(**kwargs, body=obj)

        except ApiException as e:

            # If we failed with 409 CONFLICT, it means that the object's
            # 'metadata.resourceVersion' field has a different value from when
            # we retrieved it. This means that the object was modified in
            # between our read_fn() and replace_fn() calls, in which case we
            # must re-read the object and retry.

            if e.status != HTTPStatus.CONFLICT:
                raise  # some unexpected error occurred


# ---------------------------------------------------------------------------- #
