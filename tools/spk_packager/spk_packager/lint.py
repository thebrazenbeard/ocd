from __future__ import annotations

from dataclasses import dataclass
import json

from .arch import expected_elf_machines
from .assets import png_dimensions
from .elf import elf_machine
from .lifecycle import handles_case_action
from .model import Manifest
from .profiles import get_profile
from .versioning import DSMVersion


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


def _is_within(path, root) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def lint_manifest(manifest: Manifest) -> list[Issue]:
    issues: list[Issue] = []
    profile = get_profile(manifest.profile_id)
    min_os = DSMVersion.parse(manifest.package.os_min_ver)
    if min_os < profile.minimum_os:
        issues.append(Issue(
            "error", "DSM_MIN_BELOW_PROFILE",
            f"{manifest.profile_id} requires os_min_ver >= {profile.minimum_os}, got {min_os}",
        ))
    if manifest.package.os_max_ver:
        max_os = DSMVersion.parse(manifest.package.os_max_ver)
        if max_os < min_os:
            issues.append(Issue("error", "DSM_RANGE_INVALID", "os_max_ver is lower than os_min_ver"))

    if manifest.privilege.run_as == "root":
        issues.append(Issue(
            "error" if manifest.strict else "warning",
            "ROOT_PRIVILEGE_REQUESTED",
            "DSM 7 expects package-user execution; root packages require a separately justified/signed development path and are rejected by the strict profile",
        ))
    root_overrides = [
        f"ctrl-script:{item.action}"
        for item in manifest.privilege.ctrl_scripts
        if item.run_as == "root"
    ]
    root_overrides.extend(
        f"executable:{item.relpath.as_posix()}"
        for item in manifest.privilege.executables
        if item.run_as == "root"
    )
    if root_overrides:
        issues.append(Issue(
            "error" if manifest.strict else "warning",
            "ROOT_PRIVILEGE_OVERRIDE",
            "strict packaging rejects per-action/file root overrides: "
            + ", ".join(root_overrides),
        ))

    if not manifest.allow_external_sources:
        local_inputs = [
            ("assets.icon64", manifest.assets.icon64),
            ("assets.icon256", manifest.assets.icon256),
            ("assets.license", manifest.assets.license_file),
            ("assets.wizard_dir", manifest.assets.wizard_dir),
            ("assets.resource", manifest.assets.resource_file),
        ]
        local_inputs.extend(
            (f"scripts.{name}", path)
            for name, path in manifest.scripts.as_dict().items()
        )
        local_inputs.extend(
            (f"payload.files[{index}].source", entry.source)
            for index, entry in enumerate(manifest.payload_files)
        )
        for label, path in local_inputs:
            if path is not None and not _is_within(path, manifest.root):
                issues.append(Issue(
                    "error" if manifest.strict else "warning",
                    "EXTERNAL_SOURCE_PATH",
                    f"{label} resolves outside the manifest directory: {path}",
                ))

    if manifest.strict and profile.require_icons:
        for label, path, expected in (
            ("PACKAGE_ICON.PNG", manifest.assets.icon64, (64, 64)),
            ("PACKAGE_ICON_256.PNG", manifest.assets.icon256, (256, 256)),
        ):
            if path is None or not path.is_file():
                issues.append(Issue("error", "ICON_MISSING", f"{label} is required by the strict profile"))
                continue
            try:
                actual = png_dimensions(path.read_bytes())
            except Exception as exc:
                issues.append(Issue("error", "ICON_INVALID", f"{label}: {exc}"))
            else:
                if actual != expected:
                    issues.append(Issue(
                        "error", "ICON_DIMENSIONS",
                        f"{label} must be {expected[0]}x{expected[1]}, got {actual[0]}x{actual[1]}",
                    ))

    if manifest.assets.license_file is not None:
        if not manifest.assets.license_file.is_file():
            issues.append(Issue("error", "LICENSE_MISSING", f"license file not found: {manifest.assets.license_file}"))
        elif manifest.assets.license_file.stat().st_size >= 1_000_000:
            issues.append(Issue("error", "LICENSE_TOO_LARGE", "DSM package LICENSE must be smaller than 1 MB"))
    if manifest.assets.wizard_dir is not None:
        if not manifest.assets.wizard_dir.is_dir():
            issues.append(Issue(
                "error",
                "WIZARD_DIR_MISSING",
                f"wizard directory not found: {manifest.assets.wizard_dir}",
            ))
        else:
            for path in manifest.assets.wizard_dir.rglob("*"):
                if path.is_symlink():
                    issues.append(Issue(
                        "error",
                        "WIZARD_SYMLINK",
                        f"wizard tree may not contain symlinks: {path}",
                    ))
                    continue
                if path.is_file() and not _is_within(
                    path.resolve(),
                    manifest.assets.wizard_dir,
                ):
                    issues.append(Issue(
                        "error",
                        "WIZARD_PATH_ESCAPE",
                        f"wizard file resolves outside wizard_dir: {path}",
                    ))
    if manifest.assets.resource_file is not None:
        if not manifest.assets.resource_file.is_file():
            issues.append(Issue("error", "RESOURCE_MISSING", f"resource file not found: {manifest.assets.resource_file}"))
        else:
            try:
                payload = json.loads(manifest.assets.resource_file.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("resource root must be a JSON object")
            except Exception as exc:
                issues.append(Issue("error", "RESOURCE_INVALID", f"invalid conf/resource JSON: {exc}"))

    for script_name, script_path in manifest.scripts.as_dict().items():
        if script_path is None:
            continue
        if not script_path.is_file():
            issues.append(Issue("error", "SCRIPT_MISSING", f"{script_name} script not found: {script_path}"))
            continue
        data = script_path.read_bytes()
        if not data.startswith(b"#!"):
            issues.append(Issue("warning", "SCRIPT_NO_SHEBANG", f"{script_name} does not start with a shebang"))
        if b"\r\n" in data:
            issues.append(Issue("warning", "SCRIPT_CRLF", f"{script_name} uses CRLF line endings; DSM shell scripts should use LF"))
        if script_name == "start_stop_status":
            text = data.decode("utf-8", errors="replace")
            if not handles_case_action(text, "status"):
                issues.append(Issue("error", "STATUS_HANDLER_MISSING", "custom start-stop-status lacks a status handler"))
            if manifest.package.precheckstartstop and (
                not handles_case_action(text, "prestart")
                or not handles_case_action(text, "prestop")
            ):
                issues.append(Issue(
                    "error", "PRECHECK_HANDLER_MISSING",
                    "precheckstartstop=yes requires custom start-stop-status to handle prestart and prestop",
                ))

    destinations: set[str] = set()
    service_dest = manifest.service.command if manifest.service.enabled else None
    service_found = False
    service_machine: int | None = None
    native_machines: list[tuple[str, int]] = []
    arch_machines, arch_warnings = expected_elf_machines(manifest.package.arch)
    for warning in arch_warnings:
        issues.append(Issue("warning", "ARCH_UNKNOWN", warning))

    for entry in manifest.payload_files:
        dest = entry.destination.as_posix()
        if dest in destinations:
            issues.append(Issue("error", "PAYLOAD_DUPLICATE", f"duplicate payload destination: {dest}"))
        if any(dest.startswith(existing + "/") or existing.startswith(dest + "/") for existing in destinations):
            issues.append(Issue(
                "error", "PAYLOAD_PATH_CONFLICT",
                f"payload file path conflicts with another file-as-parent path: {dest}",
            ))
        destinations.add(dest)
        if not entry.source.is_file():
            issues.append(Issue("error", "PAYLOAD_SOURCE_MISSING", f"payload source not found: {entry.source}"))
            continue
        if service_dest == dest:
            service_found = True
            if entry.mode & 0o111 == 0:
                issues.append(Issue(
                    "error", "SERVICE_NOT_EXECUTABLE",
                    f"service payload {dest} has non-executable mode {entry.mode:o}",
                ))
        data = entry.source.read_bytes()
        machine = elf_machine(data)
        if service_dest == dest:
            service_machine = machine
        if machine is None:
            continue
        native_machines.append((dest, machine))
        if entry.expected_elf_machine is not None and machine != entry.expected_elf_machine:
            issues.append(Issue(
                "error", "ELF_MACHINE_OVERRIDE_MISMATCH",
                f"{dest}: ELF e_machine={machine}, expected {entry.expected_elf_machine}",
            ))
        if "noarch" in manifest.package.arch and not manifest.package.allow_noarch_native_bundle:
            issues.append(Issue(
                "error", "NOARCH_NATIVE_BINARY",
                f"{dest} is a native ELF binary but package.arch includes noarch; set package.allow_noarch_native_bundle=true only when a portable dispatcher selects a compatible bundled binary",
            ))
        elif "noarch" not in manifest.package.arch and arch_machines and machine not in arch_machines:
            issues.append(Issue(
                "error", "ELF_ARCH_MISMATCH",
                f"{dest}: ELF e_machine={machine} does not match hints {sorted(arch_machines)} for arch={manifest.package.arch}",
            ))

    if manifest.service.enabled and not service_found:
        issues.append(Issue(
            "error", "SERVICE_PAYLOAD_MISSING",
            f"service.command {service_dest!r} is not one of the payload destinations",
        ))

    if manifest.package.allow_noarch_native_bundle:
        if manifest.package.arch != ("noarch",):
            issues.append(Issue(
                "error", "NOARCH_BUNDLE_ARCH_INVALID",
                "package.allow_noarch_native_bundle=true requires package.arch=['noarch']",
            ))
        if manifest.service.enabled and service_machine is not None:
            issues.append(Issue(
                "error", "NOARCH_BUNDLE_NATIVE_DISPATCHER",
                "a noarch native bundle requires service.command to be a non-ELF portable dispatcher",
            ))
        if not native_machines:
            issues.append(Issue(
                "warning", "NOARCH_BUNDLE_EMPTY",
                "allow_noarch_native_bundle=true but no native ELF payloads were found",
            ))
        else:
            issues.append(Issue(
                "warning", "NOARCH_NATIVE_BUNDLE_EXPLICIT",
                f"noarch package explicitly bundles native ELF payloads: {native_machines}",
            ))

    executable_paths: set[str] = set()
    for item in manifest.privilege.executables:
        relpath = item.relpath.as_posix()
        if relpath in executable_paths:
            issues.append(Issue(
                "error",
                "PRIVILEGE_EXECUTABLE_DUPLICATE",
                f"duplicate privilege.executable relpath: {relpath}",
            ))
        executable_paths.add(relpath)
        if relpath not in destinations:
            issues.append(Issue(
                "error",
                "PRIVILEGE_EXECUTABLE_TARGET_MISSING",
                f"privilege.executable target is not in payload: {relpath}",
            ))

    tool_paths: set[str] = set()
    for item in manifest.privilege.tools:
        relpath = item.relpath.as_posix()
        if relpath in tool_paths:
            issues.append(Issue(
                "error", "PRIVILEGE_TOOL_DUPLICATE",
                f"duplicate privilege.tool relpath: {relpath}",
            ))
        tool_paths.add(relpath)
        if relpath not in destinations:
            issues.append(Issue(
                "error", "PRIVILEGE_TOOL_TARGET_MISSING",
                f"privilege.tool target is not in payload: {relpath}",
            ))
        if item.capabilities is not None and min_os < DSMVersion.parse("7.0-40656"):
            issues.append(Issue(
                "error", "PRIVILEGE_TOOL_CAPABILITY_DSM_TOO_OLD",
                "privilege.tool capabilities require DSM 7.0-40656 or newer",
            ))

    if (
        native_machines
        and arch_warnings
        and manifest.strict
        and "noarch" not in manifest.package.arch
    ):
        issues.append(Issue(
            "error",
            "NATIVE_ARCH_UNVERIFIED",
            "native ELF payload is present but one or more package.arch values "
            "have no known ELF-machine mapping",
        ))
    if native_machines and len({machine for _, machine in native_machines}) > 1:
        issues.append(Issue(
            "warning", "MIXED_ELF_MACHINES",
            f"payload contains multiple ELF machine classes: {native_machines}",
        ))
    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(issue.severity == "error" for issue in issues)
