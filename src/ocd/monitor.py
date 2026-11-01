"""Bounded, read-only polling of explicitly supplied organizer roots.

This module never moves files, creates directories, or starts a background
thread.  Callers own the stop event and the lifetime of the polling loop.
Every observation is a fresh scan and plan, not permission to apply a plan.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from threading import Event
from typing import Callable, Iterable, Literal

from .config import verified_root
from .planner import build_plan
from .scanner import scan_root
from .types import FileEntry, PlanResult, RootConfig


@dataclass(frozen=True)
class MonitorEvent:
    """One changed observation or a recoverable per-root failure.

    A ``change`` contains the *complete* current plan (both moves and holds),
    not just newly seen files. It is emitted initially and when either the
    scanned files or plan change, including when the last file disappears.
    An ``error`` has no files or plan and reports the failure without stopping
    monitoring of other roots. Repeated identical errors are coalesced.
    """

    root: RootConfig
    kind: Literal["change", "error"]
    files: tuple[FileEntry, ...] = ()
    plan: PlanResult | None = None
    error: str | None = None


def watch_roots(
    roots: Iterable[RootConfig],
    on_event: Callable[[MonitorEvent], None],
    *,
    stop_event: Event,
    interval_seconds: float = 1.0,
    max_polls: int | None = None,
) -> None:
    """Poll registered roots until stopped or ``max_polls`` rounds complete.

    Only roots explicitly passed by the caller are inspected.  Each round
    rechecks the root directories before scanning, uses ``scan_root``'s
    symlink-safe traversal and per-root ``settle_seconds``, then calls
    ``build_plan``. This runs in the caller's thread and **never applies** any
    plan, even for a root configured with ``mode='apply'``.

    The first scan yields a change event (including an empty initial plan).
    Unchanged rounds are silent. Failures reading or planning one root become
    error events; a successful recovery emits a new change event. Exceptions
    from the caller's callback intentionally propagate so reports are not
    silently lost. ``max_polls`` is useful for deterministic bounded tests.
    """
    selected = tuple(roots)
    if not selected:
        raise ValueError("watch_roots requires at least one registered root")
    if any(not isinstance(root, RootConfig) for root in selected):
        raise TypeError("all monitored roots must be RootConfig instances")
    if not callable(on_event):
        raise TypeError("on_event must be callable")
    if not isinstance(stop_event, Event):
        raise TypeError("stop_event must be a threading.Event")
    if isinstance(interval_seconds, bool):
        raise ValueError("interval_seconds must be a finite, positive number")
    try:
        interval = float(interval_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("interval_seconds must be a finite, positive number") from exc
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval_seconds must be a finite, positive number")
    if max_polls is not None and (
        isinstance(max_polls, bool) or not isinstance(max_polls, int) or max_polls < 1
    ):
        raise ValueError("max_polls must be a positive integer or None")

    # Keep state by position, because RootConfig contains an unhashable dict.
    previous: list[tuple[tuple[FileEntry, ...], PlanResult] | None] = [None] * len(selected)
    errors: list[tuple[type[Exception], str] | None] = [None] * len(selected)
    polls = 0
    while not stop_event.is_set():
        for index, root in enumerate(selected):
            if stop_event.is_set():
                break
            try:
                verified_root(root)
                files = scan_root(root)
                plan = build_plan(root, files)
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                signature = (type(exc), str(exc))
                if errors[index] != signature:
                    on_event(MonitorEvent(root=root, kind="error", error=f"{type(exc).__name__}: {exc}"))
                errors[index] = signature
                previous[index] = None
                continue

            observation = (files, plan)
            if errors[index] is not None or previous[index] != observation:
                on_event(MonitorEvent(root=root, kind="change", files=files, plan=plan))
            errors[index] = None
            previous[index] = observation

        polls += 1
        if max_polls is not None and polls >= max_polls:
            return
        # Event.wait makes shutdown prompt and prevents a busy loop on errors.
        stop_event.wait(interval)
