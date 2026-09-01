# Evidence

Raw results behind every figure and number in the reports, one directory per
branch. Both directories are present on both branches, so the comparison can be
read without checking anything out.

Each directory carries a `PROVENANCE` file naming the exact commit that produced
it. That stamp is what makes the two sets comparable rather than merely
adjacent - without it there is no way to tell later which code produced which
figure.

```
evidence/
├── main/                        and  lightweight-portable/
│   ├── PROVENANCE                    branch, commit, generation time
│   ├── metrics/                      the JSON every plot is rendered from
│   ├── plots/                        01-09, as embedded in FINAL_REPORT.md §8
│   ├── logs/                         the four scenario logs + docker_harness
│   └── reports/                      the timestamped demo.sh run report
```

## How this was produced

Both branches were run sequentially on the same host, from the same commit of
the scenario scripts, with a full teardown between. Nothing was carried forward
from an earlier run. On each branch:

```bash
cd sync-demo
./certs/gen_certs.sh
uv venv --python 3.12 .venv && uv pip install --python .venv \
    -r client/requirements.txt -r server/requirements.txt \
    -r tests/requirements.txt -r evidence/requirements.txt
./demo.sh quick                               # suites + report
.venv/bin/python evidence/local_harness.py    # plots 1, 2, 3
./evidence/run_full_evidence.sh               # scenarios 1-4, A/B sampling, plots 4-6
.venv/bin/python evidence/make_plots.py       # renders plots/*.png
.venv/bin/python evidence/chunking_shift_test.py
./evidence/publish.sh                         # snapshots into this directory
```

`run_full_evidence.sh` brings the Docker stack up once and does everything
against it, running scenarios 3 and 4 *under* `evidence/ab_benchmark.py` so the
branch-comparison samples and the pass/fail record come from the same execution
rather than from two differently-conditioned runs.

## Results at a glance

Full analysis in [`../sync-demo/tradeoff_analysis.md`](../sync-demo/tradeoff_analysis.md).

| | `main` | `lightweight-portable` |
|---|---|---|
| Compiled wheels required | 3 per side | **none** |
| Suites (unit / integration / auth) | 14 / 11 / 4 pass | 14 / 11 / 4 pass |
| Scenarios 1–4 | all exit 0 | all exit 0 |
| Insert 50 KiB into an 8 MiB file | **1 chunk, 3 %** | 33 chunks, 100 % |
| Overwrite 50 KiB in place | 3 chunks, 8.8 % | **1 chunk, 3.1 %** |
| Append 50 KiB | 1 chunk, 3.0 % | 1 chunk, 3.0 % |
| Sweep: 10 % loss / 100 ms | 18.0 s | **10.7 s** |
| Sweep: 30 % loss / 100 ms | **TIMEOUT** | **64.3 s** |
| Sweep: clean link | **2.1 s** | 3.2 s |
| Scenario 3 degraded link (3 MB) | 28 s | **25 s** |
| Client peak CPU | **55.8 %** | 87.0 % |
| Client peak memory | 51.8 MiB | **30.1 MiB** |

Three things worth reading carefully rather than skimming:

**The insert row is the whole trade-off.** It is the only place the two chunkers
differ systematically, and it is a 33× difference. Overwrites and appends are
equivalent - and on the overwrite measured here, fixed-size chunking was the
*better* of the two, because `main`'s variable boundaries happened to straddle
the edit. That is luck, not an advantage, and it would reverse at a different
offset.

**The loss results are single runs of a stochastic process.** `tc`/`netem` drops
packets at random. The direction is consistent with each branch's design - a
static 8-worker pool fills a healthy pipe better, an adaptive window survives a
bad one better - but the specific seconds should not be quoted as repeatable
measurements. The 30 % row is the sharpest instance: one branch converged inside
the bound and the other did not.

**Total bytes sent is not a dedup comparison.** `metrics/*.json` records client
egress across scenarios 3 and 4, which includes every retransmission under
simulated loss, every retry after the 60 s blackout, and every chunk re-homed
onto the other non-replicating store by the random per-file endpoint choice. The
dedup question is answered by the chunker benchmark above, not by that total.

## A measurement that was not available

`docker stats` reported zero block-device reads for every sample on both
branches - blkio read accounting is not exposed for reads off a bind-mounted
source directory under this host's cgroup and storage-driver configuration. Plot
7 therefore charts network egress rate as a disclosed proxy for the client's
read-then-send cadence, and the collector records
`notes.blkio_read_available: false` in the metrics JSON so the figure's subtitle
is generated from the data rather than from an assumption.
