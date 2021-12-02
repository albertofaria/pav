# ---------------------------------------------------------------------------- #

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

# ---------------------------------------------------------------------------- #

DOMAIN = "pav.albertofaria.github.io"
"""Used for the provisioner CRD group, as a prefix for labels, annotations, and
finalizers, and in a few other places."""

PROVISIONER_GROUP = DOMAIN
PROVISIONER_VERSION = "v1alpha1"
PROVISIONER_KIND = "PavProvisioner"
PROVISIONER_PLURAL = "pavprovisioners"

INTERNAL_NAMESPACE = "pav"
"""The namespace of all namespaced objects in 'deployment.yaml'."""

CSI_SOCKET_PATH = Path("/csi/socket")
"""Absolute path, in the context of a CSI controller/node plugin container,
to the CSI Unix domain socket."""

PAV_VOLUME_DIR_PATH = Path("/var/lib/kubernetes-pav")
"""Absolute path, in the context of both the host and node agent containers, to
the directory under which /pav volumes are created."""

AGENT_HANDLER_RETRY_DELAY = timedelta(seconds=5)
"""Amount of time to wait before retrying an agent handler after an internal
failure."""

KOPF_FINALIZER = f"{DOMAIN}/kopf"
"""Finalizer for kopf to use instead of its default one."""

# ---------------------------------------------------------------------------- #
