from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath

from .archive import ArchiveFile, deterministic_tar, deterministic_tgz
from .info import render_info
from .lifecycle import NOOP_LIFECYCLE_NAMES, noop_script, render_start_stop_status
from .lint import has_errors, lint_manifest
from .model import Manifest


@dataclass(frozen=True)
class BuildResult:
    output: Path
    sha256: str
    outer_members: tuple[str, ...]
    payload_members: tuple[str, ...]


def _privilege_bytes(manifest: Manifest) -> bytes:
    payload: dict[str, object] = {"defaults": {"run-as": manifest.privilege.run_as}}
    if manifest.privilege.username:
        payload["username"] = manifest.privilege.username
    if manifest.privilege.groupname:
        payload["groupname"] = manifest.privilege.groupname
    if manifest.privilege.ctrl_scripts:
        payload["ctrl-script"] = [
            {
                "action": item.action,
                "run-as": item.run_as,
            }
            for item in manifest.privilege.ctrl_scripts
        ]
    if manifest.privilege.executables:
        payload["executable"] = [
            {
                "relpath": item.relpath.as_posix(),
                "run-as": item.run_as,
            }
            for item in manifest.privilege.executables
        ]
    if manifest.privilege.tools:
        tools: list[dict[str, str]] = []
        for item in manifest.privilege.tools:
            tool = {
                "relpath": item.relpath.as_posix(),
                "user": item.user,
                "group": item.group,
                "permission": item.permission,
            }
            if item.capabilities is not None:
                tool["capabilities"] = item.capabilities
            tools.append(tool)
        payload["tool"] = tools
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _estimated_extractsize_kb(payload: list[ArchiveFile]) -> int:
    # Synology documents extractsize as a minimum free-space hint. Real packers
    # commonly use du -sk on a staged tree, but host filesystem allocation would
    # make otherwise-identical builds non-reproducible. Use a conservative,
    # deterministic 4 KiB allocation estimate instead: one block for the target
    # root, one or more blocks per file, and one block per implied directory.
    directories: set[str] = set()
    total_kb = 4
    for item in payload:
        path = PurePosixPath(item.name)
        parent = path.parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
        file_kb = max(4, ((len(item.data) + 4095) // 4096) * 4)
        total_kb += file_kb
    total_kb += 4 * len(directories)
    return total_kb


def _wizard_members(root: Path) -> list[ArchiveFile]:
    result: list[ArchiveFile] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        result.append(ArchiveFile(f"WIZARD_UIFILES/{rel}", path.read_bytes(), 0o644))
    return result


def build_spk(manifest: Manifest, output: Path) -> BuildResult:
    issues = lint_manifest(manifest)
    if has_errors(issues):
        details = "; ".join(
            f"{issue.code}: {issue.message}"
            for issue in issues
            if issue.severity == "error"
        )
        raise ValueError(f"manifest lint failed: {details}")

    payload = [
        ArchiveFile(
            entry.destination.as_posix(),
            entry.source.read_bytes(),
            entry.mode,
        )
        for entry in manifest.payload_files
    ]
    package_tgz = deterministic_tgz(payload)
    package_checksum = hashlib.md5(package_tgz, usedforsecurity=False).hexdigest()
    extractsize_kb = _estimated_extractsize_kb(payload)

    start_stop = (
        manifest.scripts.start_stop_status.read_bytes()
        if manifest.scripts.start_stop_status is not None
        else render_start_stop_status(manifest)
    )
    outer: list[ArchiveFile] = [
        ArchiveFile(
            "INFO",
            render_info(
                manifest,
                checksum=package_checksum,
                extractsize_kb=extractsize_kb,
            ),
            0o644,
        ),
        ArchiveFile("conf/privilege", _privilege_bytes(manifest), 0o644),
        ArchiveFile("package.tgz", package_tgz, 0o644),
        ArchiveFile("scripts/start-stop-status", start_stop, 0o755),
    ]

    custom_scripts = manifest.scripts.as_dict()
    for name in NOOP_LIFECYCLE_NAMES:
        source = custom_scripts[name]
        data = source.read_bytes() if source is not None else noop_script()
        outer.append(ArchiveFile(f"scripts/{name}", data, 0o755))

    if manifest.assets.resource_file is not None:
        outer.append(
            ArchiveFile(
                "conf/resource",
                manifest.assets.resource_file.read_bytes(),
                0o644,
            )
        )
    if manifest.assets.license_file is not None:
        outer.append(
            ArchiveFile("LICENSE", manifest.assets.license_file.read_bytes(), 0o644)
        )
    if manifest.assets.icon64 is not None:
        outer.append(
            ArchiveFile(
                "PACKAGE_ICON.PNG",
                manifest.assets.icon64.read_bytes(),
                0o644,
            )
        )
    if manifest.assets.icon256 is not None:
        outer.append(
            ArchiveFile(
                "PACKAGE_ICON_256.PNG",
                manifest.assets.icon256.read_bytes(),
                0o644,
            )
        )
    if manifest.assets.wizard_dir is not None:
        outer.extend(_wizard_members(manifest.assets.wizard_dir))

    archive_bytes = deterministic_tar(outer, first_member="INFO")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(archive_bytes)

    return BuildResult(
        output=output,
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
        outer_members=tuple(sorted(item.name for item in outer)),
        payload_members=tuple(sorted(item.name for item in payload)),
    )
