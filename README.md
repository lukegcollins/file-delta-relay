# file-delta-relay

A data synchronisation utility: watches local directories and syncs their
contents to a remote server with minimal bandwidth, tolerating an unstable
connection, and letting the server verify it received an exact copy.

## Submission contents

- **`report-writeup.md`** (also rendered as `report-writeup.html`) — the
  1–2 page design document: architectural approach, a sequence diagram of
  the transfer protocol, and how each requirement (change detection,
  bandwidth, reliability, integrity) is met.
- **`change_detection.py`** — how a file change is detected (a pure,
  unit-tested decision kernel over file metadata; symlinked from
  `sync-demo/client/change_detection.py`).
- **`single_file_transfer.py`** — how a single file's transfer is managed
  (content-defined chunking, dedup, resumable upload; symlinked from
  `sync-demo/client/single_file_transfer.py`).
- **`sync-demo/`** — everything above isn't just a design on paper: a full
  working reference implementation (client, server, HTTP transport with
  multi-server failover), an audited test suite, and a Docker-based demo
  with real network-fault injection (packet loss, latency, outages). See
  [`sync-demo/README.md`](sync-demo/README.md) to run it, or
  [`sync-demo/FINAL_REPORT.md`](sync-demo/FINAL_REPORT.md) for the full
  requirement-to-evidence write-up with plots.

## Fastest way to see it work

```bash
cd sync-demo
./demo.sh quick   # no Docker: real server + real client, ~5 seconds,
                   # 8 unit tests + 11 integration checks, all four
                   # requirements exercised end-to-end
```
