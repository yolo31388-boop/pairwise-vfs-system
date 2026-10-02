"""
pairwise-vfs-system - VFSSystem

A small virtual file system for game resources:

- mount_pak(): mounts compressed pak (zip) archives with an in-memory
  index; later mounts override earlier ones.
- read_file(): sandboxed path normalization, LRU cache, size limits.
- build_index(): hash-table resource index with duplicate detection and
  on-disk serialization so mounts do not rebuild it every time.
- write_file(): atomic writes, read-only permission checks, and
  multi-file transactions with rollback.
- resolve_path(): consistent case handling across platforms, symlink
  resolution, and path length limits.
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field

DEFAULT_MAX_FILE_SIZE = 64 * 1024 * 1024  # 64 MiB
DEFAULT_MAX_PATH_LENGTH = 1024
DEFAULT_CACHE_CAPACITY = 128
MAX_SYMLINK_DEPTH = 40
INDEX_FILENAME = ".vfs_index.json"


class VFSError(Exception):
    """Base error for VFS failures."""


class PathEscapeError(VFSError):
    """A path tried to escape the VFS sandbox."""


class PathTooLongError(VFSError):
    """A path exceeded the configured maximum length."""


class FileTooLargeError(VFSError):
    """A file exceeded the configured maximum read size."""


class ReadOnlyError(PermissionError):
    """A write was attempted on a read-only path."""


def _split_segments(path, max_path_length):
    if path is None:
        raise ValueError("path must not be None")
    text = str(path).replace("\\", "/")
    if len(text) > max_path_length:
        raise PathTooLongError(
            f"path exceeds {max_path_length} characters: {text[:64]}..."
        )
    segments = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not segments:
                raise PathEscapeError(f"path escapes sandbox: {path!r}")
            segments.pop()
        else:
            segments.append(part)
    return segments


@dataclass
class Mount:
    """A mounted pak archive with a prebuilt name index."""

    pak_path: str
    mount_point: str = ""
    index: dict = field(default_factory=dict)  # canonical name -> archive name

    def lookup(self, canonical_path):
        return self.index.get(canonical_path)


class VFSTransaction:
    """Stages multiple writes and applies them atomically with rollback."""

    def __init__(self, vfs):
        self.vfs = vfs
        self._ops = []
        self._closed = False

    def write(self, path, data):
        if self._closed:
            raise VFSError("transaction already closed")
        self._ops.append((path, bytes(data)))
        return self

    def commit(self):
        if self._closed:
            raise VFSError("transaction already closed")
        backups = OrderedDict()
        try:
            for path, data in self._ops:
                canonical = self.vfs.resolve_path(path)
                if canonical not in backups:
                    backups[canonical] = (
                        self.vfs.read_file(canonical)
                        if self.vfs.exists(canonical)
                        else None
                    )
                self.vfs.write_file(canonical, data)
        except Exception:
            for canonical, old in reversed(list(backups.items())):
                try:
                    self.vfs._restore_file(canonical, old)
                except OSError:
                    pass  # best-effort rollback
            raise
        finally:
            self._closed = True
        return True

    def rollback(self):
        self._ops.clear()
        self._closed = True
        return True


class VFSSystem:
    def __init__(
        self,
        root=".",
        *,
        case_sensitive=False,
        max_file_size=DEFAULT_MAX_FILE_SIZE,
        max_path_length=DEFAULT_MAX_PATH_LENGTH,
        cache_capacity=DEFAULT_CACHE_CAPACITY,
    ):
        self.root = os.path.abspath(root)
        self.case_sensitive = case_sensitive
        self.max_file_size = max_file_size
        self.max_path_length = max_path_length
        self.cache_capacity = cache_capacity

        self.mounts = []  # later mounts override earlier ones
        self.index = {}  # built by build_index()
        self.duplicates = []
        self.state = {}

        self._cache = OrderedDict()  # canonical path -> bytes (LRU)
        self._symlinks = {}  # canonical link -> canonical target
        self._readonly = set()  # canonical paths

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------
    def resolve_path(self, path=""):
        """Normalize a virtual path into its canonical sandboxed form.

        Handles ``.``/``..`` (rejecting escapes above the root), applies
        consistent case folding so behavior matches on Windows and Linux,
        resolves registered symlinks, and enforces a length limit.
        """
        segments = _split_segments(path, self.max_path_length)
        canonical = self._canonicalize("/".join(segments))
        canonical = self._resolve_symlinks(canonical)
        if len(canonical) > self.max_path_length:
            raise PathTooLongError(
                f"resolved path exceeds {self.max_path_length} characters"
            )
        return canonical

    def _canonicalize(self, normalized):
        return normalized if self.case_sensitive else normalized.casefold()

    def _resolve_symlinks(self, canonical):
        seen = set()
        for _ in range(MAX_SYMLINK_DEPTH):
            target = self._symlinks.get(canonical)
            if target is None:
                # Check whether any path prefix is a symlink.
                parts = canonical.split("/")
                replaced = False
                for i in range(len(parts), 0, -1):
                    prefix = "/".join(parts[:i])
                    if prefix in self._symlinks:
                        rest = parts[i:]
                        canonical = "/".join(
                            [self._symlinks[prefix]] + rest
                        ).strip("/")
                        replaced = True
                        break
                if not replaced:
                    return canonical
            else:
                canonical = target
            if canonical in seen:
                raise VFSError(f"symlink loop detected at {canonical!r}")
            seen.add(canonical)
        raise VFSError(f"too many symlink levels resolving {canonical!r}")

    def add_symlink(self, link, target):
        link_c = self._canonicalize("/".join(_split_segments(link, self.max_path_length)))
        target_c = self._canonicalize(
            "/".join(_split_segments(target, self.max_path_length))
        )
        self._symlinks[link_c] = target_c
        return link_c

    # ------------------------------------------------------------------
    # Pak mounting
    # ------------------------------------------------------------------
    def mount_pak(self, pak_path=None, *, mount_point=""):
        """Mount a compressed pak archive, building a name index once.

        Later mounts take precedence over earlier ones for the same path.
        """
        if pak_path is None:
            return list(self.mounts)
        mount_point_c = self._canonicalize(
            "/".join(_split_segments(mount_point, self.max_path_length))
        )
        index = {}
        with zipfile.ZipFile(pak_path) as pak:
            for info in pak.infolist():
                if info.is_dir():
                    continue
                name = "/".join(_split_segments(info.filename, self.max_path_length))
                canonical = self._canonicalize(name)
                if mount_point_c:
                    canonical = f"{mount_point_c}/{canonical}"
                index[canonical] = info.filename
        mount = Mount(pak_path=os.fspath(pak_path), mount_point=mount_point_c, index=index)
        self.mounts.append(mount)
        for canonical in index:
            self._cache.pop(canonical, None)
        return mount

    def create_pak(self, source_dir, pak_path):
        """Pack a directory into a compressed pak archive."""
        source_dir = os.fspath(source_dir)
        with zipfile.ZipFile(pak_path, "w", zipfile.ZIP_DEFLATED) as pak:
            for dirpath, _dirnames, filenames in os.walk(source_dir):
                for filename in filenames:
                    full = os.path.join(dirpath, filename)
                    rel = os.path.relpath(full, source_dir).replace(os.sep, "/")
                    pak.write(full, rel)
        return os.fspath(pak_path)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def read_file(self, path=None):
        """Read a file from mounts (newest first) or the loose filesystem."""
        if path is None:
            return b""
        canonical = self.resolve_path(path)

        cached = self._cache.get(canonical)
        if cached is not None:
            self._cache.move_to_end(canonical)
            return cached

        data = None
        for mount in reversed(self.mounts):
            archive_name = mount.lookup(canonical)
            if archive_name is not None:
                with zipfile.ZipFile(mount.pak_path) as pak:
                    info = pak.getinfo(archive_name)
                    self._check_size(info.file_size, canonical)
                    data = pak.read(info)
                break

        if data is None:
            real = self._loose_path(canonical)
            if real is None or not os.path.isfile(real):
                raise FileNotFoundError(f"no such file in VFS: {path!r}")
            self._check_size(os.path.getsize(real), canonical)
            with open(real, "rb") as handle:
                data = handle.read()

        self._cache_store(canonical, data)
        return data

    def exists(self, path):
        try:
            canonical = self.resolve_path(path)
        except VFSError:
            return False
        if any(mount.lookup(canonical) is not None for mount in self.mounts):
            return True
        real = self._loose_path(canonical)
        return real is not None and os.path.isfile(real)

    def _check_size(self, size, canonical):
        if size > self.max_file_size:
            raise FileTooLargeError(
                f"{canonical!r} is {size} bytes, limit is {self.max_file_size}"
            )

    def _cache_store(self, canonical, data):
        if len(data) > self.max_file_size:
            return
        self._cache[canonical] = data
        self._cache.move_to_end(canonical)
        while len(self._cache) > self.cache_capacity:
            self._cache.popitem(last=False)

    def _loose_path(self, canonical):
        """Map a canonical virtual path to a real file inside the root."""
        if not canonical:
            return None
        current = self.root
        for segment in canonical.split("/"):
            if self.case_sensitive:
                current = os.path.join(current, segment)
            else:
                if not os.path.isdir(current):
                    return None
                match = None
                for entry in os.listdir(current):
                    if entry.casefold() == segment:
                        match = entry
                        break
                if match is None:
                    return None
                current = os.path.join(current, match)
        real = os.path.realpath(current)
        root_real = os.path.realpath(self.root)
        if real != root_real and not real.startswith(root_real + os.sep):
            raise PathEscapeError(f"path escapes sandbox: {canonical!r}")
        return real

    # ------------------------------------------------------------------
    # Resource index
    # ------------------------------------------------------------------
    def build_index(self, directory=None, *, use_serialized=True, save=True):
        """Build a hash-table index of resources under a directory.

        Detects case-insensitive duplicates (later files win) and
        serializes the index so subsequent mounts can reuse it.
        """
        base = os.path.abspath(directory) if directory else self.root
        index_path = os.path.join(base, INDEX_FILENAME)

        if use_serialized and os.path.isfile(index_path):
            try:
                with open(index_path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if self._serialized_index_fresh(base, payload.get("files", {})):
                    self.index = payload["files"]
                    self.duplicates = payload.get("duplicates", [])
                    return self.index
            except (ValueError, OSError):
                pass  # corrupt index: rebuild below

        files = {}
        duplicates = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for filename in sorted(filenames):
                if filename == INDEX_FILENAME:
                    continue
                full = os.path.join(dirpath, filename)
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                canonical = self._canonicalize(rel)
                stat = os.stat(full)
                entry = {"path": rel, "size": stat.st_size, "mtime": stat.st_mtime}
                if canonical in files:
                    duplicates.append(canonical)
                files[canonical] = entry  # later entries override earlier ones

        self.index = files
        self.duplicates = duplicates
        if save:
            self._atomic_write_real(
                index_path,
                json.dumps(
                    {"files": files, "duplicates": duplicates}, indent=1
                ).encode("utf-8"),
            )
        return self.index

    def _serialized_index_fresh(self, base, files):
        for entry in files.values():
            full = os.path.join(base, entry["path"])
            try:
                stat = os.stat(full)
            except OSError:
                return False
            if stat.st_size != entry["size"] or stat.st_mtime != entry["mtime"]:
                return False
        return True

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def write_file(self, path=None, data=b"", *, transaction=None):
        """Write a file atomically, honoring read-only flags.

        Pass a transaction to stage the write instead of applying it.
        """
        if path is None:
            return False
        if transaction is not None:
            transaction.write(path, data)
            return True
        canonical = self.resolve_path(path)
        self._check_writable(canonical)
        if isinstance(data, str):
            data = data.encode("utf-8")
        real = self._loose_path(canonical)
        if real is None:
            real = os.path.join(self.root, *canonical.split("/"))
        os.makedirs(os.path.dirname(real) or self.root, exist_ok=True)
        self._atomic_write_real(real, bytes(data))
        self._cache.pop(canonical, None)
        return True

    def _restore_file(self, canonical, old):
        """Rollback helper: restore bytes without permission checks."""
        real = self._loose_path(canonical)
        if real is None:
            real = os.path.join(self.root, *canonical.split("/"))
        if old is None:
            if os.path.isfile(real):
                os.remove(real)
        else:
            self._atomic_write_real(real, old)
        self._cache.pop(canonical, None)

    def delete_file(self, path):
        canonical = self.resolve_path(path)
        self._check_writable(canonical)
        real = self._loose_path(canonical)
        if real is not None and os.path.isfile(real):
            os.remove(real)
        self._cache.pop(canonical, None)
        return True

    def begin_transaction(self):
        return VFSTransaction(self)

    def set_readonly(self, path, readonly=True):
        canonical = self.resolve_path(path)
        if readonly:
            self._readonly.add(canonical)
        else:
            self._readonly.discard(canonical)
        return canonical

    def _check_writable(self, canonical):
        if canonical in self._readonly:
            raise ReadOnlyError(f"path is read-only: {canonical!r}")
        real = self._loose_path(canonical)
        if real is not None and os.path.exists(real) and not os.access(real, os.W_OK):
            raise ReadOnlyError(f"path is not writable: {canonical!r}")

    @staticmethod
    def _atomic_write_real(real_path, data):
        directory = os.path.dirname(real_path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".vfs-tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, real_path)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
