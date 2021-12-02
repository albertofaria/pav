# ---------------------------------------------------------------------------- #

from __future__ import annotations

from asyncio import CancelledError
from collections.abc import Callable, Coroutine
from functools import wraps
from sys import stderr
from traceback import print_exc
from typing import Any, TypeVar

from google.protobuf.message import Message
from google.protobuf.text_format import MessageToString
from grpc import StatusCode  # type: ignore
from grpc.aio import AbortError, ServicerContext  # type: ignore
from kubernetes_asyncio.client import (  # type: ignore
    ApiClient,
    CustomObjectsApi,
)
from mypy_extensions import Arg

from pav.shared.config import (
    PROVISIONER_GROUP,
    PROVISIONER_PLURAL,
    PROVISIONER_VERSION,
)
from pav.shared.util import log

# ---------------------------------------------------------------------------- #


async def ensure(
    condition: bool, context: ServicerContext, code: StatusCode, details: str
) -> None:

    if not condition:
        await context.abort(code=code, details=details)


async def ensure_provisioner_is_not_being_deleted(
    context: ServicerContext, api_client: ApiClient, provisioner_name: str
) -> None:

    provisioner = await CustomObjectsApi(api_client).get_cluster_custom_object(
        group=PROVISIONER_GROUP,
        version=PROVISIONER_VERSION,
        plural=PROVISIONER_PLURAL,
        name=provisioner_name,
    )

    await ensure(
        condition=not provisioner["metadata"].get("deletionTimestamp"),
        context=context,
        code=StatusCode.FAILED_PRECONDITION,
        details="The PavProvisioner is under deletion.",
    )


# ---------------------------------------------------------------------------- #

_Servicer = TypeVar("_Servicer", contravariant=True)
_Request = TypeVar("_Request", bound=Message, contravariant=True)
_Response = TypeVar("_Response", bound=Message, covariant=True)

_Rpc = Callable[
    [
        Arg(_Servicer, "self"),
        Arg(_Request, "request"),
        Arg(ServicerContext, "context"),
    ],
    Coroutine[Any, Any, _Response],
]

_call_seqnum = 0


def log_grpc(
    method: _Rpc[_Servicer, _Request, _Response]
) -> _Rpc[_Servicer, _Request, _Response]:
    @wraps(method)
    async def wrapped(
        self: _Servicer, request: _Request, context: ServicerContext
    ) -> _Response:

        global _call_seqnum
        seqnum = _call_seqnum
        _call_seqnum += 1

        header = f"{seqnum}: {type(self).__name__}.{method.__name__}()"

        log(f"entering {header} <-- {_msg_to_str(request)}")

        try:
            response = await method(self, request, context)
        except AbortError:
            log(f"\033[31mexited   {header} --> aborted\033[0m")
            raise
        except CancelledError:
            log(f"\033[31mexited   {header} --> canceled\033[0m")
            raise
        except:
            log(f"\033[31mexited   {header} --> unhandled exception:")
            print_exc()
            print("\033[0m", end="", file=stderr, flush=True)
            raise
        else:
            log(f"\033[32mexited   {header} --> {_msg_to_str(response)}\033[0m")
            return response

    return wrapped


def _msg_to_str(message: Message) -> str:

    string = MessageToString(
        message, as_utf8=True, as_one_line=True, print_unknown_fields=True
    )

    bracketed_string = f"{{ {string} }}" if string else "{ }"

    return f"{type(message).__name__} {bracketed_string}"


# ---------------------------------------------------------------------------- #
