"""Property-based tests for the classify() decision kernel.

test_change_detection.py pins down classify()'s branches with hand-picked
example cases, including the guard-window boundary. This file complements
that with generated cases across the input space -- useful specifically
because classify() is a pure function with no filesystem or network
dependency, exactly the shape property-based testing suits best.

Run:  python -m unittest tests/test_classify_properties.py -v
"""

import os
import sys
import unittest
from types import SimpleNamespace

from hypothesis import given, assume, strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "client"))

from change_detection import GUARD_NS, FileRec, Reason, classify   # noqa: E402

ns = st.integers(min_value=0, max_value=2**62)
sizes = st.integers(min_value=0, max_value=10**12)
non_synced_states = st.sampled_from(["dirty", "chunked", "pending", "anything-not-synced"])
any_state = st.sampled_from(["dirty", "chunked", "synced"])


def stat(size, mtime_ns):
    """Return a stand-in os.stat_result with the fields classify() reads."""
    return SimpleNamespace(st_size=size, st_mtime_ns=mtime_ns)


def rec(state, size, mtime_ns, verified_at_ns):
    """Return a StateDB record built from the given fields."""
    return FileRec((size, mtime_ns), state, b"h", None, verified_at_ns)


class ClassifyPropertyTests(unittest.TestCase):
    """Check the invariants classify() must hold for *every* input.

    Not just the hand-picked examples in test_change_detection.py.
    """

    @given(size=sizes, mtime=ns, now=ns)
    def test_unknown_path_is_always_new(self, size, mtime, now):
        self.assertIs(classify(None, stat(size, mtime), now), Reason.NEW)

    @given(state=non_synced_states, size=sizes, mtime=ns, verified=ns, now=ns)
    def test_non_synced_state_is_always_interrupted(self, state, size, mtime, verified, now):
        # Regardless of how closely the stat matches the recorded key: an
        # incomplete previous sync always wins.
        r = rec(state, size, mtime, verified)
        self.assertIs(classify(r, stat(size, mtime), now), Reason.INTERRUPTED)

    @given(size=sizes, mtime=ns, other_size=sizes, other_mtime=ns, verified=ns, now=ns)
    def test_any_stat_mismatch_on_synced_is_stat_changed(
            self, size, mtime, other_size, other_mtime, verified, now):
        assume((size, mtime) != (other_size, other_mtime))
        r = rec("synced", size, mtime, verified)
        self.assertIs(classify(r, stat(other_size, other_mtime), now), Reason.STAT_CHANGED)

    @given(size=sizes, mtime=ns, verified=ns, now=ns)
    def test_matching_stat_boundary_is_exactly_the_guard_window(self, size, mtime, verified, now):
        # The one piece of real arithmetic in classify(): a matching stat is
        # BOUNDARY (inconclusive, re-verify) iff mtime is within GUARD_NS of
        # verified_at, and provably unchanged (None) the instant it isn't --
        # never anything else, for any (mtime, verified) pair.
        r = rec("synced", size, mtime, verified)
        result = classify(r, stat(size, mtime), now)
        if mtime >= verified - GUARD_NS:
            self.assertIs(result, Reason.BOUNDARY)
        else:
            self.assertIsNone(result)

    @given(size=sizes, mtime=ns, verified=ns, now1=ns, now2=ns)
    def test_result_never_depends_on_the_clock_argument(self, size, mtime, verified, now1, now2):
        # now_ns is passed in but must never affect the verdict -- the
        # anchor is verified_at_ns, not "now" (a slow upload/late commit
        # must not widen or narrow the blind spot). test_change_detection.py
        # checks this for two specific values; this checks it for any two.
        r = rec("synced", size, mtime, verified)
        st_ = stat(size, mtime)
        self.assertIs(classify(r, st_, now1), classify(r, st_, now2))

    @given(state=any_state, size=sizes, mtime=ns, verified=ns, now=ns,
          st_size=sizes, st_mtime=ns)
    def test_never_raises(self, state, size, mtime, verified, now, st_size, st_mtime):
        classify(rec(state, size, mtime, verified), stat(st_size, st_mtime), now)
        classify(None, stat(st_size, st_mtime), now)


if __name__ == "__main__":
    unittest.main()
