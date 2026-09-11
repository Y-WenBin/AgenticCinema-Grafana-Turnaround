"""Every response carries the security headers, and the CSP is one the page
can actually run under.

A CSP that breaks the playground gets deleted the first time someone demos, so
the interesting assertions here are not "a header is present" but "this header
and this page agree": no inline `<script>`, no inline `on*=` handler, and no
off-origin asset that `default-src 'self'` would silently drop.
"""

from __future__ import annotations

import importlib
import re

import pytest
from fastapi.testclient import TestClient

from agent import serve as serve_mod
from agent.config import REPO_ROOT
from agent.limits import Decision

HTML = (REPO_ROOT / "web" / "index.html").read_text()


@pytest.fixture
def client():
    return TestClient(serve_mod.app)


def _csp(response) -> dict[str, str]:
    """The CSP parsed into directive -> value."""
    parts = [p.strip() for p in response.headers["content-security-policy"].split(";")]
    return {p.split(" ", 1)[0]: p.split(" ", 1)[1] if " " in p else "" for p in parts}


# --------------------------------------------------------------------------- #
# Coverage: every response, not just the happy path
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/", "/health", "/api/capacity", "/app.js",
                                  "/favicon.svg", "/no-such-route"])
def test_every_response_carries_the_headers(client, path):
    """Including the 404. A 404 renders in the same browser as a 200, and a
    header that is only right when nothing went wrong is not a control."""
    r = client.get(path, headers={"accept": "text/html"})
    for header in serve_mod.SECURITY_HEADERS:
        assert header in r.headers, f"{path} -> {r.status_code} is missing {header}"


def test_a_rate_limited_refusal_carries_them_too(client, monkeypatch):
    """429 is the response a scripted caller sees most, so it is the one most
    worth not leaking framing or sniffing protection on."""
    refused = Decision(allowed=False, reason="daily_budget",
                       message="the demo's daily budget is spent", retry_after=60)
    monkeypatch.setattr(serve_mod.gate, "admit", lambda _client: refused)

    r = client.post("/ask", json={"question": "why is SEQ0420 slipping?"})
    assert r.status_code == 429
    assert r.headers["retry-after"] == "60", "the refusal's own headers survive"
    assert r.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in r.headers


# --------------------------------------------------------------------------- #
# The CSP is worth having
# --------------------------------------------------------------------------- #


def test_script_src_is_self_with_no_escape_hatch(client):
    """`'unsafe-inline'` on script-src would make the whole policy decorative --
    it is the one directive that has to hold for the CSP to be a second wall
    behind esc()."""
    csp = _csp(client.get("/"))
    assert csp["script-src"] == "'self'"
    assert "unsafe-eval" not in csp["script-src"]


def test_the_page_has_no_inline_script_for_the_policy_to_block():
    """The CSP and web/index.html have to agree, and nothing in Python enforces
    that -- the page is a static file. An inline <script> or an on*= handler
    would be silently dead in the browser under this policy."""
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", HTML), "inline <script> in the page"
    assert not re.search(r"<[^>]+\son(?:click|load|error|submit|focus)\s*=", HTML), \
        "inline event handler in the page"


def test_no_off_origin_subresource_that_default_src_self_would_drop():
    """No CDN, no font host, no analytics. `default-src 'self'` costs nothing
    today; this is the test that notices the day it starts costing something.

    Subresources only. A plain `<a href>` to the GitHub repo is a navigation --
    CSP does not govern where a link points, and `form-action 'none'` is what
    stops the page submitting anywhere.
    """
    subresources = re.findall(r'<(?:script|img|iframe|source)\b[^>]*\bsrc="([^"]+)"', HTML)
    subresources += re.findall(r'<link\b[^>]*\bhref="([^"]+)"', HTML)
    off_origin = [u for u in subresources if re.match(r"https?://", u)]
    assert off_origin == [], off_origin


def test_the_frame_and_sniffing_controls_are_both_set(client):
    r = client.get("/")
    assert _csp(r)["frame-ancestors"] == "'none'"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_a_route_may_still_set_its_own_header(client):
    """The middleware fills gaps; it does not overwrite. `/api/backend` and the
    page set their own Cache-Control, and that must survive."""
    assert "no-store" in client.get("/", headers={"accept": "text/html"}) \
        .headers["cache-control"]


# --------------------------------------------------------------------------- #
# Interactive docs are a switch, not a judgement call
# --------------------------------------------------------------------------- #


def test_docs_are_mounted_by_default(client):
    assert client.get("/openapi.json").status_code == 200
    assert serve_mod.SERVICE["endpoints"].get("GET /docs")


def test_docs_can_be_turned_off_for_a_deployment(monkeypatch):
    """Off means *gone* -- not a redirect, not a 401 that still confirms the
    schema exists -- and the service description must stop advertising it."""
    monkeypatch.setenv("TURNAROUND_PUBLIC_DOCS", "0")
    module = importlib.reload(serve_mod)
    try:
        client = TestClient(module.app)
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404, path
        assert "GET /docs" not in module.SERVICE["endpoints"]
        assert client.get("/health").status_code == 200, "the service still works"
    finally:
        monkeypatch.delenv("TURNAROUND_PUBLIC_DOCS")
        importlib.reload(serve_mod)
