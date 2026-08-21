"""Minimal sync server: the counterpart to the client the assessment asks for.

It exists only to exercise the client end-to-end, so it is deliberately
small — but it honours the parts of the contract the client's correctness
depends on:

  * verify-on-write: every chunk's BLAKE3 is recomputed server-side and a
    mismatch is rejected (409), so the store can never be poisoned by a
    corrupt or mid-upload-mutated payload;
  * commit validates references and identity: a commit naming a chunk
    the store no longer holds returns the missing list rather than a
    broken manifest (which is what lets the client resolve the GC race),
    and a file_hash that is not the BLAKE3 of the ordered chunk digests
    is rejected;
  * content addressing: chunks live at their hash, so puts are idempotent
    and dedup is just set membership.

Endpoints (all JSON except the raw chunk body):

  Legacy (kept for backward compatibility):
    GET  /v1/health                 -> {"ok": true}
    GET  /v1/stats                  -> {"chunks","files","tombstones"}
    POST /v1/missing  {hashes:[hex]}-> {"missing":[hex]}
    PUT  /v1/chunk/<hex>   <zstd>   -> 204 | 409
    POST /v1/commit   {...}         -> {"ok":true} | {"ok":false,"missing":[hex]}
    POST /v1/delete   {path}        -> {"ok":true}
    GET  /v1/file?path=<path>       -> reassembled bytes

  New "mimicked" API (used when client enables stealth mode):
    GET  /api/v1/status             -> {"ok": true}                # health
    GET  /api/v1/stats              -> {"chunks","files","tombstones"}
    POST /api/v1/collect            -> {"missing":[hex]}           # dedup query
    PUT  /api/v1/events/<hex>       -> 204 | 409                   # chunk upload
    POST /api/v1/session            -> {"ok":true} | {"ok":false,"missing":[hex]}
    POST /api/v1/retract            -> {"ok":true}                 # delete
    GET  /api/v1/file?path=<path>   -> reassembled bytes

  The new endpoints accept the same payloads but may include additional
  dummy fields (device_id, timestamp, event_type) that are ignored.
  They are designed to resemble common telemetry/analytics APIs.

Chunk hashes cross the wire hex-encoded; on disk a chunk is a file named
by its hex digest under CHUNK_DIR. Manifests and tombstones are rows in a
small SQLite DB. State survives a container restart, so the demo can show
the server retaining chunks across a client reconnect. Several instances
may run side by side (the compose file starts two) but they are
independent stores — nothing replicates between them.

Configuration: SYNC_DATA_DIR (default /data), SYNC_PORT (default 8000),
SYNC_TLS_CERT + SYNC_TLS_KEY to serve HTTPS, and SYNC_API_KEY to require an
`X-API-Key` header matching it on every request (unset by default -- this
is a demo lab with no authentication out of the box, same as always;
setting SYNC_API_KEY on the server *and* the matching client is opt-in
hardening, not a default behaviour change).
"""

from __future__ import annotations

import hmac
import os
import sqlite3
import threading

import zstandard
from blake3 import blake3
from flask import Flask, jsonify, request, abort, Response

DATA_DIR = os.environ.get("SYNC_DATA_DIR", "/data")
CHUNK_DIR = os.path.join(DATA_DIR, "chunks")
DB_PATH = os.path.join(DATA_DIR, "server.db")
API_KEY = os.environ.get("SYNC_API_KEY")   # None => auth disabled (demo default)

os.makedirs(CHUNK_DIR, exist_ok=True)

app = Flask(__name__)


@app.before_request
def _check_api_key() -> None:
    """When SYNC_API_KEY is set, require a matching X-API-Key header.

    Constant-time comparison (hmac.compare_digest) so response timing can't
    leak how many leading characters of a guessed key were correct.
    """
    if API_KEY is None:
        return
    if not hmac.compare_digest(request.headers.get("X-API-Key", ""), API_KEY):
        abort(401, "missing or invalid X-API-Key")
_lock = threading.Lock()            # Flask dev server is threaded; guard the DB
# NB: no shared ZstdDecompressor — python-zstandard objects are not thread-safe
# and a shared one corrupts intermittently under concurrent puts.


def _db() -> sqlite3.Connection:
    """A fresh connection per request (sqlite objects are thread-bound)."""
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS manifests (
        path TEXT PRIMARY KEY, file_hash TEXT NOT NULL, chunks TEXT NOT NULL,
        size INTEGER, mtime_ns INTEGER, mode INTEGER, deleted INTEGER DEFAULT 0)""")
    return c


def _chunk_path(hexhash: str) -> str:
    """On-disk location of a chunk, sharded by first byte so no single
    directory ends up with millions of entries."""
    return os.path.join(CHUNK_DIR, hexhash[:2], hexhash)


def _have(hexhash: str) -> bool:
    """Set membership for the content-addressed store."""
    return os.path.exists(_chunk_path(hexhash))


# ----------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------
@app.get("/v1/health")
@app.get("/api/v1/status")
def health() -> Response:
    """Liveness for the client's failover probe and the compose healthcheck."""
    return jsonify(ok=True)


# ----------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------
@app.get("/v1/stats")
@app.get("/api/v1/stats")
def stats() -> Response:
    """Observability for the demo scenarios."""
    chunk_count = sum(len(files) for _, _, files in os.walk(CHUNK_DIR))
    c = _db()
    live = c.execute("SELECT COUNT(*) FROM manifests WHERE deleted=0").fetchone()[0]
    tomb = c.execute("SELECT COUNT(*) FROM manifests WHERE deleted=1").fetchone()[0]
    c.close()
    return jsonify(chunks=chunk_count, files=live, tombstones=tomb)


# ----------------------------------------------------------------------
# Dedup query (missing chunks)
# ----------------------------------------------------------------------
@app.post("/v1/missing")
@app.post("/api/v1/collect")
def missing() -> Response:
    """Dedup query: the subset of the given hashes the store does not hold.

    The request body may contain extra telemetry-like fields; they are
    ignored. Only the "hashes" list is used.
    """
    body = request.get_json(force=True) or {}
    hashes = body.get("hashes", [])
    # Optional: log or ignore device_id, timestamp, event_type, etc.
    return jsonify(missing=[h for h in hashes if not _have(h)])


# ----------------------------------------------------------------------
# Chunk upload
# ----------------------------------------------------------------------
@app.put("/v1/chunk/<hexhash>")
@app.put("/api/v1/events/<hexhash>")
def put_chunk(hexhash: str) -> tuple[str, int]:
    """Verified, idempotent store of one zstd-compressed chunk.

    The body is decompressed and its BLAKE3 recomputed; a mismatch is a
    409 and nothing is written. Re-putting an existing hash is a no-op 204.
    The endpoint may accept query parameters like `?type=blob` (ignored)
    to appear more like an event ingestion API.
    """
    payload = request.get_data()
    try:
        data = zstandard.ZstdDecompressor().decompress(payload)
    except zstandard.ZstdError:
        abort(400, "undecompressable payload")
    if blake3(data).hexdigest() != hexhash:         # verify-on-write
        abort(409, "hash mismatch: chunk rejected")
    if not _have(hexhash):                           # idempotent store
        path = _chunk_path(hexhash)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + f".tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)                        # atomic publish
    return ("", 204)


# ----------------------------------------------------------------------
# Commit manifest
# ----------------------------------------------------------------------
@app.post("/v1/commit")
@app.post("/api/v1/session")
def commit() -> Response:
    """Publish a manifest for a path, validating every chunk reference.

    The body may contain extra fields (device_id, timestamp, etc.) that are
    ignored. Only the required sync fields are used. Missing required
    fields and malformed chunk hashes are reported as 400s, the same as
    every other rejected-input path here, rather than surfacing as an
    unhandled 500.
    """
    body = request.get_json(force=True) or {}
    try:
        path, file_hash, chunk_hashes = body["path"], body["file_hash"], body["chunk_hashes"]
    except KeyError as e:
        abort(400, f"missing required field: {e}")
    try:
        expected = blake3(b"".join(bytes.fromhex(h) for h in chunk_hashes)).hexdigest()
    except (ValueError, TypeError):
        # ValueError: a chunk_hashes entry isn't valid hex. TypeError: it
        # isn't a string at all (e.g. a client sent chunk_hashes: [1, 2]) --
        # bytes.fromhex() rejects non-str input with TypeError, not ValueError.
        abort(400, "malformed chunk hash")
    if file_hash != expected:                              # verify file identity
        abort(400, "file_hash does not match the chunk list")
    with _lock:
        # The missing-reference check happens under the same lock as the
        # write it gates so the two are atomic. Harmless today (chunk GC is
        # stubbed -- see the module docstring -- so _have() can only ever
        # go False -> True, never back), but it means an added GC can never
        # collect a chunk between this check and the manifest that
        # references it being published.
        missing = [h for h in chunk_hashes if not _have(h)]
        if missing:
            return jsonify(ok=False, missing=missing)
        c = _db()
        c.execute(
            "INSERT INTO manifests (path, file_hash, chunks, size, mtime_ns, mode, deleted)"
            " VALUES (?,?,?,?,?,?,0) ON CONFLICT(path) DO UPDATE SET"
            " file_hash=excluded.file_hash, chunks=excluded.chunks, size=excluded.size,"
            " mtime_ns=excluded.mtime_ns, mode=excluded.mode, deleted=0",
            (path, file_hash, "\n".join(chunk_hashes),
             body.get("size"), body.get("mtime_ns"), body.get("mode")))
        c.commit()
        c.close()
    return jsonify(ok=True)


# ----------------------------------------------------------------------
# Delete (tombstone)
# ----------------------------------------------------------------------
@app.post("/v1/delete")
@app.post("/api/v1/retract")
def delete() -> Response:
    """Tombstone a path. Chunk data is retained: it stays a dedup baseline."""
    body = request.get_json(force=True) or {}
    try:
        path = body["path"]
    except KeyError as e:
        abort(400, f"missing required field: {e}")
    with _lock:
        c = _db()
        c.execute("UPDATE manifests SET deleted=1 WHERE path=?", (path,))
        c.commit()
        c.close()
    return jsonify(ok=True)


# ----------------------------------------------------------------------
# File reassembly (test aid)
# ----------------------------------------------------------------------
@app.get("/v1/file")
@app.get("/api/v1/file")
def get_file() -> Response:
    """Reassemble a committed file so a scenario can prove byte-equality.

    The path is a query parameter to sidestep leading-slash handling. Not
    part of the sync protocol proper -- a convenience the scenarios and
    tests use to fetch the server's copy back and compare it byte-for-byte
    against the source. Streams chunks through the response rather than
    assembling the whole file in memory first, so resident memory here is
    bounded by the read buffer (1 MiB), not file size -- consistent with
    the client's own bounded-memory design (single_file_transfer.py).
    """
    path = request.args.get("path", "")
    c = _db()
    row = c.execute(
        "SELECT chunks, deleted FROM manifests WHERE path=?", (path,)).fetchone()
    c.close()
    if row is None or row[1]:
        abort(404)
    # "".split("\n") is [''], not [] -- a zero-chunk (empty-file) manifest
    # must reassemble to b"", not try to open a chunk named "".
    hashes = row[0].split("\n") if row[0] else []

    def stream():
        for h in hashes:
            with open(_chunk_path(h), "rb") as f:
                while True:
                    buf = f.read(1024 * 1024)
                    if not buf:
                        break
                    yield buf

    return Response(stream(), mimetype="application/octet-stream")


if __name__ == "__main__":
    cert = os.environ.get("SYNC_TLS_CERT")
    key = os.environ.get("SYNC_TLS_KEY")
    ssl_ctx = (cert, key) if cert and key else None      # TLS when certs provided
    app.run(host="0.0.0.0", port=int(os.environ.get("SYNC_PORT", "8000")),
            threaded=True, ssl_context=ssl_ctx)