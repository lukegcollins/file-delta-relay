"""HTTP transport and run loop: the `srv` the two library modules expect.

Why this component: change_detection.py and single_file_transfer.py are
written against a four-call server contract (get_missing_chunks,
put_chunk, commit_file, delete_file) and say nothing about how bytes
reach a server. This module is that "how", and the runner that drives
the pipeline:

  * HttpServer implements the contract over HTTP against one or more
    server URLs in priority order, using paths that resemble common
    telemetry/analytics APIs (e.g. /api/v1/collect, /api/v1/events/<id>).
    A request goes to the first endpoint believed healthy; a connection
    error, timeout or 5xx marks the endpoint failed and the request moves
    to the next one. After `failure_threshold` consecutive failures an
    endpoint is skipped until an active health check (GET /api/v1/status)
    says it is back, and a downed higher-priority endpoint is re-probed
    every `health_check_interval` seconds (on demand in _pick, and from
    the run loop via refresh() while idle) so the client fails *back* to
    it. Failover is about availability only: the servers are independent
    stores and do not replicate to each other, so a file committed to the
    secondary during an outage lives on the secondary.
  * run_once is one reconciliation pass: scan the roots, tombstone
    deletions, push everything else through sync_file.

Transient failure semantics are deliberately layered: HttpServer moves
between endpoints with no delay, single_file_transfer._retry backs off
per chunk, and the run loop below treats a pass that still fails as
"try again next interval" with state intact. Run directly, the module
reads its configuration from SYNC_* environment variables (see
__main__) and loops forever - or once, if SYNC_ONCE=true.

The run loop polls with a randomized exponential delay (`SYNC_INTERVAL`
is the mean) to avoid a predictable timer, making the traffic pattern
less regular and more like organic API activity. Each file sync is
assigned to a randomly chosen healthy server, spreading traffic across
endpoints while preserving per-file consistency.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time

import requests

from change_detection import Reason, StateDB, scan
from single_file_transfer import ChunkRejected, CommitResult, sync_file

log = logging.getLogger("sync.client")


class ServerEndpoint:
    """Track one configured server URL and what the transport has learned about it.

    Starts healthy. mark_failure counts consecutive failures and flips
    `healthy` off at the threshold; mark_success resets both. `last_probe`
    is when an active health check last ran (time.monotonic), used to
    rate-limit re-probing of a downed endpoint.
    """

    def __init__(self, url: str, failure_threshold: int):
        self.url = url.rstrip("/")
        self.healthy = True
        self.failures = 0
        self.last_probe = 0.0
        self._threshold = failure_threshold
        self._lock = threading.Lock()

    def mark_success(self) -> None:
        """Clear the consecutive-failure count and mark the endpoint healthy."""
        with self._lock:
            if not self.healthy:
                log.info("endpoint %s healthy again", self.url)
            self.healthy = True
            self.failures = 0

    def mark_failure(self) -> None:
        """Count one more consecutive failure, marking unhealthy at the threshold."""
        with self._lock:
            self.failures += 1
            if self.healthy and self.failures >= self._threshold:
                self.healthy = False
                log.warning("endpoint %s marked unhealthy after %d consecutive"
                            " failures; failing over", self.url, self.failures)

    def claim_probe(self, interval: float) -> bool:
        """Atomically decide whether the caller should reprobe this endpoint.

        Checking `last_probe` and updating it are done as one step under
        `_lock` so that two threads racing to reprobe the same downed
        endpoint (see HttpServer._pick/refresh, called concurrently by
        single_file_transfer's upload worker pool) can't both see the same
        stale timestamp and both decide a reprobe is due -- a thundering
        herd of simultaneous health checks against one endpoint instead of
        the single one `interval` promises. Only the caller that wins the
        claim should actually issue the probe.
        """
        with self._lock:
            if time.monotonic() - self.last_probe < interval:
                return False
            self.last_probe = time.monotonic()
            return True


class HttpServer:
    """Implement the `srv` contract over HTTP, with failover across endpoints.

    Usage:
        srv = HttpServer("http://server:8000")                      # one
        srv = HttpServer(["http://primary:8000", "http://second:8001"])

    `verify` is passed straight to requests (True, False, or a CA bundle
    path). `timeout` is requests' (connect, read) pair: a short connect
    timeout detects a dead server quickly, while the read timeout only
    has to bound a single 1 MiB chunk on a slow link - a put that is
    merely slow must not be aborted and re-sent from scratch. `api_key`,
    if given, is sent as `X-API-Key` on every request -- matches the
    server's optional SYNC_API_KEY check (server/app.py); omitted, the
    server must have no key configured either, or every request 401s.
    Safe to call from several threads at once - single_file_transfer
    uploads with a worker pool.
    """

    PROBE_TIMEOUT = 5.0

    def __init__(
        self,
        base_urls: str | list[str],
        *,
        verify: bool | str = True,
        timeout: float | tuple[float, float] = (5.0, 30.0),
        health_check_interval: float = 10.0,
        failure_threshold: int = 3,
        api_key: str | None = None,
    ):
        if isinstance(base_urls, str):
            base_urls = [base_urls]
        if not base_urls:
            raise ValueError("HttpServer needs at least one server URL")
        self.endpoints = [ServerEndpoint(u, failure_threshold) for u in base_urls]
        self.timeout = timeout
        self.health_check_interval = health_check_interval
        self._verify = verify
        self._api_key = api_key
        # One requests.Session per thread rather than one shared instance.
        # urllib3's connection pool (what actually matters for concurrent
        # reuse) is thread-safe on its own, and this server never sets
        # cookies, so a single shared Session would not corrupt anything in
        # practice here -- verified with a dedicated concurrency stress test
        # (see FINAL_REPORT.md). Thread-local sessions cost nothing (each
        # worker still gets its own pooled connections to the same couple of
        # hosts) and remove the question entirely for any future server that
        # does set cookies or other session-scoped state.
        self._local = threading.local()
        # Dummy device identifier for telemetry-like request bodies
        self._device_id = os.urandom(8).hex()
        # Optional forced endpoint index (None = automatic failover)
        self.preferred_endpoint: int | None = None

    @property
    def _session(self) -> requests.Session:
        """Return this thread's Session, created and configured on first use."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.verify = self._verify
            if self._api_key:
                s.headers["X-API-Key"] = self._api_key
            self._local.session = s
        return s

    def set_preferred_endpoint(self, index: int | None) -> None:
        """Force the next requests to a specific endpoint index.

        Passing None restores normal failover.
        """
        self.preferred_endpoint = index

    # --- endpoint selection ------------------------------------------------

    def _probe(self, ep: ServerEndpoint) -> bool:
        """Run an active health check: GET /api/v1/status, capped at 5 s."""
        ep.last_probe = time.monotonic()
        try:
            r = self._session.get(f"{ep.url}/api/v1/status", timeout=self.PROBE_TIMEOUT)
            if r.status_code == 200:
                ep.mark_success()
                return True
        except requests.RequestException:
            pass
        ep.mark_failure()
        return False

    def _pick(self, exclude: list[ServerEndpoint]) -> ServerEndpoint | None:
        """Return the endpoint the next request should go to, or None if none will do.

        Priority order is configured order. A healthy endpoint wins; a
        downed one ahead of it is re-probed first if its interval has
        elapsed, which is what moves traffic back to the primary once it
        recovers. If nothing is healthy, probe every candidate now - with
        no server to talk to there is nothing to gain from waiting (this
        last resort is deliberately not interval-gated, unlike the reprobe
        above: rate-limiting it would only delay recovery when every
        endpoint is already known down).
        """
        probed: set[ServerEndpoint] = set()

        # If a specific endpoint is preferred and available, use it.
        if self.preferred_endpoint is not None:
            ep = self.endpoints[self.preferred_endpoint]
            if ep not in exclude and ep.healthy:
                return ep
            # else fall through to normal selection

        for ep in self.endpoints:
            if ep in exclude:
                continue
            if ep.healthy:
                return ep
            if ep.claim_probe(self.health_check_interval):
                probed.add(ep)
                if self._probe(ep):
                    return ep
        for ep in self.endpoints:
            if ep not in exclude and ep not in probed and self._probe(ep):
                return ep
        return None

    def refresh(self) -> None:
        """Re-probe any downed endpoint whose interval has elapsed.

        _pick only probes on demand, so an idle client would never notice
        a recovered primary; the run loop calls this once per pass so
        failback does not wait for the next piece of work.
        """
        for ep in self.endpoints:
            if not ep.healthy and ep.claim_probe(self.health_check_interval):
                self._probe(ep)

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Issue one request, moving across endpoints until one serves it.

        Connection errors, timeouts and 5xx responses count against the
        endpoint and move on to the next; each endpoint is tried at most
        once per call. 4xx responses are returned to the caller unchanged
        - another server would give the same answer (409 on a bad chunk
        is a protocol signal, not a transport failure).

        Raises ConnectionError (the builtin, which single_file_transfer
        treats as retryable) when every endpoint has failed.
        """
        tried: list[ServerEndpoint] = []
        last: Exception | None = None
        while len(tried) < len(self.endpoints):
            ep = self._pick(tried)
            if ep is None:
                break
            tried.append(ep)
            try:
                r = self._session.request(method, ep.url + path,
                                          timeout=self.timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                ep.mark_failure()
                continue
            if r.status_code >= 500:
                last = requests.HTTPError(f"{r.status_code} from {ep.url}")
                ep.mark_failure()
                continue
            ep.mark_success()
            return r
        raise ConnectionError(
            f"no server could serve {method} {path}: "
            f"{last if last else 'no healthy endpoint'}")

    # --- the srv contract (mimicked API paths) ----------------------------

    def get_missing_chunks(self, hashes: list[bytes]) -> list[bytes]:
        """Query /api/v1/collect, in one batch, for the hashes the server lacks."""
        r = self._request("POST", "/api/v1/collect", json={
            "hashes": [h.hex() for h in hashes],
            "device_id": self._device_id,
            "timestamp": int(time.time() * 1000),
            "event_type": "collect",
        })
        r.raise_for_status()
        return [bytes.fromhex(h) for h in r.json()["missing"]]

    def put_chunk(self, chunk_hash: bytes, payload: bytes) -> None:
        """Store one zstd-compressed chunk at its hash via /api/v1/events/<id>.

        The server recomputes the hash; a mismatch comes back as 409 and
        is raised as ChunkRejected so the caller abandons its manifest.
        """
        r = self._request("PUT", f"/api/v1/events/{chunk_hash.hex()}?type=blob",
                          data=payload,
                          headers={"Content-Type": "application/octet-stream"})
        if r.status_code == 409:
            raise ChunkRejected(chunk_hash.hex())
        r.raise_for_status()

    def commit_file(self, *, path: str, file_hash: bytes, chunk_hashes: list[bytes],
                    size: int, mtime_ns: int, mode: int) -> CommitResult:
        """Publish a manifest via /api/v1/session."""
        r = self._request("POST", "/api/v1/session", json={
            "path": path,
            "file_hash": file_hash.hex(),
            "chunk_hashes": [h.hex() for h in chunk_hashes],
            "size": size, "mtime_ns": mtime_ns, "mode": mode,
            "device_id": self._device_id,
            "timestamp": int(time.time() * 1000),
            "event_type": "session",
        })
        r.raise_for_status()
        body = r.json()
        return CommitResult(body["ok"],
                            [bytes.fromhex(h) for h in body.get("missing", [])])

    def delete_file(self, path: str) -> None:
        """Tombstone a path via /api/v1/retract."""
        r = self._request("POST", "/api/v1/retract", json={
            "path": path,
            "device_id": self._device_id,
            "timestamp": int(time.time() * 1000),
            "event_type": "retract",
        })
        r.raise_for_status()

    def wait_healthy(self, tries: int = 30, gap: float = 0.5) -> None:
        """Block until some endpoint answers /api/v1/status (startup ordering)."""
        for _ in range(tries):
            if any(self._probe(ep) for ep in self.endpoints):
                return
            time.sleep(gap)
        raise RuntimeError("no server became healthy")


def random_poll_interval(mean: float = 3.0) -> float:
    """Return a random delay with an exponential distribution.

    The mean is the configured SYNC_INTERVAL. Exponential distribution
    produces mostly short delays with occasional longer ones, mimicking
    irregular polling rather than a fixed timer.
    """
    return random.expovariate(1.0 / mean)


def run_once(db: StateDB, srv: HttpServer, roots: list[str]) -> dict[str, int]:
    """Run one reconciliation pass and return {"synced": n, "deleted": m}.

    A ConnectionError from the transport aborts the pass part-way: every
    endpoint has failed, so there is nothing to gain from trying the
    remaining files against a link that is entirely down -- state for the
    file in flight stays 'chunked' in `db`, and the next pass resumes it
    rather than starting over. Any other exception is isolated to the one
    path that raised it (logged, then the pass continues): a problem
    specific to a single file -- for example an as-yet-unforeseen local
    edge case -- must not take down sync for every other file, let alone
    the daemon itself.
    """
    synced = deleted = 0
    for change in scan(db, roots):
        try:
            if change.reason is Reason.DELETED:
                srv.delete_file(change.path)
                db.forget(change.path)
                deleted += 1
            else:
                # Distribute file transfers across healthy endpoints.
                healthy_indices = [i for i, ep in enumerate(srv.endpoints) if ep.healthy]
                if healthy_indices:
                    srv.set_preferred_endpoint(random.choice(healthy_indices))
                try:
                    sync_file(db, srv, change.path)
                    synced += 1
                finally:
                    srv.set_preferred_endpoint(None)
        except ConnectionError:
            raise
        except Exception:
            log.exception("run_once: %s failed for %s; continuing with the rest of this pass",
                          change.reason.name, change.path)
    return {"synced": synced, "deleted": deleted}


if __name__ == "__main__":
    # Configuration, all via environment (see docker-compose.yml):
    #   SYNC_SERVERS      comma-separated server URLs, priority order
    #   SYNC_ROOTS        colon-separated directories to sync
    #   SYNC_STATE_DB     path of the client's SQLite state file
    #   SYNC_INTERVAL     mean seconds between passes (default 3)
    #   SYNC_HEALTH_CHECK_INTERVAL  seconds between re-probes of a downed
    #                     endpoint (default 10)
    #   SYNC_CA_BUNDLE    CA file to trust for TLS; else SYNC_VERIFY_TLS
    #   SYNC_API_KEY      sent as X-API-Key; must match the server(s)'
    #                     SYNC_API_KEY, or be unset on both (demo default)
    #   SYNC_ONCE=true    run a single pass and exit
    logging.basicConfig(level=logging.INFO, format="[client] %(message)s")
    roots = os.environ.get("SYNC_ROOTS", "/data/sync").split(":")
    db = StateDB(os.environ.get("SYNC_STATE_DB", "/state/client.db"))
    verify = os.environ.get("SYNC_CA_BUNDLE") or (
        os.environ.get("SYNC_VERIFY_TLS", "true").lower() == "true")
    servers = [s.strip() for s in
               os.environ.get("SYNC_SERVERS", "http://server:8000").split(",")
               if s.strip()]
    srv = HttpServer(
        servers, verify=verify,
        health_check_interval=float(os.environ.get("SYNC_HEALTH_CHECK_INTERVAL", "10")),
        api_key=os.environ.get("SYNC_API_KEY"))
    srv.wait_healthy()
    log.info("serving %s -> %s", roots, servers)

    once = os.environ.get("SYNC_ONCE", "false").lower() == "true"
    interval_mean = float(os.environ.get("SYNC_INTERVAL", "3"))
    while True:
        srv.refresh()                   # notice a recovered primary even when idle
        try:
            tally = run_once(db, srv, roots)
            if tally["synced"] or tally["deleted"]:
                log.info("synced=%d deleted=%d", tally["synced"], tally["deleted"])
        except ConnectionError as e:
            log.warning("pass aborted by network error, retrying next pass: %s", e)
        if once:
            break
        # Randomized exponential delay: mean = interval_mean
        delay = random_poll_interval(interval_mean)
        time.sleep(delay)