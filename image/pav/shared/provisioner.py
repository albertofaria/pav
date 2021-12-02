# ---------------------------------------------------------------------------- #

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR
from enum import Enum, unique
from pathlib import Path
from typing import Any, Optional

import yamale  # type: ignore
from jinja2 import TemplateError
from kubernetes_asyncio.client import (  # type: ignore
    ApiClient,
    CustomObjectsApi,
    V1Node,
    V1PersistentVolume,
    V1PersistentVolumeClaim,
    V1StorageClass,
)

from pav.shared.config import (
    PROVISIONER_GROUP,
    PROVISIONER_PLURAL,
    PROVISIONER_VERSION,
)
from pav.shared.kubernetes import parse_and_round_quantity
from pav.shared.pods import PodTemplate
from pav.shared.templating import evaluate_templates, validate_templates

# ---------------------------------------------------------------------------- #


@unique
class VolumeMode(Enum):
    FILE_SYSTEM = "Filesystem"
    BLOCK = "Block"


@unique
class AccessMode(Enum):
    READ_WRITE_ONCE = "ReadWriteOnce"
    READ_ONLY_MANY = "ReadOnlyMany"
    READ_WRITE_MANY = "ReadWriteMany"


# ---------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RequestedVolumeProperties:

    volume_mode: VolumeMode
    access_modes: frozenset[AccessMode]
    min_capacity: int
    max_capacity: Optional[int]

    @staticmethod
    def from_pvc(pvc: V1PersistentVolumeClaim) -> RequestedVolumeProperties:

        return RequestedVolumeProperties(
            volume_mode=VolumeMode(pvc.spec.volume_mode),
            access_modes=frozenset(map(AccessMode, pvc.spec.access_modes)),
            min_capacity=parse_and_round_quantity(
                pvc.spec.resources.requests["storage"]
            ),
            max_capacity=(
                parse_and_round_quantity(pvc.spec.resources.limits["storage"])
                if "storage" in (pvc.spec.resources.limits or {})
                else None
            ),
        )


@dataclass(frozen=True)
class VolumeValidationConfig:
    volume_modes: frozenset[VolumeMode]
    access_modes: frozenset[AccessMode]
    min_capacity: int
    max_capacity: Optional[int]
    pod_template: Optional[PodTemplate]


@dataclass(frozen=True)
class VolumeCreationConfig:
    handle: Optional[str]
    capacity: Optional[int]
    pod_template: Optional[PodTemplate]


@dataclass(frozen=True)
class VolumeDeletionConfig:
    pod_template: Optional[PodTemplate]


@dataclass(frozen=True)
class VolumeStagingConfig:
    pod_template: PodTemplate


@dataclass(frozen=True)
class VolumeUnstagingConfig:
    pod_template: Optional[PodTemplate]


# ---------------------------------------------------------------------------- #


class Provisioner:

    __SCHEMA_TEXT = (
        Path(__file__).parent / "provisioner-schema.yaml"
    ).read_text()

    __SCHEMA = yamale.make_schema(
        content=__SCHEMA_TEXT.format(template="include('template'),")
    )

    __SCHEMA_WITHOUT_TEMPLATES = yamale.make_schema(
        content=__SCHEMA_TEXT.format(template="")
    )

    @staticmethod
    def validate(obj: Any) -> None:

        # validate whole provisioner against schema

        try:
            yamale.validate(schema=Provisioner.__SCHEMA, data=[(obj, None)])
        except yamale.YamaleError as e:
            assert len(e.results) == 1
            raise ValueError(
                "".join(f"\n  {msg}" for msg in e.results[0].errors)
            )

        # validate template syntax

        try:
            for key in obj["spec"].keys() - {"provisioningModes"}:
                validate_templates(obj["spec"][key])
        except TemplateError as e:
            raise ValueError(e.message)

    @staticmethod
    async def get(api_client: ApiClient, provisioner_name: str) -> Provisioner:

        obj = await CustomObjectsApi(api_client).get_cluster_custom_object(
            group=PROVISIONER_GROUP,
            version=PROVISIONER_VERSION,
            plural=PROVISIONER_PLURAL,
            name=provisioner_name,
        )

        return Provisioner(api_client, obj)

    __api_client: ApiClient
    __obj: Any
    __name: str

    def __init__(self, api_client: ApiClient, obj: object) -> None:
        """PRIVATE, DO NOT USE."""

        self.__api_client = api_client
        self.__obj = obj

        name = self.__obj["metadata"]["name"]  # type: ignore
        assert isinstance(name, str)
        self.__name = name

    @property
    def name(self) -> str:
        return self.__name

    async def eval_static_validation_config(
        self, persistent_volume: V1PersistentVolume
    ) -> VolumeValidationConfig:

        context = self.__get_static_validation_context(pv=persistent_volume)

        return await self.__eval_validation_config(context)

    async def eval_dynamic_validation_config(
        self,
        storage_class: V1StorageClass,
        persistent_volume_claim: V1PersistentVolumeClaim,
    ) -> VolumeValidationConfig:

        context = self.__get_dynamic_validation_context(
            sc=storage_class, pvc=persistent_volume_claim
        )

        return await self.__eval_validation_config(context)

    async def __eval_validation_config(
        self, context: Mapping[str, object]
    ) -> VolumeValidationConfig:

        # evaluate templates under spec.volumeValidation

        obj = await self.__evaluate_spec_field("volumeValidation", context)

        # perform additional validation

        volume_modes = frozenset(
            VolumeMode(mode) for mode in obj.get("volumeModes", ["Filesystem"])
        )

        access_modes = frozenset(
            AccessMode(mode)
            for mode in obj.get(
                "accessModes",
                ["ReadWriteOnce", "ReadOnlyMany", "ReadWriteMany"],
            )
        )

        minimum_capacity = self.__parse_capacity(
            obj.get("minCapacity", 1), ROUND_FLOOR
        )

        maximum_capacity = self.__parse_capacity_opt(
            obj.get("maxCapacity"), ROUND_CEILING
        )

        if (
            minimum_capacity is not None
            and maximum_capacity is not None
            and minimum_capacity > maximum_capacity
        ):
            raise ValueError(
                "'spec.volumeValidation.minCapacity' must not be greater than"
                " 'spec.volumeValidation.maxCapacity'"
            )

        pod_template = await self.__create_pod_template_opt(
            obj.get("podTemplate")
        )

        # return config

        return VolumeValidationConfig(
            volume_modes=volume_modes,
            access_modes=access_modes,
            min_capacity=minimum_capacity,
            max_capacity=maximum_capacity,
            pod_template=pod_template,
        )

    async def eval_creation_config(
        self,
        storage_class: V1StorageClass,
        persistent_volume_claim: V1PersistentVolumeClaim,
    ) -> VolumeCreationConfig:

        context = self.__get_creation_and_deletion_context(
            sc=storage_class, pvc=persistent_volume_claim
        )

        # evaluate templates under spec.volumeCreation

        obj = await self.__evaluate_spec_field("volumeCreation", context)

        # perform additional validation

        capacity = self.__parse_capacity_opt(obj.get("capacity"), ROUND_FLOOR)

        if (
            "Dynamic" in self.__obj["spec"]["provisioningModes"]
            and "capacity" not in obj
            and "podTemplate" not in obj
        ):
            raise ValueError(
                "At least one of 'spec.volumeCreation.capacity' or"
                " 'spec.volumeCreation.podTemplate' must be specified when"
                " 'spec.provisioningModes' contains 'Dynamic'"
            )

        pod_template = await self.__create_pod_template_opt(
            obj.get("podTemplate")
        )

        # return config

        return VolumeCreationConfig(
            handle=obj.get("handle"),
            capacity=capacity,
            pod_template=pod_template,
        )

    async def eval_deletion_config(
        self,
        storage_class: V1StorageClass,
        persistent_volume_claim: V1PersistentVolumeClaim,
    ) -> VolumeDeletionConfig:

        context = self.__get_creation_and_deletion_context(
            sc=storage_class, pvc=persistent_volume_claim
        )

        # evaluate templates under spec.volumeDeletion

        obj = await self.__evaluate_spec_field("volumeDeletion", context)

        # perform additional validation

        pod_template = await self.__create_pod_template_opt(
            obj.get("podTemplate")
        )

        # return config

        return VolumeDeletionConfig(pod_template=pod_template)

    async def eval_staging_config(
        self,
        persistent_volume_claim: V1PersistentVolumeClaim,
        persistent_volume: V1PersistentVolume,
        node: V1Node,
        read_only: bool,
    ) -> VolumeStagingConfig:

        context = self.__get_staging_and_unstaging_context(
            pvc=persistent_volume_claim,
            pv=persistent_volume,
            node=node,
            read_only=read_only,
        )

        # evaluate templates under spec.volumeStaging

        obj = await self.__evaluate_spec_field("volumeStaging", context)

        # perform additional validation

        pod_template = await self.__create_pod_template(obj["podTemplate"])

        # return config

        return VolumeStagingConfig(pod_template=pod_template)

    async def eval_unstaging_config(
        self,
        persistent_volume_claim: V1PersistentVolumeClaim,
        persistent_volume: V1PersistentVolume,
        node: V1Node,
        read_only: bool,
    ) -> VolumeUnstagingConfig:

        context = self.__get_staging_and_unstaging_context(
            pvc=persistent_volume_claim,
            pv=persistent_volume,
            node=node,
            read_only=read_only,
        )

        # evaluate templates under spec.volumeUnstaging

        obj = await self.__evaluate_spec_field("volumeUnstaging", context)

        # perform additional validation

        pod_template = await self.__create_pod_template_opt(
            obj.get("podTemplate")
        )

        # return config

        return VolumeUnstagingConfig(pod_template=pod_template)

    def __get_static_validation_context(
        self, pv: V1PersistentVolume
    ) -> dict[str, object]:

        capacity = parse_and_round_quantity(pv.spec.capacity["storage"])

        return {
            "requestedVolumeMode": pv.spec.volume_mode,
            "requestedAccessModes": list(pv.spec.access_modes),
            "requestedMinCapacity": capacity,
            "requestedMaxCapacity": capacity,
            "params": dict(pv.spec.csi.volume_attributes or {}),
            "handle": pv.spec.csi.volume_handle,
            "pv": self.__api_client.sanitize_for_serialization(pv),
        }

    def __get_dynamic_validation_context(
        self, sc: V1StorageClass, pvc: V1PersistentVolumeClaim
    ) -> dict[str, object]:

        return {
            "requestedVolumeMode": pvc.spec.volume_mode,
            "requestedAccessModes": list(pvc.spec.access_modes),
            "requestedMinCapacity": parse_and_round_quantity(
                pvc.spec.resources.requests["storage"]
            ),
            "requestedMaxCapacity": (
                parse_and_round_quantity(pvc.spec.resources.limits["storage"])
                if "storage" in (pvc.spec.resources.limits or {})
                else None
            ),
            "params": dict(sc.parameters or {}),
            "sc": self.__api_client.sanitize_for_serialization(sc),
            "pvc": self.__api_client.sanitize_for_serialization(pvc),
        }

    def __get_creation_and_deletion_context(
        self, sc: V1StorageClass, pvc: V1PersistentVolumeClaim
    ) -> dict[str, object]:

        return self.__get_dynamic_validation_context(sc=sc, pvc=pvc) | {
            "defaultHandle": f"pvc-{pvc.metadata.uid}"
        }

    def __get_staging_and_unstaging_context(
        self,
        pvc: V1PersistentVolumeClaim,
        pv: V1PersistentVolume,
        node: V1Node,
        read_only: bool,
    ) -> dict[str, object]:

        # Note that we get "accessModes" from the PVC and not from the PV, since
        # all mounts of the volume can only use the access modes specified in
        # the PVC.

        return {
            "volumeMode": pv.spec.volume_mode,
            "accessModes": list(pvc.spec.access_modes),
            "capacity": parse_and_round_quantity(pv.spec.capacity["storage"]),
            "params": dict(pv.spec.csi.volume_attributes or {}),
            "handle": pv.spec.csi.volume_handle,
            "readOnly": read_only,
            "pvc": self.__api_client.sanitize_for_serialization(pvc),
            "pv": self.__api_client.sanitize_for_serialization(pv),
            "node": self.__api_client.sanitize_for_serialization(node),
        }

    async def __evaluate_spec_field(
        self, field: str, context: Mapping[str, object]
    ) -> Any:

        # evaluate templates under the field

        evaluated_obj = await evaluate_templates(
            obj=self.__obj["spec"].get(field, {}),
            context=context,
            api_client=self.__api_client,
        )

        # validate resulting object

        try:
            yamale.validate(
                schema=Provisioner.__SCHEMA_WITHOUT_TEMPLATES.includes[field],
                data=[(evaluated_obj, None)],
            )
        except yamale.YamaleError as e:
            raise ValueError(str(e))

        # return field object

        return evaluated_obj

    def __parse_capacity(self, capacity: object, rounding_mode: str) -> int:

        cap = parse_and_round_quantity(capacity, rounding_mode=rounding_mode)

        if cap == 0:
            raise ValueError("Capacity values must be positive")

        return cap

    def __parse_capacity_opt(
        self, capacity: Optional[str], rounding_mode: str
    ) -> Optional[int]:

        if capacity is None:
            return None
        else:
            return self.__parse_capacity(capacity, rounding_mode)

    async def __create_pod_template(
        self, pod_template_spec: object
    ) -> PodTemplate:

        return await PodTemplate.new(self.__api_client, pod_template_spec)

    async def __create_pod_template_opt(
        self, pod_template_spec: Optional[object]
    ) -> Optional[PodTemplate]:

        if pod_template_spec is None:
            return None
        else:
            return await self.__create_pod_template(pod_template_spec)


# ---------------------------------------------------------------------------- #
