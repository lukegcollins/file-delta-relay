"""Render the JSON captured by the evidence harnesses into plots/*.png.

evidence/local_metrics.json and evidence/docker_metrics.json give the six
evidence plots FINAL_REPORT.md embeds. evidence/metrics_main.json and
evidence/metrics_portable.json, when present, add three more plots comparing
main against lightweight-portable.

Run:  .venv/bin/python evidence/make_plots.py
"""

from __future__ import annotations

import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLOTS = os.path.join(ROOT, "plots")
os.makedirs(PLOTS, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "client"))

# --- palette (validated categorical/status/chrome set) ---------------------
BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
MAGENTA, GREEN, VIOLET, RED = "#e87ba4", "#008300", "#4a3aa7", "#e34948"
GOOD, WARNING, SERIOUS, CRITICAL = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans", "Arial", "Helvetica"],
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK_2,
    "axes.titlecolor": INK,
    "text.color": INK,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "axes.grid": True,
    "axes.axisbelow": True,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "legend.frameon": False,
    "savefig.facecolor": SURFACE,
})


def load(name):
    """Return the parsed JSON file named by name, or None if it is absent."""
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def savefig(fig, name):
    """Write fig into the plots directory under name and close it."""
    out = os.path.join(PLOTS, name)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {out}")


def _workload_label(metrics) -> str:
    """Describe the workload a comparison run measured, from its own record.

    evidence/ab_benchmark.py stores the exact command list it ran, so the axis
    label names the real workload instead of a hardcoded guess that drifts the
    moment the comparison is re-run over different scenarios.
    """
    cmds = [os.path.basename(w.get("cmd", "")).removesuffix(".sh")
            for w in metrics.get("workload", [])]
    return " + ".join(c for c in cmds if c) or "workload not recorded"


# =============================================================================
# 1. Sync timeline: file events vs. server chunk/tombstone count over time
# =============================================================================
def plot_sync_timeline(local):
    """Plot file events against the server chunk and tombstone counts."""
    events = local["events"]
    stat_events = [e for e in events if "chunks" in e]
    t = [e["t"] for e in stat_events]
    chunks = [e["chunks"] for e in stat_events]
    # forward-fill tombstone count (only recorded once it changes)
    last = 0
    tombs = []
    for e in stat_events:
        if "tombstones" in e:
            last = e["tombstones"]
        tombs.append(last)

    fig, ax = plt.subplots(figsize=(9.5, 5))
    ax.step(t, chunks, where="post", color=BLUE, linewidth=2, label="server chunks")
    ax.step(t, tombs, where="post", color=RED, linewidth=2, label="tombstones")
    ax.scatter(t, chunks, color=BLUE, s=18, zorder=3)
    ax.scatter(t, tombs, color=RED, s=18, zorder=3)

    file_events = [e for e in events if e["type"] in
                  ("create", "edit", "rename", "delete")]
    ymax = max(chunks) * 1.35 + 1
    for e in file_events:
        ax.axvline(e["t"], color=MUTED, linewidth=0.8, linestyle=(0, (2, 2)))
        label = {"create": "create", "edit": "edit", "rename": "rename",
                 "delete": "delete"}[e["type"]]
        ax.annotate(label, (e["t"], ymax), rotation=90, va="top", ha="right",
                    fontsize=8, color=INK_2, annotation_clip=False)

    all_t = t + [e["t"] for e in file_events]
    pad = (max(all_t) - min(all_t)) * 0.06 or 0.05
    ax.set_xlim(min(all_t) - pad, max(all_t) + pad)
    ax.set_ylim(0, ymax)
    ax.set_xlabel("seconds since run start")
    ax.set_ylabel("count")
    ax.set_title("Sync timeline: file events vs. server chunk / tombstone count")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=2, fontsize=9)
    ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    savefig(fig, "01_sync_timeline.png")


# =============================================================================
# 2. Bandwidth efficiency: bytes read (logical) vs. bytes uploaded (wire)
# =============================================================================
def plot_bandwidth(local):
    """Plot logical bytes read against bytes actually sent, per operation."""
    order = ["initial_sync_2_files", "edit_blob_50kb", "rename_no_content_change",
             "no_op_pass", "delete_notes"]
    labels = {"initial_sync_2_files": "initial sync\n(2 files, 3.15 MB)",
              "edit_blob_50kb": "50 KB edit\nto blob.bin",
              "rename_no_content_change": "rename\n(no content change)",
              "no_op_pass": "no-op pass\n(nothing changed)",
              "delete_notes": "delete\n(tombstone only)"}
    by_op = {b["op"]: b for b in local["bandwidth"]}
    rows = [by_op[k] for k in order if k in by_op]

    x = range(len(rows))
    width = 0.36
    read = [r["bytes_read"] / 1024 for r in rows]
    up = [r["bytes_uploaded"] / 1024 for r in rows]

    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.bar([i - width / 2 for i in x], read, width, color=BLUE, label="bytes read (logical size touched)")
    ax.bar([i + width / 2 for i in x], up, width, color=ORANGE, label="bytes uploaded (wire, post-dedup+zlib)")
    for i, r in enumerate(rows):
        ax.annotate(f"{r['bytes_uploaded']/1024:.0f} KB", (i + width / 2, up[i]),
                    xytext=(0, 3), textcoords="offset points", ha="center",
                    fontsize=8, color=INK_2)
    ax.set_xticks(list(x))
    ax.set_xticklabels([labels[r["op"]] for r in rows], fontsize=9)
    ax.set_ylabel("KB")
    ax.set_title("Bandwidth efficiency: logical size vs. bytes actually sent")
    ax.legend(loc="upper right")
    savefig(fig, "02_bandwidth_efficiency.png")


# =============================================================================
# 3. Resume recovery: interrupted transfer, then completion from persisted state
# =============================================================================
def plot_resume(local):
    """Plot the interrupted transfer and its completion from persisted state."""
    r = local["resume"]
    fig, ax = plt.subplots(figsize=(9, 3.2))
    y = 0.5
    height = 0.5
    ax.broken_barh([(r["t_pass1_start"], r["t_drop"] - r["t_pass1_start"])],
                   (y, height), facecolors=CRITICAL,
                   label=f"pass 1: uploading, dropped after {r['drop_after_chunks']} chunks")
    ax.broken_barh([(r["t_drop"], r["t_resume_start"] - r["t_drop"])],
                   (y, height), facecolors=MUTED, alpha=0.4,
                   label="network down (simulated ConnectionError)")
    ax.broken_barh([(r["t_resume_start"], r["t_resume_end"] - r["t_resume_start"])],
                   (y, height), facecolors=GOOD,
                   label="pass 2: resumed from persisted 'chunked' state, completes")

    for i, (label, xt) in enumerate((("drop", r["t_drop"]),
                                     ("reconnect", r["t_resume_start"]),
                                     ("done", r["t_resume_end"]))):
        ax.axvline(xt, color=INK_2, linewidth=0.8, linestyle=(0, (1, 2)))
        stagger = y + height + 0.08 + 0.16 * i
        ax.annotate(label, (xt, stagger), fontsize=8, color=INK_2, ha="center")

    ax.set_ylim(0, 1.6)
    ax.set_yticks([])
    ax.set_xlabel("seconds since run start")
    ax.set_title(f"Resume after interruption ({r['file_size']/1024/1024:.0f} MB file, no re-chunking, no duplicate chunk sent)")
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.55), ncol=1, fontsize=8.5)
    savefig(fig, "03_resume_recovery.png")


# =============================================================================
# 4. Failover sequence: endpoint health over time, sync events overlaid
# =============================================================================
def plot_failover(docker):
    """Plot endpoint health over time with sync events overlaid."""
    f = docker["failover"]
    samples = f["health_samples"]
    events = f["events"]

    fig, ax = plt.subplots(figsize=(9.5, 4))
    lanes = {"primary": 1, "secondary": 0}
    colors_up = {"primary": BLUE, "secondary": AQUA}
    for server, lane in lanes.items():
        pts = [(s["t"], s["up"]) for s in samples if s["server"] == server]
        pts.sort()
        run_start = None
        for i, (t, up) in enumerate(pts):
            if up and run_start is None:
                run_start = t
            end = pts[i + 1][0] if i + 1 < len(pts) else t
            if up:
                ax.broken_barh([(t, max(end - t, 0.4))], (lane - 0.35, 0.7),
                               facecolors=colors_up[server])
            else:
                ax.broken_barh([(t, max(end - t, 0.4))], (lane - 0.35, 0.7),
                               facecolors=CRITICAL)

    for e in events:
        if e["name"] in ("primary_stopped", "primary_started", "primary_health_restored"):
            ax.axvline(e["t"], color=INK, linewidth=1, linestyle=(0, (3, 2)))
            ax.annotate(e["name"].replace("_", " "), (e["t"], 1.9), rotation=90,
                        va="top", ha="right", fontsize=8, color=INK_2)
        if e["name"] == "file_synced":
            ax.scatter([e["t"]], [lanes.get(e.get("server") or "primary", 1) + 0.0],
                       marker="D", s=40, color=INK, zorder=5)
            ax.annotate(f"{e['file']} synced\n({e['server']})", (e["t"], lanes.get(e.get("server") or "primary", 1) - 0.55),
                        fontsize=7.5, ha="center", color=INK_2)

    ax.set_yticks([0, 1])
    ax.set_yticklabels(["secondary", "primary"])
    ax.set_ylim(-0.8, 2.1)
    ax.set_xlabel("seconds since run start")
    ax.set_title("Failover / failback: endpoint health over time, file syncs overlaid")
    ax.legend([plt.Rectangle((0, 0), 1, 1, color=colors_up["primary"]),
              plt.Rectangle((0, 0), 1, 1, color=colors_up["secondary"]),
              plt.Rectangle((0, 0), 1, 1, color=CRITICAL)],
             ["primary healthy", "secondary healthy", "unhealthy"],
             loc="upper left", bbox_to_anchor=(0, -0.12), ncol=3, fontsize=8.5)
    savefig(fig, "04_failover_sequence.png")


# =============================================================================
# 5. Network emulation impact: completion time vs. loss/delay
# =============================================================================
def plot_network_sweep(docker):
    """Plot time to convergence against emulated packet loss and delay."""
    sweep = docker["network_sweep"]
    labels = [f"{r['loss']} loss\n{r['delay']} delay" for r in sweep]
    times = [r["completion_s"] for r in sweep]
    converged = [r["converged"] for r in sweep]

    fig, ax = plt.subplots(figsize=(9, 4.5))
    colors = [GOOD if c else CRITICAL for c in converged]
    bars = ax.bar(range(len(sweep)), times, color=colors)
    for i, (bar, r) in enumerate(zip(bars, sweep)):
        text = f"{r['completion_s']:.1f}s" if r["converged"] else "TIMEOUT"
        ax.annotate(text, (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                    xytext=(0, 3), textcoords="offset points", ha="center",
                    fontsize=9, color=INK)
    ax.set_xticks(range(len(sweep)))
    ax.set_xticklabels(labels, fontsize=8.5)
    ax.set_ylabel("time to convergence (s)")
    ax.set_title(f"Network emulation impact: {sweep[0]['size']//1024} KB file completion time vs. tc/netem loss+delay")
    savefig(fig, "05_network_emulation_impact.png")


# =============================================================================
# 6. Stealth mode traffic: poll-interval distribution + endpoint usage share
# =============================================================================
def plot_stealth(docker):
    """Plot the poll-interval distribution beside the endpoint usage share."""
    import transport
    samples = [transport.random_poll_interval(mean=3.0) for _ in range(5000)]

    share = docker["endpoint_usage_share"]["tally"] if docker else None

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.3))
    ax = axes[0]
    ax.hist(samples, bins=40, color=BLUE, edgecolor=SURFACE, linewidth=0.4)
    ax.axvline(3.0, color=INK, linewidth=1, linestyle=(0, (3, 2)))
    ax.annotate("mean = 3.0s\n(SYNC_INTERVAL)", (3.0, ax.get_ylim()[1] * 0.9),
               fontsize=8.5, color=INK_2)
    ax.set_xlabel("poll interval (s)")
    ax.set_ylabel("count (5000 samples)")
    ax.set_title("Poll interval distribution\n(random_poll_interval, exponential)")

    ax2 = axes[1]
    if share:
        cats = ["primary", "secondary", "unresolved"]
        vals = [share.get(c, 0) for c in cats]
        colors = [BLUE, AQUA, MUTED]
        ax2.bar(cats, vals, color=colors)
        for i, v in enumerate(vals):
            ax2.annotate(str(v), (i, v), xytext=(0, 3), textcoords="offset points",
                        ha="center", fontsize=9, color=INK)
        ax2.set_ylabel(f"files (of {share.get('primary',0)+share.get('secondary',0)+share.get('unresolved',0)})")
        ax2.set_title("Per-file endpoint choice\n(live Docker stack, both servers healthy)")
    else:
        ax2.text(0.5, 0.5, "docker_metrics.json not available", ha="center", va="center",
                 color=MUTED, transform=ax2.transAxes)
        ax2.set_axis_off()

    savefig(fig, "06_stealth_mode_traffic.png")


# =============================================================================
# 7. main vs. lightweight-portable: client network egress rate over time
#
# The honest caveat, restated in the figure itself rather than only here:
# `docker stats` block-read accounting is frequently empty for reads off a
# bind-mounted source directory, depending on the host's cgroup version and
# storage driver. Where that is the case, network egress rate stands in as the
# closest *observed* proxy for the client's read-then-send cadence -- each chunk
# read is immediately followed by a compressed upload, so egress tracks read
# activity in practice. Whether reads were actually observable is read from the
# collector's own `notes.blkio_read_available` flag (evidence/ab_benchmark.py)
# rather than assumed, so the subtitle cannot go stale if a future host does
# expose the counter.
# =============================================================================
def plot_io_comparison(m_main, m_portable):
    """Plot client network egress rate over time for both branches."""
    fig, ax = plt.subplots(figsize=(9.5, 5))
    for metrics, color, label in ((m_main, BLUE, "main"), (m_portable, ORANGE, "lightweight-portable")):
        rows = [r for r in metrics["timeseries"] if r["container"] == "sync-client"]
        rows.sort(key=lambda r: r["t"])
        t = [r["t"] for r in rows]
        v = [r["net_tx_bytes_per_s"] / 1024 for r in rows]
        ax.plot(t, v, color=color, linewidth=1.2, alpha=0.85, label=label)
    blkio = any(m.get("notes", {}).get("blkio_read_available")
                for m in (m_main, m_portable))
    caveat = ("blkio read accounting was available; this is still egress, not disk"
              if blkio else "blkio read accounting was unavailable on this host")
    ax.set_xlabel(f"seconds since run start ({_workload_label(m_portable)})")
    ax.set_ylabel("client net egress, KB/s")
    ax.set_title("Client network egress rate over time\n"
                 f"(proxy for read-then-send cadence; {caveat})")
    ax.legend(loc="upper right")
    savefig(fig, "07_io_velocity_comparison.png")


# =============================================================================
# 8. main vs. lightweight-portable: peak CPU utilization per container
# =============================================================================
def plot_cpu_comparison(m_main, m_portable):
    """Plot peak CPU utilization per container for both branches."""
    containers = ["sync-client", "sync-server-primary", "sync-server-secondary"]
    x = range(len(containers))
    width = 0.36
    main_vals = [m_main["containers"].get(c, {}).get("peak_cpu_pct", 0) for c in containers]
    portable_vals = [m_portable["containers"].get(c, {}).get("peak_cpu_pct", 0) for c in containers]

    fig, ax = plt.subplots(figsize=(8.5, 4.5))
    ax.bar([i - width / 2 for i in x], main_vals, width, color=BLUE, label="main")
    ax.bar([i + width / 2 for i in x], portable_vals, width, color=ORANGE, label="lightweight-portable")
    ax.set_xticks(list(x))
    ax.set_xticklabels(containers, fontsize=9)
    ax.set_ylabel("peak CPU %  (docker stats, single sample point)")
    ax.set_title("Peak CPU utilization per container: main vs. lightweight-portable")
    ax.legend(loc="upper right")
    savefig(fig, "08_cpu_comparison.png")


# =============================================================================
# 9. main vs. lightweight-portable: total bytes transferred over the wire
# =============================================================================
def plot_bandwidth_comparison(m_main, m_portable):
    """Plot total bytes the client sent over the wire for both branches."""
    fig, ax = plt.subplots(figsize=(8, 4.5))
    labels = ["main\n(content-defined chunking)", "lightweight-portable\n(fixed 256 KiB chunking)"]
    vals = [m_main["containers"]["sync-client"]["net_tx_bytes_total"] / 1024 / 1024,
            m_portable["containers"]["sync-client"]["net_tx_bytes_total"] / 1024 / 1024]
    bars = ax.bar(labels, vals, color=[BLUE, ORANGE])
    for bar, v in zip(bars, vals):
        ax.annotate(f"{v:.1f} MB", (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                    xytext=(0, 3), textcoords="offset points", ha="center", fontsize=9, color=INK)
    ax.set_ylabel(f"client egress, MB ({_workload_label(m_portable)})")
    ax.set_title("Total bytes sent by the client\n(includes retry/resend overhead -- not a clean dedup-ratio comparison)")
    savefig(fig, "09_bandwidth_comparison.png")


def main():
    """Render every plot whose input JSON is present."""
    local = load("local_metrics.json")
    docker = load("docker_metrics.json")
    if local is None:
        print("evidence/local_metrics.json missing -- run evidence/local_harness.py first")
        sys.exit(1)

    plot_sync_timeline(local)
    plot_bandwidth(local)
    plot_resume(local)

    if docker is None:
        print("evidence/docker_metrics.json missing -- skipping plots 4/5, "
              "and endpoint-share half of plot 6 (run evidence/run_full_evidence.sh)")
    else:
        plot_failover(docker)
        plot_network_sweep(docker)
    plot_stealth(docker)

    # evidence/ab_benchmark.py names its output after the branch it measured
    # (metrics_<branch>.json), so the comparison inputs are metrics_main.json and
    # metrics_lightweight-portable.json. metrics_portable.json is accepted as a
    # fallback for the older, hand-collected naming.
    metrics_main = load("metrics_main.json")
    metrics_portable = (load("metrics_lightweight-portable.json")
                        or load("metrics_portable.json"))
    if metrics_main is None or metrics_portable is None:
        print("evidence/metrics_main.json / metrics_lightweight-portable.json "
              "missing -- skipping plots 7/8/9. Produce them with "
              "evidence/ab_benchmark.py --label <branch> -- <scenarios...> "
              "against a running stack, once per branch.")
    else:
        plot_io_comparison(metrics_main, metrics_portable)
        plot_cpu_comparison(metrics_main, metrics_portable)
        plot_bandwidth_comparison(metrics_main, metrics_portable)


if __name__ == "__main__":
    main()
