from __future__ import annotations

import struct
import zlib


_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != _SIGNATURE or data[12:16] != b"IHDR":
        raise ValueError("not a valid PNG with IHDR")
    return struct.unpack(">II", data[16:24])


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def placeholder(width: int, height: int) -> bytes:
    rows = bytearray()
    stroke = max(1, width // 32)
    for y in range(height):
        rows.append(0)
        for x in range(width):
            on = abs(x - y) <= stroke or abs((width - 1 - x) - y) <= stroke
            rows.extend((220, 220, 220, 255) if on else (40, 45, 50, 255))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return _SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + _chunk(b"IEND", b"")
