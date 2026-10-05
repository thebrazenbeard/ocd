from __future__ import annotations

import struct


def elf_machine(data: bytes) -> int | None:
    if len(data) < 20 or data[:4] != b"\x7fELF":
        return None
    if data[5] == 1:
        endian = "<"
    elif data[5] == 2:
        endian = ">"
    else:
        raise ValueError("ELF has invalid endianness byte")
    return struct.unpack(endian + "H", data[18:20])[0]
