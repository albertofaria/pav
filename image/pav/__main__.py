# ---------------------------------------------------------------------------- #

from __future__ import annotations

from argparse import ArgumentParser, Namespace

from kubernetes_asyncio.config import load_incluster_config  # type: ignore

import pav.agent.controller
import pav.agent.node
import pav.csi
from pav.shared.kubernetes import ClusterObjectRef

# ---------------------------------------------------------------------------- #


def main() -> None:
    """
    Usage:

        python -m pav agent controller <image>
        python -m pav agent node <node_name>
        python -m pav csi-plugin <provisioner_name> <provisioner_uid> controller
        python -m pav csi-plugin <provisioner_name> <provisioner_uid> node <node_name>
    """

    args = _parse_args()

    load_incluster_config()

    if args.mode == "agent":

        if args.agent == "controller":
            pav.agent.controller.run(image=args.image)
        elif args.agent == "node":
            pav.agent.node.run(node_name=args.node_name)

    elif args.mode == "csi-plugin":

        provisioner_ref = ClusterObjectRef(
            name=args.provisioner_name, uid=args.provisioner_uid
        )

        if args.csi_plugin == "controller":
            pav.csi.run_controller(provisioner_ref)
        elif args.csi_plugin == "node":
            pav.csi.run_node(provisioner_ref, node_name=args.node_name)


def _parse_args() -> Namespace:

    parser = ArgumentParser()

    subparsers = parser.add_subparsers(dest="mode", required=True)

    # 'agent' subcommand

    agent_parser = subparsers.add_parser("agent")

    agent_subparsers = agent_parser.add_subparsers(dest="agent", required=True)
    agent_subparsers.add_parser("controller").add_argument("image")
    agent_subparsers.add_parser("node").add_argument("node_name")

    # 'csi-plugin' subcommand

    csi_parser = subparsers.add_parser("csi-plugin")
    csi_parser.add_argument("provisioner_name")
    csi_parser.add_argument("provisioner_uid")

    csi_subparsers = csi_parser.add_subparsers(dest="csi_plugin", required=True)
    csi_subparsers.add_parser("controller")
    csi_subparsers.add_parser("node").add_argument("node_name")

    # parse arguments

    return parser.parse_args()


# ---------------------------------------------------------------------------- #

if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------- #
