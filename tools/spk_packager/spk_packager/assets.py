from __future__ import annotations

from pathlib import Path

from .png import dimensions as png_dimensions
from .png import placeholder


def placeholder_png(size: int) -> bytes:
    return placeholder(size, size)


def write_placeholder_icon(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(placeholder_png(size))
