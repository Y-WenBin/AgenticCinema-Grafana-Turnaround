"""Annotations are the only telemetry that goes over the Grafana HTTP API
rather than OTLP, and the only one whose timestamp is milliseconds, not nanos."""

from datetime import UTC, datetime, timedelta

import pytest

from bridge import timewarp
from bridge.annotate import Annotator, Mark

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class _FakeResponse:
    status_code = 200

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _FakeResponse()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GRAFANA_URL", "https://stack.grafana.net/")
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "glsa_x")


def test_disabled_without_credentials(monkeypatch):
    monkeypatch.delenv("GRAFANA_URL", raising=False)
    monkeypatch.delenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", raising=False)
    assert not Annotator().enabled
    with pytest.raises(RuntimeError):
        Annotator().write([Mark(NOW, "note", ("x",))])


def test_posts_one_annotation_per_mark_in_milliseconds(configured):
    session = _FakeSession()
    n = Annotator(session=session).write(
        [Mark(NOW, "act three recut", ("director-note", "SEQ0420"))]
    )
    assert n == 1
    url, kwargs = session.posts[0]
    assert url == "https://stack.grafana.net/api/annotations"
    body = kwargs["json"]
    assert body["time"] == int(NOW.timestamp() * 1000)
    assert body["time"] == body["timeEnd"]
    assert body["tags"] == ["director-note", "SEQ0420"]
    assert kwargs["headers"]["Authorization"] == "Bearer glsa_x"


def test_timestamp_passes_through_the_active_timewarp(configured):
    timewarp.configure(
        anchor=NOW, earliest=NOW - timedelta(days=150), window=timedelta(minutes=45)
    )
    session = _FakeSession()
    Annotator(session=session).write([Mark(NOW - timedelta(days=150), "old note", ())])
    body = session.posts[0][1]["json"]
    # earliest -> anchor - window
    assert body["time"] == int((NOW - timedelta(minutes=45)).timestamp() * 1000)
