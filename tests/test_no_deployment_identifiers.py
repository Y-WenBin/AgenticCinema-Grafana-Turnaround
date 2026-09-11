"""No file in this repo names the deployment it was developed against.

None of these are credentials. They are *reconnaissance*: a stack hostname
points at a login page and an OTLP ingest endpoint, and a GCP project id is the
other half of several Google API calls. `agent/serve.py` already refuses to put
`grafana_url` in an `/ask` response for exactly this reason -- committing the
same string to a public repo would give it away anyway.

The test is written as shape rules rather than a blocklist of the specific
strings that leaked once, so it catches the *next* one too.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from agent.config import REPO_ROOT

#: Placeholders and test doubles. `stack.grafana.net` is the fixture hostname
#: the suite uses everywhere; `<name>`/`<your-stack>`/`YOUR_PROJECT_ID` are what
#: the docs tell a reader to replace.
_ALLOWED_HOSTS = {
    "stack.grafana.net",                      # the suite's fixture hostname
    "otlp-gateway-prod-<region>.grafana.net", # the docs' placeholder
    "quietfalcon9931.grafana.net",            # invented; see the detector test
}

#: Documented example values and invented fixtures, not anybody's real project.
#: Every literal that is permitted to look real is named here -- the allowlist
#: is the control, and adding to it should feel like a decision.
_ALLOWED_PROJECTS = {"other-proj", "amber-meadow-114829-k2"}

_GRAFANA_HOST = re.compile(r"\b([A-Za-z0-9][\w-]*)\.grafana\.net\b")
#: A Google Cloud project id where one is actually being *supplied*: an
#: assignment, a `--project` flag, or a backticked value. Prose containing the
#: word "project" is not a leak, so the context has to be narrow. The value must
#: also be hyphenated, which every real GCP project id is and ordinary English
#: words are not.
_GCP_PROJECT = re.compile(
    r"(?:GOOGLE_CLOUD_PROJECT|PROJECT_ID|--project[= ]|project[= ]+`)"
    r"[=\s:`\"']*([a-z][a-z0-9-]{4,28}[a-z0-9])\b")
_PLACEHOLDER = re.compile(r"^(?:<[^>]+>|your[-_].*|my[-_].*|proj|test.*|example.*|"
                          r"YOUR_PROJECT_ID|PROJECT_ID|\$\{?[A-Z_]+\}?)$", re.IGNORECASE)


def _tracked_text_files() -> list[str]:
    """Everything git tracks. Untracked and ignored files are a developer's own
    business -- `.env` and `grafana/ml/jobs.json` legitimately hold the real
    values, which is why both are git-ignored."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True)
    skip = {".png", ".svg", ".lock", ".ico", ".jpg", ".gz"}
    return [p for p in out.stdout.split("\0")
            if p and not any(p.endswith(s) for s in skip)]


@pytest.fixture(scope="module")
def tracked() -> list[tuple[str, str]]:
    files = []
    for rel in _tracked_text_files():
        try:
            files.append((rel, (REPO_ROOT / rel).read_text()))
        except (OSError, UnicodeDecodeError):
            continue
    return files


def test_no_real_grafana_stack_hostname_is_committed(tracked):
    found = {}
    for rel, text in tracked:
        for match in _GRAFANA_HOST.finditer(text):
            host = match.group(0)
            if host in _ALLOWED_HOSTS or _PLACEHOLDER.match(match.group(1)):
                continue
            found.setdefault(host, []).append(rel)
    assert not found, (
        f"a real Grafana stack hostname is committed: {found}. Use a placeholder "
        "and read the true value from GRAFANA_URL at runtime.")


def test_no_real_gcp_project_id_is_committed(tracked):
    found = {}
    for rel, text in tracked:
        for match in _GCP_PROJECT.finditer(text):
            pid = match.group(1)
            if _PLACEHOLDER.match(pid) or "-" not in pid or pid in _ALLOWED_PROJECTS:
                continue
            found.setdefault(pid, []).append(rel)
    assert not found, (
        f"a real GCP project id is committed: {found}. Use YOUR_PROJECT_ID and "
        "read GOOGLE_CLOUD_PROJECT at runtime.")


def test_the_generated_ml_jobs_file_is_not_tracked():
    """`grafana/ml/build.py` resolves the stack hostname and the numeric
    datasource id into `jobs.json`. That file is a local artifact --
    `grafana/provision.py` builds the jobs in-process and never reads it."""
    assert "grafana/ml/jobs.json" not in _tracked_text_files()


def test_the_ml_jobs_take_the_stack_from_the_environment(monkeypatch):
    """The positive half of the rule above: not merely 'no hostname committed'
    but 'the hostname comes from somewhere real'."""
    from grafana.ml import build

    monkeypatch.setenv("GRAFANA_URL", "https://whatever.example/")
    assert build.grafana_url() == "https://whatever.example"   # trailing / trimmed
    monkeypatch.delenv("GRAFANA_URL")
    assert build.grafana_url() == ""                           # no literal fallback
    assert not hasattr(build, "DS_ID"), "the hard-coded datasource id is back"


def test_the_datasource_id_is_resolved_not_assumed(monkeypatch):
    """Hard-coding it made provisioning target a datasource that exists on one
    stack and nowhere else -- a fork's ML jobs failed silently."""
    from grafana.ml import build

    monkeypatch.setenv("GRAFANA_DS_PROM_ID", "42")
    assert build.datasource_id() == 42


# --------------------------------------------------------------------------- #
# The detectors work
# --------------------------------------------------------------------------- #


def test_the_detectors_catch_a_realistic_leak():
    """A scrub test that cannot fail is worse than no scrub test.

    The fixtures below are *invented*, deliberately: writing the values this
    repo actually published into a test file would re-commit them, and a
    detector that only recognises one known string is a blocklist, not a rule.
    These have the same shape -- Grafana Cloud's two-word-plus-digits stack
    names, and a GCP project id with its auto-appended suffix -- which is why
    the scanners above flag them, and why both are named in the allowlists at
    the top of this file. That allowlist is the actual control: every literal
    permitted to look real is written down in one place.
    """
    match = _GRAFANA_HOST.search("https://quietfalcon9931.grafana.net")
    assert match, "the hostname detector no longer matches a real stack name"
    # The shape check the scanner relies on: a real stack name must not look
    # like a placeholder, or every leak would be waved through as one.
    assert not _PLACEHOLDER.match(match.group(1))

    match = _GCP_PROJECT.search("GOOGLE_CLOUD_PROJECT=amber-meadow-114829-k2")
    assert match, "the project-id detector no longer matches an assignment"
    pid = match.group(1)
    assert "-" in pid and not _PLACEHOLDER.match(pid)
