from __future__ import annotations

import binascii
import io
import struct
import tarfile
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class ArchiveFile:
    name: str
    data: bytes
    mode: int = 0o644


def validate_archive_name(name: str) -> str:
    if not name or "\\" in name:
        raise ValueError(f"unsafe archive path: {name!r}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError(f"archive path contains control characters: {name!r}")
    path = PurePosixPath(name)
    canonical = path.as_posix()
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or canonical in {"", "."}
        or canonical != name
    ):
        raise ValueError(f"unsafe or non-canonical archive path: {name!r}")
    return canonical


def _safe_name(name: str) -> str:
    return validate_archive_name(name)


def _info(item: ArchiveFile) -> tarfile.TarInfo:
    info = tarfile.TarInfo(_safe_name(item.name))
    info.size = len(item.data)
    info.mode = item.mode
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def deterministic_tar(
    files: list[ArchiveFile] | tuple[ArchiveFile, ...],
    *,
    first_member: str | None = None,
) -> bytes:
    ordered = sorted(files, key=lambda item: _safe_name(item.name))
    names = [item.name for item in ordered]
    if len(names) != len(set(names)):
        raise ValueError("duplicate archive member")
    if first_member is not None:
        match = [item for item in ordered if item.name == first_member]
        if len(match) != 1:
            raise ValueError(f"required first archive member {first_member!r} is missing")
        ordered = match + [item for item in ordered if item.name != first_member]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:", format=tarfile.USTAR_FORMAT) as tf:
        for item in ordered:
            tf.addfile(_info(item), io.BytesIO(item.data))
    return output.getvalue()


def _deterministic_gzip_stored(raw: bytes) -> bytes:
    output = bytearray(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff")
    if raw:
        offset = 0
        while offset < len(raw):
            chunk = raw[offset : offset + 0xFFFF]
            offset += len(chunk)
            output.append(0x01 if offset == len(raw) else 0x00)
            length = len(chunk)
            output.extend(struct.pack("<HH", length, length ^ 0xFFFF))
            output.extend(chunk)
    else:
        output.extend(b"\x01\x00\x00\xff\xff")
    output.extend(
        struct.pack(
            "<II",
            binascii.crc32(raw) & 0xFFFFFFFF,
            len(raw) & 0xFFFFFFFF,
        )
    )
    return bytes(output)


def deterministic_tgz(files: list[ArchiveFile] | tuple[ArchiveFile, ...]) -> bytes:
    return _deterministic_gzip_stored(deterministic_tar(files))


def safe_regular_members(tf: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = tf.getmembers()
    names = [member.name for member in members]
    if names != sorted(names):
        raise ValueError("archive members are not sorted")
    if len(names) != len(set(names)):
        raise ValueError("archive contains duplicate members")
    for member in members:
        path = PurePosixPath(member.name)
        if member.name.startswith("./"):
            raise ValueError(f"./-prefixed archive member: {member.name}")
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe member path: {member.name}")
        if not member.isfile():
            raise ValueError(f"non-regular archive member: {member.name}")
        if member.pax_headers:
            raise ValueError(f"PAX metadata is not allowed: {member.name}")
        if member.uid != 0 or member.gid != 0 or member.mtime != 0:
            raise ValueError(f"non-deterministic metadata: {member.name}")
    return members
