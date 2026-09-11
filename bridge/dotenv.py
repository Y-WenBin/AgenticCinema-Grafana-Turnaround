"""Load ``.env`` into the process environment. Stdlib only.

This lives in ``bridge/`` -- the base layer -- because every tier needs it and
``bridge/`` is the only package all of them already depend on. It was in
``agent/config.py``, which meant the agent tier read ``.env`` automatically and
``seed.*`` and ``grafana.*`` did not; running either without
``set -a && source .env && set +a`` first failed with a message about an unset
salt, which is the single most common way to lose an hour standing this project
up for the first time. Now every entry point calls this, and the incantation is
gone.

A real exported variable always wins, so CI and Cloud Run -- which set the
environment directly and ship no ``.env`` -- are unaffected.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    """Populate ``os.environ`` from ``.env`` for keys that are not already set.

    A real exported variable (CI, Cloud Run, ``set -a && . ./.env``) is never
    overwritten. Lines are ``KEY=VALUE``; ``#`` comments and blanks are skipped;
    surrounding quotes on the value are stripped. No interpolation -- values are
    taken literally, which is what an OTLP auth header needs.
    """
    env_path = path or REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
