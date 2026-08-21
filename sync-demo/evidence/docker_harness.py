"""Instrumented evidence collection against the live Docker stack.

Assumes `docker compose up --build -d` is already running (see
evidence/run_full_evidence.sh, which brings the stack up, runs the official
scenario scripts unmodified for the pass/fail record, and then calls this
module). Produces evidence/docker_metrics.json for the plot generator
(evidence/make_plots.py): endpoint-usage share, the failover/failback health
timeline, and a loss/delay -> completion-time sweep.

Run:  .venv/bin/python evidence/docker_harness.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "utils"))

import requests                                    # noqa: E402
from network_conditions import NetworkConditionManager   # noqa: E402

PRIMARY = os.environ.get("PRIMARY", "https://localhost:8000")
SECONDARY = os.environ.get("SECONDARY", "https://localhost:8001")
CA = os.path.join(ROOT, "certs", "ca.crt")
SYNC_ROOT = os.path.join(ROOT, "sync-root")
PRIMARY_CONTAINER = "sync-server-primary"
CLIENT_CONTAINER = "sync-client"

os.makedirs(SYNC_ROOT, exist_ok=True)


def health(url: str) -> bool:
    try:
        r = requests.get(f"{url}/v1/health", timeout=2, verify=CA)
        return r.status_code == 200
    except requests.RequestException:
        return False


def has_file(url: str, local_path: str) -> bool:
    name = os.path.basename(local_path)
    try:
        r = requests.get(f"{url}/v1/file", params={"path": f"/data/sync/{name}"},
                         verify=CA, timeout=5)
    except requests.RequestException:
        return False
    if not r.ok:
        return False
    with open(local_path, "rb") as f:
        return r.content == f.read()


def which_server(local_path: str) -> str | None:
    if has_file(PRIMARY, local_path):
        return "primary"
    if has_file(SECONDARY, local_path):
        return "secondary"
    return None


def wait_for(local_path: str, timeout: float) -> tuple[str | None, float]:
    """Poll until the file lands on either server; return (server, elapsed) or (None, timeout)."""
    start = time.time()
    deadline = start + timeout
    while time.time() < deadline:
        srv = which_server(local_path)
        if srv:
            return srv, time.time() - start
        time.sleep(1)
    return None, timeout


def make_file(name: str, size: int) -> str:
    path = os.path.join(SYNC_ROOT, name)
    with open(path, "wb") as f:
        f.write(os.urandom(size))
    return path


class HealthPoller:
    """Background thread sampling /v1/health on both servers at a fixed rate."""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.time() - self._t0
            self.samples.append({"t": now, "server": "primary", "up": health(PRIMARY)})
            self.samples.append({"t": now, "server": "secondary", "up": health(SECONDARY)})
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def now(self) -> float:
        return time.time() - self._t0


def endpoint_usage_share(n: int = 20, size: int = 4096) -> dict:
    """Sync N small files while both servers are healthy; tally which store each lands on.

    Exercises the real client container's random per-file endpoint choice
    (transport.run_once's random.choice(healthy_indices)), not a simulation.
    """
    paths = [make_file(f"share_{i:02d}.bin", size) for i in range(n)]
    tally = {"primary": 0, "secondary": 0, "unresolved": 0}
    for p in paths:
        srv, _elapsed = wait_for(p, timeout=30)
        tally[srv or "unresolved"] += 1
    return {"n": n, "tally": tally}


def failover_sequence() -> dict:
    """Stop the primary mid-run, confirm the client keeps syncing via the
    secondary, restart the primary, confirm new work returns to it -- while a
    background thread samples both servers' health for the timeline plot."""
    poller = HealthPoller()
    poller.start()
    events: list[dict] = []

    def event(name: str, **fields) -> None:
        events.append({"t": poller.now(), "name": name, **fields})

    baseline = make_file("failover_baseline.bin", 512 * 1024)
    event("file_created", file="baseline")
    srv, elapsed = wait_for(baseline, 60)
    event("file_synced", file="baseline", server=srv, elapsed=elapsed)

    subprocess.run(["docker", "stop", PRIMARY_CONTAINER], check=True,
                   capture_output=True)
    event("primary_stopped")

    failover_file = make_file("failover_during_outage.bin", 512 * 1024)
    event("file_created", file="failover")
    srv, elapsed = wait_for(failover_file, 90)
    event("file_synced", file="failover", server=srv, elapsed=elapsed)

    subprocess.run(["docker", "start", PRIMARY_CONTAINER], check=True,
                   capture_output=True)
    event("primary_started")

    deadline = time.time() + 60
    while time.time() < deadline and not health(PRIMARY):
        time.sleep(1)
    event("primary_health_restored", up=health(PRIMARY))

    failback_file = make_file("failback_after_recovery.bin", 512 * 1024)
    event("file_created", file="failback")
    srv, elapsed = wait_for(failback_file, 90)
    event("file_synced", file="failback", server=srv, elapsed=elapsed)

    time.sleep(2)  # a couple more health samples after the last event
    poller.stop()
    return {"health_samples": poller.samples, "events": events}


def network_sweep() -> list[dict]:
    """completion time vs. netem loss/delay, on a fixed-size file each time."""
    net = NetworkConditionManager(CLIENT_CONTAINER)
    configs = [
        {"loss": "0%", "delay": "0ms"},
        {"loss": "10%", "delay": "50ms"},
        {"loss": "10%", "delay": "100ms"},
        {"loss": "30%", "delay": "100ms"},
        {"loss": "20%", "delay": "200ms"},
    ]
    size = 512 * 1024
    results = []
    for i, cfg in enumerate(configs):
        net.clear()
        time.sleep(1)
        if cfg["loss"] != "0%" or cfg["delay"] != "0ms":
            net.apply(delay=cfg["delay"], loss=cfg["loss"])
        path = make_file(f"sweep_{i}_{cfg['loss']}_{cfg['delay']}.bin", size)
        _srv, elapsed = wait_for(path, timeout=90)
        converged = elapsed < 90
        results.append({**cfg, "size": size, "completion_s": elapsed,
                        "converged": converged})
        net.clear()
        time.sleep(2)
    return results


def main() -> None:
    print("== endpoint usage share (20 files, both servers healthy) ==")
    share = endpoint_usage_share()
    print(f"   {share}")

    print("== failover / failback sequence ==")
    failover = failover_sequence()
    print(f"   {len(failover['events'])} events, {len(failover['health_samples'])} health samples")

    print("== network emulation sweep ==")
    sweep = network_sweep()
    for r in sweep:
        print(f"   loss={r['loss']:>4} delay={r['delay']:>5} "
              f"-> {'%.1fs' % r['completion_s'] if r['converged'] else 'TIMEOUT'}")

    out = {
        "endpoint_usage_share": share,
        "failover": failover,
        "network_sweep": sweep,
    }
    out_path = os.path.join(HERE, "docker_metrics.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
