"""Refuse to start on a half-filled ``.env``, and say so in one sentence.

``.env.example`` ships placeholders — ``<project-id>``, ``https://<your-stack>.
grafana.net``, ``change-me`` — because a reader has to see the *shape* of each
value. They are also non-empty strings, which is what made them dangerous: every
guard in the project tested ``bool(value)``, so a freshly copied ``.env`` passed
validation and the run proceeded until something far downstream failed on a URL
nobody could resolve. Copying the example and running the documented commands
produced three different tracebacks, none of which named the actual problem.

So a placeholder is *not a value*. :func:`real` erases one, which lets the
existing emptiness checks (``Settings.grafana_ready``, ``vertex_ready``) fire
exactly as they were always meant to, and :func:`reporting` turns the resulting
:class:`ConfigError` into the sentence and exit code a person can act on rather
than a stack they have to read.

This lives in ``bridge/`` for the same reason ``load_env`` does: it is the one
package every tier already depends on, and the seeder, the provisioner and the
agent all need the same answer to "is this value real?".
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager

#: What an unfilled value looks like. Deliberately broad at the edges a reader
#: would actually leave behind: the angle-bracket form used throughout
#: ``.env.example`` and ``docs/SETUP.md``, the ``change-me`` sentinel, and the
#: ``YOUR_THING`` shout-case convention. Also catches ``$(openssl rand …)``,
#: which is what someone gets if they paste a shell substitution into a file
#: that -- by design, see ``bridge/dotenv.py`` -- does not interpolate.
_PLACEHOLDER_RE = re.compile(
    r"""^(?:
          .*<[^>]*>.*            # <project-id>, https://<your-stack>.grafana.net
        | change[-_]?me
        | your[-_].*             # your-stack, your_token
        | YOUR_[A-Z0-9_]+        # YOUR_PROJECT_ID
        | \$\(.*\)               # $(openssl rand -hex 16)
        | xxx+
        | (?:todo|tbd|fixme|placeholder)
      )$""",
    re.VERBOSE | re.IGNORECASE,
)


class ConfigError(RuntimeError):
    """Configuration is missing or still a placeholder. The message names which.

    Carries no traceback worth reading: every raise site states the key, the
    file it belongs in, and the document that explains where the value comes
    from. :func:`reporting` prints exactly that and nothing else.
    """


def is_placeholder(value: str | None) -> bool:
    """Whether ``value`` is example text rather than something to connect with."""
    return bool(value) and _PLACEHOLDER_RE.match(value.strip()) is not None


def real(value: str | None) -> str:
    """``value``, or ``""`` if it is blank or still a placeholder.

    The point of collapsing both to the empty string is that callers keep the
    one truth test they already had. ``Settings.grafana_ready`` does not need to
    learn about placeholders; it needs the placeholder to stop looking like a
    URL.
    """
    value = (value or "").strip()
    return "" if is_placeholder(value) else value


def require(where: str = ".env", **values: str | None) -> None:
    """Raise :class:`ConfigError` naming every keyword whose value is not real.

    Keywords are the environment variable names, so the message a person reads
    is the list of lines they still have to fill in::

        require(GRAFANA_URL=url, GRAFANA_SERVICE_ACCOUNT_TOKEN=token)
        # ConfigError: unset or still a placeholder in .env:
        #   GRAFANA_URL, GRAFANA_SERVICE_ACCOUNT_TOKEN
    """
    missing = [name for name, value in values.items() if not real(value)]
    if missing:
        raise ConfigError(
            f"unset or still a placeholder in {where}: {', '.join(missing)}\n"
            f"  copy .env.example to {where} and fill those in "
            f"— docs/SETUP.md walks through where each value comes from."
        )


@contextmanager
def reporting(*, exit_code: int = 2) -> Iterator[None]:
    """Wrap an entry point so a configuration failure reads as a sentence.

    ``agent/engine.py`` already turns a forty-line Vertex traceback into one
    actionable line; the seeder and the provisioner deserve the same, and their
    most likely failure is the most mundane one in the project — an ``.env``
    that has been copied but not yet edited.

    Only :class:`ConfigError` is caught, which is why ``bridge.privacy`` raises
    a *subclass* of it for an unset salt rather than being caught wholesale
    here: a missing salt is a misconfiguration, but an ``assert_no_pii``
    failure is a crew name reaching an exporter. Those must not share an exit
    path — one is a line to fill in, the other is a bug that has to be loud.
    Anything unexpected still raises with its stack intact.
    """
    try:
        yield
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        raise SystemExit(exit_code) from None


__all__ = ["ConfigError", "is_placeholder", "real", "reporting", "require"]
