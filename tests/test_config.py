"""Runtime configuration: ``.env`` loading and the single resolved ``Settings``.

``agent/config.py`` decides which model runs, how many Gemini calls a run may
make, which MCP mode is used and where the binary is. Every one of those is
read from the environment *after* ``.env`` is loaded, so none of it can be a
module-level constant -- a constant freezes at import time, which is before
``load_env()`` has run, and a value set only in ``.env`` would be silently
ignored. These tests hold that line.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from agent import config
from agent.config import (
    DEFAULT_ANALYST_MODEL,
    DEFAULT_MAX_LLM_CALLS,
    Settings,
    _find_mcp_grafana,
    _int_env,
    _signed_int_env,
    bootstrap_vertex,
    load_env,
    settings,
)
from bridge import dotenv

ENV_KEYS = (
    "GRAFANA_URL", "GRAFANA_SERVICE_ACCOUNT_TOKEN", "GRAFANA_CLOUD_MCP_TOKEN",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI", "GRAFANA_DS_PROM_UID", "GRAFANA_DS_LOKI_UID",
    "GRAFANA_DS_TEMPO_UID", "TURNAROUND_MCP_MODE", "TURNAROUND_GEMINI_MODEL",
    "TURNAROUND_MAX_LLM_CALLS", "TURNAROUND_MCP_GRAFANA_BIN",
    # The public-endpoint limits and the salt belong here for the same reason as
    # the rest: a value left set by another test, or by `tests/conftest.py`,
    # would let `.env` parsing look correct when it is not.
    "TURNAROUND_ASKS_PER_HOUR", "TURNAROUND_MAX_LLM_CALLS",
    "TURNAROUND_CONCURRENT_ASKS", "TURNAROUND_PSEUDONYM_SALT",
    "OTEL_EXPORTER_OTLP_HEADERS",
)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """A process with none of Turnaround's variables set and no repo ``.env``.

    ``settings()`` reads the repo's real ``.env``; point it at an empty tmp dir
    so a developer's local credentials cannot make a test pass or fail.

    Both ``REPO_ROOT``s are redirected. ``load_env`` lives in ``bridge.dotenv``
    now (so ``seed.*`` and ``grafana.*`` can use it too) and resolves the path
    against *that* module's global -- patching only ``agent.config.REPO_ROOT``
    silently stopped working, and the suite quietly began reading the developer's
    real ``.env``. ``tests/conftest.py`` holds the backstop that made that
    visible instead of subtle.
    """
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(dotenv, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "CLOUD_MCP_TOKEN_FILE", tmp_path / ".secrets" / "token")
    return tmp_path


def _write_env(root: Path, body: str) -> Path:
    path = root / ".env"
    path.write_text(body)
    return path


# --------------------------------------------------------------------------- #
# load_env
# --------------------------------------------------------------------------- #


def test_load_env_populates_unset_keys(clean_env, monkeypatch):
    _write_env(clean_env, "GRAFANA_URL=https://stack.grafana.net\n")
    load_env()
    assert os.environ["GRAFANA_URL"] == "https://stack.grafana.net"


def test_a_real_exported_variable_always_wins(clean_env, monkeypatch):
    """CI and Cloud Run export the truth; ``.env`` is only a local fallback."""
    monkeypatch.setenv("GRAFANA_URL", "https://exported.example")
    _write_env(clean_env, "GRAFANA_URL=https://from-dotenv.example\n")
    load_env()
    assert os.environ["GRAFANA_URL"] == "https://exported.example"


def test_comments_blanks_and_malformed_lines_are_skipped(clean_env):
    _write_env(clean_env, "\n# a comment\nNOT_A_PAIR\n  \nGRAFANA_URL=https://ok\n")
    load_env()
    assert os.environ["GRAFANA_URL"] == "https://ok"
    assert "NOT_A_PAIR" not in os.environ


def test_surrounding_quotes_are_stripped_but_the_value_is_literal(clean_env):
    """An OTLP auth header is a literal: no interpolation, no unescaping."""
    _write_env(clean_env, 'GRAFANA_SERVICE_ACCOUNT_TOKEN="glsa_$NOT_EXPANDED_x"\n')
    load_env()
    assert os.environ["GRAFANA_SERVICE_ACCOUNT_TOKEN"] == "glsa_$NOT_EXPANDED_x"


def test_the_last_value_of_a_repeated_key_wins(clean_env):
    """Because this is the documented way to set the salt:

        echo "TURNAROUND_PSEUDONYM_SALT=$(openssl rand -hex 16)" >> .env

    and a ``.env`` copied from ``.env.example`` already carries that key set to
    ``change-me``. With first-wins the append did nothing, the entry point
    refused to start, and the message it printed was the very command the user
    had just run.
    """
    _write_env(clean_env,
               "TURNAROUND_PSEUDONYM_SALT=change-me\n"
               "TURNAROUND_PSEUDONYM_SALT=2b5f9c0ad41e7a63\n")
    load_env()
    assert os.environ["TURNAROUND_PSEUDONYM_SALT"] == "2b5f9c0ad41e7a63"


def test_an_exported_variable_still_beats_the_last_line(clean_env, monkeypatch):
    """Last-wins is about the file's own lines, not about the environment."""
    monkeypatch.setenv("GRAFANA_URL", "https://exported.example")
    _write_env(clean_env, "GRAFANA_URL=https://first\nGRAFANA_URL=https://second\n")
    load_env()
    assert os.environ["GRAFANA_URL"] == "https://exported.example"


class TestTrailingComments:
    """``TURNAROUND_ASKS_PER_HOUR=8      # per visitor`` used to parse as the
    whole string. ``_int_env`` answers an unparseable value with the default, so
    the three rate limits shipped in ``.env.example`` were inert -- and a user
    lowering the daily budget to bound a real bill silently kept 200. Nothing
    looked wrong, because the defaults matched the file.
    """

    def test_a_spaced_hash_starts_a_comment(self, clean_env):
        _write_env(clean_env, "TURNAROUND_MAX_LLM_CALLS=50      # everyone together\n")
        load_env()
        assert os.environ["TURNAROUND_MAX_LLM_CALLS"] == "50"

    def test_the_limit_actually_reaches_the_settings(self, clean_env):
        """The point of the fix, rather than the mechanism of it."""
        _write_env(clean_env, "TURNAROUND_MAX_LLM_CALLS=50     # a longer run\n")
        load_env()
        assert settings().max_llm_calls == 50

    def test_a_hash_with_no_space_before_it_is_part_of_the_value(self, clean_env):
        """An OTLP base64 payload may contain one, and it is not a comment."""
        _write_env(clean_env, "OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20ab#cd\n")
        load_env()
        assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Basic%20ab#cd"

    def test_a_quoted_value_keeps_everything_inside_the_quotes(self, clean_env):
        _write_env(clean_env, 'GRAFANA_SERVICE_ACCOUNT_TOKEN="glsa_a b # c"   # mine\n')
        load_env()
        assert os.environ["GRAFANA_SERVICE_ACCOUNT_TOKEN"] == "glsa_a b # c"


def test_a_missing_env_file_is_not_an_error(clean_env):
    load_env()  # no .env written
    assert "GRAFANA_URL" not in os.environ


def test_an_equals_sign_in_the_value_survives(clean_env):
    """base64 payloads in ``OTEL_EXPORTER_OTLP_HEADERS`` end in ``=`` padding."""
    _write_env(clean_env, "GRAFANA_SERVICE_ACCOUNT_TOKEN=Basic dXNlcjpwYXNz==\n")
    load_env()
    assert os.environ["GRAFANA_SERVICE_ACCOUNT_TOKEN"] == "Basic dXNlcjpwYXNz=="


# --------------------------------------------------------------------------- #
# The regression this module exists to prevent
# --------------------------------------------------------------------------- #


def test_dotenv_actually_drives_the_model_the_ceiling_and_the_mode(clean_env):
    """R4 + R7 regression: a knob set only in ``.env`` must reach ``Settings``.

    These three were once module-level constants read at import time, i.e.
    before ``load_env()`` ran, so ``.env`` set them and nothing used them: a run
    silently used flash and a ceiling of 40 while ``.env`` said otherwise.
    """
    _write_env(clean_env, "TURNAROUND_GEMINI_MODEL=gemini-2.5-pro\n"
                          "TURNAROUND_MAX_LLM_CALLS=7\n"
                          "TURNAROUND_MCP_MODE=hosted\n")
    cfg = settings()
    assert cfg.analyst_model == "gemini-2.5-pro"
    assert cfg.max_llm_calls == 7
    assert cfg.mcp_mode == "hosted"


def test_no_module_constant_reads_the_environment_at_import(clean_env):
    """The structural guard behind the test above.

    ``agent/config.py`` must not call ``os.environ`` at module level: anything
    it assigns there is fixed before ``load_env()`` runs. The four names below
    were exactly that, and each one silently ignored its ``.env`` value.
    """
    for gone in ("ANALYST_MODEL", "PRODUCER_MODEL", "MAX_LLM_CALLS", "MCP_MODE"):
        assert not hasattr(config, gone), (
            f"agent.config.{gone} is back as a module constant; it freezes at "
            "import, before load_env(), so .env cannot reach it. Put it on Settings."
        )

    # Generalised: no module-level assignment anywhere in the file may read the
    # environment. Inside a function body is fine -- that runs after load_env().
    module_level_assignments = [
        node for node in ast.parse(Path(config.__file__).read_text()).body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    reads_env = [
        node.lineno for node in module_level_assignments
        for sub in ast.walk(node)
        if isinstance(sub, ast.Attribute) and sub.attr == "environ"
    ]
    assert not reads_env, f"os.environ read at import time on line(s) {reads_env}"


def test_settings_defaults_when_nothing_is_configured(clean_env):
    cfg = settings()
    assert cfg.analyst_model == DEFAULT_ANALYST_MODEL
    assert cfg.max_llm_calls == DEFAULT_MAX_LLM_CALLS
    assert cfg.mcp_mode == "oss"
    assert cfg.vertex_ready is False
    assert cfg.grafana_ready is False


# --------------------------------------------------------------------------- #
# Individual knobs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("raw", "expected"), [
    ("12", 12),
    (" 12 ", 12),
    ("0", DEFAULT_MAX_LLM_CALLS),      # a ceiling of zero would block every run
    ("-5", DEFAULT_MAX_LLM_CALLS),
    ("", DEFAULT_MAX_LLM_CALLS),
    ("many", DEFAULT_MAX_LLM_CALLS),
    ("4.5", DEFAULT_MAX_LLM_CALLS),
])
def test_int_env_falls_back_rather_than_raising(monkeypatch, raw, expected):
    monkeypatch.setenv("TURNAROUND_TEST_INT", raw)
    assert _int_env("TURNAROUND_TEST_INT", DEFAULT_MAX_LLM_CALLS) == expected


def test_an_empty_model_override_falls_back_to_the_default(clean_env, monkeypatch):
    monkeypatch.setenv("TURNAROUND_GEMINI_MODEL", "   ")
    assert settings().analyst_model == DEFAULT_ANALYST_MODEL


@pytest.mark.parametrize("raw", ["hosted", "HOSTED", " Hosted "])
def test_hosted_mode_is_matched_case_and_whitespace_insensitively(clean_env, monkeypatch, raw):
    monkeypatch.setenv("TURNAROUND_MCP_MODE", raw)
    cfg = settings()
    assert cfg.mcp_mode == "hosted"
    assert cfg.hosted_mcp is True


@pytest.mark.parametrize("raw", ["oss", "", "gibberish"])
def test_anything_that_is_not_hosted_is_oss(clean_env, monkeypatch, raw):
    """Fail safe: an unrecognised mode must land on the read-only, unattended
    path, never on the one with no ``--disable-write`` server behind it."""
    monkeypatch.setenv("TURNAROUND_MCP_MODE", raw)
    assert settings().mcp_mode == "oss"
    assert settings().hosted_mcp is False


def test_a_trailing_slash_on_the_grafana_url_is_removed(clean_env, monkeypatch):
    """It is concatenated into API paths and an ``X-Grafana-URL`` header."""
    monkeypatch.setenv("GRAFANA_URL", "https://stack.grafana.net/")
    assert settings().grafana_url == "https://stack.grafana.net"


def test_the_oauth_token_file_is_the_fallback_for_the_cloud_mcp_bearer(clean_env, monkeypatch):
    token_file = clean_env / ".secrets" / "token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("  from-the-login-flow\n")
    monkeypatch.setattr(config, "CLOUD_MCP_TOKEN_FILE", token_file)
    assert settings().grafana_cloud_mcp_token == "from-the-login-flow"

    monkeypatch.setenv("GRAFANA_CLOUD_MCP_TOKEN", "from-the-environment")
    assert settings().grafana_cloud_mcp_token == "from-the-environment"


def test_a_missing_token_file_is_an_empty_bearer_not_a_crash(clean_env):
    assert settings().grafana_cloud_mcp_token == ""


# --------------------------------------------------------------------------- #
# Readiness + the mcp-grafana binary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("project", "url", "token", "vertex", "grafana"), [
    ("proj", "https://s.net", "glsa_x", True, True),
    ("", "https://s.net", "glsa_x", False, True),
    ("proj", "", "glsa_x", True, False),
    ("proj", "https://s.net", "", True, False),
])
def test_readiness_flags_are_honest(project, url, token, vertex, grafana):
    """``/healthz`` publishes these; a false positive sends a reviewer hunting
    inside a tool call for what is really a missing variable."""
    cfg = Settings(grafana_url=url, grafana_token=token, gcp_project=project,
                   gcp_location="us-central1", mcp_grafana_bin="mcp-grafana")
    assert cfg.vertex_ready is vertex
    assert cfg.grafana_ready is grafana


def test_an_explicit_binary_override_wins_over_the_path(monkeypatch):
    monkeypatch.setenv("TURNAROUND_MCP_GRAFANA_BIN", "/custom/mcp-grafana")
    assert _find_mcp_grafana() == "/custom/mcp-grafana"


def test_the_binary_falls_back_to_a_bare_name_so_the_error_names_it(monkeypatch):
    """``uv run`` does not inherit shell PATH additions. When nothing is found,
    return the bare name so the failure says ``mcp-grafana``, not ``None``."""
    monkeypatch.delenv("TURNAROUND_MCP_GRAFANA_BIN", raising=False)
    monkeypatch.setattr(config.shutil, "which", lambda _name: None)
    monkeypatch.setattr(config.Path, "is_file", lambda _self: False)
    assert _find_mcp_grafana() == "mcp-grafana"


# --------------------------------------------------------------------------- #
# bootstrap_vertex
# --------------------------------------------------------------------------- #


def test_bootstrap_forces_vertex_and_never_offers_the_public_api(clean_env, monkeypatch):
    """R4: the runtime is Vertex-only by deployment constraint, and the public
    Generative Language API is a different surface with different auth, quotas
    and data handling -- reaching it by accident is a real failure, not a
    nuance."""
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
    bootstrap_vertex()
    # forced on even when the environment explicitly asked for the other path
    assert os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "TRUE"

    source = Path(config.__file__).read_text()
    assert "GOOGLE_API_KEY" not in source
    assert "GEMINI_API_KEY" not in source


def test_bootstrap_defaults_the_location_but_respects_an_explicit_one(clean_env, monkeypatch):
    assert bootstrap_vertex().gcp_location == "us-central1"
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")
    assert bootstrap_vertex().gcp_location == "europe-west4"


def test_a_relative_credentials_path_is_resolved_against_the_repo(clean_env, monkeypatch):
    """``.env`` carries ``.secrets/sa.json``; the subprocess and the genai client
    both run with a different cwd, so a relative path would not resolve."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", ".secrets/sa.json")
    bootstrap_vertex()
    resolved = Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    assert resolved.is_absolute()
    assert resolved == clean_env / ".secrets" / "sa.json"


def test_an_absolute_credentials_path_is_left_alone(clean_env, monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/abs/sa.json")
    bootstrap_vertex()
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/abs/sa.json"


class TestAValueWeCannotUseSaysSo:
    """The silence here is why N-2 went unnoticed for the life of the project.

    A trailing comment made every rate limit unparseable, ``_int_env`` answered
    with the default, and the defaults happened to match the numbers in the
    file -- so nothing looked wrong anywhere. The parser bug is fixed; this is
    the reason it was *invisible*, and it would have hidden the next one too.
    """

    @pytest.fixture(autouse=True)
    def _forget_previous_warnings(self):
        config._REJECTED.clear()
        yield
        config._REJECTED.clear()

    def test_an_unset_value_is_not_a_mistake_and_says_nothing(self, clean_env, capsys):
        assert _int_env("TURNAROUND_MAX_LLM_CALLS", 40) == 40
        assert capsys.readouterr().err == ""

    def test_a_value_we_cannot_parse_names_itself_and_the_number_in_force(
        self, clean_env, monkeypatch, capsys
    ):
        monkeypatch.setenv("TURNAROUND_MAX_LLM_CALLS", "50     # a longer run")
        assert _int_env("TURNAROUND_MAX_LLM_CALLS", 40) == 40
        err = capsys.readouterr().err
        assert "TURNAROUND_MAX_LLM_CALLS" in err
        assert "50     # a longer run" in err   # quoted, so the cause is visible
        assert "40" in err                        # and what is actually in force

    def test_zero_where_a_positive_number_belongs_is_reported(
        self, clean_env, monkeypatch, capsys
    ):
        """Someone setting 0 to mean "no limit" gets 200 and needs to know."""
        monkeypatch.setenv("TURNAROUND_MAX_LLM_CALLS", "0")
        assert _int_env("TURNAROUND_MAX_LLM_CALLS", 40) == 40
        assert "greater than zero" in capsys.readouterr().err

    def test_it_is_said_once_not_once_per_request(self, clean_env, monkeypatch, capsys):
        """Anything long-lived resolves settings more than once -- the HTTP
        service this used to ship with did it per request."""
        monkeypatch.setenv("TURNAROUND_MAX_LLM_CALLS", "lots")
        for _ in range(5):
            _int_env("TURNAROUND_MAX_LLM_CALLS", 40)
        assert capsys.readouterr().err.count("TURNAROUND_MAX_LLM_CALLS") == 1

    def test_a_different_bad_value_is_reported_again(
        self, clean_env, monkeypatch, capsys
    ):
        """Deduping on the name alone would swallow the next mistake."""
        monkeypatch.setenv("TURNAROUND_MAX_LLM_CALLS", "lots")
        _int_env("TURNAROUND_MAX_LLM_CALLS", 40)
        monkeypatch.setenv("TURNAROUND_MAX_LLM_CALLS", "loads")
        _int_env("TURNAROUND_MAX_LLM_CALLS", 40)
        assert capsys.readouterr().err.count("TURNAROUND_MAX_LLM_CALLS") == 2

    def test_the_thinking_budget_keeps_its_three_meanings(
        self, clean_env, monkeypatch, capsys
    ):
        """0 (off) and -1 (dynamic) are values, not rejects; -2 is neither."""
        for raw, expected in (("0", 0), ("-1", -1), ("256", 256)):
            monkeypatch.setenv("TURNAROUND_THINKING_BUDGET", raw)
            assert _signed_int_env("TURNAROUND_THINKING_BUDGET", 99) == expected
        assert capsys.readouterr().err == ""
        monkeypatch.setenv("TURNAROUND_THINKING_BUDGET", "-2")
        assert _signed_int_env("TURNAROUND_THINKING_BUDGET", 99) == 99
        assert "-1 (dynamic)" in capsys.readouterr().err


class TestTheParserHandlesTheShapesPeopleWrite:
    """Cases found by probing the phase-2 parser rather than by reading it."""

    def test_a_hash_inside_a_value_does_not_protect_the_real_comment(self, clean_env):
        """Partitioning on the *first* ``#`` stopped at the payload's own one
        and left ``   # my token`` in an Authorization header."""
        _write_env(clean_env, "OTEL_EXPORTER_OTLP_HEADERS=Basic%20ab#cd   # my token\n")
        load_env()
        assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == "Basic%20ab#cd"

    def test_an_unterminated_quote_is_syntax_not_data(self, clean_env):
        """A stray ``"`` riding into a token is a 401 nobody can explain."""
        _write_env(clean_env, 'GRAFANA_SERVICE_ACCOUNT_TOKEN="glsa_abc\n')
        load_env()
        assert os.environ["GRAFANA_SERVICE_ACCOUNT_TOKEN"] == "glsa_abc"

    def test_an_exported_line_is_still_a_key(self, clean_env):
        """A ``.env`` is very often also sourced -- ``set -a && . ./.env`` is in
        this project's own docs -- and ``export KEY=value`` is valid there.
        Both readers of one file have to agree about what is in it."""
        _write_env(clean_env, "export GRAFANA_URL=https://stack.grafana.net\n")
        load_env()
        assert os.environ["GRAFANA_URL"] == "https://stack.grafana.net"

    def test_a_key_that_merely_starts_with_export_is_untouched(self, clean_env):
        _write_env(clean_env, "GRAFANA_URL=https://ok\nexported=1\n")
        load_env()
        assert os.environ["exported"] == "1"
