from __future__ import annotations

from collections.abc import Mapping
import re

from .model import Manifest

_INFO_KEY = re.compile(r"^[A-Za-z0-9_]+$")


_RESERVED = {
    "package", "version", "displayname", "os_min_ver", "os_max_ver", "description",
    "maintainer", "arch", "thirdparty", "precheckstartstop", "ctl_stop",
    "checksum", "extractsize",
}


def _quote(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("INFO values may not contain newlines, carriage returns, or NUL")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_info(
    manifest: Manifest,
    *,
    checksum: str | None = None,
    extractsize_kb: int | None = None,
) -> bytes:
    pkg = manifest.package
    fields: list[tuple[str, str]] = [
        ("package", pkg.package_id),
        ("version", pkg.version),
    ]
    if pkg.display_name:
        fields.append(("displayname", pkg.display_name))
    fields.extend([
        ("os_min_ver", pkg.os_min_ver),
        ("description", pkg.description),
        ("maintainer", pkg.maintainer),
        ("arch", " ".join(pkg.arch)),
        ("thirdparty", "yes" if pkg.thirdparty else "no"),
        ("precheckstartstop", "yes" if pkg.precheckstartstop else "no"),
    ])
    if pkg.os_max_ver:
        fields.append(("os_max_ver", pkg.os_max_ver))
    if pkg.ctl_stop is not None:
        fields.append(("ctl_stop", "yes" if pkg.ctl_stop else "no"))
    if checksum is not None:
        if not re.fullmatch(r"[0-9a-f]{32}", checksum):
            raise ValueError("checksum must be a lowercase MD5 hex string")
        fields.append(("checksum", checksum))
    if extractsize_kb is not None:
        if extractsize_kb < 0:
            raise ValueError("extractsize_kb must be non-negative")
        fields.append(("extractsize", str(extractsize_kb)))

    for key in manifest.info_extra:
        if key in _RESERVED:
            raise ValueError(f"info.extra may not override reserved INFO field {key!r}")
        if not _INFO_KEY.fullmatch(key):
            raise ValueError(f"invalid INFO field name {key!r}")
    fields.extend((key, manifest.info_extra[key]) for key in sorted(manifest.info_extra))

    return "".join(f"{key}={_quote(value)}\n" for key, value in fields).encode("utf-8")


def _unquote(value: str, line_number: int) -> str:
    if not value.startswith('"'):
        if value.endswith('"'):
            raise ValueError(f"INFO line {line_number} has an unmatched quote")
        return value
    if len(value) < 2 or not value.endswith('"'):
        raise ValueError(f"INFO line {line_number} has an unmatched quote")

    body = value[1:-1]
    result: list[str] = []
    index = 0
    while index < len(body):
        ch = body[index]
        if ch != "\\":
            result.append(ch)
            index += 1
            continue
        index += 1
        if index >= len(body):
            raise ValueError(f"INFO line {line_number} ends with an incomplete escape")
        escaped = body[index]
        if escaped not in {'"', "\\"}:
            raise ValueError(
                f"INFO line {line_number} contains unsupported escape \\{escaped}"
            )
        result.append(escaped)
        index += 1
    return "".join(result)


def parse_info(data: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_number, raw in enumerate(data.decode("utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"INFO line {line_number} is not key=value")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not _INFO_KEY.fullmatch(key):
            raise ValueError(f"INFO line {line_number} has invalid field name {key!r}")
        value = _unquote(value, line_number)
        if key in result:
            raise ValueError(f"duplicate INFO field {key!r}")
        result[key] = value
    return result


def render_properties(fields: Mapping[str, str]) -> bytes:
    return "".join(
        f"{key}={_quote(str(value))}\n"
        for key, value in fields.items()
    ).encode("utf-8")
