from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

from .arch import expected_elf_machines
from .archive import validate_archive_name
from .assets import png_dimensions
from .elf import elf_machine
from .info import parse_info
from .lifecycle import handles_case_action
from .model import PRIVILEGE_ACTIONS, validate_arch_values, validate_package_id
from .profiles import get_profile
from .versioning import DSMVersion, validate_package_version


@dataclass
class VerificationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "details": self.details,
        }


def _safe_members(
    tf: tarfile.TarFile,
    report: VerificationReport,
    layer: str,
    *,
    strict: bool,
) -> list[tarfile.TarInfo]:
    members = tf.getmembers()
    names = [m.name for m in members]
    if len(names) != len(set(names)):
        report.errors.append(f"{layer}: duplicate archive members")
    if strict and names != sorted(names):
        report.errors.append(
            f"{layer}: archive members are not lexicographically sorted"
        )
    for member in members:
        candidate = member.name
        if member.isdir():
            candidate = candidate.rstrip("/")
        if candidate.startswith("./"):
            if layer == "outer" or strict:
                report.errors.append(
                    f"{layer}: ./-prefixed path {member.name!r}"
                )
            candidate = candidate[2:]
        try:
            validate_archive_name(candidate)
        except ValueError as exc:
            report.errors.append(f"{layer}: {exc}")
        if not (member.isfile() or member.isdir()):
            report.errors.append(
                f"{layer}: unsupported archive member type {member.name!r}"
            )
        if member.pax_headers:
            if layer == "outer" or strict:
                report.errors.append(
                    f"{layer}: PAX metadata on {member.name!r}"
                )
            else:
                report.warnings.append(
                    f"{layer}: PAX metadata on {member.name!r}"
                )
        if (
            strict
            and (member.uid != 0 or member.gid != 0 or member.mtime != 0)
        ):
            report.errors.append(
                f"{layer}: non-deterministic metadata on {member.name!r}"
            )
    return members


def _check_gzip_header(
    data: bytes,
    report: VerificationReport,
    *,
    strict: bool,
) -> None:
    if len(data) < 10 or data[:3] != b"\x1f\x8b\x08":
        report.errors.append("package.tgz does not have a valid gzip header")
        return
    flags = data[3]
    mtime = int.from_bytes(data[4:8], "little")
    report.details["package_tgz_gzip_mtime"] = mtime
    report.details["package_tgz_gzip_flags"] = flags
    if mtime != 0:
        message = (
            f"package.tgz gzip mtime is nonzero ({mtime}); "
            "archive is not reproducible"
        )
        if strict:
            report.errors.append(message)
        else:
            report.warnings.append(message)
    metadata_flags = flags & (0x04 | 0x08 | 0x10)
    if metadata_flags:
        message = (
            "package.tgz gzip header carries optional filename/comment/extra "
            f"metadata flags 0x{metadata_flags:02x}"
        )
        if strict:
            report.errors.append(message)
        else:
            report.warnings.append(message)


def _validate_info_semantics(
    info: dict[str, str],
    report: VerificationReport,
) -> tuple[str, ...]:
    try:
        validate_package_id(info.get("package", ""))
    except ValueError as exc:
        report.errors.append(f"INFO package invalid: {exc}")

    arch_values = tuple(x for x in info.get("arch", "").split() if x)
    try:
        validate_arch_values(arch_values)
    except ValueError as exc:
        report.errors.append(f"INFO arch invalid: {exc}")

    for key in ("thirdparty", "precheckstartstop", "ctl_stop"):
        if key in info and info[key] not in {"yes", "no"}:
            report.errors.append(
                f"INFO {key} must be 'yes' or 'no', got {info[key]!r}"
            )
    return arch_values


def verify_spk(
    path: Path,
    *,
    profile_id: str = "dsm-7.2.2+",
    strict: bool = True,
    allow_noarch_native_bundle: bool = False,
) -> VerificationReport:
    report = VerificationReport()
    path = path.resolve()
    profile = get_profile(profile_id)
    report.details["profile"] = profile_id
    report.details["spk"] = str(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        report.errors.append(f"cannot read SPK: {exc}")
        return report
    report.details["sha256"] = hashlib.sha256(raw).hexdigest()
    report.details["bytes"] = len(raw)
    try:
        outer_tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except Exception as exc:
        report.errors.append(f"outer archive is invalid: {exc}")
        return report
    info: dict[str, str] = {}
    info_arch_values: tuple[str, ...] = ()
    declared_extractsize: int | None = None
    package_bytes: bytes | None = None
    privilege_executable_paths: set[str] = set()
    privilege_tool_paths: set[str] = set()
    with outer_tf:
        outer_members = _safe_members(
            outer_tf,
            report,
            "outer",
            strict=strict,
        )
        outer_names = {m.name for m in outer_members}
        outer_by_name = {m.name: m for m in outer_members}
        report.details["outer_members"] = len(outer_members)
        if outer_members and outer_members[0].name != "INFO":
            report.errors.append(
                f"outer: INFO must be the first archive member, found {outer_members[0].name!r}"
            )
        required = {
            "INFO", "package.tgz", "conf/privilege", "scripts/start-stop-status",
            "scripts/preinst", "scripts/postinst", "scripts/preuninst", "scripts/postuninst",
            "scripts/preupgrade", "scripts/postupgrade",
        }
        if strict and profile.require_icons:
            required.update({"PACKAGE_ICON.PNG", "PACKAGE_ICON_256.PNG"})
        missing = sorted(required - outer_names)
        if missing:
            report.errors.append(f"missing outer members: {missing}")
        for name, member in outer_by_name.items():
            if (
                member.isfile()
                and name.startswith("scripts/")
                and member.mode & 0o111 == 0
            ):
                report.errors.append(
                    f"{name} is not executable (mode {member.mode:o})"
                )

        def read_outer(name: str) -> bytes | None:
            try:
                member = outer_tf.extractfile(name)
            except KeyError:
                return None
            return member.read() if member else None

        info_bytes = read_outer("INFO")
        if info_bytes is not None:
            try:
                info = parse_info(info_bytes)
            except Exception as exc:
                report.errors.append(f"INFO is invalid: {exc}")
        report.details["info"] = info
        if info:
            info_arch_values = _validate_info_semantics(info, report)

        privilege = read_outer("conf/privilege")
        if privilege is not None:
            try:
                parsed = json.loads(privilege)
                if not isinstance(parsed, dict):
                    raise ValueError("root must be a JSON object")
                run_as = parsed["defaults"]["run-as"]
                if run_as not in {"package", "root"}:
                    raise ValueError("defaults.run-as must be package or root")
                if run_as == "root":
                    message = "conf/privilege requests root execution; DSM 7 strict packaging expects package-user execution"
                    if strict:
                        report.errors.append(message)
                    else:
                        report.warnings.append(message)

                ctrl_scripts = parsed.get("ctrl-script", [])
                if not isinstance(ctrl_scripts, list):
                    raise ValueError("ctrl-script must be an array")
                seen_actions: set[str] = set()
                for index, item in enumerate(ctrl_scripts):
                    if not isinstance(item, dict):
                        raise ValueError(f"ctrl-script[{index}] must be an object")
                    action = item.get("action")
                    entry_run_as = item.get("run-as")
                    if action not in PRIVILEGE_ACTIONS:
                        raise ValueError(
                            f"ctrl-script[{index}].action is unsupported: {action!r}"
                        )
                    if action in seen_actions:
                        raise ValueError(f"duplicate ctrl-script action {action!r}")
                    seen_actions.add(action)
                    if entry_run_as not in {"package", "root"}:
                        raise ValueError(
                            f"ctrl-script[{index}].run-as must be package or root"
                        )
                    if entry_run_as == "root":
                        message = (
                            f"conf/privilege ctrl-script {action!r} requests root execution"
                        )
                        if strict:
                            report.errors.append(message)
                        else:
                            report.warnings.append(message)

                executables = parsed.get("executable", [])
                if not isinstance(executables, list):
                    raise ValueError("executable must be an array")
                seen_executables: set[str] = set()
                for index, item in enumerate(executables):
                    if not isinstance(item, dict):
                        raise ValueError(f"executable[{index}] must be an object")
                    relpath = str(item.get("relpath", ""))
                    try:
                        validate_archive_name(relpath)
                    except ValueError as exc:
                        raise ValueError(
                            f"executable[{index}].relpath is unsafe: {exc}"
                        ) from exc
                    if relpath in seen_executables:
                        raise ValueError(f"duplicate executable relpath {relpath!r}")
                    seen_executables.add(relpath)
                    privilege_executable_paths.add(relpath)
                    entry_run_as = item.get("run-as")
                    if entry_run_as not in {"package", "root"}:
                        raise ValueError(
                            f"executable[{index}].run-as must be package or root"
                        )
                    if entry_run_as == "root":
                        message = (
                            f"conf/privilege executable {relpath!r} requests root ownership"
                        )
                        if strict:
                            report.errors.append(message)
                        else:
                            report.warnings.append(message)

                tools = parsed.get("tool", [])
                if not isinstance(tools, list):
                    raise ValueError("tool must be an array")
                seen_tool_paths: set[str] = set()
                for index, tool in enumerate(tools):
                    if not isinstance(tool, dict):
                        raise ValueError(f"tool[{index}] must be an object")
                    relpath = str(tool.get("relpath", ""))
                    try:
                        validate_archive_name(relpath)
                    except ValueError as exc:
                        raise ValueError(
                            f"tool[{index}].relpath is unsafe: {exc}"
                        ) from exc
                    if relpath in seen_tool_paths:
                        raise ValueError(f"duplicate tool relpath {relpath!r}")
                    seen_tool_paths.add(relpath)
                    privilege_tool_paths.add(relpath)
                    if tool.get("user") != "package" or tool.get("group") != "package":
                        raise ValueError(f"tool[{index}] user/group must both be package")
                    if not re.fullmatch(r"[0-7]{4}", str(tool.get("permission", ""))):
                        raise ValueError(f"tool[{index}].permission must be four octal digits")
                    capabilities = tool.get("capabilities")
                    if capabilities is not None and not re.fullmatch(
                        r"cap_[a-z0-9_]+(?:,cap_[a-z0-9_]+)*",
                        str(capabilities),
                    ):
                        raise ValueError(f"tool[{index}].capabilities is invalid")
            except Exception as exc:
                report.errors.append(f"conf/privilege is invalid: {exc}")

        resource = read_outer("conf/resource")
        if resource is not None:
            try:
                parsed_resource = json.loads(resource)
                if not isinstance(parsed_resource, dict):
                    raise ValueError("root must be a JSON object")
            except Exception as exc:
                report.errors.append(f"conf/resource is invalid: {exc}")

        license_bytes = read_outer("LICENSE")
        if license_bytes is not None and len(license_bytes) >= 1_000_000:
            report.errors.append("LICENSE must be smaller than 1 MB")

        if any(name.startswith("WIZARD_UIFILES/") for name in outer_names) and not profile.wizard_uifiles_available:
            report.errors.append(f"WIZARD_UIFILES is not available in profile {profile_id}")

        if strict:
            for name, dims in (("PACKAGE_ICON.PNG", (64, 64)), ("PACKAGE_ICON_256.PNG", (256, 256))):
                data = read_outer(name)
                if data is not None:
                    try:
                        actual = png_dimensions(data)
                        if actual != dims:
                            report.errors.append(f"{name} dimensions {actual} != {dims}")
                    except Exception as exc:
                        report.errors.append(f"{name} invalid: {exc}")
        script = read_outer("scripts/start-stop-status")
        if script is not None:
            text = script.decode("utf-8", errors="replace")
            if not handles_case_action(text, "status"):
                report.errors.append("start-stop-status does not handle status")
            if info.get("precheckstartstop", "yes") == "yes":
                if not handles_case_action(text, "prestart") or not handles_case_action(text, "prestop"):
                    report.errors.append("precheckstartstop=yes but start-stop-status lacks prestart/prestop")
            if "exit 3" not in text:
                report.warnings.append("start-stop-status does not visibly contain DSM status code 3 for a stopped service")

        if info:
            necessary = ("package", "version", "os_min_ver", "description", "arch", "maintainer")
            missing_fields = [key for key in necessary if not info.get(key)]
            if missing_fields:
                report.errors.append(f"INFO missing necessary fields: {missing_fields}")
            try:
                validate_package_version(info.get("version", ""))
            except Exception as exc:
                report.errors.append(f"INFO version invalid: {exc}")
            try:
                min_os = DSMVersion.parse(info.get("os_min_ver", ""))
                if min_os < profile.minimum_os:
                    report.errors.append(f"os_min_ver {min_os} is below profile minimum {profile.minimum_os}")
                if info.get("os_max_ver"):
                    max_os = DSMVersion.parse(info["os_max_ver"])
                    if max_os < min_os:
                        report.errors.append("INFO os_max_ver is lower than os_min_ver")
            except Exception as exc:
                report.errors.append(f"INFO OS version range invalid: {exc}")
            if info.get("extractsize"):
                try:
                    declared_extractsize = int(info["extractsize"])
                    if declared_extractsize < 0:
                        raise ValueError("must be non-negative")
                    report.details["extractsize_kb"] = declared_extractsize
                except Exception as exc:
                    report.errors.append(f"INFO extractsize invalid: {exc}")

        package_bytes = read_outer("package.tgz")
        if package_bytes is not None:
            _check_gzip_header(
                package_bytes,
                report,
                strict=strict,
            )
            actual_md5 = hashlib.md5(
                package_bytes,
                usedforsecurity=False,
            ).hexdigest()
            report.details["package_tgz_md5"] = actual_md5
            declared_md5 = info.get("checksum")
            if declared_md5:
                if not re.fullmatch(r"[0-9a-fA-F]{32}", declared_md5):
                    report.errors.append("INFO checksum is not a 32-character MD5 hex string")
                elif declared_md5.lower() != actual_md5:
                    report.errors.append(
                        f"INFO checksum mismatch: declared {declared_md5.lower()}, actual {actual_md5}"
                    )
            elif strict and profile.require_payload_checksum:
                report.errors.append(
                    "INFO checksum is required by the strict profile for later DSM manual-install compatibility"
                )
            else:
                report.warnings.append("INFO checksum is absent")

    if package_bytes is None:
        return report
    try:
        inner_tf = tarfile.open(fileobj=io.BytesIO(package_bytes), mode="r:gz")
    except Exception as exc:
        report.errors.append(f"package.tgz is invalid: {exc}")
        return report

    with inner_tf:
        inner_members = _safe_members(
            inner_tf,
            report,
            "payload",
            strict=strict,
        )
        report.details["payload_members"] = len(inner_members)
        payload_bytes = sum(member.size for member in inner_members if member.isfile())
        minimum_extractsize_kb = (payload_bytes + 1023) // 1024
        report.details["payload_bytes"] = payload_bytes
        report.details["minimum_extractsize_kb"] = minimum_extractsize_kb
        if (
            declared_extractsize is not None
            and declared_extractsize < minimum_extractsize_kb
        ):
            report.errors.append(
                "INFO extractsize is below the payload byte lower bound: "
                f"declared {declared_extractsize}, minimum {minimum_extractsize_kb}"
            )
        inner_names = {
            member.name
            for member in inner_members
            if member.isfile()
        }
        missing_executable_targets = sorted(
            privilege_executable_paths - inner_names
        )
        if missing_executable_targets:
            report.errors.append(
                "conf/privilege executable targets missing from payload: "
                f"{missing_executable_targets}"
            )
        missing_tool_targets = sorted(privilege_tool_paths - inner_names)
        if missing_tool_targets:
            report.errors.append(
                f"conf/privilege tool targets missing from payload: {missing_tool_targets}"
            )
        arch_values = info_arch_values
        allowed_machines, warnings = expected_elf_machines(arch_values)
        report.warnings.extend(warnings)
        seen_elf: list[dict[str, object]] = []
        for member in inner_members:
            if not member.isfile():
                continue
            handle = inner_tf.extractfile(member)
            if handle is None:
                continue
            data = handle.read()
            machine = elf_machine(data)
            if machine is None:
                continue
            seen_elf.append({"path": member.name, "e_machine": machine})
            if "noarch" in arch_values:
                message = (
                    f"payload {member.name} is ELF e_machine={machine} while INFO arch includes noarch"
                )
                if allow_noarch_native_bundle:
                    report.warnings.append(message + " (explicit multi-arch bundle allowance)")
                else:
                    report.errors.append(message)
            elif allowed_machines and machine not in allowed_machines:
                report.errors.append(f"payload {member.name} ELF e_machine={machine} does not match INFO arch machine hints {sorted(allowed_machines)}")
        if (
            seen_elf
            and warnings
            and strict
            and "noarch" not in arch_values
        ):
            report.errors.append(
                "native ELF payload is present but one or more INFO arch values "
                "have no known ELF-machine mapping"
            )
        report.details["elf_payloads"] = seen_elf
    return report
