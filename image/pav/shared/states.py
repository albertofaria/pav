# ---------------------------------------------------------------------------- #

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, ClassVar, Optional, TypeVar

from grpc import StatusCode  # type: ignore

# ---------------------------------------------------------------------------- #

_StateT = TypeVar("_StateT", bound="_State")


@dataclass(frozen=True)
class _State:

    __ENCODE: ClassVar[Mapping[Any, Callable[[Any], Any]]] = {
        StatusCode: lambda sc: sc.name,
        Path: str,
        int: str,
        bool: str,
        str: str,
        Optional[int]: lambda i: None if i is None else str(i),
        Optional[str]: lambda s: s,
        "StatusCode": lambda sc: sc.name,
        "Path": str,
        "int": str,
        "bool": str,
        "str": str,
        "Optional[int]": lambda i: None if i is None else str(i),
        "Optional[str]": lambda s: s,
    }

    __DECODE: ClassVar[Mapping[Any, Callable[[Any], Any]]] = {
        StatusCode: lambda v: StatusCode[v],
        Path: Path,
        int: int,
        bool: bool,
        str: str,
        Optional[int]: lambda v: None if v is None else int(v),
        Optional[str]: lambda v: v,
        "StatusCode": lambda v: StatusCode[v],
        "Path": Path,
        "int": int,
        "bool": bool,
        "str": str,
        "Optional[int]": lambda v: None if v is None else int(v),
        "Optional[str]": lambda v: v,
    }

    @classmethod
    def _from_json(
        cls: type[_StateT], state_namespace_type: type, json_string: str
    ) -> _StateT:

        obj = json.loads(json_string)
        assert isinstance(obj, dict) and all(type(key) is str for key in obj)

        state_cls = vars(state_namespace_type)[obj.pop("name")]
        assert issubclass(state_cls, cls)

        assert obj.keys() == {field.name for field in fields(state_cls)}

        kwargs = {
            field.name: _State.__DECODE[field.type](obj[field.name])
            for field in fields(state_cls)
        }

        state = state_cls(**kwargs)
        assert isinstance(state, cls)

        return state

    def to_json(self) -> str:

        obj = {
            field.name: _State.__ENCODE[field.type](getattr(self, field.name))
            for field in fields(self)
        }

        return json.dumps({"name": type(self).__name__} | obj)


# ---------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VolumeProvisioningState(_State):
    @classmethod
    def from_json(cls, json_string: str) -> VolumeProvisioningState:
        return cls._from_json(
            state_namespace_type=VolumeProvisioningStates,
            json_string=json_string,
        )


@dataclass(frozen=True)
class VolumeProvisioningStateAfterCreated(VolumeProvisioningState):
    pass


@dataclass(frozen=True)
class VolumeProvisioningStateWithFailure(VolumeProvisioningState):
    error_code: StatusCode
    error_details: str


class VolumeProvisioningStates:
    """
    Possible states of the state machine for volume validation, creation, and
    deletion.

    The diagram below depicts the possible state transitions, except those that
    end in UnrecoverableError. States enclosed in [ ] are handled by the
    controller agent; others are either handled by a node agent or are not
    handled at all.

    ```
    +-- [LaunchValidationPod] ---------------------------------------+
    |             |                                                  |
    |             v                                                  |
    |    AwaitValidationPod ---------------------+                   |
    |             |                              |                   |
    |             v                              v                   |
    |    RemoveValidationPod      RemoveValidationPodAfterFailure -->+
    |             |                                                  |
    |             v                                                  |
    +--> [LaunchCreationPod] --------------------------------------->+
            |     |                                                  |
    +-------+     v                                                  |
    |     AwaitCreationPod ----------------------+                   |
    |             |                              |                   |
    |             v                              v                   |
    |     RemoveCreationPod        RemoveCreationPodAfterFailure     |
    |             |                              |                   |
    |             v                              |                   |
    +--------> Created                           |                   |
                  |                              |                   |
                  v                              v                   |
    +--- [LaunchDeletionPod]      [LaunchDeletionPodAfterFailure] -->+
    |             |                              |                   |
    |             v                              v                   |
    |     AwaitDeletionPod         AwaitDeletionPodAfterFailure      |
    |             |                              |                   |
    |             v                              v                   |
    |     RemoveDeletionPod        RemoveDeletionPodAfterFailure     |
    |             |                              |                   |
    |             v                              v                   |
    +--------> Deleted                    CreationFailed <-----------+
    ```
    """

    @dataclass(frozen=True)
    class LaunchValidationPod(VolumeProvisioningState):
        pass

    @dataclass(frozen=True)
    class AwaitValidationPod(VolumeProvisioningState):
        validation_pod_namespace: str

    @dataclass(frozen=True)
    class RemoveValidationPod(VolumeProvisioningState):
        validation_pod_namespace: str

    @dataclass(frozen=True)
    class LaunchCreationPod(VolumeProvisioningState):
        pass

    @dataclass(frozen=True)
    class AwaitCreationPod(VolumeProvisioningState):
        creation_pod_namespace: str
        handle: Optional[str]
        capacity: Optional[int]

    @dataclass(frozen=True)
    class RemoveCreationPod(VolumeProvisioningState):
        creation_pod_namespace: str
        handle: str
        capacity: int

    @dataclass(frozen=True)
    class Created(VolumeProvisioningState):
        handle: str
        capacity: int

    @dataclass(frozen=True)
    class LaunchDeletionPod(VolumeProvisioningStateAfterCreated):
        pass

    @dataclass(frozen=True)
    class AwaitDeletionPod(VolumeProvisioningStateAfterCreated):
        deletion_pod_namespace: str

    @dataclass(frozen=True)
    class RemoveDeletionPod(VolumeProvisioningStateAfterCreated):
        deletion_pod_namespace: str

    @dataclass(frozen=True)
    class Deleted(VolumeProvisioningStateAfterCreated):
        pass

    @dataclass(frozen=True)
    class RemoveValidationPodAfterFailure(VolumeProvisioningStateWithFailure):
        validation_pod_namespace: str

    @dataclass(frozen=True)
    class RemoveCreationPodAfterFailure(VolumeProvisioningStateWithFailure):
        creation_pod_namespace: str

    @dataclass(frozen=True)
    class LaunchDeletionPodAfterFailure(VolumeProvisioningStateWithFailure):
        pass

    @dataclass(frozen=True)
    class AwaitDeletionPodAfterFailure(VolumeProvisioningStateWithFailure):
        deletion_pod_namespace: str

    @dataclass(frozen=True)
    class RemoveDeletionPodAfterFailure(VolumeProvisioningStateWithFailure):
        deletion_pod_namespace: str

    @dataclass(frozen=True)
    class CreationFailed(VolumeProvisioningStateWithFailure):
        pass

    @dataclass(frozen=True)
    class UnrecoverableFailure(VolumeProvisioningStateWithFailure):
        pass


# ---------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VolumeStagingState(_State):
    @classmethod
    def from_json(cls, json_string: str) -> VolumeStagingState:
        return cls._from_json(
            state_namespace_type=VolumeStagingStates,
            json_string=json_string,
        )


@dataclass(frozen=True)
class VolumeStagingStateAfterStaged(VolumeStagingState):
    pass


@dataclass(frozen=True)
class VolumeStagingStateFailure(VolumeStagingState):
    error_code: StatusCode
    error_details: str


class VolumeStagingStates:
    """
    Possible states of the state machine for volume staging and unstaging.

    The diagram below depicts the possible state transitions, except those that
    end in UnrecoverableError. All states are either handled by a node agent or
    are not handled at all.

    ```
          LaunchStagingPod ---------------------+------------------+
                  |                             |                  |
                  v                             v                  |
           AwaitStagingPod ---------------------+                  |
                  |                             |                  |
                  v                             |                  |
               Staged                           |                  |
                  |                             |                  |
                  v                             v                  |
          RemoveStagingPod        RemoveStagingPodAfterFailure     |
                  |                             |                  |
                  v                             v                  |
    +--- LaunchUnstagingPod      LaunchUnstagingPodAfterFailure -->+
    |             |                             |                  |
    |             v                             v                  |
    |     AwaitUnstagingPod       AwaitUnstagingPodAfterFailure    |
    |             |                             |                  |
    |             v                             v                  |
    |    RemoveUnstagingPod      RemoveUnstagingPodAfterFailure    |
    |             |                             |                  |
    |             v                             v                  |
    +-------> Unstaged                    StagingFailed <----------+
    ```
    """

    @dataclass(frozen=True)
    class LaunchStagingPod(VolumeStagingState):
        pass

    @dataclass(frozen=True)
    class AwaitStagingPod(VolumeStagingState):
        staging_pod_namespace: str

    @dataclass(frozen=True)
    class Staged(VolumeStagingState):
        staging_pod_namespace: str

    @dataclass(frozen=True)
    class RemoveStagingPod(VolumeStagingStateAfterStaged):
        staging_pod_namespace: str

    @dataclass(frozen=True)
    class LaunchUnstagingPod(VolumeStagingStateAfterStaged):
        pass

    @dataclass(frozen=True)
    class AwaitUnstagingPod(VolumeStagingStateAfterStaged):
        unstaging_pod_namespace: str

    @dataclass(frozen=True)
    class RemoveUnstagingPod(VolumeStagingStateAfterStaged):
        unstaging_pod_namespace: str

    @dataclass(frozen=True)
    class Unstaged(VolumeStagingStateAfterStaged):
        pass

    @dataclass(frozen=True)
    class RemoveStagingPodAfterFailure(VolumeStagingStateFailure):
        staging_pod_namespace: str

    @dataclass(frozen=True)
    class LaunchUnstagingPodAfterFailure(VolumeStagingStateFailure):
        pass

    @dataclass(frozen=True)
    class AwaitUnstagingPodAfterFailure(VolumeStagingStateFailure):
        unstaging_pod_namespace: str

    @dataclass(frozen=True)
    class RemoveUnstagingPodAfterFailure(VolumeStagingStateFailure):
        unstaging_pod_namespace: str

    @dataclass(frozen=True)
    class StagingFailed(VolumeStagingStateFailure):
        pass

    @dataclass(frozen=True)
    class UnrecoverableFailure(VolumeStagingStateFailure):
        pass


# ---------------------------------------------------------------------------- #
