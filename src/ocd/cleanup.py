"""Conservative, read-only cleanup observations for an explicit directory.

A large file is not evidence of redundancy or permission to delete it.
This module cannot authorize or perform any filesystem mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .usage import UsageReport, scan_usage


BLOCKED_REASON = (
    "No verified retention, independent restore, live-use, filesystem "
    "allocation, or deletion authorization evidence"
)


@dataclass(frozen=True)
class CleanupObservation:
    path: Path
    logical_bytes: int

    @property
    def deletion_eligible(self) -> bool:
        return False

    @property
    def reclaimable_bytes(self) -> None:
        return None


@dataclass(frozen=True)
class CleanupReview:
    usage: UsageReport
    observations: tuple[CleanupObservation, ...]
    blocked_reason: str = BLOCKED_REASON


def review_cleanup(
    root: Path, *, max_entries: int = 100_000, max_depth: int = 32,
    top: int = 20,
) -> CleanupReview:
    """Return only bounded large-file observations; incomplete scans hold all.

    The observations are a read-only snapshot, never an executable deletion
    list. Reclaimable bytes remain unknown even for a complete inventory.
    """
    usage = scan_usage(
        root, max_entries=max_entries, max_depth=max_depth, top=top,
    )
    observations = (
        tuple(CleanupObservation(item.path, item.logical_bytes)
              for item in usage.largest_files)
        if usage.complete else ()
    )
    return CleanupReview(usage, observations)
