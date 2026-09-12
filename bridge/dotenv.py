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


#: A key may be written ``export KEY=value``. Valid shell, and this project's
#: own ``deploy/deploy.sh`` sources ``.env`` with ``set -a``, so a user whose
#: file works for the deploy script would otherwise find every ``export`` line
#: silently invisible to the Python entry points reading the same file. Two
#: consumers of one file must not disagree about what is in it.
_EXPORT = "export "


def _key(raw: str) -> str:
    """The key from the left of the first ``=``, with any ``export`` stripped."""
    key = raw.strip()
    if key.startswith(_EXPORT):
        key = key[len(_EXPORT):].strip()
    return key


def _strip_comment(value: str) -> str:
    """Remove a trailing ``# ...`` that is preceded by whitespace.

    Scans for the first ``#`` that *qualifies*, rather than partitioning on the
    first ``#`` there is. Those differ whenever a value legitimately contains
    one: ``Basic%20ab#cd   # my token`` has a leading ``#`` that is part of the
    payload, and stopping at it left the real comment in the value.
    """
    for i, char in enumerate(value):
        if char == "#" and (i == 0 or value[i - 1].isspace()):
            return value[:i]
    return value


def _value(raw: str) -> str:
    """One ``.env`` value: quotes honoured, trailing comment removed.

    An inline comment needs whitespace before the ``#``, and a quoted value has
    no comment inside it at all -- the same rule ``python-dotenv`` and Compose
    use, and the reason it matters here is ``OTEL_EXPORTER_OTLP_HEADERS``, whose
    base64 may legitimately contain a ``#`` with nothing in front of it.

    Before this, ``TURNAROUND_ASKS_PER_HOUR=8      # per visitor`` parsed as the
    whole string. ``agent/config.py`` reads it with ``_int_env``, which answered
    an unusable value with the default -- so the three rate limits shipped in
    ``.env.example`` were *inert*, and a user lowering their daily budget to
    bound a real bill silently kept 200. Nothing looked wrong, because the
    defaults matched the file.

    An *unterminated* quote is treated as punctuation the user meant as syntax,
    not as data -- the lenient reading the original parser had, kept because the
    alternative is a token that silently carries a stray ``"`` into an
    Authorization header.
    """
    value = raw.strip()
    for quote in ('"', "'"):
        if value.startswith(quote):
            close = value.find(quote, 1)
            if close > 0:
                return value[1:close]
    return _strip_comment(value).strip().strip('"').strip("'")


def load_env(path: Path | None = None) -> None:
    """Populate ``os.environ`` from ``.env`` for keys that are not already set.

    A real exported variable (CI, Cloud Run, ``set -a && . ./.env``) is never
    overwritten. Lines are ``KEY=VALUE`` or ``export KEY=VALUE``; blank lines
    and whole-line ``#`` comments are skipped, a trailing comment and
    surrounding quotes are removed from the value (:func:`_value`). No
    interpolation -- values are taken literally, which is what an OTLP auth
    header needs.

    **A repeated key takes its last value**, which is the convention everything
    else in this space follows and, more to the point, is what makes the
    documented way of setting the salt work:

        echo "TURNAROUND_PSEUDONYM_SALT=$(openssl rand -hex 16)" >> .env

    A ``.env`` copied from ``.env.example`` already carries that key set to
    ``change-me``. Appending used to leave the placeholder winning, so the fix
    that ``docs/SETUP.md`` and the error message both prescribe did nothing and
    the message repeated itself verbatim.

    With no ``path``, :func:`env_path` decides which file that is.
    """
    env_path_ = path or env_path()
    if env_path_ is None or not env_path_.is_file():
        return
    parsed: dict[str, str] = {}
    for raw in env_path_.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed[_key(key)] = _value(value)
    for key, value in parsed.items():
        os.environ.setdefault(key, value)
