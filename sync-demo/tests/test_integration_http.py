"""End-to-end over real HTTP, no Docker required.

Starts the actual Flask server as a subprocess, points the actual HTTP
client at it, and exercises every core requirement plus resume and
failover. This is the proof that the client<->server wiring works; the
Docker setup only packages what runs here.

If the demo certificates exist (./certs/gen_certs.sh), the server is
started over TLS and the client verifies it against the demo CA — the
same configuration docker-compose.yml uses. Otherwise it runs over plain
HTTP; either way the checks are identical.

Run:  python tests/test_integration_http.py

Each numbered check prints one line on success; the first failed
assertion aborts the run with a traceback and a non-zero exit code.
"""

import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CERTS = os.path.join(ROOT, "certs")
sys.path.insert(0, os.path.join(ROOT, "client"))

import requests                                         # noqa: E402
import single_file_transfer as sft                      # noqa: E402
import transport                                        # noqa: E402
from change_detection import StateDB                    # noqa: E402
from transport import HttpServer, run_once              # noqa: E402


def free_port() -> int:
    """A TCP port nothing is listening on right now."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    """Run all checks against a freshly started server; clean up afterwards."""
    # The transport logs endpoint state changes; show them inline (stdout,
    # so they land in order next to the check that triggered them).
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout,
                        format="   [transport] %(message)s")
    workdir = tempfile.mkdtemp(prefix="syncdemo-")
    data_dir = os.path.join(workdir, "srv")      # server chunk store + db
    sync_dir = os.path.join(workdir, "sync")     # client watched tree
    state_db = os.path.join(workdir, "client.db")
    os.makedirs(sync_dir)
    port = free_port()

    # TLS when the demo certs are present (SAN covers 127.0.0.1), else http.
    cert = os.path.join(CERTS, "server.crt")
    key = os.path.join(CERTS, "server.key")
    ca = os.path.join(CERTS, "ca.crt")
    env = dict(os.environ, SYNC_DATA_DIR=data_dir, SYNC_PORT=str(port))
    if all(os.path.exists(p) for p in (cert, key, ca)):
        env.update(SYNC_TLS_CERT=cert, SYNC_TLS_KEY=key)
        scheme, verify = "https", ca
    else:
        scheme, verify = "http", True
    base = f"{scheme}://127.0.0.1:{port}"

    server = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server", "app.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        srv = HttpServer(base, verify=verify)
        srv.wait_healthy()
        db = StateDB(state_db)
        print(f"0. server up over {scheme}"
              + (" (client verifying against certs/ca.crt)" if scheme == "https" else ""))

        def server_bytes(path):
            r = requests.get(f"{base}/v1/file", params={"path": path}, verify=verify)
            return r.content if r.ok else None

        def chunk_count():
            cd = os.path.join(data_dir, "chunks")
            return sum(len(files) for _, _, files in os.walk(cd))

        # Files are given an mtime an hour in the past so they sit well
        # outside the change detector's guard window and the no-op pass
        # really is a no-op (see change_detection.GUARD_NS).
        old = time.time_ns() - 3600 * 10**9

        # --- REQ: change detection + integrity, initial sync ---------------
        f1 = os.path.join(sync_dir, "notes.txt")
        f2 = os.path.join(sync_dir, "blob.bin")
        with open(f1, "wb") as f:
            f.write(b"hello world\n" * 500)
        b2 = os.urandom(3 * 1024 * 1024)
        with open(f2, "wb") as f:
            f.write(b2)
        for p in (f1, f2):
            os.utime(p, ns=(old, old))
        tally = run_once(db, srv, [sync_dir])
        assert tally["synced"] == 2, tally
        assert server_bytes(f1) == b"hello world\n" * 500
        assert server_bytes(f2) == b2                       # byte-exact on server
        print("1. initial sync ok: 2 files, server copies byte-exact (integrity)")

        # --- REQ: change detection, nothing changed -> no work -------------
        assert run_once(db, srv, [sync_dir]) == {"synced": 0, "deleted": 0}
        print("2. no-op pass ok: unchanged tree does nothing (change detection)")

        # --- REQ: bandwidth, small edit -> only new chunks move ------------
        blob = bytearray(b2)
        blob[1_500_000:1_550_000] = os.urandom(50_000)
        with open(f2, "wb") as f:
            f.write(blob)
        os.utime(f2, ns=(old + 10**9, old + 10**9))
        before = chunk_count()
        run_once(db, srv, [sync_dir])
        added = chunk_count() - before
        assert server_bytes(f2) == bytes(blob)
        assert added <= 3, f"expected localized upload, {added} new chunks"
        print(f"3. 50 KB edit ok: only {added} new chunk(s) stored (bandwidth)")

        # --- REQ: bandwidth, rename -> zero new chunks ---------------------
        f2r = os.path.join(sync_dir, "renamed.bin")
        os.rename(f2, f2r)
        os.utime(f2r, ns=(old + 2 * 10**9, old + 2 * 10**9))
        before = chunk_count()
        run_once(db, srv, [sync_dir])                       # detects delete + new path
        assert chunk_count() == before, "rename must move no chunk data"
        assert server_bytes(f2r) == bytes(blob)
        print("4. rename ok: new path committed with zero new chunks (dedup)")

        # --- REQ: reliability, interrupt mid-upload then resume ------------
        big = os.path.join(sync_dir, "big.bin")
        payload = os.urandom(6 * 1024 * 1024)
        with open(big, "wb") as f:
            f.write(payload)
        os.utime(big, ns=(old, old))
        # Wrap put_chunk to die after 3 successful puts, simulating a drop.
        # Uploads run on a worker pool, so the counter needs a lock.
        real_put = srv.put_chunk
        sent = {"n": 0}
        lock = threading.Lock()

        def flaky_put(h, data):
            with lock:
                if sent["n"] >= 3:
                    raise ConnectionError("simulated network drop")
                sent["n"] += 1
            return real_put(h, data)

        srv.put_chunk = flaky_put
        sft.MAX_ATTEMPTS = 2                                 # fail fast in the test
        try:
            sft.sync_file(db, srv, big)
            raise AssertionError("expected the simulated drop to abort this pass")
        except ConnectionError:
            pass
        # File is left mid-flight: some chunks up, state persisted as 'chunked'.
        rec = db.get(big)
        assert rec.state == "chunked", rec.state
        partial = chunk_count()
        srv.put_chunk = real_put                             # network restored
        sft.MAX_ATTEMPTS = 6
        sft.sync_file(db, srv, big)                          # resumes, no re-chunk
        assert server_bytes(big) == payload
        assert db.get(big).state == "synced"
        print(f"5. resume ok: dropped after 3 chunks ({partial} stored), "
              f"reconnected and finished from persisted state (reliability)")

        # --- REQ: reliability, resume across a client process restart ------
        db2 = StateDB(state_db)                              # brand-new handle
        assert db2.get(big).state == "synced"
        assert db2.prev_hashes(big), "baseline manifest must survive restart"
        assert run_once(db2, srv, [sync_dir]) == {"synced": 0, "deleted": 0}
        print("6. restart ok: fresh client reuses persisted state, re-syncs nothing")

        # --- REQ: deletion propagation -------------------------------------
        os.remove(f1)
        tally = run_once(db, srv, [sync_dir])
        assert tally["deleted"] == 1, tally
        assert server_bytes(f1) is None                     # tombstoned
        print("7. delete ok: removed file tombstoned on server")

        # --- REQ: reliability, failover across servers ---------------------
        # A transport whose first (preferred) endpoint is a port nobody
        # listens on must still complete a sync via the second one, and
        # must stop preferring the dead endpoint after the failure threshold.
        # Drive sync_file directly: failover is a transport property, exercised
        # in priority order (try the dead endpoint first, fail over to the live
        # one, mark the dead one down after failure_threshold failures). run_once
        # deliberately assigns each file to a *random* healthy endpoint to spread
        # load, so it would not deterministically touch — and down — the dead one.
        dead = f"http://127.0.0.1:{free_port()}"
        srv2 = HttpServer([dead, base], verify=verify, failure_threshold=3)
        f3 = os.path.join(sync_dir, "after-failover.bin")
        b3 = os.urandom(512 * 1024)
        with open(f3, "wb") as f:
            f.write(b3)
        os.utime(f3, ns=(old, old))
        sft.sync_file(db, srv2, f3)
        assert server_bytes(f3) == b3
        assert not srv2.endpoints[0].healthy and srv2.endpoints[1].healthy
        print("8. failover ok: dead primary skipped, synced via secondary (reliability)")

        # --- regression: a brand-new empty file must not crash the client --
        # fastcdc's backend mmaps its input, and mmap refuses a zero-length
        # file; chunk_manifest() special-cases size==0 rather than let that
        # propagate out of a plain `touch` inside a synced root.
        empty = os.path.join(sync_dir, "empty.txt")
        open(empty, "wb").close()
        os.utime(empty, ns=(old, old))
        tally = run_once(db, srv, [sync_dir])
        assert tally["synced"] == 1, tally
        assert server_bytes(empty) == b""
        print("9. empty file ok: zero-byte file sync"
              " and server reassembly both succeed (reliability)")

        # --- regression: a transiently unreadable directory must not be ----
        # --- mistaken for a deletion -----------------------------------
        guarded_dir = os.path.join(sync_dir, "guarded")
        os.makedirs(guarded_dir)
        guarded_file = os.path.join(guarded_dir, "still-here.bin")
        with open(guarded_file, "wb") as f:
            f.write(b"do not tombstone me")
        os.utime(guarded_file, ns=(old, old))
        run_once(db, srv, [sync_dir])
        assert server_bytes(guarded_file) == b"do not tombstone me"
        if os.getuid() == 0:
            print("10. skipped (running as root: chmod 000 does not restrict root)")
        else:
            os.chmod(guarded_dir, 0o000)
            try:
                tally = run_once(db, srv, [sync_dir])
            finally:
                os.chmod(guarded_dir, 0o755)   # restore so cleanup can remove it
            assert tally["deleted"] == 0, tally
            assert server_bytes(guarded_file) == b"do not tombstone me", \
                "a permission hiccup must never tombstone a file that still exists"
            print("10. permission hiccup ok: an unreadable directory is not"
                  " mistaken for a deletion (integrity)")

        # --- regression: one bad file must not take down the whole pass ----
        bad = os.path.join(sync_dir, "bad.bin")
        good = os.path.join(sync_dir, "good.bin")
        for p, body in ((bad, b"bad"), (good, b"good")):
            with open(p, "wb") as f:
                f.write(body)
            os.utime(p, ns=(old, old))
        real_sync_file = transport.sync_file

        def flaky_sync_file(db, srv, path):
            if path == bad:
                raise RuntimeError("simulated unexpected per-file failure")
            return real_sync_file(db, srv, path)

        print("   (the traceback below is expected: run_once logging the")
        print("   simulated failure for bad.bin and moving on is the fix)")
        transport.sync_file = flaky_sync_file
        try:
            tally = run_once(db, srv, [sync_dir])
        finally:
            transport.sync_file = real_sync_file
        assert tally["synced"] == 1, tally
        assert server_bytes(good) == b"good"
        print("11. per-file isolation ok: one file's unexpected exception"
              " does not block the rest of the pass (reliability)")

        print("\nALL INTEGRATION CHECKS PASSED")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
