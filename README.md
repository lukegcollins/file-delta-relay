# file-delta-relay

A data synchronisation utility: watches local directories and syncs their
contents to a remote server with minimal bandwidth, tolerating an unstable
connection, and letting the server verify it received an exact copy.

> **You are on `main`.** See [Branches](#branches) below for what that means, and
> why `lightweight-portable` is the recommended starting point for review.

## The design in one paragraph

Split each file into chunks; name every chunk by the hash of its own bytes; let
a file's manifest be the ordered list of those names. One mechanism then pays
three of the four requirements at once. **Bandwidth**: a chunk the server already
holds - from an earlier version, another file, or a rename - is never sent
again, so deduplication is set membership rather than diffing. **Reliability**:
because a chunk's address *is* its content, uploading it twice is harmless, so
resume after a drop is not a protocol feature with its own state machine, it is
the same set-difference query asked again. **Integrity**: defining a file's
identity as the hash of its ordered chunk hashes lets the server prove
end-to-end equality without ever re-reading assembled bytes. **Change
detection**, the fourth, is a local state database checked against file
metadata, with the content hash as the final arbiter whenever metadata is
inconclusive.

## Submission contents

- **[`docs/main/architecture_report_main_full.md`](docs/main/architecture_report_main_full.md)** -
  the design document (also exported as
  [`architecture_report_main_full.pdf`](docs/main/architecture_report_main_full.pdf)):
  architectural approach, sequence diagrams for both assessed modules, how each
  requirement is met, and what the compiled dependencies buy, measured.
  [`docs/lightweight-portable/`](docs/lightweight-portable/) carries the same
  document for `lightweight-portable`, and
  [`docs/two-page-summary/`](docs/two-page-summary/) has the submitted summary.
- **[`change_detection.py`](change_detection.py)** - how a file change is
  detected: a pure, unit- and property-tested decision kernel over file
  metadata, plus the SQLite state store. Symlinked from
  `sync-demo/client/change_detection.py`.
- **[`single_file_transfer.py`](single_file_transfer.py)** - how a single file's
  transfer is managed: content-defined chunking (FastCDC), dedup, and resumable
  upload. Symlinked from `sync-demo/client/single_file_transfer.py`.
- **[`sync-demo/`](sync-demo/)** - a full working reference implementation:
  client, server, HTTP transport with multi-server failover, a test suite
  spanning unit, property-based and integration levels, and a Docker demo with
  real network-fault injection (packet loss, latency, outages). See
  [`sync-demo/README.md`](sync-demo/README.md) to run it, or
  [`sync-demo/FINAL_REPORT.md`](sync-demo/FINAL_REPORT.md) for the full
  requirement-to-evidence write-up with plots.
- **[`evidence/`](evidence/)** - per-branch snapshots of the raw results behind
  every figure and number in the reports: metrics JSON, plots, scenario logs,
  and a `PROVENANCE` file recording the exact commit each snapshot came from.

## Branches

The project ships two implementations of the same protocol. They pass the same
test suites and the same Docker scenarios; they differ in what they are willing
to depend on.

| | `main` | `lightweight-portable` |
|---|---|---|
| Client dependencies | `requests`, `fastcdc`, `blake3`, `zstandard` | `requests` |
| Server dependencies | `flask`, `blake3`, `zstandard` | `flask` |
| Compiled wheels required | yes - three C/Rust extensions | **none** |
| Chunk boundaries | content-defined (`fastcdc`), ~256 KiB average | fixed 256 KiB |
| Hash | BLAKE3 | `hashlib.blake2b`, 256-bit |
| Compression | zstd | `zlib` level 1 |
| Upload concurrency | static, 8 workers | AIMD-adaptive window, 1–8 |

**Reviewers: start with `lightweight-portable`.** It exposes the underlying
mechanics - chunking, hashing, compression, and congestion response - as code
rather than as library calls, which makes the change-detection and
bandwidth-management requirements directly readable. It also runs on hosts where
installing a compiled wheel is not an option.

**Its honest shortcoming** is worth stating rather than discovering: fixed-size
chunking is not shift-resistant. Insert a byte near the start of a large file and
every following chunk boundary moves, so every downstream chunk gets a new hash
and dedup collapses for that edit. `fastcdc` on `main` picks boundaries from
content and does not have this problem. The loss is narrow - overwrites,
appends, and renames are all unaffected, and deduplication across versions,
files and renames still works because it depends on content addressing rather
than on boundary selection - but on an insert-heavy workload `main` is the right
branch. §1.1 of the design document works through exactly which edit shapes
degrade and which do not.

## Requirements, and where each is met

1. **Change detection** - `classify()` keys files by `path → (size, mtime_ns)`,
   deliberately not by inode (atomic save-via-rename allocates a new one) and
   not by ctime (a `chmod` churns it). Because mtime can lie, a file whose mtime
   falls within a guard window of its last *content verification* is re-read
   rather than skipped. The check errs toward false positives, which dedup makes
   nearly free, and never toward false negatives.
2. **Bandwidth** - four stacked reductions: unchanged files cost a `stat` and no
   read; changed files transfer only the chunks the server lacks; the query
   itself stays proportional to the change because the client diffs locally
   first; the residue is compressed. Measured: inserting 50 KiB at the front of
   an 8 MiB file re-sent **one chunk, 3 % of the file** - content-defined
   boundaries re-synchronise after the edit - and a rename put **zero** bytes on
   the wire.
3. **Reliability** - every step is idempotent or resumes from persisted state.
   Measured: a 6 MB file interrupted after 3 chunks resumed from persisted state
   and completed without re-chunking or re-sending anything already delivered.
   Multi-server failover and failback keep the client working through a stopped
   primary.
4. **Integrity** - three verified layers: every chunk re-hashed server-side on
   write (a content-addressed store that trusts client-supplied keys is poisoned
   permanently by one bad upload), the manifest pins ordering, and the file hash
   over ordered chunk hashes is recomputed at commit rather than taken on trust.

## Fastest way to see it work

```bash
cd sync-demo
./demo.sh quick   # no Docker: real server + real client, a few seconds.
                  # 14 unit tests, 11 integration checks, 4 auth checks;
                  # all four requirements exercised end to end.
```

Then, for the full picture including real packet loss and container outages:

```bash
./certs/gen_certs.sh
./evidence/run_full_evidence.sh     # ~15 min: brings the stack up once, runs
                                    # all four scenarios, collects every metric
.venv/bin/python evidence/make_plots.py
```
