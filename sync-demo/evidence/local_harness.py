"""Instrumented, in-process evidence run: no Docker.

Starts the real Flask server as a subprocess (over TLS if the demo certs
exist) and drives the real client modules directly, exactly like
tests/test_integration_http.py, but records timestamped events and
byte-level counters to evidence/local_metrics.json for the plot generator
(evidence/make_plots.py) to turn into the sync-timeline, bandwidth, and
resume-recovery figures.

Run:  .venv/bin/python evidence/local_harness.py
"""

from __future__ import annotations

import json
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
from change_detection import StateDB                     # noqa: E402
from transport import HttpServer, run_once               # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Recorder:
    """Collects timestamped events and per-operation byte counters."""

    def __init__(self):
        self.t0 = time.time()
        self.events: list[dict] = []
        self.bandwidth: list[dict] = []
        self.resume: dict = {}
        self._current_op: str | None = None
        self._op_bytes_uploaded = 0
        self._op_chunks_uploaded = 0

    def now(self) -> float:
        return time.time() - self.t0

    def event(self, type_: str, **fields) -> None:
        self.events.append({"t": self.now(), "type": type_, **fields})

    def begin_op(self, name: str) -> None:
        self._current_op = name
        self._op_bytes_uploaded = 0
        self._op_chunks_uploaded = 0

    def record_upload(self, nbytes: int) -> None:
        if self._current_op is not None:
            self._op_bytes_uploaded += nbytes
            self._op_chunks_uploaded += 1

    def end_op(self, *, logical_bytes: int) -> None:
        self.bandwidth.append({
            "op": self._current_op,
            "bytes_read": logical_bytes,
            "bytes_uploaded": self._op_bytes_uploaded,
            "chunks_uploaded": self._op_chunks_uploaded,
        })
        self._current_op = None


def main() -> None:
    rec = Recorder()
    workdir = tempfile.mkdtemp(prefix="syncdemo-evidence-")
    data_dir = os.path.join(workdir, "srv")
    sync_dir = os.path.join(workdir, "sync")
    state_db = os.path.join(workdir, "client.db")
    os.makedirs(sync_dir)
    port = free_port()

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

        def stats():
            r = requests.get(f"{base}/v1/stats", verify=verify)
            return r.json()

        # Wrap put_chunk once so every upload, in every phase below, is
        # counted without threading a counter through sync_file/_upload.
        real_put_chunk = srv.put_chunk

        def counted_put_chunk(chunk_hash, payload):
            rec.record_upload(len(payload))
            return real_put_chunk(chunk_hash, payload)

        srv.put_chunk = counted_put_chunk

        old = time.time_ns() - 3600 * 10**9  # outside the guard window

        # ---------------------------------------------------------------
        # Part 1: sync timeline + bandwidth (create, edit, rename, delete)
        # ---------------------------------------------------------------
        f1 = os.path.join(sync_dir, "notes.txt")
        f2 = os.path.join(sync_dir, "blob.bin")
        notes_body = b"hello world\n" * 500
        blob_body = os.urandom(3 * 1024 * 1024)
        rec.event("baseline", chunks=stats()["chunks"])

        with open(f1, "wb") as f:
            f.write(notes_body)
        with open(f2, "wb") as f:
            f.write(blob_body)
        for p in (f1, f2):
            os.utime(p, ns=(old, old))
        rec.event("create", paths=["notes.txt", "blob.bin"],
                   sizes=[len(notes_body), len(blob_body)])

        rec.begin_op("initial_sync_2_files")
        run_once(db, srv, [sync_dir])
        rec.end_op(logical_bytes=len(notes_body) + len(blob_body))
        rec.event("sync_done", chunks=stats()["chunks"], op="initial_sync_2_files")

        rec.begin_op("no_op_pass")
        run_once(db, srv, [sync_dir])
        rec.end_op(logical_bytes=0)
        rec.event("no_op_pass", chunks=stats()["chunks"])

        blob = bytearray(blob_body)
        edit_span = 50_000
        blob[1_500_000:1_500_000 + edit_span] = os.urandom(edit_span)
        with open(f2, "wb") as f:
            f.write(blob)
        os.utime(f2, ns=(old + 10**9, old + 10**9))
        rec.event("edit", path="blob.bin", bytes_changed=edit_span)

        rec.begin_op("edit_blob_50kb")
        run_once(db, srv, [sync_dir])
        rec.end_op(logical_bytes=len(blob))
        rec.event("sync_done", chunks=stats()["chunks"], op="edit_blob_50kb")

        f2r = os.path.join(sync_dir, "archive.bin")
        os.rename(f2, f2r)
        os.utime(f2r, ns=(old + 2 * 10**9, old + 2 * 10**9))
        rec.event("rename", src="blob.bin", dst="archive.bin")

        rec.begin_op("rename_no_content_change")
        run_once(db, srv, [sync_dir])
        rec.end_op(logical_bytes=len(blob))
        rec.event("sync_done", chunks=stats()["chunks"], op="rename_no_content_change")

        os.remove(f1)
        rec.event("delete", path="notes.txt")
        rec.begin_op("delete_notes")
        run_once(db, srv, [sync_dir])
        rec.end_op(logical_bytes=0)
        s = stats()
        rec.event("sync_done", chunks=s["chunks"], tombstones=s["tombstones"], op="delete_notes")

        # ---------------------------------------------------------------
        # Part 2: resume-after-interruption timeline
        # ---------------------------------------------------------------
        big = os.path.join(sync_dir, "big.bin")
        payload = os.urandom(6 * 1024 * 1024)
        with open(big, "wb") as f:
            f.write(payload)
        os.utime(big, ns=(old, old))
        rec.event("create", paths=["big.bin"], sizes=[len(payload)])

        drop_after = 3
        sent = {"n": 0}
        lock = threading.Lock()

        def flaky_put(h, data):
            with lock:
                if sent["n"] >= drop_after:
                    raise ConnectionError("simulated network drop")
                sent["n"] += 1
            return counted_put_chunk(h, data)

        srv.put_chunk = flaky_put
        sft.MAX_ATTEMPTS = 2
        rec.begin_op("resume_pass1_interrupted")
        t_pass1_start = rec.now()
        try:
            sft.sync_file(db, srv, big)
            raise AssertionError("expected the simulated drop to abort this pass")
        except ConnectionError:
            pass
        t_drop = rec.now()
        chunks_before_drop = stats()["chunks"]
        rec.end_op(logical_bytes=len(payload))  # whole file was chunked; only drop_after chunks got out
        rec.event("resume_interrupted", t_start=t_pass1_start, t_drop=t_drop,
                   chunks_sent=sent["n"], server_chunks=chunks_before_drop)

        time.sleep(2.5)  # represents the outage duration before the link is restored
        srv.put_chunk = counted_put_chunk  # "network restored"
        sft.MAX_ATTEMPTS = 6
        rec.begin_op("resume_pass2_completes")
        t_resume_start = rec.now()
        sft.sync_file(db, srv, big)
        t_resume_end = rec.now()
        rec.end_op(logical_bytes=len(payload))
        rec.event("resume_completed", t_start=t_resume_start, t_end=t_resume_end,
                   server_chunks=stats()["chunks"])
        rec.resume = {
            "file_size": len(payload),
            "drop_after_chunks": drop_after,
            "t_pass1_start": t_pass1_start,
            "t_drop": t_drop,
            "t_resume_start": t_resume_start,
            "t_resume_end": t_resume_end,
        }

        out_path = os.path.join(HERE, "local_metrics.json")
        with open(out_path, "w") as f:
            json.dump({
                "events": rec.events,
                "bandwidth": rec.bandwidth,
                "resume": rec.resume,
            }, f, indent=2)
        print(f"wrote {out_path}")
        print(f"  events: {len(rec.events)}, bandwidth ops: {len(rec.bandwidth)}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
