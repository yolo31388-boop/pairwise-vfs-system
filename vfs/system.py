"""
pairwise-vfs-system - VFSSystem

A virtual file system with pak mounting, caching, transactional writes
and safe path resolution.

Pak format (little-endian)::

    [8B magic "VPAK0001"][8B index length][JSON index][file data blobs]

Index JSON maps normalized virtual paths to
``{"offset", "size", "compressed_size", "compressed", "sha1"}``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import tempfile
import zlib
from collections import OrderedDict

MAGIC = b"VPAK0001"
_HEADER = struct.Struct("<8sQ")

DEFAULT_MAX_FILE_SIZE = 64 * 1024 * 1024  # 64 MiB
DEFAULT_MAX_PATH_LENGTH = 260
DEFAULT_CACHE_CAPACITY = 128


class VFSError(Exception):
    """Base error for all VFS failures."""


class PathEscapeError(VFSError):
    """A path tried to escape the virtual sandbox root."""


class PathTooLongError(VFSError):
    """A path exceeded the maximum allowed length."""


class FileTooLargeError(VFSError):
    """A file exceeded the configured read size limit."""


class ReadOnlyError(VFSError):
    """Attempted to write to a read-only target."""


class PakFormatError(VFSError):
    """A pak file is malformed or corrupt."""


class _PakEntry:
    __slots__ = ("offset", "size", "compressed_size", "compressed", "sha1")

    def __init__(self, offset, size, compressed_size, compressed, sha1=""):
        self.offset = offset
        self.size = size
        self.compressed_size = compressed_size
        self.compressed = compressed
        self.sha1 = sha1

    def to_dict(self):
        return {
            "offset": self.offset,
            "size": self.size,
            "compressed_size": self.compressed_size,
            "compressed": self.compressed,
            "sha1": self.sha1,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            int(data["offset"]),
            int(data["size"]),
            int(data.get("compressed_size", data["size"])),
            bool(data.get("compressed", False)),
            data.get("sha1", ""),
        )


class _PakMount:
    __slots__ = ("pak_path", "mount_point", "entries", "read_only", "order")

    def __init__(self, pak_path, mount_point, entries, read_only, order):
        self.pak_path = pak_path
        self.mount_point = mount_point
        self.entries = entries  # normalized virtual path -> _PakEntry
        self.read_only = read_only
        self.order = order


class _Transaction:
    """Stages writes so a group of files commits or rolls back together."""

    def __init__(self, vfs):
        self._vfs = vfs
        self._staged = []  # (target_path, temp_path)
        self._backups = []  # (target_path, backup_path)
        self._done = False

    def stage(self, target_path, temp_path):
        self._staged.append((target_path, temp_path))

    def commit(self):
        if self._done:
            raise VFSError("transaction already finished")
        try:
            for target, _tmp in self._staged:
                if os.path.exists(target):
                    backup = target + ".vfsbak"
                    shutil.copy2(target, backup)
                    self._backups.append((target, backup))
            for target, tmp in self._staged:
                os.replace(tmp, target)
        except OSError:
            self.rollback()
            raise
        else:
            self._done = True
            for _target, backup in self._backups:
                if os.path.exists(backup):
                    os.remove(backup)

    def rollback(self):
        if self._done:
            return
        for target, tmp in self._staged:
            if os.path.exists(tmp):
                os.remove(tmp)
        for target, backup in self._backups:
            if os.path.exists(backup):
                os.replace(backup, target)
        self._done = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is None and not self._done:
            self.commit()
        elif exc_type is not None:
            self.rollback()
        return False


class VFSSystem:
    def __init__(
        self,
        case_sensitive=False,
        max_file_size=DEFAULT_MAX_FILE_SIZE,
        max_path_length=DEFAULT_MAX_PATH_LENGTH,
        cache_capacity=DEFAULT_CACHE_CAPACITY,
    ):
        self.state = {}
        self.case_sensitive = case_sensitive
        self.max_file_size = max_file_size
        self.max_path_length = max_path_length
        self.cache_capacity = cache_capacity
        self._mounts = []  # later mounts override earlier ones
        self._index = {}  # normalized path -> (_PakMount, _PakEntry)
        self._cache = OrderedDict()  # LRU: normalized path -> bytes
        self._read_only = set()  # normalized paths that reject writes
        self.duplicates = []  # duplicate paths detected while indexing

    # ------------------------------------------------------------------
    # path handling
    # ------------------------------------------------------------------
    def resolve_path(self, path=None, *, follow_symlinks=True):
        """Normalize a virtual path.

        Handles case folding (consistent on Windows and Linux), collapses
        ``.``/``..`` segments, blocks sandbox escapes, resolves symlinks
        for disk-backed paths and enforces a maximum path length.
        """
        if path is None:
            return "/"
        if not isinstance(path, str):
            raise VFSError(f"path must be str, got {type(path).__name__}")
        if len(path) > self.max_path_length:
            raise PathTooLongError(
                f"path exceeds {self.max_path_length} characters: {path[:32]}..."
            )
        path = path.replace("\\", "/")
        if not self.case_sensitive:
            path = path.lower()

        parts = []
        for segment in path.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                if not parts:
                    raise PathEscapeError(f"path escapes sandbox root: {path!r}")
                parts.pop()
            else:
                parts.append(segment)
        normalized = "/" + "/".join(parts)

        if follow_symlinks and os.path.islink(normalized):
            real = os.path.realpath(normalized)
            if not self.case_sensitive:
                real = real.lower()
            normalized = real
        return normalized

    # ------------------------------------------------------------------
    # pak creation / mounting
    # ------------------------------------------------------------------
    @staticmethod
    def create_pak(source_dir, pak_path, compress=True):
        """Pack every file under ``source_dir`` into a pak archive."""
        entries = {}
        blobs = []
        offset = 0
        for root, _dirs, files in os.walk(source_dir):
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, source_dir).replace(os.sep, "/")
                virtual = "/" + rel.lower()
                data = open(full, "rb").read()
                if compress:
                    blob = zlib.compress(data, 9)
                    if len(blob) >= len(data):
                        blob, is_compressed = data, False
                    else:
                        is_compressed = True
                else:
                    blob, is_compressed = data, False
                entries[virtual] = _PakEntry(
                    offset=offset,
                    size=len(data),
                    compressed_size=len(blob),
                    compressed=is_compressed,
                    sha1=hashlib.sha1(data).hexdigest(),
                ).to_dict()
                blobs.append(blob)
                offset += len(blob)

        index_blob = json.dumps(entries, sort_keys=True).encode("utf-8")
        tmp_path = pak_path + ".tmp"
        with open(tmp_path, "wb") as fh:
            fh.write(_HEADER.pack(MAGIC, len(index_blob)))
            fh.write(index_blob)
            for blob in blobs:
                fh.write(blob)
        os.replace(tmp_path, pak_path)
        return pak_path

    @staticmethod
    def _read_pak_index(pak_path):
        with open(pak_path, "rb") as fh:
            header = fh.read(_HEADER.size)
            if len(header) != _HEADER.size:
                raise PakFormatError(f"not a pak file: {pak_path}")
            magic, index_len = _HEADER.unpack(header)
            if magic != MAGIC:
                raise PakFormatError(f"bad pak magic in {pak_path}")
            raw = fh.read(index_len)
            if len(raw) != index_len:
                raise PakFormatError(f"truncated pak index in {pak_path}")
        try:
            table = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PakFormatError(f"corrupt pak index in {pak_path}") from exc
        data_offset = _HEADER.size + index_len
        entries = {}
        for virtual, meta in table.items():
            entry = _PakEntry.from_dict(meta)
            entry.offset += data_offset
            entries[virtual] = entry
        return entries

    def _index_cache_path(self, pak_path):
        return pak_path + ".idx"

    def _load_serialized_index(self, pak_path):
        cache_path = self._index_cache_path(pak_path)
        if not os.path.exists(cache_path):
            return None
        if os.path.getmtime(cache_path) < os.path.getmtime(pak_path):
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as fh:
                table = json.load(fh)
        except (OSError, ValueError):
            return None
        return {v: _PakEntry.from_dict(m) for v, m in table.items()}

    def _save_serialized_index(self, pak_path, entries):
        cache_path = self._index_cache_path(pak_path)
        table = {v: e.to_dict() for v, e in entries.items()}
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(table, fh, sort_keys=True)
        os.replace(tmp_path, cache_path)

    def mount_pak(self, pak_path=None, mount_point="/", *, read_only=True):
        """Mount a pak archive.

        The pak index is parsed (or loaded from its serialized cache) once
        at mount time, so reads never rescan the archive. Later mounts
        override earlier mounts for the same virtual path.
        """
        if pak_path is None:
            return [
                {
                    "pak_path": m.pak_path,
                    "mount_point": m.mount_point,
                    "entries": len(m.entries),
                    "read_only": m.read_only,
                }
                for m in self._mounts
            ]
        if not os.path.isfile(pak_path):
            raise VFSError(f"pak not found: {pak_path}")

        entries = self._load_serialized_index(pak_path)
        if entries is None:
            entries = self._read_pak_index(pak_path)
            self._save_serialized_index(pak_path, entries)

        mount_point = self.resolve_path(mount_point)
        mount = _PakMount(
            pak_path=pak_path,
            mount_point=mount_point,
            entries=entries,
            read_only=read_only,
            order=len(self._mounts),
        )
        self._mounts.append(mount)
        self._rebuild_index()
        return mount

    def unmount_pak(self, pak_path):
        for i, mount in enumerate(self._mounts):
            if mount.pak_path == pak_path:
                del self._mounts[i]
                self._rebuild_index()
                return True
        return False

    def _rebuild_index(self):
        self._index = {}
        self.duplicates = []
        for mount in self._mounts:  # ascending order: later mounts win
            for virtual, entry in mount.entries.items():
                key = self.resolve_path(mount.mount_point.rstrip("/") + virtual)
                if key in self._index:
                    self.duplicates.append(key)
                self._index[key] = (mount, entry)
        self._cache.clear()

    # ------------------------------------------------------------------
    # resource index
    # ------------------------------------------------------------------
    def build_index(self, paths=None, *, save_to=None, detect_duplicates=True):
        """Build a hash-table resource index with duplicate detection.

        With no arguments, rebuilds and returns the live mount index.
        The index can be serialized to disk and later restored with
        :meth:`load_index` so mounts do not have to rescan archives.
        """
        if paths is None:
            if not self._index and self._mounts:
                self._rebuild_index()
            index = dict(self._index)
        else:
            index = {}
            for path in paths:
                key = self.resolve_path(path)
                if detect_duplicates and key in index:
                    self.duplicates.append(key)
                    continue  # first one wins; later duplicates are rejected
                index[key] = path
        if save_to is not None:
            serializable = {
                k: (v if isinstance(v, str) else v[1].to_dict())
                for k, v in index.items()
            }
            tmp_path = save_to + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(serializable, fh, sort_keys=True)
            os.replace(tmp_path, save_to)
        return index

    def load_index(self, index_path):
        """Load a previously serialized index from disk."""
        with open(index_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def read_file(self, path=None, *, use_cache=True, max_size=None):
        """Read a file from the mounted paks.

        Paths are normalized (``..`` escapes are rejected), results are
        served from an LRU cache so decompression happens once, and files
        larger than the size limit are refused.
        """
        if path is None:
            return b""
        key = self.resolve_path(path)
        limit = self.max_file_size if max_size is None else max_size

        if use_cache and key in self._cache:
            self._cache.move_to_end(key)
            data = self._cache[key]
            if len(data) > limit:
                raise FileTooLargeError(
                    f"{key} is {len(data)} bytes, limit is {limit} bytes"
                )
            return data

        if key not in self._index:
            raise FileNotFoundError(f"no such virtual file: {key}")
        mount, entry = self._index[key]

        if entry.size > limit:
            raise FileTooLargeError(
                f"{key} is {entry.size} bytes, limit is {limit} bytes"
            )

        with open(mount.pak_path, "rb") as fh:
            fh.seek(entry.offset)
            blob = fh.read(entry.compressed_size)
        if len(blob) != entry.compressed_size:
            raise PakFormatError(f"truncated data for {key} in {mount.pak_path}")
        data = zlib.decompress(blob) if entry.compressed else blob
        if len(data) != entry.size:
            raise PakFormatError(f"size mismatch for {key} in {mount.pak_path}")

        if use_cache:
            self._cache[key] = data
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_capacity:
                self._cache.popitem(last=False)
        return data

    def invalidate_cache(self, path=None):
        if path is None:
            self._cache.clear()
        else:
            self._cache.pop(self.resolve_path(path), None)

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------
    def begin_transaction(self):
        """Start a transaction staging writes for an all-or-nothing commit."""
        return _Transaction(self)

    def mark_read_only(self, path):
        self._read_only.add(self.resolve_path(path))

    def unmark_read_only(self, path):
        self._read_only.discard(self.resolve_path(path))

    def _check_writable(self, key):
        if key in self._read_only:
            raise ReadOnlyError(f"path is read-only: {key}")
        for mount in self._mounts:
            if mount.read_only and key in {
                self.resolve_path(mount.mount_point.rstrip("/") + v)
                for v in mount.entries
            }:
                raise ReadOnlyError(f"path belongs to a read-only mount: {key}")

    def write_file(self, path=None, data=b"", *, transaction=None):
        """Write a file atomically with permission checks.

        Data is written to a temporary file and renamed into place, so a
        crash mid-write never corrupts the target. Pass a transaction from
        :meth:`begin_transaction` to commit multiple files together.
        """
        if path is None:
            return False
        key = self.resolve_path(path)
        self._check_writable(key)
        if isinstance(data, str):
            data = data.encode("utf-8")
        if len(data) > self.max_file_size:
            raise FileTooLargeError(
                f"refusing to write {len(data)} bytes, limit is {self.max_file_size}"
            )

        target = key
        parent = os.path.dirname(target) or "/"
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".vfs-", dir=parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            os.remove(tmp_path)
            raise

        if transaction is not None:
            transaction.stage(target, tmp_path)
        else:
            os.replace(tmp_path, target)
        self.invalidate_cache(key)
        return True
