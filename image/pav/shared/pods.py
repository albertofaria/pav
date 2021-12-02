# ---------------------------------------------------------------------------- #

from __future__ import annotations

import subprocess
from asyncio import sleep
from copy import deepcopy
from http import HTTPStatus
from pathlib import Path, PurePath
from shutil import rmtree
from typing import Any, Optional, Union

import yaml
from kubernetes_asyncio.client import (  # type: ignore
    ApiClient,
    ApiException,
    CoreV1Api,
    V1Pod,
)

from pav.shared.config import PAV_VOLUME_DIR_PATH
from pav.shared.kubernetes import synchronously_delete_pod, watch_pod
from pav.shared.util import find_top_level_mounts

# ---------------------------------------------------------------------------- #


class PodTemplate:
    """Not the same as Kubernetes' PodTemplate."""

    @staticmethod
    async def new(
        api_client: ApiClient, pod_template_spec: object
    ) -> PodTemplate:
        """
        Create a PodTemplate from the given object defining a Kubernetes
        PodTemplateSpec.

        This ensures that the given object is a valid definition of a Kubernetes
        PodTemplateSpec by asking the API server to dry-run instantiate a pod
        from it, and raises ValueError if it is not valid.

        This never mutates 'pod_template_spec'.
        """

        # deep copy while ensuring that only primitive-ish types are used

        template = yaml.safe_load(yaml.safe_dump(pod_template_spec))

        # create minimal pod definition from template

        namespace = "default"

        if isinstance(template, dict):

            if not set(template.keys()).issubset({"metadata", "spec"}):
                raise ValueError(
                    "May only specify fields 'metadata' and 'spec'"
                )

            template["apiVersion"] = "v1"
            template["kind"] = "Pod"
            template.setdefault("metadata", {})
            template.setdefault("spec", {})

            if isinstance(template["metadata"], dict):

                template["metadata"].pop("name", None)
                template["metadata"]["generateName"] = "pod-"

                namespace = template["metadata"].get("namespace", "default")

            if isinstance(template["spec"], dict):

                template["spec"].setdefault("initContainers", [])
                template["spec"].setdefault("containers", [])
                template["spec"].setdefault("volumes", [])

                all_containers = []

                if isinstance(template["spec"]["initContainers"], list):
                    all_containers += template["spec"]["initContainers"]

                if isinstance(template["spec"]["containers"], list):
                    all_containers += template["spec"]["containers"]

                mount = {"name": "pav", "mountPath": "/pav"}

                for container in all_containers:
                    if isinstance(container, dict):
                        container.setdefault("volumeMounts", [])
                        if container["volumeMounts"] is None:
                            container["volumeMounts"] = []
                        if isinstance(container["volumeMounts"], list):
                            container["volumeMounts"].insert(0, mount)

                if template["spec"]["volumes"] is None:
                    template["spec"]["volumes"] = []

                if isinstance(template["spec"]["volumes"], list):
                    volume = {"name": "pav", "emptyDir": {}}
                    template["spec"]["volumes"].insert(0, volume)

        # validate backing pod definition

        try:

            await CoreV1Api(api_client).create_namespaced_pod(
                body=template, namespace=namespace, dry_run="All"
            )

        except ApiException as e:

            if e.status == HTTPStatus.BAD_REQUEST:
                raise ValueError(e.reason)
            else:
                raise  # some other error occurred

        # return pod template wrapper object

        return PodTemplate(
            api_client=api_client,
            template=yaml.safe_load(yaml.safe_dump(pod_template_spec)),
        )

    __api_client: ApiClient
    __template: Any
    __namespace: str

    def __init__(self, api_client: ApiClient, template: Any) -> None:
        """PRIVATE, DO NOT USE."""

        namespace = template.get("metadata", {}).get("namespace", "default")
        assert type(namespace) is str

        self.__api_client = api_client
        self.__template = template
        self.__namespace = namespace

    @property
    def namespace(self) -> str:
        """Namespace that pod objects instantiated from this template will
        belong to."""
        return self.__namespace

    async def create(
        self,
        pod_name: str,
        *,
        node_name: Optional[str] = None,
        pav_volume_name: Optional[str] = None,
        pav_volume_bidirectional_mount_propagation: bool = False,
    ) -> Pod:
        """Create a pod from this template, or do nothing if a pod with the same
        name in the expected namespace already exists."""

        # instantiate pod definition from template

        pod_definition = self.__instantiate_pod_definition(
            pod_name=pod_name,
            node_name=node_name,
            pav_volume_name=pav_volume_name or pod_name,
            pav_volume_bidirectional_mount_propagation=(
                pav_volume_bidirectional_mount_propagation
            ),
        )

        # create pod object

        try:

            await CoreV1Api(self.__api_client).create_namespaced_pod(
                body=pod_definition, namespace=self.namespace
            )

        except ApiException as e:

            if e.status == HTTPStatus.CONFLICT:
                pass  # pod with same name and namespace already exists, success
            else:
                raise  # failed due to some other reason, reraise exception

        # return pod handle

        return Pod(
            api_client=self.__api_client,
            name=pod_name,
            namespace=self.namespace,
            pav_volume_name=pav_volume_name,
        )

    def __instantiate_pod_definition(
        self,
        pod_name: str,
        node_name: Optional[str],
        pav_volume_name: str,
        pav_volume_bidirectional_mount_propagation: bool,
    ) -> object:

        pod = deepcopy(self.__template)

        pod["apiVersion"] = "v1"
        pod["kind"] = "Pod"

        # set pod name

        metadata = pod.setdefault("metadata", {})
        metadata["name"] = pod_name
        metadata.pop("generateName", None)

        # set node on which to run the pod

        if node_name is not None:
            pod["spec"]["nodeName"] = node_name

        # add /pav volume definition

        volume = {
            "name": "pav",
            "hostPath": {
                "path": str(PAV_VOLUME_DIR_PATH / pav_volume_name),
                "type": "DirectoryOrCreate",
            },
        }

        pod["spec"].setdefault("volumes", []).insert(0, volume)

        # mount /pav volume in all containers

        all_containers = (
            pod["spec"].get("initContainers", []) + pod["spec"]["containers"]
        )

        for container in all_containers:

            privileged = container.get("securityContext", {}).get(
                "privileged", False
            )

            volume_mount = {"name": "pav", "mountPath": "/pav"}

            if pav_volume_bidirectional_mount_propagation and privileged:
                volume_mount["mountPropagation"] = "Bidirectional"

            container.setdefault("volumeMounts", []).insert(0, volume_mount)

        # return pod definition

        return pod


# ---------------------------------------------------------------------------- #


class Pod:

    __api_client: ApiClient
    __name: str
    __namespace: str
    __pav_volume_path: Path

    def __init__(
        self,
        api_client: ApiClient,
        name: str,
        namespace: str,
        *,
        pav_volume_name: Optional[str] = None,
    ) -> None:
        """`pav_volume_name` defaults to `name`."""

        if pav_volume_name is None:
            pav_volume_name = name

        self.__api_client = api_client
        self.__name = name
        self.__namespace = namespace
        self.__pav_volume_path = PAV_VOLUME_DIR_PATH / pav_volume_name

    @property
    def name(self) -> str:
        """The pod's name."""
        return self.__name

    @property
    def namespace(self) -> str:
        """The pod's namespace."""
        return self.__namespace

    @property
    def pav_volume_path_in_host(self) -> Path:
        """Path to the /pav volume in the host."""
        return self.__pav_volume_path

    def read_file_in_pav_volume(
        self, relative_path: Union[PurePath, str]
    ) -> Optional[str]:
        """
        Return the contents of a UTF-8 encoded file under the /pav volume.

        MUST ONLY BE CALLED FROM THE NODE AGENT OF THE POD'S NODE.

        Returns `None` if the file does not exist or is not (a symlink to) a
        regular file.
        """

        path = self.__pav_volume_path / relative_path

        if not path.is_file():
            return None

        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    async def wait_until_scheduled(self) -> str:
        """
        Wait until the pod is scheduled to a node.

        Returns the name of the node to which the pod was scheduled.
        """

        async def callback(pod: V1Pod) -> Optional[str]:
            return pod.spec.node_name or None

        return await watch_pod(
            api_client=self.__api_client,
            name=self.name,
            namespace=self.namespace,
            callback=callback,
        )

    async def wait_until_terminated(self) -> bool:
        """
        Wait until the pod terminates.

        Returns True if terminated successfully; False otherwise.
        """

        async def callback(pod: V1Pod) -> Optional[bool]:

            if pod.status.phase == "Succeeded":
                return True
            elif pod.status.phase == "Failed":
                return False
            else:
                return None

        return await watch_pod(
            api_client=self.__api_client,
            name=self.name,
            namespace=self.namespace,
            callback=callback,
        )

    async def wait_until_terminated_or_ready(self) -> bool:
        """
        Wait until the pod terminates or creates file /pav/ready.

        MUST ONLY BE CALLED FROM THE NODE AGENT OF THE POD'S NODE.

        Returns True if terminated successfully or the file was created; False
        if terminated in failure.
        """

        ready_file_path = self.__pav_volume_path / "ready"

        while True:

            pod = await CoreV1Api(self.__api_client).read_namespaced_pod(
                name=self.name, namespace=self.namespace
            )

            if pod.status.phase == "Succeeded" or ready_file_path.exists():
                return True
            elif pod.status.phase == "Failed":
                return False

            await sleep(1.0)

    async def delete(self) -> None:
        """
        Delete the pod and the /pav volume.

        MUST ONLY BE CALLED FROM THE NODE AGENT OF THE POD'S NODE.

        Ignores errors due to the pod no longer existing.
        """

        # delete pod

        await synchronously_delete_pod(
            api_client=self.__api_client,
            name=self.name,
            namespace=self.namespace,
        )

        # unmount any mount points left behind in the /pav volume

        # NOTE: Must find and unmount mounts several times until no more mounts
        # exist, because of layered mounts (i.e., mounts that hide other
        # mounts).

        while mount_points := find_top_level_mounts(self.__pav_volume_path):

            for mp in mount_points:

                # NOTE: We use --force to abort pending requests on the file
                # system that may never get served because, for instance, the
                # remote or backing FUSE process is gone.

                # NOTE: The mount point path was retrieved from
                # /proc/self/mountinfo and so should already be canonical, but
                # we use --no-canonicalize nonetheless to prevent umount from
                # submitting more file system metadata requests.

                subprocess.run(
                    args=[
                        "/bin/umount",
                        "--force",
                        "--no-canonicalize",
                        "--recursive",
                        str(mp),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                    check=True,
                )

        # remove /pav volume directory

        if self.__pav_volume_path.exists():
            rmtree(self.__pav_volume_path)


# ---------------------------------------------------------------------------- #
