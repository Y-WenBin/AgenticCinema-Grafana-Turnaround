"""Compress historical time into the ingestion window.

Grafana Cloud's hosted Mimir refuses any sample older than a roughly one-hour
out-of-order window (``err-mimir-sample-timestamp-too-old``); hosted Loki
refuses old lines on the same principle. A backfill whose history spans five
months therefore cannot be pushed over live OTLP unchanged -- every point past
the window is dropped, silently for logs and with a 400 for metrics.

The honest fix is to leave the *simulation* in real calendar time -- every day
and week bucket, every argument about overlapping passes -- and compress only
at the final step, where a ``datetime`` becomes a Unix-nanosecond count for the
exporter. A monotonic affine map sends ``[earliest .. anchor]`` onto
``[anchor - window .. anchor]``: every series keeps its exact shape, only the
time axis is scaled. Dashboards and agent queries then use proportionally
shorter ranges -- ``increase(...[7m])`` where the real span would say ``[7d]``.

Inactive unless :func:`configure` is called, so the unit suite and any
real-time self-instrumentation are untouched.

**The warp is process-wide**, and deliberately so: it has to be read at the one
place a ``datetime`` becomes an exporter timestamp, which is three layers below
anyone who knows a warp exists (``bridge/emit.py``, ``bridge/metrics.py``,
``bridge/annotate.py``). Threading a ``Warp`` down all three call chains would
put a parameter nobody reads into every signature on the seeding hot path.

The cost is the usual one: two different compressions cannot be live in one
process at the same time. Nothing in the project wants that -- ``seed.populate``
configures it once per run -- but "the last call wins" is a bad way to find out,
so :func:`warped` gives a scope with a guaranteed restore, and ``configure``
refuses to quietly replace a *different* live warp.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

_active: _Warp | None = None


@dataclass(frozen=True, slots=True)
class _Warp:
    anchor: datetime  # the simulation's "now"; maps to itself
    earliest: datetime  # the oldest timestamp in the backfill; maps to anchor - window
    window: timedelta  # target wall-clock span for the whole history

    @property
    def scale(self) -> float:
        real = (self.anchor - self.earliest).total_seconds()
        return self.window.total_seconds() / real if real > 0 else 1.0

    def apply(self, when: datetime) -> datetime:
        # Monotonic in `when`: ordering, and every `ended >= started`, survive.
        delta = (self.anchor - when).total_seconds()
        return self.anchor - timedelta(seconds=delta * self.scale)


def configure(*, anchor: datetime, earliest: datetime, window: timedelta) -> None:
    """Turn compression on.

    Idempotent for the same settings. Replacing a *different* live warp is an
    error rather than a silent reshaping of everything emitted after it: the
    symptom would be a series whose first half is on one time base and whose
    second half is on another, which looks like corrupt data rather than like a
    misuse of this module. Call :func:`reset`, or use :func:`warped`.
    """
    global _active
    if earliest >= anchor:
        raise ValueError("earliest must precede anchor")
    warp = _Warp(anchor=anchor, earliest=earliest, window=window)
    if _active is not None and _active != warp:
        raise RuntimeError(
            "a different time warp is already active in this process; emitting "
            "through both would put one series on two time bases. Call "
            "timewarp.reset() first, or scope it with timewarp.warped()."
        )
    _active = warp


@contextmanager
def warped(*, anchor: datetime, earliest: datetime, window: timedelta) -> Iterator[None]:
    """:func:`configure` for the duration of a block, restored afterwards.

    The supported way to compress twice in one process -- and the reason the
    global above is a convenience rather than something a caller is stuck with.
    """
    global _active
    previous = _active
    _active = None
    configure(anchor=anchor, earliest=earliest, window=window)
    try:
        yield
    finally:
        _active = previous


def reset() -> None:
    """Turn compression off. Used by the test fixture and anything real-time."""
    global _active
    _active = None


def is_active() -> bool:
    return _active is not None


def apply(when: datetime) -> datetime:
    """Map one timestamp through the active warp, or return it unchanged."""
    return _active.apply(when) if _active is not None else when


def describe() -> str:
    if _active is None:
        return "time-warp: off (real timestamps)"
    days = (_active.anchor - _active.earliest).total_seconds() / 86400
    minutes = _active.window.total_seconds() / 60
    return (
        f"time-warp: {days:.0f}d of history compressed to {minutes:.0f}m "
        f"wall-clock (scale {_active.scale:.6f}, "
        f"1 real day -> {_active.scale * 86400:.1f}s)"
    )
