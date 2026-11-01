"""Shared records for the Windows/Linux organizer.

No record grants deletion authority. A plan is data until explicitly applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RootConfig:
    source: Path
    destination: Path
    mode: str = "observe"
    tags: dict[str, str] = field(default_factory=dict)
    settle_seconds: float = 2.0


@dataclass(frozen=True)
class FileEntry:
    path: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class MovePlan:
    source: Path
    destination: Path
    tag: str
    size: int
    source_mtime_ns: int


@dataclass(frozen=True)
class Hold:
    path: Path
    reason: str


@dataclass(frozen=True)
class PlanResult:
    moves: tuple[MovePlan, ...]
    holds: tuple[Hold, ...]
