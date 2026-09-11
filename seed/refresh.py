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

In production this is a Cloud Run job on a schedule, not a process babysat by
a laptop (see deploy/).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import UTC, datetime

from bridge.dotenv import load_env


def _seed() -> bool:
    started = datetime.now(UTC)
    proc = subprocess.run(
        [sys.executable, "-m", "seed.populate"],
        capture_output=True,
        text=True,
        check=False,
    )
    tail = proc.stdout.strip().splitlines()[-1:] or [proc.stderr.strip()[-200:]]
    stamp = started.strftime("%H:%M:%SZ")
    print(f"[{stamp}] {'ok' if proc.returncode == 0 else 'FAILED'}: {tail[0]}", flush=True)
    return proc.returncode == 0


def main() -> None:
    # Load `.env` first, so this entry point needs no `set -a && source .env`
    # incantation. A real exported variable still wins (bridge/dotenv.py).
    load_env()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=15, metavar="MINUTES")
    ap.add_argument("--once", action="store_true", help="seed once and exit")
    args = ap.parse_args()

    ok = _seed()
    if args.once:
        sys.exit(0 if ok else 1)
    while True:
        time.sleep(args.interval * 60)
        _seed()


if __name__ == "__main__":
    main()
