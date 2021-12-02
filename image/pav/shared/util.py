# ---------------------------------------------------------------------------- #

from __future__ import annotations

import struct
from collections.abc import Callable, Iterable, MutableMapping, MutableSequence
from datetime import datetime
from fcntl import ioctl
from pathlib import Path
from sys import stderr
from typing import Optional, TypeVar, Union

# ---------------------------------------------------------------------------- #

T = TypeVar("T")
U = TypeVar("U")


def remove_if_in(sequence: MutableSequence[T], item: T) -> None:
    if item in sequence:
        sequence.remove(item)


class NoDefaultValue:
    pass


def mutate_value(
    mapping: MutableMapping[T, U],
    key: T,
    f: Callable[[U], U],
    *,
    default_value: Union[NoDefaultValue, U] = NoDefaultValue(),
) -> U:

    value = (
        mapping[key]
        if isinstance(default_value, NoDefaultValue)
        else mapping.get(key, default_value)
    )

    new_value = f(value)
    mapping[key] = new_value

    return new_value


def ensure_singleton(iterable: Iterable[T]) -> T:

    it = iter(iterable)
    value = next(it)

    try:
        next(it)
    except StopIteration:
        return value  # iterable is singleton
    else:
        raise ValueError  # iterable has more than one element


def ensure_empty_or_singleton(iterable: Iterable[T]) -> Optional[T]:

    it = iter(iterable)
    value = next(it, None)

    try:
        next(it)
    except StopIteration:
        return value  # iterable is empty or singleton
    else:
        raise ValueError  # iterable has more than one element


# ---------------------------------------------------------------------------- #


def log(obj: object) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    print(f"\033[36m[{now}]\033[0m {obj}", file=stderr, flush=True)


# ---------------------------------------------------------------------------- #


def get_block_device_size(path: Path) -> int:
    """Result is in bytes."""

    command = 0x80081272  # BLKGETSIZE64

    with path.open("rb") as file:
        buffer = ioctl(file, command, b" " * 8)

    size = struct.unpack("Q", buffer)[0]
    assert type(size) is int

    return size


def find_top_level_mounts(directory_path: Path) -> set[Path]:
    """Return a sequence of all mount points under the given directory
    (excluding the directory itself) that are top-level, i.e., that themselves
    are not under any mount point other than the given directory itself or any
    of its parents."""

    assert directory_path.is_absolute()

    def decode_path(b: bytes) -> Path:
        return Path(b.decode("unicode_escape").encode("latin1").decode("utf-8"))

    all_mount_points = {
        decode_path(line.split()[4])
        for line in Path("/proc/self/mountinfo").read_bytes().split(b"\n")
        if line
    }

    assert all(mp.is_absolute() for mp in all_mount_points)

    mount_points_under_dir = {
        mp for mp in all_mount_points if directory_path in mp.parents
    }

    return {
        mp
        for mp in mount_points_under_dir
        if mount_points_under_dir.isdisjoint(mp.parents)
    }


# ---------------------------------------------------------------------------- #
