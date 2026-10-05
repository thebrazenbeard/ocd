from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArchHint:
    family: str
    elf_machine: int | None
    evidence: str


_HINTS: dict[str, ArchHint] = {}


def _add(names: tuple[str, ...], family: str, machine: int, evidence: str) -> None:
    for name in names:
        _HINTS[name] = ArchHint(family, machine, evidence)


_add(
    (
        "x64", "x86_64", "x86", "apollolake", "avoton", "braswell",
        "broadwell", "broadwellnk", "broadwellnkv2", "broadwellntb",
        "broadwellntbap", "bromolow", "cedarview", "coffeelake",
        "denverton", "dockerx64", "epyc7002", "epyc7003",
        "epyc7003ntb", "geminilake", "geminilakenk", "grantley",
        "kvmx64", "purley", "skylaked", "v1000", "v1000nk", "r1000",
        "r1000nk",
    ),
    "x86_64",
    62,
    "Synology DSM platform mapping cross-checked against SynoCommunity architecture families",
)
_add(
    ("evansport",),
    "i686",
    3,
    "Synology DSM platform mapping cross-checked against SynoCommunity architecture families",
)
_add(
    ("88f6281",),
    "armv5",
    40,
    "SynoCommunity DSM architecture family mapping; ELF machine class is ARM",
)
_add(
    (
        "armv7", "alpine", "alpine4k", "armada370", "armada375",
        "armada38x", "armadaxp", "comcerto2k", "monaco",
    ),
    "armv7",
    40,
    "Synology DSM platform mapping cross-checked against SynoCommunity architecture families",
)
_add(
    ("hi3535",),
    "armv7l",
    40,
    "SynoCommunity DSM architecture family mapping; ELF machine class is ARM",
)
_add(
    ("aarch64", "armv8", "rtd1296", "rtd1619", "rtd1619b", "armada37xx"),
    "aarch64",
    183,
    "Synology DSM platform mapping cross-checked against SynoCommunity architecture families",
)
_add(
    ("powerpc", "ppc824x", "ppc853x", "ppc854x", "qoriq"),
    "powerpc",
    20,
    "SynoCommunity DSM architecture family mapping; ELF machine class is PowerPC",
)


def arch_hint(name: str) -> ArchHint | None:
    if name == "noarch":
        return ArchHint("noarch", None, "Synology noarch package semantics")
    return _HINTS.get(name)


def expected_elf_machines(
    arch_values: tuple[str, ...],
) -> tuple[set[int], list[str]]:
    machines: set[int] = set()
    warnings: list[str] = []
    for value in arch_values:
        hint = arch_hint(value)
        if hint is None:
            warnings.append(f"no ELF-machine hint for Synology arch {value!r}")
            continue
        if hint.elf_machine is not None:
            machines.add(hint.elf_machine)
    return machines, warnings
