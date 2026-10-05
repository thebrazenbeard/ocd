from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import re
import tomllib
from typing import Any

from .profiles import get_profile
from .versioning import DSMVersion, validate_package_version

_SAFE_PACKAGE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_ARCH = re.compile(r"^[A-Za-z0-9_.+-]+$")
_SCRIPT_NAMES = (
    "start_stop_status",
    "preinst",
    "postinst",
    "preuninst",
    "postuninst",
    "preupgrade",
    "postupgrade",
)
PRIVILEGE_ACTIONS = {
    "preinst",
    "postinst",
    "preuninst",
    "postuninst",
    "preupgrade",
    "postupgrade",
    "start",
    "stop",
    "status",
    "prestart",
    "prestop",
}


def validate_package_id(value: str) -> None:
    if (
        not value
        or value in {".", ".."}
        or not _SAFE_PACKAGE_ID.fullmatch(value)
    ):
        raise ValueError(
            "package.id must use only letters, digits, '.', '_' or '-' and may not be '.' or '..'"
        )


def validate_arch_values(values: tuple[str, ...]) -> None:
    if not values:
        raise ValueError("package.arch must contain at least one value")
    if any(not value or not _SAFE_ARCH.fullmatch(value) for value in values):
        raise ValueError("package.arch values contain unsupported characters")
    if len(set(values)) != len(values):
        raise ValueError("package.arch may not contain duplicate values")
    if "noarch" in values and values != ("noarch",):
        raise ValueError(
            "package.arch may not combine noarch with architecture-specific values"
        )


@dataclass(frozen=True)
class PayloadFile:
    source: Path
    destination: PurePosixPath
    mode: int = 0o644
    expected_elf_machine: int | None = None


@dataclass(frozen=True)
class PackageConfig:
    package_id: str
    version: str
    description: str
    maintainer: str
    arch: tuple[str, ...]
    os_min_ver: str
    display_name: str | None = None
    os_max_ver: str | None = None
    thirdparty: bool = True
    precheckstartstop: bool = True
    ctl_stop: bool | None = None
    allow_noarch_native_bundle: bool = False


@dataclass(frozen=True)
class ServiceConfig:
    enabled: bool = True
    command: str | None = None
    args: tuple[str, ...] = ()
    pid_file: str = "service.pid"
    log_file: str = "service.log"
    state_dirs: tuple[str, ...] = ()
    start_probe_seconds: int = 1
    stop_timeout_seconds: int = 10


@dataclass(frozen=True)
class PrivilegeControlScript:
    action: str
    run_as: str = "package"


@dataclass(frozen=True)
class PrivilegeExecutable:
    relpath: PurePosixPath
    run_as: str = "package"


@dataclass(frozen=True)
class PrivilegeTool:
    relpath: PurePosixPath
    user: str = "package"
    group: str = "package"
    permission: str = "0700"
    capabilities: str | None = None


@dataclass(frozen=True)
class PrivilegeConfig:
    run_as: str = "package"
    username: str | None = None
    groupname: str | None = None
    ctrl_scripts: tuple[PrivilegeControlScript, ...] = ()
    executables: tuple[PrivilegeExecutable, ...] = ()
    tools: tuple[PrivilegeTool, ...] = ()


@dataclass(frozen=True)
class AssetsConfig:
    icon64: Path | None = None
    icon256: Path | None = None
    license_file: Path | None = None
    wizard_dir: Path | None = None
    resource_file: Path | None = None


@dataclass(frozen=True)
class ScriptsConfig:
    start_stop_status: Path | None = None
    preinst: Path | None = None
    postinst: Path | None = None
    preuninst: Path | None = None
    postuninst: Path | None = None
    preupgrade: Path | None = None
    postupgrade: Path | None = None

    def as_dict(self) -> dict[str, Path | None]:
        return {name: getattr(self, name) for name in _SCRIPT_NAMES}


@dataclass(frozen=True)
class Manifest:
    path: Path
    profile_id: str
    strict: bool
    allow_external_sources: bool
    package: PackageConfig
    service: ServiceConfig
    privilege: PrivilegeConfig
    assets: AssetsConfig
    scripts: ScriptsConfig
    payload_files: tuple[PayloadFile, ...]
    info_extra: dict[str, str] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.path.parent


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a table")
    return value


def _string_value(
    table: dict[str, Any],
    key: str,
    *,
    default: str | None = None,
    required: bool = False,
) -> str | None:
    if key not in table:
        if required:
            raise ValueError(f"{key} is required and must be a TOML string")
        return default
    value = table[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a TOML string")
    if required and not value:
        raise ValueError(f"{key} must not be empty")
    return value


def _bool_value(table: dict[str, Any], key: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a TOML boolean")
    return value


def _int_value(
    table: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be a TOML integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return value


def _relative_posix(value: Any, field_name: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a path string")
    if not value or "\\" in value:
        raise ValueError(f"{field_name} must be a canonical relative POSIX path")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field_name} may not contain control characters")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or str(path) in {"", "."}
        or path.as_posix() != value
    ):
        raise ValueError(f"{field_name} must be a canonical relative POSIX path")
    return path


def _mode(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("payload file mode may not be a TOML boolean")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        result = int(value, 8)
    else:
        raise ValueError("payload file mode must be an integer or octal string")
    if result < 0 or result > 0o7777:
        raise ValueError(f"invalid file mode: {result:o}")
    return result


def _local_path(base: Path, value: Any, field_name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty path string")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field_name} may not contain control characters")
    return (base / value).resolve()


def load_manifest(path: str | Path) -> Manifest:
    manifest_path = Path(path).resolve()
    with manifest_path.open("rb") as handle:
        raw = tomllib.load(handle)

    compatibility = _section(raw, "compatibility")
    profile_id = _string_value(
        compatibility,
        "profile",
        default="dsm-7.2.2+",
    )
    assert profile_id is not None
    get_profile(profile_id)
    strict = _bool_value(compatibility, "strict", True)
    allow_external_sources = _bool_value(
        compatibility,
        "allow_external_sources",
        False,
    )

    pkg = _section(raw, "package")
    package_id = _string_value(pkg, "id", required=True)
    version = _string_value(pkg, "version", required=True)
    assert package_id is not None
    assert version is not None
    validate_package_id(package_id)
    validate_package_version(version)

    arch_raw = pkg.get("arch", ["noarch"])
    if isinstance(arch_raw, str):
        if not arch_raw:
            raise ValueError(
                "package.arch must be a non-empty string or list of strings"
            )
        arch = (arch_raw,)
    elif (
        isinstance(arch_raw, list)
        and arch_raw
        and all(isinstance(x, str) and x for x in arch_raw)
    ):
        arch = tuple(arch_raw)
    else:
        raise ValueError("package.arch must be a non-empty string or list of strings")
    validate_arch_values(arch)

    os_min_ver = _string_value(pkg, "os_min_ver", required=True)
    assert os_min_ver is not None
    DSMVersion.parse(os_min_ver)
    os_max_ver = _string_value(pkg, "os_max_ver")
    if os_max_ver is not None:
        DSMVersion.parse(os_max_ver)

    display_name = _string_value(pkg, "display_name")
    description = _string_value(pkg, "description", required=True)
    maintainer = _string_value(pkg, "maintainer", required=True)
    assert description is not None
    assert maintainer is not None

    package = PackageConfig(
        package_id=package_id,
        display_name=display_name,
        version=version,
        description=description,
        maintainer=maintainer,
        arch=arch,
        os_min_ver=os_min_ver,
        os_max_ver=os_max_ver,
        thirdparty=_bool_value(pkg, "thirdparty", True),
        precheckstartstop=_bool_value(pkg, "precheckstartstop", True),
        ctl_stop=(
            _bool_value(pkg, "ctl_stop", False)
            if "ctl_stop" in pkg
            else None
        ),
        allow_noarch_native_bundle=_bool_value(
            pkg,
            "allow_noarch_native_bundle",
            False,
        ),
    )
    if not package.description:
        raise ValueError("package.description is required")
    if not package.maintainer:
        raise ValueError("package.maintainer is required")

    svc = _section(raw, "service")
    enabled = _bool_value(svc, "enabled", True)
    command = svc["command"] if "command" in svc else None
    if command is not None and not isinstance(command, str):
        raise ValueError("service.command must be a path string")
    if enabled and not command:
        raise ValueError("service.command is required when service.enabled=true")
    if command is not None:
        _relative_posix(command, "service.command")

    args = svc.get("args", [])
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        raise ValueError("service.args must be an array of strings")
    if any("\x00" in x or "\n" in x or "\r" in x for x in args):
        raise ValueError("service.args may not contain NUL, newline, or carriage return")
    state_dirs = svc.get("state_dirs", [])
    if not isinstance(state_dirs, list) or not all(isinstance(x, str) for x in state_dirs):
        raise ValueError("service.state_dirs must be an array of strings")
    for item in state_dirs:
        _relative_posix(item, "service.state_dirs[]")

    pid_file = svc.get("pid_file", "service.pid")
    log_file = svc.get("log_file", "service.log")
    _relative_posix(pid_file, "service.pid_file")
    _relative_posix(log_file, "service.log_file")
    service = ServiceConfig(
        enabled=enabled,
        command=command,
        args=tuple(args),
        pid_file=pid_file,
        log_file=log_file,
        state_dirs=tuple(state_dirs),
        start_probe_seconds=_int_value(
            svc,
            "start_probe_seconds",
            1,
            minimum=0,
        ),
        stop_timeout_seconds=_int_value(
            svc,
            "stop_timeout_seconds",
            10,
            minimum=1,
        ),
    )
    def file_path_conflicts(left: str, right: str) -> bool:
        return (
            left == right
            or left.startswith(right + "/")
            or right.startswith(left + "/")
        )

    if file_path_conflicts(service.pid_file, service.log_file):
        raise ValueError(
            "service.pid_file and service.log_file may not be equal or parent/child paths"
        )
    state_dir_set = set(service.state_dirs)
    if len(state_dir_set) != len(service.state_dirs):
        raise ValueError("service.state_dirs may not contain duplicates")
    for directory in service.state_dirs:
        if (
            directory == service.pid_file
            or directory.startswith(service.pid_file + "/")
            or directory == service.log_file
            or directory.startswith(service.log_file + "/")
        ):
            raise ValueError(
                "a service state directory may not equal or be nested beneath a pid/log file path"
            )

    priv = _section(raw, "privilege")
    raw_ctrl_scripts = priv.get("ctrl_script", [])
    if not isinstance(raw_ctrl_scripts, list):
        raise ValueError("[[privilege.ctrl_script]] must be an array of tables")
    ctrl_scripts: list[PrivilegeControlScript] = []
    seen_actions: set[str] = set()
    for index, item in enumerate(raw_ctrl_scripts):
        if not isinstance(item, dict):
            raise ValueError(f"privilege.ctrl_script[{index}] must be a table")
        action = _string_value(item, "action", required=True)
        run_as_entry = _string_value(item, "run_as", default="package")
        assert action is not None
        assert run_as_entry is not None
        if action not in PRIVILEGE_ACTIONS:
            raise ValueError(
                f"privilege.ctrl_script[{index}].action is unsupported: {action!r}"
            )
        if action in seen_actions:
            raise ValueError(f"duplicate privilege.ctrl_script action: {action}")
        seen_actions.add(action)
        if run_as_entry not in {"package", "root"}:
            raise ValueError(
                f"privilege.ctrl_script[{index}].run_as must be 'package' or 'root'"
            )
        ctrl_scripts.append(
            PrivilegeControlScript(action=action, run_as=run_as_entry)
        )

    raw_executables = priv.get("executable", [])
    if not isinstance(raw_executables, list):
        raise ValueError("[[privilege.executable]] must be an array of tables")
    executables: list[PrivilegeExecutable] = []
    seen_executables: set[str] = set()
    for index, item in enumerate(raw_executables):
        if not isinstance(item, dict):
            raise ValueError(f"privilege.executable[{index}] must be a table")
        relpath = _relative_posix(
            item.get("relpath", ""),
            f"privilege.executable[{index}].relpath",
        )
        run_as_entry = _string_value(item, "run_as", default="package")
        assert run_as_entry is not None
        relpath_text = relpath.as_posix()
        if relpath_text in seen_executables:
            raise ValueError(
                f"duplicate privilege.executable relpath: {relpath_text}"
            )
        seen_executables.add(relpath_text)
        if run_as_entry not in {"package", "root"}:
            raise ValueError(
                f"privilege.executable[{index}].run_as must be 'package' or 'root'"
            )
        executables.append(
            PrivilegeExecutable(relpath=relpath, run_as=run_as_entry)
        )

    raw_tools = priv.get("tool", [])
    if not isinstance(raw_tools, list):
        raise ValueError("[[privilege.tool]] must be an array of tables")
    privilege_tools: list[PrivilegeTool] = []
    capability_re = re.compile(r"^cap_[a-z0-9_]+(?:,cap_[a-z0-9_]+)*$")
    permission_re = re.compile(r"^[0-7]{4}$")
    for index, item in enumerate(raw_tools):
        if not isinstance(item, dict):
            raise ValueError(f"privilege.tool[{index}] must be a table")
        relpath = _relative_posix(
            item.get("relpath", ""),
            f"privilege.tool[{index}].relpath",
        )
        user = _string_value(item, "user", default="package")
        group = _string_value(item, "group", default="package")
        permission = _string_value(item, "permission", default="0700")
        capabilities = _string_value(item, "capabilities")
        assert user is not None
        assert group is not None
        assert permission is not None
        if user != "package" or group != "package":
            raise ValueError("privilege.tool user and group must both be 'package'")
        if not permission_re.fullmatch(permission):
            raise ValueError(
                f"privilege.tool[{index}].permission must be four octal digits"
            )
        if capabilities is not None and not capability_re.fullmatch(capabilities):
            raise ValueError(
                f"privilege.tool[{index}].capabilities must be comma-separated cap_* names"
            )
        privilege_tools.append(
            PrivilegeTool(
                relpath=relpath,
                user=user,
                group=group,
                permission=permission,
                capabilities=capabilities,
            )
        )

    run_as = _string_value(priv, "run_as", default="package")
    username = _string_value(priv, "username")
    groupname = _string_value(priv, "groupname")
    assert run_as is not None
    privilege = PrivilegeConfig(
        run_as=run_as,
        username=username,
        groupname=groupname,
        ctrl_scripts=tuple(ctrl_scripts),
        executables=tuple(executables),
        tools=tuple(privilege_tools),
    )
    if privilege.run_as not in {"package", "root"}:
        raise ValueError("privilege.run_as must be 'package' or 'root'")

    assets_raw = _section(raw, "assets")
    def asset(key: str) -> Path | None:
        return (
            _local_path(
                manifest_path.parent,
                assets_raw[key],
                f"assets.{key}",
            )
            if key in assets_raw
            else None
        )

    assets = AssetsConfig(
        icon64=asset("icon64"),
        icon256=asset("icon256"),
        license_file=asset("license"),
        wizard_dir=asset("wizard_dir"),
        resource_file=asset("resource"),
    )

    scripts_raw = _section(raw, "scripts")
    scripts_kwargs: dict[str, Path | None] = {}
    for name in _SCRIPT_NAMES:
        scripts_kwargs[name] = (
            _local_path(
                manifest_path.parent,
                scripts_raw[name],
                f"scripts.{name}",
            )
            if name in scripts_raw else None
        )
    scripts = ScriptsConfig(**scripts_kwargs)

    payload = _section(raw, "payload")
    entries = payload.get("files", [])
    if not isinstance(entries, list):
        raise ValueError("[[payload.files]] entries are required")
    payload_files: list[PayloadFile] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"payload.files[{index}] must be a table")
        source_value = entry.get("source", "")
        source = _local_path(
            manifest_path.parent,
            source_value,
            f"payload.files[{index}].source",
        )
        destination = _relative_posix(
            entry.get("destination", ""),
            f"payload.files[{index}].destination",
        )
        expected = entry.get("expected_elf_machine")
        if expected is not None:
            if isinstance(expected, bool) or not isinstance(expected, int):
                raise ValueError(
                    f"payload.files[{index}].expected_elf_machine must be a TOML integer"
                )
            if expected < 0 or expected > 65535:
                raise ValueError(
                    f"payload.files[{index}].expected_elf_machine must be in 0..65535"
                )
        payload_files.append(
            PayloadFile(
                source=source,
                destination=destination,
                mode=_mode(entry.get("mode", "0644")),
                expected_elf_machine=expected,
            )
        )
    if not payload_files:
        raise ValueError("at least one [[payload.files]] entry is required")

    info = _section(raw, "info")
    extra = info.get("extra", {})
    if not isinstance(extra, dict):
        raise ValueError("[info.extra] must be a table")
    info_extra = {str(k): str(v) for k, v in extra.items()}

    return Manifest(
        path=manifest_path,
        profile_id=profile_id,
        strict=strict,
        allow_external_sources=allow_external_sources,
        package=package,
        service=service,
        privilege=privilege,
        assets=assets,
        scripts=scripts,
        payload_files=tuple(payload_files),
        info_extra=info_extra,
    )
