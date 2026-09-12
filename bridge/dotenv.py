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

#: The directory this package was imported from. In a git checkout that is the
#: repo root; in a wheel install it is ``site-packages``, which is why it is the
#: *last* place :func:`env_path` looks rather than the only one.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Stop walking up at whatever looks like the top of a project, so a stray
#: ``/.env`` or one in a home directory can never be picked up by accident.
_ROOT_MARKERS = ("pyproject.toml", ".git")


def env_path() -> Path | None:
    """The ``.env`` to load, or ``None``.

    Nearest first: the working directory, then upward to the project root, then
    the package's own directory. The walk exists because ``REPO_ROOT`` is only
    the repo when the code is running *from* the repo -- installed as a wheel it
    resolves to ``site-packages``, so an installed copy used to look for the
    user's ``.env`` inside its own dependencies and silently find nothing.

    This is the one place the search is defined, which makes it the one place
    the test suite has to neutralise to stay hermetic (``tests/conftest.py``).
    """
    start = Path.cwd().resolve()
    for directory in (start, *start.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
        if any((directory / marker).exists() for marker in _ROOT_MARKERS):
            break
    fallback = REPO_ROOT / ".env"
    return fallback if fallback.is_file() else None


def load_env(path: Path | None = None) -> None:
    """Populate ``os.environ`` from ``.env`` for keys that are not already set.

    A real exported variable (CI, Cloud Run, ``set -a && . ./.env``) is never
    overwritten. Lines are ``KEY=VALUE``; ``#`` comments and blanks are skipped;
    surrounding quotes on the value are stripped. No interpolation -- values are
    taken literally, which is what an OTLP auth header needs.

    With no ``path``, :func:`env_path` decides which file that is.
    """
    env_path_ = path or env_path()
    if env_path_ is None or not env_path_.is_file():
        return
    for raw in env_path_.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
