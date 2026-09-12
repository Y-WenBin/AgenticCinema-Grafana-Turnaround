"""Re-seed on a loop so the compressed window always overlaps "now".

The whole history is compressed into a ~45-minute wall-clock window ending at
the moment of seeding (``bridge/timewarp``). Hosted Mimir will not accept
anything older than about an hour, so that window also *ages out* of "recent"
in about an hour: dashboards set to `now-15m`, alert rules evaluating the last
15 minutes, and ML training windows all go empty once the last seed is far
enough back.

Running this keeps a fresh copy landing every ``--interval`` minutes, so the
newest points are always a few minutes old. Each run is a full re-seed; the
series are deterministic, so they overwrite rather than fork. Counters restart
from zero each run, which reads as a sawtooth over a long span -- fine for a
demo, and the reason queries use cumulative ratios and short rate windows
rather than `increase()` over hours.

    uv run python -m seed.refresh                 # every 15 minutes, forever
    uv run python -m seed.refresh --interval 10 --once

In production this is a scheduled job, not a process babysat by a laptop.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import UTC, datetime

from bridge.dotenv import load_env


def _seed() -> bool:
    """One full re-seed. Prints a line; on failure that line says why.

    The failure branch reads `stderr`, not `stdout`. It used to take the last
    line of stdout whenever there was one, falling back to stderr only if stdout
    was empty -- and `seed.populate` prints "simulating the show..." before
    anything can go wrong, so stdout was never empty and stderr was never shown.
    Every failure reported itself as `FAILED: simulating the show...`, which
    names the one thing that did work.
    """
    started = datetime.now(UTC)
    proc = subprocess.run(
        [sys.executable, "-m", "seed.populate"],
        capture_output=True,
        text=True,
        check=False,
    )
    stamp = started.strftime("%H:%M:%SZ")
    if proc.returncode == 0:
        tail = proc.stdout.strip().splitlines()[-1:] or [""]
        print(f"[{stamp}] ok: {tail[0]}", flush=True)
        return True
    # The seeder's own configuration errors are a sentence on stderr
    # (`bridge.startup.reporting`). Pass it through whole rather than clipping
    # it: the sentence ends in the command that fixes the problem.
    detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
    print(f"[{stamp}] FAILED:\n{detail}", file=sys.stderr, flush=True)
    return False


def main() -> None:
    # Load `.env` first, so this entry point needs no `set -a && source .env`
    # incantation. A real exported variable still wins (bridge/dotenv.py).
    load_env()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=15, metavar="MINUTES")
    ap.add_argument("--once", action="store_true", help="seed once and exit")
    args = ap.parse_args()

    # The first seed decides whether looping is worth anything. Every failure
    # this command actually hits is a standing one -- an unset salt, a token
    # that was never filled in -- so retrying it on a timer just produces the
    # same error every fifteen minutes forever. It used to do exactly that, and
    # because the reason was swallowed (see `_seed`) it looked like a hang.
    if not _seed():
        sys.exit(1)
    if args.once:
        sys.exit(0)
    while True:
        time.sleep(args.interval * 60)
        # Past the first success the config is known good, so a failure here is
        # transient -- a flaky network, a stack briefly refusing writes. Those
        # are worth retrying on the next tick.
        _seed()


if __name__ == "__main__":
    main()
