"""Measure how each branch's chunker responds to overwrite vs. shifting edits.

Why this exists: the trade-off between fixed-size and content-defined chunking
is the single largest design difference between `main` and
`lightweight-portable`, and it is easy to state loosely and get wrong. The
scenario suite does not settle it, because every file it writes is created or
overwritten whole -- a workload under which the two strategies are expected to
perform about the same, and under which whichever one happens to place a
boundary more conveniently for a given edit will look better by luck.

This measures the thing that actually differs. For one file it applies three
edits and reports, for each, how many chunk hashes changed:

  overwrite  50 KB replaced in place, file length unchanged. Both strategies
             should re-send only the chunk(s) the edit lands in.
  append     50 KB added at EOF. Both should re-send only the final chunk.
  insert     50 KB inserted at the FRONT, shifting every following byte. This
             is the case fixed-size chunking cannot absorb: every subsequent
             boundary moves with the data, so every downstream chunk gets a new
             hash. Content-defined boundaries follow the content, so they should
             re-synchronise after the edit and leave most chunks matching.

It imports whichever chunker the current branch ships, so running it on each
branch and comparing the `insert` row is the measurement. No server, no Docker,
no network -- this is a property of the chunker alone.

Run:  .venv/bin/python evidence/chunking_shift_test.py
      .venv/bin/python evidence/chunking_shift_test.py --json   (machine-readable)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "client"))

import single_file_transfer as sft            # noqa: E402

FILE_SIZE = 8 * 1024 * 1024
EDIT_SIZE = 50 * 1024
SEED = 20260821            # fixed so the comparison is reproducible run to run


def _chunk_hashes(path: str) -> list[bytes]:
    """Return the ordered chunk digests the branch's own chunker produces."""
    chunks, _file_hash = sft.chunk_manifest(path)
    return [h for _o, _n, h in chunks]


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


def _describe_chunker() -> dict:
    """Report which chunking strategy this branch actually ships."""
    size = getattr(sft, "CHUNK_SIZE", None)
    if size is not None:
        return {"strategy": "fixed-size", "chunk_size": size,
                "detail": f"fixed {size // 1024} KiB"}
    return {"strategy": "content-defined",
            "chunk_size": getattr(sft, "AVG_CHUNK", None),
            "detail": "content-defined (fastcdc)"}


def run() -> dict:
    """Apply overwrite/append/insert edits and count changed chunks for each."""
    rng = random.Random(SEED)
    base = bytes(rng.getrandbits(8) for _ in range(FILE_SIZE))
    edit = bytes(rng.getrandbits(8) for _ in range(EDIT_SIZE))
    mid = FILE_SIZE // 2

    variants = {
        "overwrite": base[:mid] + edit + base[mid + EDIT_SIZE:],
        "append": base + edit,
        "insert": edit + base,
    }

    results = {"chunker": _describe_chunker(), "file_size": FILE_SIZE,
               "edit_size": EDIT_SIZE, "edits": {}}

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "f.bin")
        _write(p, base)
        original = _chunk_hashes(p)
        results["baseline_chunks"] = len(original)
        known = set(original)

        for name, data in variants.items():
            _write(p, data)
            after = _chunk_hashes(p)
            # "Changed" = chunks the server would not already hold. That is the
            # quantity the requirement is about: bytes that must cross the wire.
            new = [h for h in after if h not in known]
            resend = sum(1 for _ in new)
            results["edits"][name] = {
                "chunks_after": len(after),
                "chunks_resent": resend,
                "pct_of_file_resent": round(100 * resend / max(len(after), 1), 1),
            }
    return results


def main() -> int:
    """Print the chunker's response to each edit shape."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument("-o", "--out", default=None,
                    help="also write the JSON result to this path")
    args = ap.parse_args()

    r = run()
    if args.out:
        with open(args.out, "w") as f:
            json.dump(r, f, indent=2)
    if args.json:
        print(json.dumps(r, indent=2))
        return 0

    c = r["chunker"]
    print(f"chunker:  {c['detail']}")
    print(f"file:     {r['file_size'] // 1024 // 1024} MiB "
          f"({r['baseline_chunks']} chunks)   edit: {r['edit_size'] // 1024} KiB")
    print()
    print(f"  {'edit':<11} {'chunks after':>13} {'re-sent':>9} {'% of file':>11}")
    print(f"  {'-' * 11} {'-' * 13} {'-' * 9} {'-' * 11}")
    for name in ("overwrite", "append", "insert"):
        e = r["edits"][name]
        print(f"  {name:<11} {e['chunks_after']:>13} {e['chunks_resent']:>9} "
              f"{e['pct_of_file_resent']:>10.1f}%")
    print()
    print("  The `insert` row is the whole argument: a fixed-size chunker must")
    print("  re-send essentially the entire file, a content-defined one re-syncs")
    print("  its boundaries after the edit and re-sends only what really changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
