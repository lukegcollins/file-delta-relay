"""Managing the transfer of a single file: chunked, resumable, verified.

Why this component: the transfer path is where three of the four
requirements converge. Diffing the manifest before asking the server
makes bandwidth proportional to what actually changed; content-addressed,
idempotent uploads make resume a repeated set-difference query rather
than a protocol feature; and hashing the ordered chunk hashes gives the
server end-to-end integrity without ever re-reading assembled bytes.
It also carries the two invariants that keep the design honest on a
client machine:

  1. Torn-read guard — stat before and after chunking; if the file
     changed underneath us, abandon the manifest rather than commit a
     mix of two file generations.
  2. Bounded data memory — manifests hold (offset, length, hash),
     never chunk data, so file bytes resident at once ~= WORKERS x
     MAX_CHUNK (8 MiB here) for a file of any size. Manifest
     *metadata* does scale with chunk count: ~190 B/chunk as the
     tuples used here for readability (~0.07% of file size; ~0.8 GB
     for a 1 TB file), ~44 B/chunk in the packed struct the StateDB
     stores. At multi-TB scale, hold the packed form in memory and
     spill it to SQLite instead.

Server contract (server-side app exists and is under our control):

  get_missing_chunks(hashes) -> subset   # batched set membership
  put_chunk(hash, zstd_bytes)            # recomputes hash; raises
                                         # ChunkRejected on mismatch;
                                         # idempotent
  commit_file(...) -> CommitResult       # atomic; validates that every
                                         # referenced chunk still exists

`db` is the client's SQLite state layer (change_detection.StateDB, WAL
mode): one row per path holding (size, mtime_ns), state, file_hash, the
in-flight manifest, the last synced manifest and the verification
anchor, with states dirty -> chunked -> synced. The persisted manifest
is the resume point.
"""

from __future__ import annotations

import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol

import zstandard
from blake3 import blake3
from fastcdc import fastcdc

if TYPE_CHECKING:
    from change_detection import StateDB

log = logging.getLogger(__name__)

MIN_CHUNK, AVG_CHUNK, MAX_CHUNK = 64 * 1024, 256 * 1024, 1024 * 1024
WORKERS = 8          # resident memory ~= WORKERS * MAX_CHUNK
MAX_ATTEMPTS = 6
MAX_COMMIT_ROUNDS = 4
BOUNDARY_GUARD_NS = 2_000_000_000   # keep equal to change_detection.GUARD_NS

Chunk = tuple[int, int, bytes]          # (offset, length, blake3 digest)


class SyncServer(Protocol):
    """The four-call server contract sync_file/_upload are written against.

    transport.HttpServer is the real implementation; tests substitute plain
    objects or monkeypatched methods. Formalised here as a Protocol so the
    contract described in this module's docstring is also type-checkable.
    """

    def get_missing_chunks(self, hashes: list[bytes]) -> list[bytes]: ...
    def put_chunk(self, chunk_hash: bytes, payload: bytes) -> None: ...
    def commit_file(self, *, path: str, file_hash: bytes, chunk_hashes: list[bytes],
                    size: int, mtime_ns: int, mode: int) -> "CommitResult": ...
    def delete_file(self, path: str) -> None: ...


@dataclass
class CommitResult:
    """Outcome of one commit_file call: success, or the chunk refs the
    server could not find (GC'd between the missing-chunk query and the
    commit, or never sent to this particular server -- see the cross-server
    note on `known`/`ask` in sync_file below) so the caller can re-upload
    and retry."""

    ok: bool
    missing: list[bytes]


class ChunkRejected(Exception):
    """The server verified a put and the hash did not match: the file no
    longer matches the manifest being uploaded (mutated mid-upload).
    Verify-on-write keeps the store clean; the client's job is to abandon
    the dead manifest and re-chunk the disk's current content."""


def chunk_manifest(path: str) -> tuple[list[Chunk], bytes]:
    """One sequential pass. Keeps offsets and hashes, never chunk data."""
    if os.path.getsize(path) == 0:
        # fastcdc's backend mmaps the input file, and mmap refuses to map a
        # zero-length file. An empty file has no content chunks; its
        # identity is still well defined -- blake3 of an empty chunk-hash
        # list, the same value the general formula below would produce
        # from an empty `chunks` -- so special-case it rather than let
        # fastcdc raise ValueError on a plain `touch`.
        return [], blake3(b"").digest()
    chunks: list[Chunk] = []
    for c in fastcdc(path, MIN_CHUNK, AVG_CHUNK, MAX_CHUNK, fat=True, hf=None):
        chunks.append((c.offset, c.length, blake3(c.data).digest()))
    file_hash = blake3(b"".join(h for _, _, h in chunks)).digest()
    return chunks, file_hash            # file identity = hash of ordered chunk hashes


def sync_file(db: "StateDB", srv: SyncServer, path: str) -> None:
    """Bring the server's copy of `path` up to date with the disk's.

    Resumes from a persisted manifest when one matches the current stat,
    otherwise chunks afresh; uploads only the chunks the server lacks;
    commits; records the result. Network errors propagate (the runner
    retries on its next pass with state intact); a torn read or a
    rejected chunk marks the path dirty and returns.

    Also absorbs FileNotFoundError from any of the stat/open calls below:
    the path was discovered by a scan that ran before this call, and the
    file can vanish in the gap (a real TOCTOU window, not a hypothetical
    one -- see evidence/local_harness.py and the regression test for it).
    Treated the same as a torn read: mark_dirty (a no-op if there was
    never a row for this path) and return. The next scan()'s deletion
    sweep is what actually detects and tombstones the deletion -- it
    notices by absence, not by catching this exception -- so there is
    nothing more to do here than stop cleanly.
    """
    try:
        _sync_file(db, srv, path)
    except FileNotFoundError:
        log.warning("sync_file: %s vanished mid-sync; deferring to the next scan", path)
        db.mark_dirty(path)


def _sync_file(db: "StateDB", srv: SyncServer, path: str) -> None:
    st = os.stat(path)
    key = (st.st_size, st.st_mtime_ns)

    rec = db.get(path)
    if (rec and rec.state == "synced" and rec.key == key
            and st.st_mtime_ns < rec.verified_at_ns - BOUNDARY_GUARD_NS):
        return      # provably unchanged. A matching stat whose mtime is near the
                    # last sync can hide a same-tick write (coarse filesystem
                    # timestamps), so boundary cases fall through here and get
                    # re-verified by content -- see change_detection.classify().
                    # False positives are cheap: dedup turns them into one local
                    # read and zero new bytes on the wire.

    if rec and rec.state == "chunked" and rec.key == key:
        manifest, file_hash = rec.manifest, rec.file_hash   # resume after crash
    else:
        manifest, file_hash = chunk_manifest(path)
        verified_at = time.time_ns()    # guard anchor: content read up to here
        st2 = os.stat(path)
        if (st2.st_size, st2.st_mtime_ns) != key:   # torn-read guard
            db.mark_dirty(path)                     # re-queued for the next pass
            return
        # Persisting the anchor with the manifest keeps it correct across
        # a resumed upload -- commit time must never become the anchor.
        db.save_chunked(path, key, manifest, file_hash, verified_at)

    # Bandwidth: only ask about hashes not in the last *synced* manifest;
    # everything in it is already on the server. This baseline is local and
    # server-agnostic, so if this sync lands on a *different* server than
    # last time (failover, or run_once's random per-file endpoint choice --
    # see transport.py), some "known" chunks may not actually be on *this*
    # server. That is not a correctness gap: commit_file below validates
    # every reference and reports the true gaps as `missing`, and the retry
    # loop re-asks and re-uploads them, so a cross-server sync always
    # converges -- it just costs one extra round trip the first time a file
    # crosses to a store it has never been on (verified in
    # evidence/local_harness.py).
    known = db.prev_hashes(path)
    ask = [h for _, _, h in manifest if h not in known]

    try:
        for _ in range(MAX_COMMIT_ROUNDS):
            missing = set(srv.get_missing_chunks(ask))   # one batched round trip
            _upload(srv, path, [c for c in manifest if c[2] in missing])
            result = srv.commit_file(
                path=path,
                file_hash=file_hash,
                chunk_hashes=[h for _, _, h in manifest],
                size=st.st_size, mtime_ns=st.st_mtime_ns, mode=st.st_mode,
            )
            if result.ok:
                break
            ask = result.missing                    # lost a race with server GC
        else:
            # Racing GC repeatedly means a server-side problem; the structural
            # fix is pinning chunks to an upload session so GC can't collect
            # refs mid-flight. Fail loudly rather than re-upload forever.
            raise RuntimeError(f"commit kept losing the GC race for {path!r}")
    except ChunkRejected:
        # Disk changed under a chunk we still had to send: the manifest is
        # dead, and verify-on-write kept the store clean. Without this, a
        # resume would retry the doomed manifest forever (the mutated file's
        # stat still matches the saved key). Abandon it; next pass re-chunks.
        db.mark_dirty(path)
        return

    db.mark_synced(path, key, file_hash)

    # Optional counter-forensics: remove source after successful sync.
    if os.environ.get("SYNC_DELETE_AFTER", "false").lower() == "true":
        os.remove(path)
        db.forget(path)


def _upload(srv: SyncServer, path: str, chunks: list[Chunk]) -> None:
    """Re-read each missing chunk by offset with a few in flight.

    Content-addressed puts are idempotent, so parallel retries and
    re-sends after a reconnect are safe by construction.
    """
    if not chunks:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            def put(chunk: Chunk) -> None:
                offset, length, digest = chunk
                data = os.pread(fd, length, offset)     # thread-safe positioned read
                # Compression stacks with dedup. For media-heavy trees a
                # per-chunk 'stored' encoding flag skips zstd when it
                # doesn't shrink the chunk (protocol change, noted only).
                payload = zstandard.compress(data, 3)
                _retry(lambda: srv.put_chunk(digest, payload))
            list(pool.map(put, chunks))                 # surfaces the first failure
    finally:
        os.close(fd)


def _retry(op: Callable[[], None], attempts: int | None = None) -> None:
    """Bounded, jittered backoff. Only network errors are retryable —
    a server *rejection* (bad hash, quota) raises straight through.
    Backoff is deliberately per worker: independent streams keep
    progressing and jitter desynchronizes them; a batch-level retry
    loop would reintroduce head-of-line blocking.

    MAX_ATTEMPTS is read at call time, not bound as a default, so a
    runner (or test) can lower it on the module and have it take effect.
    """
    if attempts is None:
        attempts = MAX_ATTEMPTS
    delay = 1.0
    for i in range(attempts):
        try:
            op()
            return
        except ConnectionError:
            if i == attempts - 1:
                raise
            time.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, 60.0)
