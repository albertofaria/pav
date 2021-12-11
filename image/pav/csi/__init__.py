# ---------------------------------------------------------------------------- #

from __future__ import annotations

import asyncio
from math import inf
from signal import SIGTERM
from typing import Optional

import grpc.aio  # type: ignore
from kubernetes_asyncio.client import ApiClient  # type: ignore

from pav.csi.controller import Controller
from pav.csi.identity import Identity
from pav.csi.node import Node
from pav.csi.spec.csi_pb2_grpc import (
    add_ControllerServicer_to_server,
    add_IdentityServicer_to_server,
    add_NodeServicer_to_server,
)
from pav.shared.config import CSI_SOCKET_PATH
from pav.shared.kubernetes import ClusterObjectRef

# ---------------------------------------------------------------------------- #


def run_controller(provisioner_ref: ClusterObjectRef) -> None:
    asyncio.run(_run_async(provisioner_ref, None))


def run_node(provisioner_ref: ClusterObjectRef, node_name: str) -> None:
    asyncio.run(_run_async(provisioner_ref, node_name))


async def _run_async(
    provisioner_ref: ClusterObjectRef, node_name: Optional[str]
) -> None:

    async with ApiClient() as api_client:

        # set up CSI plugin server

        server = grpc.aio.server()
        server.add_insecure_port(f"unix://{CSI_SOCKET_PATH}")

        identity = Identity(provisioner_ref)
        add_IdentityServicer_to_server(identity, server)

        if node_name is None:
            controller = Controller(api_client, provisioner_ref)
            add_ControllerServicer_to_server(controller, server)
        else:
            node = Node(api_client, provisioner_ref, node_name)
            add_NodeServicer_to_server(node, server)

        # set up signal handler to allow for graceful termination

        asyncio.get_running_loop().add_signal_handler(
            SIGTERM, lambda: asyncio.create_task(server.stop(inf))
        )

        # run CSI plugin server

        await server.start()
        await server.wait_for_termination()


# ---------------------------------------------------------------------------- #
