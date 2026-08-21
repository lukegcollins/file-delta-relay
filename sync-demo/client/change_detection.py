"""Detecting how a file has changed: metadata quick check + state store.

Why this component: change detection is where a sync client earns its
"lightweight" adjective — the common case is that nothing changed, and
that case must cost a stat, not a read. The design here is three rules:

  1. Key files by path -> (size, mtime_ns). Not inode: atomic saves
     (write-temp-then-rename) allocate a new inode for the "same" file.
     Not ctime: a chmod churns it, and its semantics differ across
     platforms. When metadata is inconclusive, the content hash — not
     the stat — is the final arbiter.
  2. The quick check can prove change but cannot always prove sameness.
     mtime has coarse granularity on some filesystems (FAT: 2 s) and a
     write in the same tick can leave stat identical. So any file
     whose mtime falls within GUARD of its last content verification is
     reported as inconclusive and re-verified. The check therefore errs
     toward false positives — which deduplication makes nearly free
     (one local read, zero new bytes on the wire) — and never toward
     false negatives, which would silently violate integrity.
  3. Events narrow, scans reconcile. OS watchers (inotify, FSEvents,
     USN) feed check(); a periodic scan() sweep catches dropped events
     and detects deletions; watcher queue overflow falls back to a full
     scan. The database, not the watcher, is the source of truth.

classify() is the pure decision kernel shared by both modes, so the
policy is unit-testable without a filesystem. StateDB is the concrete
SQLite layer (WAL mode) consumed by single_file_transfer.py — the two
files compose into a working client skeleton:

    for change in scan(db, roots):
        if change.reason is Reason.DELETED:
            ...commit tombstone, then db.forget(change.path)
        else:
            sync_file(db, srv, change.path)

Out of scope, by design: a forged utime(2) defeats any metadata check —
the answer is a periodic full-hash sweep, not a cleverer stat. Multi-
process coordination (the DB doubles as the work queue) is likewise the
runner's job, not the detector's.
"""

from __future__ import annotations

import os
import sqlite3
import stat as stat_mod
import struct
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Iterable, Iterator, Protocol

GUARD_NS = 2_000_000_000        # 2 s: coarsest common mtime granularity;
                                # also absorbs modest clock skew. Remote
                                # filesystems with untrusted clocks (NFS)
                                # want a wider guard or periodic full-hash
                                # sweeps.


class Reason(Enum):
    """Why a path needs attention; the verdict of classify()."""

    NEW = auto()                # no record of this path
    STAT_CHANGED = auto()       # size or mtime_ns differs from last sync
    INTERRUPTED = auto()        # a previous sync never completed
    BOUNDARY = auto()           # stat matches but mtime is within GUARD of
                                #   the last content verification:
                                #   inconclusive, re-verify
    DELETED = auto()            # in the DB, no longer on disk


@dataclass(frozen=True)
class Change:
    """One unit of work for the runner: a path and why it was selected."""

    path: str
    reason: Reason


class _StatLike(Protocol):
    """The two stat fields classify() actually reads.

    A Protocol rather than os.stat_result so the pure kernel stays testable
    with plain stand-ins (see tests/test_change_detection.py's SimpleNamespace
    fixtures) without weakening the type of the real call sites.
    """

    st_size: int
    st_mtime_ns: int


def classify(rec: "FileRec | None", st: _StatLike, now_ns: int) -> Reason | None:
    """The decision kernel: what does one stat result mean for one file?

    Pure function of (db record, stat, clock) so the boundary arithmetic
    is testable in isolation. Returns None only when it can *prove* the
    file unchanged since the last completed sync.
    """
    if rec is None:
        return Reason.NEW
    if rec.state != "synced":
        return Reason.INTERRUPTED
    if (st.st_size, st.st_mtime_ns) != rec.key:
        return Reason.STAT_CHANGED
    if st.st_mtime_ns >= rec.verified_at_ns - GUARD_NS:
        return Reason.BOUNDARY  # same-tick write could hide here. The
        # anchor is when content was last READ and matched, never commit
        # time: a slow upload must not widen the blind spot between
        # chunking and commit.
    return None


def _contains(root: str, path: str) -> bool:
    """True if `path` is strictly inside the (already abspath'd) `root`.

    `path == root` is never "inside" it, consistent for every root
    including the degenerate root == '/'. Plain `path.startswith(root +
    os.sep)` breaks for that case: os.path.abspath('/') + os.sep == '//',
    which no ordinary single-leading-slash absolute path ever starts with,
    so every event and every deletion under a root of '/' would silently
    vanish.
    """
    if path == root:
        return False
    prefix = root if root == os.sep else root + os.sep
    return path.startswith(prefix)


def scan(db: "StateDB", roots: list[str]) -> Iterator[Change]:
    """Reconciliation sweep, and the only detector of deletions.

    os.scandir gives file type from the directory read (d_type), and on
    Windows the full stat too; on Linux entry.stat() is one cheap
    dirfd-relative syscall. Either way the unchanged case reads no file
    content.
    """
    roots = [os.path.abspath(r) for r in roots]
    seen: set[str] = set()
    # Directories that exist but could not be read this pass (e.g. a
    # transient permission change). Their contents are known to still
    # exist, so paths under them must not be reported deleted just because
    # this pass could not confirm them -- see the comment at the deletion
    # sweep below.
    unreadable: list[str] = []
    stack = list(roots)
    while stack:
        d = stack.pop()
        try:
            entries = os.scandir(d)
        except PermissionError:
            unreadable.append(d)
            continue
        except (FileNotFoundError, NotADirectoryError):
            continue    # the directory itself is actually gone (or was
                        # replaced by a non-directory); the deletion sweep
                        # below correctly reports everything under it.
        with entries:
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue                    # symlinks, sockets, ...
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue                        # raced with a delete
                seen.add(e.path)
                reason = classify(db.get(e.path), st, time.time_ns())
                if reason:
                    yield Change(e.path, reason)
    # Deletion sweep. A removed directory surfaces as one event per file
    # inside it; runners batch the tombstone commits. A path under a
    # directory this pass could not read (`unreadable`) is skipped rather
    # than reported deleted: the guiding policy (see the module docstring)
    # is false positives over false negatives, and "briefly unreadable"
    # must never be reported as "gone" -- a later pass, once the directory
    # is readable again, resolves it correctly either way.
    for path in db.all_paths():
        if path in seen:
            continue
        if not any(_contains(r, path) for r in roots):
            continue
        if any(path == d or _contains(d, path) for d in unreadable):
            continue
        yield Change(path, Reason.DELETED)


def check(db: "StateDB", paths: Iterable[str],
         roots: list[str] | None = None) -> Iterator[Change]:
    """Primary path: run the kernel over candidate paths from an OS watcher.

    A vanished path is a deletion. Pass roots to drop stray events
    outside the monitored trees (renames across a watch boundary can
    produce them).
    """
    roots = [os.path.abspath(r) for r in roots] if roots else None
    for path in map(os.path.abspath, paths):
        if roots and not any(_contains(r, path) for r in roots):
            continue
        try:
            st = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            if db.get(path):
                yield Change(path, Reason.DELETED)
            continue
        except OSError:
            continue    # transiently unreadable (e.g. a permission change):
                        # inconclusive, not evidence of deletion -- same
                        # policy as scan()'s `unreadable` handling above.
        if not stat_mod.S_ISREG(st.st_mode):
            continue
        reason = classify(db.get(path), st, time.time_ns())
        if reason:
            yield Change(path, reason)


# --- state store -----------------------------------------------------------

_CHUNK = struct.Struct("<QI32s")    # offset u64, length u32, blake3 digest


Manifest = list[tuple[int, int, bytes]]     # [(offset, length, blake3 digest), ...]


def _pack(manifest: Manifest) -> bytes:
    """Serialise [(offset, length, digest), ...] to the 44-byte/chunk blob."""
    return b"".join(_CHUNK.pack(o, n, h) for o, n, h in manifest)


def _unpack(blob: bytes) -> Manifest:
    """Inverse of _pack."""
    return [(o, n, h) for o, n, h in _CHUNK.iter_unpack(blob)]


@dataclass
class FileRec:
    """What the state store knows about one path."""

    key: tuple[int, int]            # (size, mtime_ns) at last chunking
    state: str                      # dirty | chunked | synced
    file_hash: bytes | None
    manifest: Manifest | None       # resume point while state == chunked
    verified_at_ns: int             # content last read & matched: guard anchor


class StateDB:
    """SQLite state layer shared by the detector and the transfer path.

    Two manifest slots matter: `manifest` is the in-flight resume point;
    `synced_manifest` is the last committed one and stays the dedup
    baseline even while a new sync is under way.
    """

    _SCHEMA = """CREATE TABLE IF NOT EXISTS files (
        path            TEXT PRIMARY KEY,
        size            INTEGER NOT NULL,
        mtime_ns        INTEGER NOT NULL,
        state           TEXT NOT NULL,
        file_hash       BLOB,
        manifest        BLOB,
        synced_manifest BLOB,
        verified_at_ns  INTEGER NOT NULL DEFAULT 0)"""

    def __init__(self, dbpath: str):
        self._c = sqlite3.connect(dbpath)
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.execute("PRAGMA synchronous=NORMAL")
        self._c.execute(self._SCHEMA)
        self._c.commit()

    def get(self, path: str) -> FileRec | None:
        """The record for `path`, or None if it has never been seen."""
        row = self._c.execute(
            "SELECT size, mtime_ns, state, file_hash, manifest, verified_at_ns"
            " FROM files WHERE path=?", (path,)).fetchone()
        if row is None:
            return None
        size, mtime_ns, state, fh, mblob, verified_at = row
        return FileRec((size, mtime_ns), state, fh,
                       _unpack(mblob) if mblob else None, verified_at)

    def save_chunked(self, path: str, key: tuple[int, int], manifest: Manifest,
                     file_hash: bytes, verified_at_ns: int) -> None:
        """Persist a fresh manifest as the resume point (state -> chunked).

        Committed before the first byte is uploaded: a crash after this
        resumes at upload instead of re-chunking.
        """
        self._c.execute(
            "INSERT INTO files (path, size, mtime_ns, state, file_hash,"
            " manifest, verified_at_ns) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET size=excluded.size,"
            " mtime_ns=excluded.mtime_ns, state='chunked',"
            " file_hash=excluded.file_hash, manifest=excluded.manifest,"
            " verified_at_ns=excluded.verified_at_ns",
            (path, key[0], key[1], "chunked", file_hash, _pack(manifest),
             verified_at_ns))
        self._c.commit()

    def mark_synced(self, path: str, key: tuple[int, int], file_hash: bytes) -> None:
        """The commit succeeded: promote the manifest to the dedup baseline.

        Deliberately does NOT touch verified_at_ns: the guard anchor is
        the verification instant recorded at save_chunked, and it must
        survive a resumed upload unchanged.
        """
        self._c.execute(
            "UPDATE files SET state='synced', synced_manifest=manifest,"
            " manifest=NULL WHERE path=?", (path,))
        self._c.commit()

    def mark_dirty(self, path: str) -> None:
        """Discard the in-flight manifest (e.g. torn read) but keep the
        old dedup baseline; the path is re-chunked on the next pass."""
        self._c.execute(
            "UPDATE files SET state='dirty', manifest=NULL WHERE path=?", (path,))
        self._c.commit()

    def prev_hashes(self, path: str) -> set[bytes]:
        """Chunk hashes of the last *synced* manifest: already on the server."""
        row = self._c.execute(
            "SELECT synced_manifest FROM files WHERE path=?", (path,)).fetchone()
        return {h for _, _, h in _unpack(row[0])} if row and row[0] else set()

    def all_paths(self) -> list[str]:
        """Every tracked path. At millions of files: index by root instead."""
        return [p for (p,) in self._c.execute("SELECT path FROM files")]

    def forget(self, path: str) -> None:
        """Drop the row once the tombstone commit has succeeded."""
        self._c.execute("DELETE FROM files WHERE path=?", (path,))
        self._c.commit()
