"""A half-filled ``.env`` must stop the run, and say so in one sentence.

The failure this guards against is the most common one in the project and it
used to be invisible: `.env.example` ships `<project-id>` and
`https://<your-stack>.grafana.net`, both non-empty, so every `bool(value)` check
passed and the run proceeded until something far downstream failed on a name
that does not resolve. Three documented commands, three tracebacks, none naming
the cause.

So the contract is two-part and both halves are tested here: a placeholder is
erased to the empty string (so the emptiness checks that already existed do the
work), and an entry point reports the result as a sentence with an exit code.
"""

from __future__ import annotations

import re

import pytest

# The real repository root, bound at import before ``tests/conftest.py``
# repoints ``dotenv.REPO_ROOT`` at a tmpdir. That fixture exists so no test
# reads the developer's ``.env``; this one has to read the checked-in
# ``.env.example``, which is a tracked file rather than a secret.
from bridge.dotenv import REPO_ROOT
from bridge.startup import ConfigError, is_placeholder, real, reporting, require


class TestPlaceholders:
    @pytest.mark.parametrize("value", [
        "<project-id>",
        "https://<your-stack>.grafana.net",
        "<region>",
        "change-me",
        "change_me",
        "CHANGE-ME",
        "your-stack",
        "your_token",
        "YOUR_PROJECT_ID",
        "$(openssl rand -hex 16)",   # docs/SETUP.md showed this inside a .env block
        "xxxxx",
        "TODO",
        "  <project-id>  ",          # whitespace is not a value either
    ])
    def test_example_text_is_not_a_value(self, value):
        assert is_placeholder(value) is True
        assert real(value) == ""

    @pytest.mark.parametrize("value", [
        "amber-meadow-114829-k2",
        "https://stack.grafana.net",
        "glsa_aBcD1234",
        "grafanacloud-prom",
        "us-central1",
        "gemini-2.5-flash",
        "a1b2c3d4e5f60718",
        # Adversarial but legitimate: a real secret may contain any of these as
        # a substring. The pattern is anchored, so only a whole-value match counts.
        "yourtoken-but-actually-real",
        "prefix-change-me-suffix",
    ])
    def test_a_real_value_survives_untouched(self, value):
        assert is_placeholder(value) is False
        assert real(value) == value

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_blank_is_erased_but_is_not_called_a_placeholder(self, value):
        """`is_placeholder` answers "is this example text", which an empty
        string is not. Both still collapse to "" -- that is `real`'s job."""
        assert is_placeholder(value) is False
        assert real(value) == ""


class TestRequire:
    def test_names_every_unfilled_key_at_once(self):
        """One run, one list. Naming the first missing key and stopping makes a
        reader fix, re-run, and discover the next one."""
        with pytest.raises(ConfigError) as exc:
            require(GRAFANA_URL="https://<your-stack>.grafana.net",
                    GRAFANA_SERVICE_ACCOUNT_TOKEN="",
                    GOOGLE_CLOUD_PROJECT="amber-meadow-114829-k2")
        message = str(exc.value)
        assert "GRAFANA_URL" in message
        assert "GRAFANA_SERVICE_ACCOUNT_TOKEN" in message
        assert "GOOGLE_CLOUD_PROJECT" not in message   # this one was filled in

    def test_points_at_the_file_and_the_document_that_explains_it(self):
        with pytest.raises(ConfigError) as exc:
            require(GRAFANA_URL="")
        message = str(exc.value)
        assert ".env.example" in message
        assert "docs/SETUP.md" in message

    def test_silence_when_everything_is_real(self):
        require(GRAFANA_URL="https://stack.grafana.net", TOKEN="glsa_x")


class TestReporting:
    def test_a_config_error_becomes_a_sentence_and_exit_2(self, capsys):
        with pytest.raises(SystemExit) as exc, reporting():
            require(GRAFANA_URL="<your-stack>")
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "GRAFANA_URL" in err
        assert "Traceback" not in err

    def test_an_unset_salt_is_a_config_error_not_a_traceback(self, capsys, monkeypatch):
        """The single most common first-run failure: `.env.example` ships
        `TURNAROUND_PSEUDONYM_SALT=change-me`, so the very first documented
        command after copying it used to end in twenty frames out of `hmac`."""
        from bridge.privacy import pseudonymize

        monkeypatch.setenv("TURNAROUND_PSEUDONYM_SALT", "change-me")
        with pytest.raises(SystemExit) as exc, reporting():
            pseudonymize("artist-1")
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "TURNAROUND_PSEUDONYM_SALT" in err
        assert "openssl rand" in err          # the fix is copy-pasteable

    def test_a_real_privacy_violation_is_not_swallowed_as_configuration(self):
        """The distinction the guard exists to preserve. An unset salt is a line
        to fill in; a crew name reaching an exporter is a bug, and it must keep
        its stack and escape the entry point rather than exiting 2 with a
        tidy sentence that reads like a typo."""
        from bridge.privacy import PrivacyViolation, assert_no_pii

        with pytest.raises(PrivacyViolation), reporting():
            assert_no_pii({"artist.email": "someone@studio.example"})

    def test_an_unexpected_error_keeps_its_stack(self):
        with pytest.raises(ZeroDivisionError), reporting():
            _ = 1 / 0

    def test_success_passes_straight_through(self):
        with reporting():
            value = real("https://stack.grafana.net")
        assert value == "https://stack.grafana.net"


class TestSettingsHonourPlaceholders:
    """The payoff: `Settings` already had the right checks, they just never
    fired. Nothing in `agent/config.py` knows about placeholders -- it calls
    `real()`, and the existing `grafana_ready` / `vertex_ready` do the rest."""

    def test_a_freshly_copied_env_reads_as_unconfigured(self, monkeypatch):
        from agent.config import settings

        monkeypatch.setenv("GRAFANA_URL", "https://<your-stack>.grafana.net")
        monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "glsa_xxx")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "<project-id>")

        cfg = settings()
        assert cfg.grafana_url == ""
        assert cfg.gcp_project == ""
        assert cfg.grafana_ready is False
        assert cfg.vertex_ready is False

    def test_resolved_settings_names_each_unfilled_key_separately(self, monkeypatch):
        """The old message joined two keys with a slash, which sent readers to
        check a line that was already right."""
        from agent import engine

        monkeypatch.setenv("GRAFANA_URL", "https://stack.grafana.net")
        monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "<project-id>")

        with pytest.raises(engine.NotConfigured) as exc:
            engine.resolved_settings()
        message = str(exc.value)
        assert "GOOGLE_CLOUD_PROJECT" in message
        assert "GRAFANA_SERVICE_ACCOUNT_TOKEN" in message
        assert "GRAFANA_URL," not in message      # this one is fine; don't accuse it

    def test_a_filled_in_env_resolves(self, monkeypatch):
        from agent import engine

        monkeypatch.setenv("GRAFANA_URL", "https://stack.grafana.net/")
        monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "glsa_real")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "amber-meadow-114829-k2")

        cfg = engine.resolved_settings()
        assert cfg.grafana_url == "https://stack.grafana.net"   # trailing / stripped
        assert cfg.grafana_ready and cfg.vertex_ready


class TestTheExampleFileIsHonest:
    """Every key in ``.env.example`` is a key something reads.

    ``.env.example`` is the first file a new user copies, so anything in it
    reads as a prerequisite. It carried five that nothing read, and two of them
    were expensive: ``MCP_GRAFANA_RO_URL`` and ``MCP_GRAFANA_RW_URL`` pointed at
    ``localhost:8000`` and ``:8001``, implying two MCP HTTP servers a user had
    to stand up -- when the design spawns ``mcp-grafana`` over stdio with no
    port at all. A false prerequisite in that file costs more than a missing
    one.
    """

    #: Only the packages that ship. A key read solely by a test is still dead
    #: weight in the file a user copies.
    SOURCES = ("agent", "bridge", "seed", "grafana", "observability", "deploy")

    @staticmethod
    def _keys() -> list[str]:
        text = (REPO_ROOT / ".env.example").read_text()
        keys = []
        for line in text.splitlines():
            line = line.strip().lstrip("#").strip()  # commented-out keys count
            match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
            if match:
                keys.append(match.group(1))
        return keys

    def test_the_file_parses_into_keys(self):
        keys = self._keys()
        assert len(keys) >= 10
        assert "GRAFANA_URL" in keys
        assert len(keys) == len(set(keys)), "a key appears twice"

    @pytest.mark.parametrize("key", _keys.__func__())
    def test_every_key_is_read_by_something_that_ships(self, key):
        haystack = ""
        for source in self.SOURCES:
            root = REPO_ROOT / source
            if root.is_dir():
                for path in root.rglob("*"):
                    if path.suffix in (".py", ".sh", ".yaml", ".yml") and path.is_file():
                        haystack += path.read_text()
        haystack += (REPO_ROOT / "Dockerfile").read_text()
        assert key in haystack, (
            f"{key} is in .env.example but nothing reads it -- delete it, or the "
            "file is telling new users to configure something that does nothing"
        )
