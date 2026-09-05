"""Shared fixtures.

The time-warp in ``bridge.timewarp`` is process-global module state. No unit
test configures it, but an autouse reset keeps a stray call in one test from
bleeding into the next.
"""

import pytest

from bridge import timewarp


@pytest.fixture(autouse=True)
def _no_timewarp():
    timewarp.reset()
    yield
    timewarp.reset()
