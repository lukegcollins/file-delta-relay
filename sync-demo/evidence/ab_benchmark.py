"""Sample container resource usage while a workload runs, for the A/B comparison.

Why this exists: evidence/make_plots.py draws three branch-comparison figures
(plots 7, 8, 9) from evidence/metrics_<label>.json, and until now nothing in the
repository produced those files -- the numbers behind the first trade-off report
came from an ad hoc shell pipeline that was never committed, so the comparison
could not be reproduced from a clean clone. This module is that missing
collector, written so the A/B is a repeatable command rather than a one-off.

What it measures: `docker stats` sampled continuously while one or more
workload commands run, reduced to a per-container time series (CPU %, memory,
cumulative and per-second network I/O, cumulative block I/O) plus per-container
peaks and totals.

What it does NOT measure, stated up front because the distinction matters when
reading plot 7: block-device READ accounting is frequently unavailable for a
bind-mounted source directory, depending on the host's cgroup version and
storage driver. When every sample reports zero read bytes, that is recorded
honestly in `notes.blkio_read_available` rather than presented as a measurement
of zero disk reads. Network egress is the observable stand-in for the client's
read-then-send cadence, and the plot's own title says so.

Run (from sync-demo/):

    .venv/bin/python evidence/ab_benchmark.py --label lightweight-portable \\
        -- ./scenarios/03_failover_and_blackout.sh ./scenarios/04_stealth_mode.sh

The stack must already be up (`docker compose up --build -d`). Each `--`
argument is one workload command, run in order; the sampler runs across all of
them so the resulting time series is directly comparable between branches as
long as the same command list is used for both.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULT_CONTAINERS = ["sync-client", "sync-server-primary", "sync-server-secondary"]

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# docker reports memory in binary units (MiB) and I/O counters in decimal ones
# (MB), so the two scales have to be kept separate rather than folded into one
# table -- treating "1MB" as 1048576 would silently inflate every network total.
_DECIMAL = {"b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12}
_BINARY = {"b": 1, "kib": 2**10, "mib": 2**20, "gib": 2**30, "tib": 2**40}


def _parse_size(text: str) -> float:
    """Return `text` ("1.2kB", "204.3MiB", "0B") as a count of bytes."""
    text = text.strip()
    m = re.match(r"^([0-9.]+)\s*([A-Za-z]*)$", text)
    if not m:
        return 0.0
    value, unit = float(m.group(1)), m.group(2).lower()
    if unit in _BINARY:
        return value * _BINARY[unit]
    return value * _DECIMAL.get(unit, 1)


def _parse_pair(text: str) -> tuple[float, float]:
    """Return both halves of a docker "A / B" counter, in bytes."""
    left, _, right = text.partition("/")
    return _parse_size(left), _parse_size(right)


class StatsSampler:
    """Stream `docker stats` for a fixed container set on a background thread.

    Uses the streaming form rather than repeated `--no-stream` calls: each
    `--no-stream` invocation pays roughly two seconds of start-up before it can
    compute a CPU delta, which would make the sampling interval coarser than the
    events being measured. The streaming form redraws its table about once a
    second and needs its ANSI cursor control stripped, which `_ANSI` handles.
    """

    def __init__(self, containers: list[str]):
        self.containers = containers
        self.samples: list[dict] = []
        self._t0 = time.time()
        self._proc: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stop = threading.Event()

    def _run(self) -> None:
        # scenarios/03_failover_and_blackout.sh deliberately `docker stop`s the
        # primary mid-run, and docker stats exits when a named container goes
        # away. Restarting the stream keeps the series continuous across the
        # outage instead of silently ending it at the moment the interesting
        # part starts; the gap shows up honestly as a widened sample interval.
        while not self._stop.is_set():
            self._proc = subprocess.Popen(
                ["docker", "stats", "--format", "{{json .}}", *self.containers],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                bufsize=1)
            for line in self._proc.stdout:
                if self._stop.is_set():
                    return
                self._record(_ANSI.sub("", line).strip())
            if self._stop.is_set():
                return
            self._stop.wait(1.0)     # the stream ended on its own; try again

    def _record(self, line: str) -> None:
        """Parse one stats line and append a sample, ignoring anything malformed."""
        if not line.startswith("{"):
            return
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return
        # A stopped container is reported with "--" in every numeric column.
        # Skip those rows rather than recording a zero: a container that is
        # deliberately down has no usage to attribute to it, and a fabricated
        # zero would drag its peak and rate series toward the floor.
        cpu = row.get("CPUPerc", "").rstrip("%")
        try:
            cpu_pct = float(cpu)
        except ValueError:
            return
        rx, tx = _parse_pair(row.get("NetIO", "0B / 0B"))
        rd, wr = _parse_pair(row.get("BlockIO", "0B / 0B"))
        mem, _ = _parse_pair(row.get("MemUsage", "0B / 0B"))
        self.samples.append({
            "t": time.time() - self._t0,
            "container": row.get("Name", "?"),
            "cpu_pct": cpu_pct,
            "mem_bytes": mem,
            "net_rx_bytes": rx,
            "net_tx_bytes": tx,
            "blk_read_bytes": rd,
            "blk_write_bytes": wr,
        })

    def start(self) -> None:
        """Begin sampling."""
        self._thread.start()

    def stop(self) -> None:
        """Stop sampling and reap the docker process."""
        self._stop.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._thread.join(timeout=5)

    def now(self) -> float:
        """Return seconds elapsed since sampling started."""
        return time.time() - self._t0


def _derive(samples: list[dict]) -> None:
    """Add a per-second egress rate to each sample, in place.

    `docker stats` reports NetIO cumulatively since container start, so the rate
    is the first difference over the observed gap between consecutive samples of
    the same container. The first sample of each container has no predecessor
    and is assigned a rate of zero rather than being dropped, so the series
    still starts at the run's true t=0.
    """
    prev: dict[str, dict] = {}
    for s in samples:
        p = prev.get(s["container"])
        if p is None:
            s["net_tx_bytes_per_s"] = 0.0
        else:
            dt = s["t"] - p["t"]
            delta = s["net_tx_bytes"] - p["net_tx_bytes"]
            s["net_tx_bytes_per_s"] = (delta / dt) if dt > 0 and delta >= 0 else 0.0
        prev[s["container"]] = s


_COUNTERS = ("net_tx_bytes", "net_rx_bytes", "blk_read_bytes", "blk_write_bytes")


def _aggregate(samples: list[dict]) -> dict:
    """Return per-container peaks and whole-run totals for each counter.

    docker's I/O counters are cumulative *since the container started*, so a
    `docker start` in the middle of a run (scenario 3 restarts the primary)
    resets them to near zero. Taking the last or largest value would then charge
    the run only the longer of the two segments and quietly lose the other. Each
    counter is therefore carried across resets: when a value drops below its
    predecessor, the predecessor is banked and accumulation continues from the
    new baseline. `restarts` records how many times that happened, so a total
    that spans a restart is visibly labelled as one.
    """
    out: dict[str, dict] = {}
    prev: dict[str, dict[str, float]] = {}
    carry: dict[str, dict[str, float]] = {}
    for s in samples:
        name = s["container"]
        c = out.setdefault(name, {
            "peak_cpu_pct": 0.0, "peak_mem_bytes": 0.0,
            "net_tx_bytes_total": 0.0, "net_rx_bytes_total": 0.0,
            "blk_read_bytes_total": 0.0, "blk_write_bytes_total": 0.0,
            "samples": 0, "restarts": 0,
        })
        p = prev.setdefault(name, {})
        k = carry.setdefault(name, {})
        c["peak_cpu_pct"] = max(c["peak_cpu_pct"], s["cpu_pct"])
        c["peak_mem_bytes"] = max(c["peak_mem_bytes"], s["mem_bytes"])
        reset_here = False
        for field in _COUNTERS:
            value, last = s[field], p.get(field)
            if last is not None and value < last:
                k[field] = k.get(field, 0.0) + last     # counter reset: bank it
                reset_here = True
            c[f"{field}_total"] = k.get(field, 0.0) + value
            p[field] = value
        c["restarts"] += reset_here
        c["samples"] += 1
    return out


def _observed_interval(samples: list[dict]) -> float:
    """Return the median gap between consecutive samples of one container.

    Reported alongside the data so the figures state the cadence actually
    achieved rather than the cadence intended.
    """
    by_container: dict[str, list[float]] = {}
    for s in samples:
        by_container.setdefault(s["container"], []).append(s["t"])
    gaps = [b - a for ts in by_container.values()
            for a, b in zip(sorted(ts), sorted(ts)[1:])]
    return round(statistics.median(gaps), 3) if gaps else 0.0


def _git(*args: str) -> str:
    """Return the output of a git command, or "unknown" if git fails."""
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def main() -> int:
    """Run the workload under measurement and write evidence/metrics_<label>.json."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True,
                    help="branch label; output goes to evidence/metrics_<label>.json")
    ap.add_argument("--containers", default=",".join(DEFAULT_CONTAINERS),
                    help="comma-separated container names to sample")
    ap.add_argument("--settle", type=float, default=3.0,
                    help="seconds of idle sampling before and after the workload")
    ap.add_argument("--log-dir", default=None,
                    help="write each command's combined output to "
                         "<log-dir>/<command basename>.log as well as the console")
    ap.add_argument("commands", nargs="+",
                    help="workload commands, run in order (put them after --)")
    args = ap.parse_args()

    containers = [c.strip() for c in args.containers.split(",") if c.strip()]
    missing = [c for c in containers if subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", c],
        capture_output=True, text=True).stdout.strip() != "true"]
    if missing:
        print(f"not running: {', '.join(missing)} -- bring the stack up first "
              f"(docker compose up --build -d)", file=sys.stderr)
        return 1

    sampler = StatsSampler(containers)
    sampler.start()
    time.sleep(args.settle)

    if args.log_dir:
        os.makedirs(args.log_dir, exist_ok=True)

    workload = []
    failures = 0
    for cmd in args.commands:
        # shlex.split rather than shell=True: the workload is a list of scripts
        # to run, not a shell program, so there is nothing to gain from a shell
        # and no reason to inherit its metacharacter handling.
        argv = shlex.split(cmd)
        print(f"== {cmd} ==", flush=True)
        t_start = sampler.now()
        log_path = None
        if args.log_dir:
            stem = os.path.basename(argv[0]).removesuffix(".sh")
            log_path = os.path.join(args.log_dir, f"{stem}.log")
            with open(log_path, "w") as log:
                rc = subprocess.run(argv, cwd=ROOT, stdout=log,
                                    stderr=subprocess.STDOUT).returncode
            with open(log_path) as log:
                sys.stdout.write(log.read())
        else:
            rc = subprocess.run(argv, cwd=ROOT).returncode
        workload.append({"cmd": cmd, "argv": argv, "t_start": t_start,
                         "t_end": sampler.now(), "returncode": rc,
                         "log": log_path})
        failures += rc != 0
        print(f"   exit code: {rc}", flush=True)

    time.sleep(args.settle)
    sampler.stop()

    samples = sampler.samples
    _derive(samples)
    containers_agg = _aggregate(samples)
    blkio_read_seen = any(s["blk_read_bytes"] > 0 for s in samples)

    out = {
        "label": args.label,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "commit": _git("rev-parse", "HEAD"),
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sample_interval_s_observed_median": _observed_interval(samples),
        "sample_count": len(samples),
        "workload": workload,
        "timeseries": samples,
        "containers": containers_agg,
        "notes": {
            "blkio_read_available": blkio_read_seen,
            "blkio_read_comment": (
                "docker reported non-zero block reads for at least one sample"
                if blkio_read_seen else
                "every sample reported zero block-device reads: blkio read "
                "accounting is unavailable for the bind-mounted source directory "
                "under this host's cgroup/storage-driver setup. Plot 7 therefore "
                "shows network egress rate as a disclosed proxy for the client's "
                "read-then-send cadence, not a disk measurement."),
        },
    }

    out_path = os.path.join(HERE, f"metrics_{args.label}.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"wrote {out_path} "
          f"({len(samples)} samples, median interval "
          f"{out['sample_interval_s_observed_median']}s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
