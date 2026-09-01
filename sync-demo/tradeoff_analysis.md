# `main` vs. `lightweight-portable`: measured trade-off analysis

Both branches implement the same client/server protocol and pass the same
suites. They differ in what they are willing to depend on, and this document
measures what that costs and what it buys.

Every number below comes from one clean run per branch, executed sequentially on
the same host, from the same commit of the scenario scripts, with a full teardown
between. Nothing is carried forward from an earlier comparison. The raw results
are in [`../evidence/main/`](../evidence/main/) and
[`../evidence/lightweight-portable/`](../evidence/lightweight-portable/), each
with a `PROVENANCE` file naming the exact commit that produced it.

## Executive summary

`main` is the throughput-optimised branch: content-defined chunking (`fastcdc`),
BLAKE3 hashing, zstd compression, and 8 static upload workers. It requires three
compiled wheels per side.

`lightweight-portable` re-implements the same contract using only the standard
library (`hashlib.blake2b`, `zlib`, fixed 256 KiB chunking) plus an
AIMD-controlled upload-concurrency window in place of a static worker count. It
requires none.

The headline results, each measured rather than asserted:

- **The dedup cost of fixed-size chunking is real, large, and confined to one
  edit shape.** On an insertion that shifts the whole file, `lightweight-portable`
  re-sends **100 %** of the file where `main` re-sends **3 %**. On overwrites and
  appends the two are equivalent - and on the specific overwrite tested,
  `lightweight-portable` was the better of the two.
- **Under packet loss, `lightweight-portable` was the more robust branch.** It
  converged at 30 % loss / 100 ms delay where `main` timed out, and was faster at
  10 % / 100 ms. `main` was faster on a clean or lightly-degraded link.
- **Resource profile differs as designed:** `lightweight-portable` trades CPU for
  memory - 87 % vs 56 % peak client CPU, but 30 MiB vs 52 MiB peak client memory.

## Methodology, and what this comparison does *not* control

Both branches ran through byte-identical `scenarios/03_failover_and_blackout.sh`
code, including the convergence-timing instrumentation, which is now committed on
both branches rather than hand-patched into a worktree for one run. Both ran with
the nginx `client_max_body_size` fix. Both were measured by the same committed
collector (`evidence/ab_benchmark.py`), sampling `docker stats` at a median
0.5 s interval across the same workload.

This is still not a single-variable experiment, and two limits matter when
reading the numbers:

1. **Network results are single runs of a stochastic process.** `tc`/`netem`
   loss is random. The convergence and timeout results below are directionally
   consistent with each branch's design, but a single run bounds nothing about
   run-to-run variance. Where a result is a single sample, it says so.
2. **Two independent implementations differ in more than one place** by
   necessity. The table below is every remaining difference and why it exists.

| File | Why it differs |
|---|---|
| `client/requirements.txt`, `server/requirements.txt` | The entire point of the branch: zero compiled dependencies. |
| `server/app.py` | Must verify with the same hash and compression the client uses (`blake2b`/`zlib` vs `blake3`/`zstandard`). A hard correctness requirement, not a tunable - if these did not match, every sync would fail. |
| `client/single_file_transfer.py` | The chunking/hashing/compression swap, the fixed 256 KiB chunk size, and the AIMD upload-concurrency controller. |
| `client/change_detection.py` | XDG-compliant state DB default path. |
| `client/transport.py` | `failure_threshold` 3→5, read timeout 30 s→45 s, and the AIMD wiring. **Load-bearing, not incidental** - see "Resilience" below. |
| `client/Dockerfile`, `server/Dockerfile` | Comment-only. Confirmed by diff. |
| `nginx/nginx.conf` | Comment differences only; both raise `client_max_body_size`. |
| `tests/test_integration_http.py` | Chunk-count assertions differ because the chunkers do (see "Bandwidth"). |

## 1. Dependency and footprint comparison

| | `main` | `lightweight-portable` |
|---|---|---|
| Client third-party deps | `requests`, `fastcdc`, `blake3`, `zstandard` | `requests` |
| Server third-party deps | `flask`, `blake3`, `zstandard` | `flask` |
| Compiled/binary wheels | Yes - `blake3` and `zstandard` are C/Rust extensions; `fastcdc` needs a native rolling-hash backend | **None** |
| Chunking | Content-defined (`fastcdc`), 64 KiB–1 MiB, ~256 KiB average | Fixed 256 KiB |
| Hash | BLAKE3 | `hashlib.blake2b`, 256-bit |
| Compression | zstd | `zlib` level 1 |
| Upload concurrency | Static, 8 workers | AIMD-adaptive window, 1–8, starting at 2 |
| Client state DB default | Caller-supplied only | XDG: `$XDG_CACHE_HOME/system_sync/state.db` |
| HTTP `User-Agent` | `python-requests/X.Y` | `file-delta-relay-client/1.0` |
| Docker base image | Needs a manylinux-compatible base | Any Python 3.12 base; no native compilation |

Verified rather than assumed: each branch's virtualenv was rebuilt from its own
declared requirements before its run. `lightweight-portable`'s contains no
`fastcdc`, `blake3` or `zstandard`, and the client still imports and runs.

## 2. The chunking trade-off, measured

This is the central design difference, and the scenario suite does **not**
settle it - every file the scenarios write is created or overwritten whole, a
workload under which both strategies perform about the same and whichever one
happens to place a boundary more conveniently wins by luck.

`evidence/chunking_shift_test.py` measures the thing that actually differs. It
takes an 8 MiB file, applies three edit shapes, and counts how many chunk hashes
the server would not already hold - that is, how many chunks must cross the wire.
No server, no Docker, no network; this is a property of the chunker alone, and it
runs in seconds on either branch.

| Edit (50 KiB, on an 8 MiB file) | `main` (content-defined) | `lightweight-portable` (fixed 256 KiB) |
|---|---|---|
| **Overwrite** in place | 3 chunks - **8.8 %** | 1 chunk - **3.1 %** |
| **Append** at EOF | 1 chunk - **3.0 %** | 1 chunk - **3.0 %** |
| **Insert** at the front (shifts the file) | 1 chunk - **3.0 %** | 33 chunks - **100 %** |

Three things follow, and the second is the one that is easy to get backwards:

**The insert case is a 33× difference, and it is the real cost.** Inserting bytes
shifts every following byte, so every fixed-size boundary moves with the data and
every downstream chunk gets a new hash. Content-defined boundaries follow the
content, re-synchronise a chunk or two after the edit, and leave the rest
matching. For insert-heavy workloads - version-controlled prose, uncompressed
media edited in place, anything where bytes shift rather than overwrite - `main`
is unambiguously the correct branch, and installing the compiled wheel is the
right answer.

**On overwrites, fixed-size chunking was the *better* of the two here**, and that
is not a point in its favour so much as a caution about reading single
measurements. `main`'s content-defined boundaries happened to place the 50 KiB
edit across three chunks; the fixed boundaries happened to contain it in one.
Move the edit offset and the result moves with it. Over many edits the two
converge; there is no systematic overwrite advantage in either direction.

**Appends are identical**, as expected: existing chunks keep their offsets, so
only the final partial chunk changes under either strategy.

The same effect shows up in the scenario suite's own numbers. In
`local_harness.py`'s 50 KB edit to a 3 MB file, `lightweight-portable` put
**262,230 bytes** on the wire (one 256 KiB chunk) against `main`'s **550,146
bytes** (two chunks, straddled). Both are correct behaviour for an overwrite;
neither says anything about the insert case, which is why the dedicated test
above exists.

## 3. Resilience under packet loss

`docker_harness.py`'s sweep, a 512 KB file through the client container's own
`tc`/`netem` shaping, 90 s bound:

| Condition | `main` | `lightweight-portable` |
|---|---|---|
| 0 % loss, 0 ms | **2.1 s** | 3.2 s |
| 10 % loss, 50 ms | **6.3 s** | 9.2 s |
| 10 % loss, 100 ms | 18.0 s | **10.7 s** |
| 30 % loss, 100 ms | **TIMEOUT** | **64.3 s** |
| 20 % loss, 200 ms | TIMEOUT | TIMEOUT |

And scenario 3's own degraded-link step, a 3 MB file at 10 % loss / 100 ms:

| | `main` | `lightweight-portable` |
|---|---|---|
| Time to converge | 28 s | **25 s** |
| Whole scenario 3 wall time | **111.3 s** | 114.5 s |

The pattern is coherent with the designs rather than noise-shaped: `main` is
faster when the link is healthy, where 8 static workers and larger chunks fill
the pipe; `lightweight-portable` degrades better, where a smaller chunk, a longer
read timeout, a higher failure threshold and a window that halves on a latency
gradient all reduce the chance of a slow-but-working link being mistaken for a
dead one. The 30 %/100 ms row is the sharpest instance: one branch converged and
the other did not.

That said - **these are single runs of a random process.** The direction matches
the design intent and the mechanism is understood, but the specific seconds
should not be quoted as repeatable measurements.

One result worth reading carefully in both columns: 30 % loss converged (on one
branch) while 20 % loss did not, on either. TCP throughput scales roughly as
`1/(RTT·√p)`, so 20 % at 200 ms is about 1.6× harder than 30 % at 100 ms despite
the lower drop rate. Round-trip time dominates, not loss.

### Why `transport.py`'s tuning is load-bearing

`failure_threshold` 3→5 and read timeout 30→45 s are not cosmetic. `mark_failure`
counts *consecutive* failures across every concurrent uploader, so under a
link-wide condition - loss shaped onto the client's own egress, not a server
outage - three unlucky requests in a row can mark an endpoint down while most
requests are still succeeding. Observed directly during this branch's
development: both endpoints flapping unhealthy/healthy in a tight loop while a
degraded-but-working link burned the pass's budget on failover churn. Reverting
the tuning reintroduces that. `main` does not need it because 8 workers over
smaller average chunks already absorb the same condition.

## 4. Resource profile

`evidence/ab_benchmark.py`, sampling `docker stats` at a median 0.5 s interval
across scenarios 3 and 4 back to back (711 samples for `main`, 735 for
`lightweight-portable`):

| Container | Metric | `main` | `lightweight-portable` |
|---|---|---:|---:|
| `sync-client` | peak CPU | 55.8 % | **87.0 %** |
| `sync-client` | peak memory | 51.8 MiB | **30.1 MiB** |
| `sync-client` | total egress | 56.20 MB | **29.00 MB** |
| `sync-server-primary` | peak CPU | 45.5 % | 37.9 % |
| `sync-server-secondary` | peak CPU | 33.7 % | 45.6 % |

The CPU and memory figures are the expected shape of the trade: `zlib` and
`hashlib.blake2b` are slower than zstd and BLAKE3, so the portable client works
harder per byte; a 256 KiB chunk and a window that starts at 2 rather than 8
static workers means far less data buffered at once, so it holds less.

The egress figures are **not** a clean dedup comparison and should not be read as
one. Total bytes sent across scenarios 3 and 4 includes every retransmission,
every retry after a simulated blackout, and every chunk re-homed onto the other
non-replicating store when the client's random per-file endpoint choice sent a
file somewhere it had not been before. The figure is reported because it was
measured, not because the 2× ratio has been attributed to a cause.

**Block-device read accounting was unavailable** on this host for reads off a
bind-mounted source directory - every sample reported zero, on both branches.
Plot 7 therefore charts network egress rate as a disclosed proxy for the client's
read-then-send cadence, and the metrics JSON records
`notes.blkio_read_available: false` so the figure's caption is generated from the
data rather than from an assumption.

## 5. Test suite results

| Suite | `main` | `lightweight-portable` |
|---|---|---|
| Unit - `test_change_detection.py` | PASS (8/8) | PASS (8/8) |
| Property - `test_classify_properties.py` | PASS (6/6) | PASS (6/6) |
| Integration - `test_integration_http.py` | PASS (11 checks) | PASS (11 checks) |
| API key - `test_api_key_auth.py` | PASS (4/4) | PASS (4/4) |
| Scenarios 1–4 (live Docker) | all exit 0 | all exit 0 |

One behavioural difference is worth recording: integration check 3 asserts a
50 KB edit stores a small bounded number of chunks. On `lightweight-portable`
that is **deterministically 1**, because fixed boundaries do not move with the
random test content. On `main` it legitimately varies between runs, and in an
earlier round was seen to exceed its bound once before passing on retry. That
flakiness is a property of testing a content-defined chunker with random data,
not a defect - but determinism is a small, real advantage of the portable branch
for CI.

## 6. Recommendation

| Environment | Branch | Why |
|---|---|---|
| Locked-down, air-gapped, or no build toolchain | `lightweight-portable` | Zero compiled dependencies; `pip install` never touches a C or Rust compiler. |
| Insert-heavy edits on large files | `main` | 33× less data on the wire for a shifting edit (§2). This is the one case where the difference is decisive. |
| Unstable or lossy links | `lightweight-portable` | Converged where `main` timed out at 30 % loss, and faster at 10 %/100 ms. Single runs, but the mechanism is understood (§3). |
| Healthy, high-throughput links | `main` | Faster at 0 % and 10 %/50 ms; zstd and BLAKE3 cost less CPU per byte. |
| Memory-constrained hosts | `lightweight-portable` | 30 MiB vs 52 MiB peak client memory. |
| CPU-constrained hosts | `main` | 56 % vs 87 % peak client CPU. |
| Overwrite- or append-dominated workloads | Either | Measured equivalent (§2). Choose on dependency policy, not on dedup. |

The short version: if the host can install compiled wheels and the workload
inserts into large files, use `main`. Otherwise `lightweight-portable` gives up
one specific and well-understood property, and is the more robust of the two
under a bad link.
