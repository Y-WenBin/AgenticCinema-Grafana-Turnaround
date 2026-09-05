"""Push production events to Grafana as annotations.

A director note or a date change is not a metric and not a log line -- it is a
mark on every chart, the thing a producer reads the burndown *against*. Grafana
models exactly that as an annotation, so the ontology maps them straight across
([`bridge/ontology.py`](ontology.py) section 4).

Annotations go through the Grafana HTTP API rather than OTLP -- there is no OTLP
annotation signal -- so this needs `GRAFANA_URL` and a service-account token.
The timestamp passes through the same [`timewarp`](timewarp.py) as every span
and sample, so the note lands where the compressed history puts it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

import requests

from bridge import timewarp


@dataclass(frozen=True, slots=True)
class Mark:
    """A point-in-time production event: a note, a cut change, a date slip."""

    at: object  # datetime; kept loose so seed.model.Annotation slots straight in
    text: str
    tags: tuple[str, ...]


class Annotator:
    """Writes annotations to a Grafana stack, or nowhere if unconfigured."""

    def __init__(self, *, session: requests.Session | None = None) -> None:
        self._url = os.environ.get("GRAFANA_URL", "").rstrip("/")
        self._token = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "")
        self._session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self._url and self._token)

    def write(self, marks: Iterable[Mark]) -> int:
        """Post each mark. Returns the number accepted."""
        if not self.enabled:
            raise RuntimeError(
                "GRAFANA_URL / GRAFANA_SERVICE_ACCOUNT_TOKEN unset; annotations need the "
                "Grafana HTTP API. Copy .env.example and fill them in."
            )
        written = 0
        for mark in marks:
            when = timewarp.apply(mark.at)
            resp = self._session.post(
                f"{self._url}/api/annotations",
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "time": int(when.timestamp() * 1000),
                    "timeEnd": int(when.timestamp() * 1000),
                    "text": mark.text,
                    "tags": list(mark.tags),
                },
                timeout=15,
            )
            resp.raise_for_status()
            written += 1
        return written
