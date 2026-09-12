"""Privacy invariants, enforced in code rather than in policy documents.

A tool that measures artist hours can very easily become a tool for punishing
artists. The whole premise of this product is that crunch is a *scheduling*
failure, not a personal one, so the telemetry is built so that it cannot
comfortably be used the other way:

  * artists appear only as salted pseudonyms; real names never leave Kitsu
  * crew-load signals require a pool of at least MIN_POOL_SIZE people
  * there is no per-person output metric, only load and waste

These are not decorative. `assert_no_pii` is called on every attribute bag
before it is exported, and the aggregation floor is applied before any
crew-load figure reaches a dashboard, an alert, or the agent.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Iterable, Mapping

from bridge.startup import ConfigError, is_placeholder

#: Minimum number of distinct people behind any crew-load figure.
#: Below this, an "overloaded pool" alert is really an alert about one person.
MIN_POOL_SIZE = 3

_SALT_ENV = "TURNAROUND_PSEUDONYM_SALT"

#: The one command that produces a working salt, defined once and quoted
#: wherever it is offered -- the error below and ``docs/SETUP.md``.
#:
#: It is a constant rather than three copies of a string because the last time
#: they were three copies they were all *wrong in the same way*: appending to a
#: ``.env`` copied from ``.env.example`` could not take effect, because a
#: repeated key took its first value and the example file already set this one
#: to ``change-me``. The instruction read correctly and did nothing. Nothing
#: catches that by reading; ``tests/test_startup.py`` catches it by running the
#: command against a copy of ``.env.example`` and checking a pseudonym comes
#: out the other side.
SALT_COMMAND = f'echo "{_SALT_ENV}=$(openssl rand -hex 16)" >> .env'
_PSEUDONYM_LENGTH = 8


class PrivacyViolation(RuntimeError):
    """Raised when data that could identify an individual reaches an exporter."""


class MissingSalt(PrivacyViolation, ConfigError):
    """The pseudonym salt is unset or still example text.

    Both parents are load-bearing. It is a `PrivacyViolation` because refusing
    to emit is a privacy decision, and callers that already guard on that keep
    working. It is also a `ConfigError` because, unlike every other
    `PrivacyViolation`, the cause is a line nobody filled in rather than a leak
    -- which lets `bridge.startup.reporting` turn it into one sentence at an
    entry point without also swallowing a real `assert_no_pii` failure.
    """


def _salt() -> bytes:
    salt = (os.environ.get(_SALT_ENV) or "").strip()
    if not salt or is_placeholder(salt):
        raise MissingSalt(
            f"{_SALT_ENV} is unset or still the placeholder. Refusing to emit "
            "artist telemetry with a guessable pseudonym salt: without a real "
            "salt the pseudonyms are reversible by anyone who can list the crew.\n"
            "  generate one and append it to .env:\n"
            f"    {SALT_COMMAND}"
        )
    return salt.encode()


def pseudonymize(artist_id: str) -> str:
    """Stable, non-reversible pseudonym for one person.

    HMAC rather than a bare hash: the artist id space is small enough that a
    plain digest could be brute-forced from a crew list in seconds.
    """
    if not artist_id:
        raise ValueError("artist_id is required")
    digest = hmac.new(_salt(), artist_id.encode(), hashlib.sha256).hexdigest()
    return digest[:_PSEUDONYM_LENGTH]


# Anything that looks like a human name, an email, or a Kitsu UUID must not be
# exported. Kept deliberately broad: a false positive costs a label, a false
# negative costs someone their job.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", re.IGNORECASE)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
_PII_KEY_RE = re.compile(
    r"(^|[._-])(email|mail|first_?name|last_?name|full_?name|phone|person_id|user_id)($|[._-])",
    re.IGNORECASE,
)


def assert_no_pii(attributes: Mapping[str, object], *, where: str = "export") -> None:
    """Fail loudly if an attribute bag carries identifying data.

    Called on the way out to the OTLP exporter. It is intentionally a hard
    failure: silently dropping a field would let a regression ship unnoticed.
    """
    for key, value in attributes.items():
        if _PII_KEY_RE.search(key):
            raise PrivacyViolation(f"{where}: attribute {key!r} may identify a person")
        if not isinstance(value, str):
            continue
        if _EMAIL_RE.search(value):
            raise PrivacyViolation(f"{where}: attribute {key!r} contains an email address")
        if _UUID_RE.search(value):
            raise PrivacyViolation(
                f"{where}: attribute {key!r} contains a raw source id; pseudonymize it"
            )


def may_report_crew_load(pool_members: Iterable[str], *, minimum: int = MIN_POOL_SIZE) -> bool:
    """Whether a crew-load figure for this pool may be surfaced at all."""
    return len(set(pool_members)) >= minimum


def aggregation_floor[T](
    groups: Mapping[T, Iterable[str]],
    *,
    minimum: int = MIN_POOL_SIZE,
) -> dict[T, int]:
    """Keep only groups large enough to talk about without naming an individual.

    `groups` maps a pool key (department, vendor, sequence) to the artist
    pseudonyms in it. Returns the surviving pools and their headcount; small
    pools are dropped entirely rather than merged, because merging two small
    pools into an "other" bucket still lets a supervisor infer who is in it.
    """
    kept: dict[T, int] = {}
    for key, members in groups.items():
        people = set(members)
        if may_report_crew_load(people, minimum=minimum):
            kept[key] = len(people)
    return kept

