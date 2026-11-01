"""Versioned, credential-free configuration for explicitly allowed roots.

Configuration is inert data. Reading it never scans or changes user files.
Only a root explicitly present here can be selected for a CLI operation.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from .types import RootConfig
from .planner import _normalized_folder_key


CONFIG_VERSION = 1
_ROOT_KEYS = frozenset({"source", "destination", "mode", "tags", "settle_seconds"})
_EXTENSION = re.compile(r"\.?[a-zA-Z0-9]{1,16}\Z")


class ConfigError(ValueError):
    """Configuration cannot be safely interpreted or changed."""


@dataclass(frozen=True)
class Config:
    roots: tuple[RootConfig, ...] = ()
    version: int = CONFIG_VERSION


def default_config_path() -> Path:
    """Return a per-user location on Windows and XDG-compatible systems."""
    override = os.environ.get("OCD_CONFIG")
    if override:
        return Path(override).expanduser().absolute()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        directory = Path(base).expanduser().absolute() if base else Path.home() / "AppData" / "Roaming"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        directory = Path(base) if base and Path(base).is_absolute() else Path.home() / ".config"
    return directory / "ocd" / "config.json"


def normalize_tags(tags: dict[str, str]) -> dict[str, str]:
    """Normalize explicit extension and slash-separated ancestor folder tags.

    A folder tag uses ``folder:relative/subdir``. Version-one configurations
    with only extension tags remain valid without a schema migration.
    """
    normalized: dict[str, str] = {}
    folder_keys: set[str] = set()
    for extension, label in tags.items():
        if not isinstance(label, str) or not label.strip():
            raise ConfigError(f"tag {extension!r} requires a nonempty label")
        if isinstance(extension, str) and extension.startswith("folder:"):
            key = _normalized_folder_key(extension)
            if key is None:
                raise ConfigError(f"invalid folder tag: {extension!r}")
            portable_key = key.casefold()
            if portable_key in folder_keys:
                raise ConfigError(f"ambiguous portable folder tag: {key}")
            folder_keys.add(portable_key)
        else:
            if not isinstance(extension, str) or not _EXTENSION.fullmatch(extension):
                raise ConfigError(f"invalid extension tag: {extension!r}")
            key = "." + extension.lower().lstrip(".")
            if key in normalized:
                raise ConfigError(f"duplicate extension tag: {key}")
        normalized[key] = label
    return normalized


def _absolute_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field} must be a nonempty absolute path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ConfigError(f"{field} must be a normalized absolute path")
    if path == Path(path.anchor):
        raise ConfigError(f"{field} cannot be a filesystem root")
    return path


def _root_from_dict(item: object) -> RootConfig:
    if not isinstance(item, dict) or set(item) != _ROOT_KEYS:
        raise ConfigError("each root must contain only source, destination, mode, tags, settle_seconds")
    source = _absolute_path(item["source"], "source")
    destination = _absolute_path(item["destination"], "destination")
    mode = item["mode"]
    if mode not in ("observe", "apply") or not isinstance(mode, str):
        raise ConfigError("mode must be 'observe' or 'apply'")
    raw_tags = item["tags"]
    if not isinstance(raw_tags, dict):
        raise ConfigError("tags must be an extension-to-label object")
    tags = normalize_tags(raw_tags)
    settle = item["settle_seconds"]
    if isinstance(settle, bool) or not isinstance(settle, (int, float)):
        raise ConfigError("settle_seconds must be a finite nonnegative number")
    try:
        settle = float(settle)
    except (OverflowError, ValueError) as exc:
        raise ConfigError("settle_seconds must be a finite nonnegative number") from exc
    if not math.isfinite(settle) or settle < 0:
        raise ConfigError("settle_seconds must be a finite nonnegative number")
    if _overlap(source, destination):
        raise ConfigError("source and destination must be separate, non-overlapping directories")
    return RootConfig(source, destination, mode, tags, settle)


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _check_root_conflicts(roots: tuple[RootConfig, ...]) -> None:
    for index, first in enumerate(roots):
        for second in roots[index + 1 :]:
            if _overlap(first.source, second.source):
                raise ConfigError(f"registered sources overlap: {first.source} and {second.source}")
            if _overlap(first.source, second.destination) or _overlap(second.source, first.destination):
                raise ConfigError("a registered source overlaps another root's destination")


def _unique_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"duplicate configuration key: {key!r}")
        result[key] = value
    return result


def load_config(path: Path | None = None) -> Config:
    """Read config strictly; missing, malformed and future versions fail closed."""
    path = Path(path) if path is not None else default_config_path()
    try:
        with path.open("r", encoding="utf-8") as stream:
            document = json.load(stream, object_pairs_hook=_unique_object_pairs)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration not found at {path}; run 'ocd init' first") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read configuration at {path}: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"version", "roots"}:
        raise ConfigError("configuration must contain only 'version' and 'roots' (no credentials)")
    if type(document["version"]) is not int or document["version"] != CONFIG_VERSION:
        raise ConfigError(f"unsupported configuration version: {document['version']!r}")
    if not isinstance(document["roots"], list):
        raise ConfigError("roots must be a list")
    roots = tuple(_root_from_dict(item) for item in document["roots"])
    _check_root_conflicts(roots)
    return Config(roots=roots)


def _to_dict(config: Config) -> dict[str, object]:
    if type(config.version) is not int or config.version != CONFIG_VERSION:
        raise ConfigError("cannot write an unsupported configuration version")
    roots = tuple(_root_from_dict({
        "source": str(root.source),
        "destination": str(root.destination),
        "mode": root.mode,
        "tags": root.tags,
        "settle_seconds": root.settle_seconds,
    }) for root in config.roots)
    _check_root_conflicts(roots)
    return {"version": CONFIG_VERSION, "roots": [{
        "source": str(root.source),
        "destination": str(root.destination),
        "mode": root.mode,
        "tags": root.tags,
        "settle_seconds": root.settle_seconds,
    } for root in roots]}


def init_config(path: Path | None = None) -> Path:
    """Create an empty config exclusively; never replace an existing file."""
    path = Path(path) if path is not None else default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ConfigError(f"configuration already exists at {path}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(_to_dict(Config()), stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def save_config(config: Config, path: Path | None = None) -> Path:
    """Atomically replace a validated config without exposing credentials."""
    document = _to_dict(config)
    path = Path(path) if path is not None else default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".ocd-config-", suffix=".tmp", delete=False
        ) as stream:
            temp_path = Path(stream.name)
            if os.name == "posix":
                os.chmod(temp_path, 0o600)
            json.dump(document, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return path


def add_root(config: Config, source: Path, destination: Path, *, mode: str = "observe",
             tags: dict[str, str] | None = None, settle_seconds: float = 2.0) -> Config:
    """Admit two existing canonical directories to the explicit allowlist."""
    for name, path in (("source", source), ("destination", destination)):
        if not Path(path).is_absolute():
            raise ConfigError(f"{name} must be absolute")
    try:
        source = Path(source).resolve(strict=True)
        destination = Path(destination).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConfigError(f"root directory cannot be resolved: {exc}") from exc
    if not source.is_dir() or not destination.is_dir():
        raise ConfigError("source and destination must both be existing directories")
    new_root = _root_from_dict({
        "source": str(source),
        "destination": str(destination),
        "mode": mode,
        "tags": tags or {},
        "settle_seconds": settle_seconds,
    })
    updated = Config(roots=(*config.roots, new_root))
    _check_root_conflicts(updated.roots)
    return updated


def _on_windows() -> bool:
    return os.name == "nt"


def _unlinked_directory_chain(path: Path) -> bool:
    """Reject Windows reparse ancestors while tolerating genuine 8.3 aliases.

    Windows ``Path.resolve`` expands short DOS names (e.g. RUNNER~1) into
    long names. Comparing their spellings would reject ordinary directories,
    even though neither name is a reparse-point redirection. Inspect every
    component with lstat instead; a junction/symlink in *any* ancestor is an
    authorization failure, irrespective of where resolve leads.
    """
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for part in (*reversed(path.parents), path):
        try:
            details = part.lstat()
        except OSError:
            return False
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or bool(getattr(details, "st_file_attributes", 0) & reparse)
        ):
            return False
    return True


def verified_root(root: RootConfig) -> RootConfig:
    """Detect removed or redirected roots before scan, plan, or apply."""
    for name, path in (("source", root.source), ("destination", root.destination)):
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ConfigError(f"registered {name} unavailable: {path}: {exc}") from exc
        # 8.3 DOS aliases expand when resolved on Windows; their spelling is
        # not a stable indication of a symlink. Enforce no-reparse on the
        # entire ancestor chain instead, then verify same filesystem object.
        if _on_windows():
            valid = _unlinked_directory_chain(path)
            try:
                valid = valid and os.path.samefile(path, resolved)
            except OSError:
                valid = False
        else:
            valid = resolved == path and path.is_dir()
        if not valid:
            raise ConfigError(f"registered {name} has changed or is not a directory: {path}")
    return root
