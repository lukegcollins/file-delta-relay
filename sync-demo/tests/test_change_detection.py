"""Unit tests for the pure decision kernel in change_detection.classify().

No filesystem, no server: classify() is a function of (db record, stat,
clock), so every branch of the change-detection policy is pinned down here
with hand-built inputs - including the guard-window boundary that the
integration test can only exercise indirectly.

Run:  python -m unittest tests/test_change_detection.py -v
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "client"))

from change_detection import GUARD_NS, FileRec, Reason, _contains, classify   # noqa: E402

NOW = 1_700_000_000 * 10**9          # an arbitrary "now", in ns
HOUR = 3600 * 10**9


def stat(size=100, mtime_ns=NOW - HOUR):
    """Return a stand-in os.stat_result with the fields classify() reads."""
    return SimpleNamespace(st_size=size, st_mtime_ns=mtime_ns)


def rec(state="synced", size=100, mtime_ns=NOW - HOUR, verified_at_ns=NOW - HOUR + 1):
    """Return a StateDB record.

    By default a clean sync verified just after mtime.
    """
    return FileRec((size, mtime_ns), state, b"h", None, verified_at_ns)


class ClassifyTests(unittest.TestCase):
    """Cover each classify() branch, including the guard-window boundary."""

    def test_unknown_path_is_new(self):
        self.assertIs(classify(None, stat(), NOW), Reason.NEW)

    def test_incomplete_sync_is_interrupted(self):
        for state in ("dirty", "chunked"):
            self.assertIs(classify(rec(state=state), stat(), NOW), Reason.INTERRUPTED)

    def test_size_or_mtime_change_is_stat_changed(self):
        self.assertIs(classify(rec(), stat(size=101), NOW), Reason.STAT_CHANGED)
        self.assertIs(classify(rec(), stat(mtime_ns=NOW - HOUR + 1), NOW),
                      Reason.STAT_CHANGED)

    def test_matching_stat_well_before_verification_is_unchanged(self):
        # mtime an hour before the content was verified: provably unchanged.
        r = rec(mtime_ns=NOW - 2 * HOUR, verified_at_ns=NOW - HOUR)
        self.assertIsNone(classify(r, stat(mtime_ns=NOW - 2 * HOUR), NOW))

    def test_mtime_inside_guard_window_is_boundary(self):
        # A same-tick write could hide behind a matching stat when the mtime
        # sits within GUARD_NS of the verification instant: re-verify.
        verified = NOW - HOUR
        for mtime in (verified, verified - 1, verified - GUARD_NS + 1,
                      verified - GUARD_NS):
            r = rec(mtime_ns=mtime, verified_at_ns=verified)
            self.assertIs(classify(r, stat(mtime_ns=mtime), NOW), Reason.BOUNDARY,
                          f"mtime {verified - mtime} ns before verification")
        # Just outside the window it is provably unchanged.
        mtime = verified - GUARD_NS - 1
        self.assertIsNone(classify(rec(mtime_ns=mtime, verified_at_ns=verified),
                                   stat(mtime_ns=mtime), NOW))

    def test_anchor_is_verification_not_commit_time(self):
        # verified_at_ns is the anchor; "now" (e.g. a late commit) plays no
        # part, so a slow upload cannot widen or narrow the blind spot.
        mtime = NOW - 2 * HOUR
        r = rec(mtime_ns=mtime, verified_at_ns=NOW - HOUR)
        self.assertIsNone(classify(r, stat(mtime_ns=mtime), NOW))
        self.assertIsNone(classify(r, stat(mtime_ns=mtime), NOW + 10 * HOUR))


class ContainsTests(unittest.TestCase):
    """Cover _contains(), the other pure helper scan()/check() build on.

    _contains() answers: does a path fall under a root. Regression coverage
    for the degenerate root == '/' case, where a naive
    `path.startswith(root + os.sep)` silently matches nothing
    (os.path.abspath('/') + os.sep == '//').
    """

    def test_ordinary_root(self):
        self.assertTrue(_contains("/data/sync", "/data/sync/f.txt"))
        self.assertFalse(_contains("/data/sync", "/data/sync2/f.txt"))
        self.assertFalse(_contains("/data/sync", "/data/sync"))  # root itself, not "under" it

    def test_root_is_filesystem_root(self):
        self.assertTrue(_contains("/", "/etc/passwd"))
        self.assertTrue(_contains("/", "/a/b/c"))
        self.assertFalse(_contains("/", "/"))


if __name__ == "__main__":
    unittest.main()
