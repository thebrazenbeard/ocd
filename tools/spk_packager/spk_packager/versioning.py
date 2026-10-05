from __future__ import annotations

from dataclasses import dataclass
import re

_DSM_RE = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)-(?P<build>\d+)$")
_PKG_RE = re.compile(r"^\d+(?:[._-]\d+)+$")


@dataclass(frozen=True, order=True)
class DSMVersion:
    major: int
    minor: int
    build: int

    @classmethod
    def parse(cls, value: str) -> "DSMVersion":
        match = _DSM_RE.fullmatch(value)
        if not match:
            raise ValueError(f"invalid DSM version {value!r}; expected X.Y-BUILD")
        return cls(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("build")),
        )

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}-{self.build}"


def validate_package_version(value: str) -> None:
    if not _PKG_RE.fullmatch(value):
        raise ValueError(
            f"invalid package version {value!r}; use numeric components separated by '.', '-' or '_'"
        )
    maximum = (1 << 31) - 1
    for component in re.split(r"[._-]", value):
        if int(component) > maximum:
            raise ValueError(
                f"invalid package version {value!r}; each numeric component must be <= {maximum}"
            )
