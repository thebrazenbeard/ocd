"""Explicit, verified artifact downloads into a local directory.

Fetching is opt-in: importing this module or loading a manifest never contacts
the network.  This is a download-only boundary, not a cloud synchronizer.

Only HTTPS is accepted over the network; loopback HTTP is accepted to support
local servers and offline tests.  Linux publication uses anonymous staging
files and atomic, no-replace hard links. Windows publication uses a native
handle-held, no-replace rename on local fixed NTFS/ReFS volumes, never a
generic path-based overwrite or follow-reparse fallback.
"""

from __future__ import annotations

import ctypes
from contextlib import ExitStack
from dataclasses import dataclass
import errno
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import unicodedata
from typing import Mapping
from urllib import error, parse, request


class ArtifactError(ValueError):
    """An artifact manifest or requested download failed a safety check."""


@dataclass(frozen=True)
class ArtifactEntry:
    url: str
    sha256: str
    size: int
    destination_relpath: str


_HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_GITHUB_API_ASSET = re.compile(r"/repos/[^/]+/[^/]+/releases/assets/[0-9]+/?\Z")
_GITHUB_RELEASE_ASSET = re.compile(r"/[^/]+/[^/]+/releases/download/[^/]+/.+\Z")
_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
    | {f"COM{i}" for i in "¹²³"}
    | {f"LPT{i}" for i in "¹²³"}
)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5
_CHUNK_SIZE = 1024 * 1024


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def _validate_url(url: object, *, redirected: bool = False) -> str:
    if not isinstance(url, str) or not url or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise ArtifactError("artifact URL must be a nonempty HTTPS URL")
    try:
        parts = parse.urlsplit(url)
        port = parts.port  # Accessing this validates the port syntax.
    except ValueError:
        raise ArtifactError("malformed artifact URL") from None
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or (port is not None and port == 0)
    ):
        raise ArtifactError("artifact URL must not contain credentials or a fragment")
    # Signed CDN URLs can occur on a *redirect*, but the persistent manifest
    # must not carry bearer tokens or signed query credentials of its own.
    if not redirected:
        sensitive = {
            "token", "access_token", "auth", "authorization", "key", "api_key",
            "apikey", "signature", "sig", "jwt", "x-amz-credential",
            "x-amz-signature", "x-goog-credential", "x-goog-signature",
        }
        for key, _ in parse.parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in sensitive:
                raise ArtifactError("artifact URL cannot embed query credentials")
    if parts.scheme == "https":
        return url
    if parts.scheme == "http" and _is_loopback(parts.hostname):
        return url
    raise ArtifactError("artifact URL must use HTTPS (loopback HTTP is allowed)")


def _validate_relpath(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise ArtifactError("destination_relpath must be a relative POSIX-style file path")
    components = value.split("/")
    for part in components:
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or ":" in part
            or any(char in '<>"|?*' or ord(char) < 32 or ord(char) == 127 or unicodedata.category(char) in {"Cf", "Cs"} for char in part)
            or part.split(".", 1)[0].upper() in _WINDOWS_DEVICES
        ):
            raise ArtifactError("unsafe destination_relpath component")
    return value


def _parse_entry(value: object) -> ArtifactEntry:
    if not isinstance(value, dict) or set(value) != {
        "url", "sha256", "size", "destination_relpath"
    }:
        raise ArtifactError("each artifact must define url, sha256, size and destination_relpath")
    url = _validate_url(value["url"])
    digest = value["sha256"]
    if not isinstance(digest, str) or not _HEX_SHA256.fullmatch(digest):
        raise ArtifactError("sha256 must be a 64-character hex digest")
    size = value["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ArtifactError("size must be a nonnegative integer byte count")
    path = _validate_relpath(value["destination_relpath"])
    return ArtifactEntry(url, digest.lower(), size, path)


def _unique_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactError("duplicate JSON key in artifact manifest")
        result[key] = value
    return result


def load_manifest(path: str | os.PathLike[str]) -> tuple[ArtifactEntry, ...]:
    """Read and validate a JSON list of artifact entries, without downloading.

    A top-level ``{"artifacts": [...]}`` object is accepted as well.  Unknown
    fields are rejected to avoid silently ignoring a misspelled safety field.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle, object_pairs_hook=_unique_json_keys)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ArtifactError("unable to read a valid JSON artifact manifest") from None
    if isinstance(manifest, dict) and set(manifest) == {"artifacts"}:
        manifest = manifest["artifacts"]
    if not isinstance(manifest, list):
        raise ArtifactError("artifact manifest must be a JSON array")
    entries = tuple(_parse_entry(item) for item in manifest)
    paths = [entry.destination_relpath.casefold() for entry in entries]
    if len(paths) != len(set(paths)):
        raise ArtifactError("duplicate destination_relpath in manifest")
    return entries


def _secure_directory_flags() -> int:
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise ArtifactError("secure directory-relative publication unavailable on this platform")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_root(destination_root: Path) -> int:
    """Anchor every existing root ancestor without following a symlink."""
    if not destination_root.is_absolute() or ".." in destination_root.parts:
        raise ArtifactError("destination_root must be absolute and contain no '..'")
    flags = _secure_directory_flags()
    try:
        fd = os.open(destination_root.anchor, flags)
        for part in destination_root.parts[1:]:
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            finally:
                os.close(fd)
            fd = next_fd
        return fd
    except OSError:
        raise ArtifactError("destination root is missing or has an unsafe ancestor") from None


def _open_parent(root_fd: int, relpath: str) -> int:
    """Create missing subdirectories under anchored, no-follow handles."""
    flags = _secure_directory_flags()
    fd = os.dup(root_fd)
    try:
        for part in relpath.split("/")[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError:
        os.close(fd)
        raise ArtifactError("destination has a symlink or unsafe directory ancestor") from None
    except BaseException:
        os.close(fd)
        raise


def _assert_absent(parent_fd: int, filename: str) -> None:
    try:
        os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise ArtifactError("destination already exists; existing files are never overwritten")


def _assert_parent_still_attached(
    destination_root: Path, root_fd: int, parent_fd: int, relpath: str
) -> None:
    """Detect a root/parent rename or symlink swap before reporting success.

    Anchored operations cannot escape even without this extra check, but a
    detached directory must not be reported as the requested visible path.
    """
    flags = _secure_directory_flags()
    current_root = _open_root(destination_root)
    try:
        a, b = os.fstat(current_root), os.fstat(root_fd)
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise ArtifactError("destination root changed while fetching")
        fd = os.dup(root_fd)
        try:
            for part in relpath.split("/")[:-1]:
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            a, b = os.fstat(fd), os.fstat(parent_fd)
            if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                raise ArtifactError("destination parent changed while fetching")
        finally:
            os.close(fd)
    except OSError:
        raise ArtifactError("destination parent became unsafe while fetching") from None
    finally:
        os.close(current_root)


def _create_anonymous_stage(parent_fd: int) -> int:
    """Get an unpublished inode; no pathname can replace the verified bytes."""
    if not sys.platform.startswith("linux") or not hasattr(os, "O_TMPFILE"):
        raise ArtifactError("safe anonymous staging is unavailable on this platform")
    flags = os.O_TMPFILE | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(".", flags, 0o600, dir_fd=parent_fd)
    except OSError:
        raise ArtifactError("safe anonymous staging is unavailable on this filesystem") from None


def _publish_noreplace(stage_fd: int, parent_fd: int, filename: str) -> None:
    """Atomically link the *verified inode*, exclusively, by its open handle."""
    libc = ctypes.CDLL(None, use_errno=True)
    linkat = getattr(libc, "linkat", None)
    if linkat is None:
        raise ArtifactError("atomic no-replace publication unavailable")
    linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    linkat.restype = ctypes.c_int
    # Linux AT_EMPTY_PATH links an O_TMPFILE inode without ever resolving an
    # attacker-replaceable staging pathname.
    result = linkat(stage_fd, b"", parent_fd, os.fsencode(filename), 0x1000)
    if result != 0:
        number = ctypes.get_errno()
        if number == errno.EEXIST:
            raise ArtifactError("destination already exists; existing files are never overwritten")
        raise OSError(number, os.strerror(number))


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _github_asset_url(url: str) -> bool:
    parts = parse.urlsplit(url)
    if parts.scheme != "https" or parts.port not in (None, 443):
        return False
    host = (parts.hostname or "").lower()
    return bool(
        (host == "api.github.com" and _GITHUB_API_ASSET.fullmatch(parts.path))
        or (host == "github.com" and _GITHUB_RELEASE_ASSET.fullmatch(parts.path))
    )


def _headers_for_initial_url(url: str, token_env: str) -> Mapping[str, str]:
    headers = {"Accept": "application/octet-stream", "Accept-Encoding": "identity"}
    if _github_asset_url(url):
        token = os.environ.get(token_env)
        if token:
            if "\r" in token or "\n" in token:
                raise ArtifactError("invalid GitHub token environment value")
            headers["Authorization"] = f"Bearer {token}"
    return headers


def _open_download(url: str, *, token_env: str, timeout: float):
    """Open a verified-scheme response, stripping auth on every redirect."""
    initial_scheme = parse.urlsplit(url).scheme
    opener = request.build_opener(_NoRedirect())
    current = url
    for number in range(_MAX_REDIRECTS + 1):
        headers = (
            _headers_for_initial_url(current, token_env)
            if number == 0
            else {"Accept": "application/octet-stream", "Accept-Encoding": "identity"}
        )
        req = request.Request(current, headers=headers, method="GET")
        try:
            return opener.open(req, timeout=timeout)
        except error.HTTPError as exc:
            if exc.code not in _REDIRECTS:
                exc.close()
                raise ArtifactError(f"download failed (HTTP {exc.code})") from None
            location = exc.headers.get("Location")
            exc.close()
            if not location or number == _MAX_REDIRECTS:
                raise ArtifactError("download exceeded the redirect limit") from None
            current = _validate_url(parse.urljoin(current, location), redirected=True)
            if initial_scheme == "https" and parse.urlsplit(current).scheme != "https":
                raise ArtifactError("HTTPS downloads cannot redirect to HTTP")
        except (OSError, ValueError):
            raise ArtifactError("download failed before verification") from None
    raise ArtifactError("download exceeded the redirect limit")


def _download_verified(entry: ArtifactEntry, target, *, token_env: str, timeout: float) -> None:
    """Write and sync only manifest-exact bytes to an unpublished open file."""
    digest = hashlib.sha256()
    total = 0
    with _open_download(entry.url, token_env=token_env, timeout=timeout) as source:
        encoding = source.headers.get("Content-Encoding", "identity")
        if encoding.lower() != "identity":
            raise ArtifactError("compressed HTTP transfer refused")
        while True:
            try:
                chunk = source.read(_CHUNK_SIZE)
            except (OSError, ValueError, http.client.HTTPException):
                raise ArtifactError("incomplete download") from None
            if not chunk:
                break
            total += len(chunk)
            if total > entry.size:
                raise ArtifactError("download exceeds manifest size")
            target.write(chunk)
            digest.update(chunk)
    if total != entry.size:
        raise ArtifactError("incomplete download: size does not match manifest")
    if not hmac.compare_digest(digest.hexdigest(), entry.sha256):
        raise ArtifactError("download SHA-256 does not match manifest")
    target.flush()
    os.fsync(target.fileno())


def _windows_mark_delete(handle: int, api) -> None:
    """Mark precisely the still-open staging *handle* for deletion on close.

    Never unlink an attacker-replaceable temporary *pathname* on failure.
    """
    class _Disposition(ctypes.Structure):
        _fields_ = [("DeleteFile", ctypes.c_uint8)]

    value = _Disposition(1)
    if not api._set(handle, 4, ctypes.byref(value), ctypes.sizeof(value)):
        raise ctypes.WinError(ctypes.get_last_error())


def _fetch_verified_windows(
    entry: ArtifactEntry, root: Path, *, token_env: str, timeout: float,
) -> Path:
    """Publish verified bytes on a strictly pinned local Win32 directory chain.

    Native handles to every ancestor exclude FILE_SHARE_DELETE, preventing
    replacement by junctions during I/O. The named random staging file is
    held with DELETE access but without sharing write/delete; a same-volume
    FileRenameInfo with ReplaceIfExists=FALSE publishes it. All unsupported
    filesystem/drive/path variants fail closed. This path still requires a
    native adversarial qualification matrix before production deployment.
    """
    if os.name != "nt":
        raise ArtifactError("native Windows publication unavailable")
    try:
        import msvcrt
        from .win32 import (
            _Win32Api, _path_parts, _directory_chain, _hold_directories,
            _checked_info, UnsafeWindowsMove,
        )
    except (ImportError, OSError):
        raise ArtifactError("native Windows publication unavailable") from None

    if not root.is_absolute() or ".." in root.parts:
        raise ArtifactError("destination_root must be absolute and contain no '..'")
    destination = root.joinpath(*entry.destination_relpath.split("/"))
    try:
        drive, root_parts = _path_parts(str(root))
        dst_drive, dest_parts = _path_parts(str(destination))
    except UnsafeWindowsMove as exc:
        raise ArtifactError(str(exc)) from None
    if drive != dst_drive or len(dest_parts) <= len(root_parts) or (
        tuple(part.casefold() for part in dest_parts[:len(root_parts)])
        != tuple(part.casefold() for part in root_parts)
    ):
        raise ArtifactError("artifact destination escapes its rooted Windows path")

    phase = "initialize native API"
    try:
        api = _Win32Api()
        phase = "validate fixed local drive"
        api.require_local_drive(drive)
        with ExitStack() as stack:
            phase = "pin destination directory ancestors"
            parent_handle, root_info = _hold_directories(
                api, _directory_chain(drive, root_parts), stack
            )
            current = str(root)
            for component in dest_parts[len(root_parts):-1]:
                phase = "prepare and pin destination subdirectory"
                current = current.rstrip("\\") + "\\" + component
                try:
                    os.mkdir(current)
                except FileExistsError:
                    pass
                handle = api.open(current)
                if handle is None:
                    raise ArtifactError("artifact parent directory disappeared")
                stack.callback(api.close, handle)
                info = _checked_info(api, handle, directory=True)
                if info.volume != root_info.volume:
                    raise ArtifactError("artifact parent traversed another volume")
                parent_handle = handle

            phase = "check destination vacancy"
            occupied = api.open(str(destination), missing_ok=True)
            if occupied is not None:
                api.close(occupied)
                raise ArtifactError("destination already exists; existing files are never overwritten")

            # CREATE_NEW gives the private staging filename exclusive creation;
            # the source handle stays open throughout hashing and publication.
            # No sharing of WRITE or DELETE means other opens cannot alter or
            # replace the staging file before the native rename completes.
            leaf = ".ocd-" + secrets.token_hex(24) + ".stage"
            stage = current.rstrip("\\") + "\\" + leaf
            phase = "create unpublished staging file"
            stage_handle = api._create(
                "\\\\?\\" + stage,
                0x80000000 | 0x40000000 | 0x00010000,
                0x00000001, None, 1, 0x00200000, None,
            )
            if stage_handle in (None, ctypes.c_void_p(-1).value):
                raise ctypes.WinError(ctypes.get_last_error())
            stage_owned = True
            target = None
            published = False
            try:
                # The CRT takes ownership of the Win32 handle on success.
                phase = "adopt staging handle for verified download"
                descriptor = msvcrt.open_osfhandle(stage_handle, os.O_BINARY | os.O_WRONLY)
                stage_owned = False
                target = os.fdopen(descriptor, "wb")
                phase = "download and verify staging bytes"
                _download_verified(entry, target, token_env=token_env, timeout=timeout)
                phase = "revalidate staging file handle"
                opened_info = _checked_info(api, stage_handle, directory=False)
                if opened_info.volume != root_info.volume or opened_info.size != entry.size:
                    raise ArtifactError("unpublished Windows staging file changed")

                phase = "recheck destination vacancy"
                occupied = api.open(str(destination), missing_ok=True)
                if occupied is not None:
                    api.close(occupied)
                    raise ArtifactError("destination already exists; existing files are never overwritten")
                # The held destination-parent handle remains non-NULL; this
                # is the anchored, no-replace destination namespace.  Never
                # silently fall back to a CWD-relative or path-based rename.
                phase = "native FileRenameInfo with pinned parent handle"
                api.rename(stage_handle, parent_handle, dest_parts[-1])
                published = True
                # Re-check the *held* inode after SetFileInformationByHandle;
                # opening the destination through the normal api.open would
                # conflict with our stage handle's DELETE access because that
                # helper intentionally excludes FILE_SHARE_DELETE.
                phase = "revalidate published file handle"
                info = _checked_info(api, stage_handle, directory=False)
                if info.identity != opened_info.identity or info.size != entry.size:
                    raise ArtifactError("published Windows artifact changed identity")
                return destination
            finally:
                cleanup_error = None
                if not published:
                    try:
                        _windows_mark_delete(stage_handle, api)
                    except OSError as exc:
                        # Never fall back to a pathname-based unlink, which
                        # could delete a different file after a rename race.
                        cleanup_error = exc
                if target is not None:
                    target.close()
                elif stage_owned:
                    api.close(stage_handle)
                if cleanup_error is not None:
                    raise ArtifactError(
                        "unpublished Windows staging cleanup failed; inspect "
                        "the .ocd-*.stage files in the destination directory"
                    ) from None
    except ArtifactError:
        raise
    except (OSError, UnsafeWindowsMove) as exc:
        raise ArtifactError(
            f"secure Windows artifact publication failed during {phase}: {exc}"
        ) from None


def fetch_verified(
    entry: ArtifactEntry,
    destination_root: str | os.PathLike[str],
    *,
    token_env: str = "GITHUB_TOKEN",
    timeout: float = 30.0,
) -> Path:
    """Explicitly download, verify, and atomically publish a *new* local file.

    The result is the local path.  ``GITHUB_TOKEN`` (or ``token_env``) is read
    from the process environment only for recognized GitHub Release asset URLs;
    it is never stored.  The token is omitted from redirected requests.

    No existing target is replaced, no existing user data is deleted, and no
    upload or remote mutation is performed.  Unpublished staging bytes are
    discarded automatically if a download fails or is interrupted.
    """
    if not isinstance(entry, ArtifactEntry):
        raise ArtifactError("fetch_verified requires an ArtifactEntry")
    # A caller can construct ArtifactEntry directly; apply the same validation.
    entry = _parse_entry(vars(entry))
    if not isinstance(token_env, str) or not token_env:
        raise ArtifactError("token_env must name an environment variable")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ArtifactError("timeout must be a positive finite number")
    if os.name == "nt":
        # Source-level Win32 tests do not establish production safety against
        # junction swaps, hardlink collisions, and filesystem-specific rename
        # behavior.  Permit isolated disposable-fixture qualification only
        # with a deliberately explicit process-local opt-in.  This must run
        # before resolving paths, creating directories, or opening the URL.
        if os.environ.get("OCD_EXPERIMENTAL_WINDOWS_ARTIFACT_PUBLISH") != "1":
            raise ArtifactError(
                "Windows artifact publication is unqualified and disabled; "
                "set OCD_EXPERIMENTAL_WINDOWS_ARTIFACT_PUBLISH=1 only for "
                "isolated qualification fixtures"
            )
        root = Path(destination_root)
        return _fetch_verified_windows(entry, root, token_env=token_env, timeout=timeout)
    root = Path(destination_root)
    root_fd = _open_root(root)
    parent_fd: int | None = None
    stage_fd: int | None = None
    try:
        parent_fd = _open_parent(root_fd, entry.destination_relpath)
        filename = entry.destination_relpath.split("/")[-1]
        _assert_absent(parent_fd, filename)

        stage_fd = _create_anonymous_stage(parent_fd)
        with os.fdopen(os.dup(stage_fd), "wb") as target:
            _download_verified(entry, target, token_env=token_env, timeout=timeout)

        # linkat() creates the verified inode's new name or returns EEXIST,
        # with no overwrite or staging-path race.
        _assert_parent_still_attached(root, root_fd, parent_fd, entry.destination_relpath)
        _publish_noreplace(stage_fd, parent_fd, filename)
        os.fsync(parent_fd)
        _assert_parent_still_attached(root, root_fd, parent_fd, entry.destination_relpath)
        return root / entry.destination_relpath
    except ArtifactError:
        raise
    except OSError:
        raise ArtifactError("secure local artifact publication failed") from None
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


__all__ = ["ArtifactEntry", "ArtifactError", "load_manifest", "fetch_verified"]
