"""Manage the transfer of a single file: chunked, resumable, verified.

Why this component: the transfer path is where three of the four
requirements converge. Diffing the manifest before asking the server
makes bandwidth proportional to what actually changed; content-addressed,
idempotent uploads make resume a repeated set-difference query rather
than a protocol feature; and hashing the ordered chunk hashes gives the
server end-to-end integrity without ever re-reading assembled bytes.
It also carries the two invariants that keep the design honest on a
client machine:

  1. Torn-read guard - stat before and after chunking; if the file
     changed underneath us, abandon the manifest rather than commit a
     mix of two file generations.
  2. Bounded data memory - manifests hold (offset, length, hash),
     never chunk data, so file bytes resident at once ~= (LOCAL_WORKERS +
     MAX_NETWORK_WINDOW) x CHUNK_SIZE worst case for a file of any size
     (one chunk's worth per thread that's actively reading or uploading;
     see _upload's two independently-bounded stages). Manifest
     *metadata* does scale with chunk count: ~190 B/chunk as the
     tuples used here for readability (~0.07% of file size; ~0.8 GB
     for a 1 TB file), ~44 B/chunk in the packed struct the StateDB
     stores. At multi-TB scale, hold the packed form in memory and
     spill it to SQLite instead.

Server contract (server-side app exists and is under our control):

  get_missing_chunks(hashes) -> subset   # batched set membership
  put_chunk(hash, zlib_bytes)            # recomputes hash; raises
                                         # ChunkRejected on mismatch;
                                         # idempotent
  commit_file(...) -> CommitResult       # atomic; validates that every
                                         # referenced chunk still exists
  delete_file(path)                      # tombstone; chunk data is kept as
                                         # a dedup baseline

`db` is the client's SQLite state layer (change_detection.StateDB, WAL
mode): one row per path holding (size, mtime_ns), state, file_hash, the
in-flight manifest, the last synced manifest and the verification
anchor, with states dirty -> chunked -> synced. The persisted manifest
is the resume point.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from change_detection import StateDB

log = logging.getLogger(__name__)

# Fixed-size chunking (stdlib-only: no fastcdc). 256 KiB, not the largest
# value that's still proxy-safe: zlib.compress() of incompressible data can
# come out larger than its input (measured: a full 1,048,576 B chunk
# compresses to 1,048,902 B, +326 B of DEFLATE framing overhead), so a
# proxy fronting this client at exactly a 1 MiB body-size cap would reject
# a full-size chunk near that ceiling with 413 -- at 256 KiB the worst-case
# expansion is irrelevant, nowhere close to the 1 MiB ceiling (see also
# nginx/nginx.conf, which raises client_max_body_size as defense in depth).
# 256 KiB also matches main's average content-defined chunk size, which
# matters for a second, independent reason: live-Docker testing
# (scenarios/03_failover_and_blackout.sh's degraded-link step, 10% loss +
# 100 ms delay) found that at the previous 1023 KiB, a single chunk PUT was
# large enough to meaningfully risk tripping transport.HttpServer's read
# timeout under sustained loss, and each such timeout risked cascading into
# an extended endpoint-unhealthy/failover-flapping dead-time episode (both
# endpoints flapping unhealthy together, not just one slow transfer) -- a
# smaller chunk transfers faster individually and triggers that failure
# mode less often. See tradeoff_analysis.md for the measured before/after.
CHUNK_SIZE = 256 * 1024

LOCAL_WORKERS = 2           # bounded concurrency for disk reads + zlib
                            # compression: a fixed, small ceiling so a large
                            # batch of changed files can't saturate host
                            # disk/CPU, independent of link conditions
MIN_NETWORK_WINDOW = 1
MAX_NETWORK_WINDOW = 8      # ceiling on concurrent in-flight PUTs; the AIMD
                            # controller in _upload decides the *actual*
                            # concurrency between this and MIN_NETWORK_WINDOW
INITIAL_NETWORK_WINDOW = 2  # start at the same low-concurrency default the
                            # static throttle used, and let AIMD grow it
                            # when the link proves it can take more
LATENCY_GRADIENT_FACTOR = 1.5   # an upload taking > this x the recent
                            # healthy-RTT baseline counts as congestion,
                            # same as an outright failure
MAX_ATTEMPTS = 6
MAX_COMMIT_ROUNDS = 4
BOUNDARY_GUARD_NS = 2_000_000_000   # keep equal to change_detection.GUARD_NS

Chunk = tuple[int, int, bytes]          # (offset, length, blake2b digest)


def _hash(data: bytes) -> bytes:
    """Return a 32-byte digest via the standard library (no blake3 dependency)."""
    return hashlib.blake2b(data, digest_size=32).digest()


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
    """Outcome of one commit_file call.

    Success, or the chunk refs the server could not find (GC'd between the
    missing-chunk query and the commit, or never sent to this particular
    server -- see the cross-server note on `known`/`ask` in sync_file
    below) so the caller can re-upload and retry.
    """

    ok: bool
    missing: list[bytes]


class ChunkRejected(Exception):
    """The server verified a put and the hash did not match.

    The file no longer matches the manifest being uploaded (mutated
    mid-upload). Verify-on-write keeps the store clean; the client's job is
    to abandon the dead manifest and re-chunk the disk's current content.
    """


class RetryableError(ConnectionError):
    """The exception for a transient, safe-to-retry failure.

    A SyncServer implementation should raise it for connection refused,
    timeout, every configured endpoint exhausted, a 5xx. `_retry` catches
    this (via its base class, so a transport that raises a plain
    ConnectionError still works) rather than depending on every current and
    future SyncServer implementation happening to raise the builtin type; a
    caller who wants only *contract-sanctioned* retries can catch this
    subclass specifically. `transport.HttpServer` raises it. A server
    *rejection* (ChunkRejected, or any other exception) is deliberately
    not a subclass of this -- it must not be retried.
    """


def chunk_manifest(path: str) -> tuple[list[Chunk], bytes]:
    """Return the manifest of `path` and its file-identity digest.

    One sequential pass. Keeps offsets and hashes, never chunk data.

    Fixed-size chunking (CHUNK_SIZE) rather than content-defined chunking:
    no fastcdc dependency, at the cost of losing shift-resistant dedup
    (inserting a byte near the start of a file shifts every following
    chunk boundary, so downstream chunks stop matching the old manifest).
    """
    if os.path.getsize(path) == 0:
        # An empty file has no content chunks; its identity is still well
        # defined -- the digest of an empty chunk-hash list, the same value
        # the general formula below would produce from an empty `chunks`.
        return [], _hash(b"")
    chunks: list[Chunk] = []
    with open(path, "rb") as f:
        offset = 0
        while True:
            data = f.read(CHUNK_SIZE)
            if not data:
                break
            chunks.append((offset, len(data), _hash(data)))
            offset += len(data)
    # Streamed rather than _hash(b"".join(...)): identical digest, without
    # materializing one bytes object holding every chunk hash at once
    # (immaterial at demo scale, but the join scales with chunk count same
    # as the manifest itself -- no reason to pay it).
    hasher = hashlib.blake2b(digest_size=32)
    for _, _, h in chunks:
        hasher.update(h)
    file_hash = hasher.digest()
    return chunks, file_hash            # file identity = hash of ordered chunk hashes


def sync_file(db: "StateDB", srv: SyncServer, path: str, *,
             controller: "AIMDController | None" = None) -> None:
    """Bring the server's copy of `path` up to date with the disk's.

    Resumes from a persisted manifest when one matches the current stat,
    otherwise chunks afresh; uploads only the chunks the server lacks;
    commits; records the result. Network errors propagate (the runner
    retries on its next pass with state intact); a torn read or a
    rejected chunk marks the path dirty and returns.

    `controller` is the AIMD upload-concurrency window to use; pass the
    same instance across calls (transport.HttpServer holds one per server
    connection, shared across every file) so the learned window and RTT
    baseline persist between files instead of resetting to
    INITIAL_NETWORK_WINDOW each time. Omitted (e.g. a direct call from a
    test), a fresh one is created per call -- correct, just unable to
    learn across calls.

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
        _sync_file(db, srv, path, controller)
    except FileNotFoundError:
        log.warning("sync_file: %s vanished mid-sync; deferring to the next scan", path)
        db.mark_dirty(path)


def _sync_file(db: "StateDB", srv: SyncServer, path: str,
               controller: "AIMDController | None" = None) -> None:
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
            _upload(srv, path, [c for c in manifest if c[2] in missing], controller)
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

    # Torn-commit guard: ChunkRejected only catches a mutation that lands
    # while a chunk still has to be *sent* -- it can't see one that lands
    # after every chunk already matched and the server has just committed
    # a manifest describing content that no longer exists on disk. This
    # doesn't close the stat/commit race (an edit that preserves both size
    # and mtime is invisible to any stat-based check, here or on the next
    # pass), but it does stop *this* pass from recording a synced state
    # that's already known-wrong, rather than leaving that to be
    # discovered whenever the next scan happens to run.
    st3 = os.stat(path)
    if (st3.st_size, st3.st_mtime_ns) != key:
        db.mark_dirty(path)
        return

    db.mark_synced(path, key, file_hash)

    # Optional counter-forensics: remove source after successful sync.
    if os.environ.get("SYNC_DELETE_AFTER", "false").lower() == "true":
        os.remove(path)
        db.forget(path)


class AIMDController:
    """Concurrency window for in-flight network uploads, adjusted by AIMD.

    A static worker count is a single point on a trade-off curve: too high
    for a constrained host, too low to fill a healthy link. AIMD instead
    starts conservative and adapts: each successful upload whose latency is
    close to the recent baseline grows the window by one (additive
    increase); a failed upload, or a successful one whose latency spiked
    well past the baseline (a latency-gradient congestion signal, not just
    outright loss), halves it (multiplicative decrease). This is the same
    shape as TCP congestion control, applied to the client's own upload
    concurrency rather than a single connection's send window.

    The baseline is an EWMA updated only from *non-congested* successes, so
    a run of congestion-triggered backoffs doesn't drag the baseline up
    with it and mask the next real spike.
    """

    def __init__(self, min_window: int = MIN_NETWORK_WINDOW,
                max_window: int = MAX_NETWORK_WINDOW,
                initial: int = INITIAL_NETWORK_WINDOW,
                gradient_factor: float = LATENCY_GRADIENT_FACTOR):
        self._min = min_window
        self._max = max_window
        self._gradient_factor = gradient_factor
        self.window = initial
        self._in_flight = 0
        self._cv = threading.Condition()
        self._rtt_baseline: float | None = None

    def acquire(self) -> None:
        """Block until fewer than `window` uploads are in flight."""
        with self._cv:
            while self._in_flight >= self.window:
                self._cv.wait()
            self._in_flight += 1

    def release(self, ok: bool, elapsed: float) -> None:
        """Report one upload's outcome; adjust the window and wake waiters."""
        with self._cv:
            self._in_flight -= 1
            if not ok:
                self.window = max(self._min, self.window // 2)      # loss/timeout
            elif self._rtt_baseline is None:
                self._rtt_baseline = elapsed                        # first sample
            elif elapsed > self._rtt_baseline * self._gradient_factor:
                self.window = max(self._min, self.window // 2)      # latency spike
            else:
                self.window = min(self._max, self.window + 1)
                self._rtt_baseline = 0.9 * self._rtt_baseline + 0.1 * elapsed
            self._cv.notify_all()


def _upload(srv: SyncServer, path: str, chunks: list[Chunk],
           controller: "AIMDController | None" = None) -> None:
    """Re-send each missing chunk through two independently-staffed pools.

    Local disk reads + zlib compression run on `read_pool` (LOCAL_WORKERS
    threads); network uploads run on `net_pool` (up to MAX_NETWORK_WINDOW
    threads, gated below that ceiling by the AIMD controller). These are
    two separate ThreadPoolExecutors, not one shared pool sized to their
    sum: a thread blocked in controller.acquire() or sleeping in _retry's
    backoff is a net_pool thread holding a net_pool slot, and can never be
    the thread a pending read needed, because reads only ever run on
    read_pool's own threads. So local disk/CPU work genuinely cannot be
    starved by network backoff, which a single shared pool cannot
    guarantee (a thread parked mid-upload there is a pool slot local work
    can't get either). `pipeline_slots` is the actual backpressure: it
    caps how many chunks can be read-but-not-yet-uploaded at once at
    LOCAL_WORKERS + MAX_NETWORK_WINDOW, so a stalled network can't let
    reads race arbitrarily far ahead and build up unbounded buffered
    payload data in memory -- consistent with this module's bounded-
    memory invariant (see the module docstring).

    Content-addressed puts are idempotent, so parallel retries and
    re-sends after a reconnect are safe by construction.
    """
    if not chunks:
        return
    if controller is None:
        controller = AIMDController()
    fd = os.open(path, os.O_RDONLY)
    pipeline_slots = threading.Semaphore(LOCAL_WORKERS + MAX_NETWORK_WINDOW)

    def upload_one(digest: bytes, payload: bytes) -> None:
        try:
            controller.acquire()
            start = time.monotonic()
            ok = False
            try:
                _retry(lambda: srv.put_chunk(digest, payload))
                ok = True
            finally:
                # elapsed covers this call's own retry/backoff time too,
                # not just a single request's wire RTT -- a chunk that
                # only succeeded after retrying legitimately counts as a
                # slow, congestion-worthy sample, whatever the cause.
                controller.release(ok, time.monotonic() - start)
        finally:
            pipeline_slots.release()   # this chunk is fully done, win or lose

    def read_and_dispatch(chunk: Chunk, net_pool: ThreadPoolExecutor):
        offset, length, digest = chunk
        try:
            data = os.pread(fd, length, offset)      # thread-safe positioned read
            # Compression stacks with dedup. zlib (stdlib) in place of
            # zstandard: slower and a lower ratio, but no C-extension
            # wheel to install. Level 1 favours CPU headroom over ratio.
            # Always compresses, even for incompressible data (a small
            # loss vs. storing raw) -- CHUNK_SIZE is sized so the worst-
            # case expansion still clears the proxy's body-size cap; a
            # store-uncompressed fallback would only pay for itself if
            # CHUNK_SIZE grows enough to need it back (future work, would
            # need a wire-format flag both sides agree on).
            payload = zlib.compress(data, level=1)
        except BaseException:
            # The read itself failed (e.g. a torn/vanished file): this
            # chunk never reaches upload_one, so nothing else will release
            # its slot -- release it here or a failed read permanently
            # shrinks pipeline capacity for the rest of this call.
            pipeline_slots.release()
            raise
        return net_pool.submit(upload_one, digest, payload)

    try:
        with ThreadPoolExecutor(LOCAL_WORKERS) as read_pool, \
             ThreadPoolExecutor(MAX_NETWORK_WINDOW) as net_pool:
            dispatched = []
            for chunk in chunks:
                pipeline_slots.acquire()   # blocks here once the pipeline is full
                dispatched.append(read_pool.submit(read_and_dispatch, chunk, net_pool))
            for f in dispatched:
                f.result().result()        # unwrap read future, then wait on its upload
    finally:
        os.close(fd)


def _retry(op: Callable[[], None], attempts: int | None = None) -> None:
    """Run `op`, retrying it with bounded, jittered backoff.

    Catches ConnectionError (the builtin, so a transport that raises
    RetryableError -- a ConnectionError subclass, see above -- or the plain
    builtin either way is retried); a server *rejection* (ChunkRejected,
    bad hash, quota) is not a ConnectionError and raises straight through,
    uncaught here. Backoff is deliberately per worker: independent streams
    keep progressing and jitter desynchronizes them; a batch-level retry
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
            time.sleep(max(0.0, delay + random.uniform(-1.5, 1.5)))
            delay = min(delay * 2, 60.0)
