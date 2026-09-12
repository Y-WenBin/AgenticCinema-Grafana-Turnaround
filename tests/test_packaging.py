"""What ships in the wheel, and what an installed copy can still find.

The project used to be a git-checkout-only tool that was nonetheless built and
distributed as a package. `uv build && pip install` produced something where:

  * ``grafana.provision`` -- a documented setup step in both the README and
    docs/SETUP.md -- raised ``ModuleNotFoundError``, because the package was
    simply not in the wheel;
  * ``/app.js`` and ``/favicon.svg`` 500'd, because the playground is repo-root
    ``web/`` and a wheel has no repo root;
  * ``.env`` was looked for inside ``site-packages``, because ``REPO_ROOT``
    resolves to wherever the code was imported from.

All three worked perfectly from a checkout, which is exactly why none of them
was noticed. These tests assert the declarations that fix them; CI additionally
builds the wheel and drives the installed console scripts, because a manifest
being right on paper is not the same as a wheel being right.
"""

from __future__ import annotations

import tomllib

import pytest

from agent.config import REPO_ROOT
from bridge import dotenv

PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())


@pytest.fixture(scope="module")
def wheel() -> dict:
    return PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]


class TestWheelContents:
    def test_every_importable_package_ships(self, wheel):
        """A package that exists in the repo and is named by a documented
        command has to be in the wheel. `grafana` was not, for the whole life of
        the project, because nothing imported it from `agent/`."""
        declared = set(wheel["packages"])
        on_disk = {
            path.parent.name
            for path in REPO_ROOT.glob("*/__init__.py")
            if path.parent.name not in {"tests", ".design"}
        }
        assert on_disk <= declared, f"not in the wheel: {sorted(on_disk - declared)}"

    def test_the_playground_ships_where_serve_looks_for_it(self, wheel):
        """`agent/serve.py::_web_root` falls back to `agent/_web`; this is the
        other half of that contract. If they disagree the failure is a 500 on a
        page that works fine in development."""
        from agent.serve import _web_root

        assert wheel["force-include"]["web"] == "agent/_web"
        assert _web_root().name in {"web", "_web"}
        assert (_web_root() / "index.html").is_file()

    def test_every_console_script_points_at_something_callable(self):
        """An entry point naming a function that does not exist is a clean
        install followed by an ImportError on first use."""
        import importlib

        scripts = PYPROJECT["project"]["scripts"]
        assert scripts, "no console scripts declared"
        for name, target in scripts.items():
            module_name, _, attr = target.partition(":")
            module = importlib.import_module(module_name)
            assert callable(getattr(module, attr, None)), f"{name} -> {target} is not callable"

    def test_there_is_a_command_for_every_documented_workflow_step(self):
        """The five things docs/SETUP.md tells a reader to run. A checkout can
        use `python -m`; an installed copy cannot, because that relies on the
        `pythonpath = ["."]` pytest setting rather than on the package."""
        targets = set(PYPROJECT["project"]["scripts"].values())
        for module in ("seed.populate:main", "seed.refresh:main",
                       "grafana.provision:main", "agent.run:main", "agent.serve:main"):
            assert module in targets, f"no console script runs {module}"


#: The real search, captured at import time. `tests/conftest.py` replaces
#: `dotenv.env_path` for every test -- that is what keeps the suite from reading
#: the developer's own credentials -- so the tests *of* the search have to put
#: the real one back, against a `tmp_path` that holds nothing sensitive.
_REAL_ENV_PATH = dotenv.env_path


class TestEnvDiscovery:
    """`.env` has to be found from where the *user* is, not from where the code
    was installed."""

    @pytest.fixture(autouse=True)
    def _real_search(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dotenv, "env_path", _REAL_ENV_PATH)
        monkeypatch.setattr(dotenv, "REPO_ROOT", tmp_path / "nowhere")

    def test_a_dotenv_in_the_working_directory_wins(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text("TURNAROUND_TEST_KEY=from-cwd\n")
        monkeypatch.chdir(tmp_path)
        assert dotenv.env_path() == tmp_path / ".env"

    def test_the_search_walks_up_to_the_project_root(self, tmp_path, monkeypatch):
        """Run from `src/` inside a project and the project's `.env` still
        applies -- the same thing every other tool in a developer's day does."""
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        (tmp_path / ".env").write_text("TURNAROUND_TEST_KEY=from-root\n")
        nested = tmp_path / "src" / "deep"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert dotenv.env_path() == tmp_path / ".env"

    def test_the_search_stops_at_the_project_root(self, tmp_path, monkeypatch):
        """It must not climb out of the project and pick up a `.env` from a home
        directory or `/` -- a credential nobody meant to apply here."""
        outer = tmp_path / "outer"
        project = outer / "project"
        project.mkdir(parents=True)
        (outer / ".env").write_text("TURNAROUND_TEST_KEY=should-not-be-read\n")
        (project / ".git").mkdir()
        monkeypatch.chdir(project)
        assert dotenv.env_path() is None

    def test_no_dotenv_anywhere_is_not_an_error(self, tmp_path, monkeypatch):
        """Cloud Run sets the environment directly and ships no `.env`."""
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        monkeypatch.chdir(tmp_path)
        assert dotenv.env_path() is None
        dotenv.load_env()           # no raise
