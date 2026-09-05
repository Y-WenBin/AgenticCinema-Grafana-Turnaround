"""Shared fixtures.

The time-warp in ``bridge.timewarp`` is process-global module state. No unit
test configures it, but an autouse reset keeps a stray call in one test from
bleeding into the next.
"""

import os

import pytest

from bridge import timewarp

# The privacy layer refuses to build a pseudonym without a real salt, so any
# test that loads the simulation needs one. A fixed non-placeholder value keeps
# the suite hermetic; it never leaves the test process.
os.environ.setdefault("TURNAROUND_PSEUDONYM_SALT", "turnaround-test-salt")


@pytest.fixture(autouse=True)
def _no_timewarp():
    timewarp.reset()
    yield
    timewarp.reset()
