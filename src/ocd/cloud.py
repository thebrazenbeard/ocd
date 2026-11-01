"""Explicit on-demand artifact storage for OCD's local/cloud boundary.

This module provides a content-addressed *local* blob cache and a read-only
HTTPS manifest provider. It does not run a daemon, silently synchronize
folders, upload to cloud accounts, or authorize deletion of a source file.

The local store uses the same Linux anonymous-file, handle-anchored,
no-overwrite publication boundary as :mod:`ocd.artifacts`. Unsupported
filesystems and operating systems fail closed rather than weakening the
invariants. A native Windows store remains a qualification task.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat

from . import artifacts


_DIGEST_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_BLOCK_SIZE = 1024 * 1024


class CloudStoreError(artifacts.ArtifactError):
    """An explicit artifact operation failed before safe publication."""


@dataclass(frozen=True)
class BlobRef:
    """Immutable verified byte identity, independent of a host or account.

    A model job can retain this small record in Git while storing the bytes
    in a controlled local/remote artifact store. ``size`` is an exact count.
    """

    sha256: str
    size: int

    def __post_init__(self) -> None:
        if not isinstance(self.sha256, str) or not _DIGEST_RE.fullmatch(self.sha256):
            raise CloudStoreError("blob SHA-256 must be exactly 64 hexadecimal characters")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise CloudStoreError("blob size must be a nonnegative integer")
        object.__setattr__(self, "sha256", self.sha256.lower())

    @property
    def key(self) -> str:
        """Deterministic portable POSIX-style key inside a blob-store root."""
        return f"sha256/{self.sha256[:2]}/{self.sha256}"


def _open_regular(parent_fd: int, name: str) -> int:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise CloudStoreError("secure content-store reads unavailable on this platform")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CloudStoreError("artifact source is not an ordinary file")
        return fd
    except BaseException:
        if "fd" in locals():
            os.close(fd)
        raise


def _stream_verify(source_fd: int, target_fd: int | None, blob: BlobRef) -> None:
    """Verify bounded streaming bytes; optionally persist into private staging."""
    digest = hashlib.sha256()
    count = 0
    with os.fdopen(os.dup(source_fd), "rb", closefd=True) as source:
        if target_fd is None:
            while chunk := source.read(_BLOCK_SIZE):
                count += len(chunk)
                if count > blob.size:
                    raise CloudStoreError("artifact exceeds the declared size")
                digest.update(chunk)
        else:
            with os.fdopen(os.dup(target_fd), "wb", closefd=True) as target:
                while chunk := source.read(_BLOCK_SIZE):
                    count += len(chunk)
                    if count > blob.size:
                        raise CloudStoreError("artifact exceeds the declared size")
                    target.write(chunk)
                    digest.update(chunk)
                if count == blob.size and hmac.compare_digest(digest.hexdigest(), blob.sha256):
                    target.flush()
                    os.fsync(target.fileno())
    if count != blob.size:
        raise CloudStoreError("artifact is shorter than the declared size")
    if not hmac.compare_digest(digest.hexdigest(), blob.sha256):
        raise CloudStoreError("artifact SHA-256 does not match its identity")


def _publish_copy(
    source_fd: int,
    blob: BlobRef,
    destination_root: Path,
    destination_relpath: str,
    *,
    existing_verified_ok: bool = False,
) -> Path:
    relpath = artifacts._validate_relpath(destination_relpath)
    root_fd: int | None = None
    parent_fd: int | None = None
    stage_fd: int | None = None
    try:
        root_fd = artifacts._open_root(destination_root)
        parent_fd = artifacts._open_parent(root_fd, relpath)
        leaf = relpath.rsplit("/", 1)[-1]
        try:
            artifacts._assert_absent(parent_fd, leaf)
        except artifacts.ArtifactError:
            if not existing_verified_ok:
                raise
            # An existing content-addressed identity is reusable only after
            # independently verifying both requested source and stored bytes.
            # Otherwise a corrupt source would be reported as imported.
            _stream_verify(source_fd, None, blob)
            existing_fd = _open_regular(parent_fd, leaf)
            try:
                _stream_verify(existing_fd, None, blob)
            finally:
                os.close(existing_fd)
            artifacts._assert_parent_still_attached(destination_root, root_fd, parent_fd, relpath)
            return destination_root / relpath

        stage_fd = artifacts._create_anonymous_stage(parent_fd)
        _stream_verify(source_fd, stage_fd, blob)
        artifacts._assert_parent_still_attached(destination_root, root_fd, parent_fd, relpath)
        artifacts._publish_noreplace(stage_fd, parent_fd, leaf)
        os.fsync(parent_fd)
        artifacts._assert_parent_still_attached(destination_root, root_fd, parent_fd, relpath)
        return destination_root / relpath
    except CloudStoreError:
        raise
    except artifacts.ArtifactError as exc:
        raise CloudStoreError(str(exc)) from None
    except OSError:
        raise CloudStoreError("secure artifact publication failed") from None
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        if root_fd is not None:
            os.close(root_fd)


class LocalContentStore:
    """A deliberately invoked SHA-256-addressed local cache.

    The caller creates ``root`` in advance and selects every source and
    destination. ``import_file`` does not remove its source; ``materialize``
    never overwrites a destination. Both verify the exact complete bytes.
    """

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise CloudStoreError("content-store root must be an absolute path without '..'")

    def import_file(self, source_path: str | os.PathLike[str], blob: BlobRef) -> Path:
        if not isinstance(blob, BlobRef):
            raise CloudStoreError("import_file requires a BlobRef")
        source = Path(source_path)
        if not source.is_absolute() or ".." in source.parts or not source.name:
            raise CloudStoreError("artifact source must be an absolute path without '..'")
        source_parent_fd: int | None = None
        source_fd: int | None = None
        try:
            source_parent_fd = artifacts._open_root(source.parent)
            source_fd = _open_regular(source_parent_fd, source.name)
            return _publish_copy(source_fd, blob, self.root, blob.key, existing_verified_ok=True)
        except CloudStoreError:
            raise
        except artifacts.ArtifactError as exc:
            raise CloudStoreError(str(exc)) from None
        except OSError:
            raise CloudStoreError("artifact source could not be safely opened") from None
        finally:
            if source_fd is not None:
                os.close(source_fd)
            if source_parent_fd is not None:
                os.close(source_parent_fd)

    def materialize(
        self,
        blob: BlobRef,
        destination_root: str | os.PathLike[str],
        destination_relpath: str,
    ) -> Path:
        """Copy exactly one verified blob into an explicitly chosen location."""
        if not isinstance(blob, BlobRef):
            raise CloudStoreError("materialize requires a BlobRef")
        root_fd: int | None = None
        blob_parent_fd: int | None = None
        blob_fd: int | None = None
        try:
            root_fd = artifacts._open_root(self.root)
            # The SHA prefix directory must already exist; never create it
            # in a read path, and never follow a replaced ancestor symlink.
            flags = artifacts._secure_directory_flags()
            blob_parent_fd = os.dup(root_fd)
            for component in blob.key.split("/")[:-1]:
                new_fd = os.open(component, flags, dir_fd=blob_parent_fd)
                os.close(blob_parent_fd)
                blob_parent_fd = new_fd
            blob_fd = _open_regular(blob_parent_fd, blob.sha256)
            return _publish_copy(blob_fd, blob, Path(destination_root), destination_relpath)
        except CloudStoreError:
            raise
        except artifacts.ArtifactError as exc:
            raise CloudStoreError(str(exc)) from None
        except OSError:
            raise CloudStoreError("content-addressed blob could not be safely opened") from None
        finally:
            if blob_fd is not None:
                os.close(blob_fd)
            if blob_parent_fd is not None:
                os.close(blob_parent_fd)
            if root_fd is not None:
                os.close(root_fd)


class HTTPSManifestProvider:
    """Explicit read-only network provider, selected by manifest path/key.

    Existing :func:`ocd.artifacts.fetch_verified` enforces HTTPS, checksum,
    size, controlled token handling, and atomic no-replace publication.
    There is no automatic polling, account sync, or upload capability.
    """

    def __init__(self, manifest_path: str | os.PathLike[str]):
        self.entries = {
            entry.destination_relpath: entry for entry in artifacts.load_manifest(manifest_path)
        }

    def fetch(
        self,
        key: str,
        destination_root: str | os.PathLike[str],
        *,
        token_env: str = "GITHUB_TOKEN",
        timeout: float = 30.0,
    ) -> Path:
        try:
            entry = self.entries[artifacts._validate_relpath(key)]
        except KeyError:
            raise CloudStoreError("artifact is not present in the explicit manifest") from None
        return artifacts.fetch_verified(
            entry, destination_root, token_env=token_env, timeout=timeout
        )


__all__ = ["BlobRef", "CloudStoreError", "LocalContentStore", "HTTPSManifestProvider"]
