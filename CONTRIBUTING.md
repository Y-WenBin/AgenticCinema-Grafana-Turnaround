# Contributing

Corrections, adapters and "we pointed this at our Deadline farm and…" reports are
all welcome. This page is the short version; [`AGENTS.md`](AGENTS.md) is the
orientation for anyone — or anything — about to change code, and
[`PROJECT.md`](PROJECT.md) is the technical reference.

## Get it running

```bash
git clone https://github.com/Y-WenBin/AgenticCinema-Grafana-Turnaround.git turnaround
cd turnaround
uv sync --group dev
uv run pytest -q          # offline: no network, no credentials, no accounts
uv run ruff check .
```

If those pass, your checkout is good. To see the product do something before you
have a Grafana Cloud account or a GCP project:

```bash
uv run python -m seed.populate --dry-run
```

That runs the whole simulated show against in-memory exporters and prints what
*would* have been written. Nothing leaves the machine.

Going further — a live stack, the agent, a real tracker or farm — is
[`docs/SETUP.md`](docs/SETUP.md).

## What CI will check

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | What it proves |
|---|---|
| `tests` | `ruff check .` and the full suite, on Python 3.12 and 3.13 |
| `wheel install` | the built wheel works **from outside the repo** — every console script exists, `grafana`/`seed`/`agent` import, and `turnaround-seed --dry-run` runs the whole simulation |

The `wheel install` job exists because every packaging bug this project has had
was invisible from a checkout. If you add a package or a data file, that job is
the one that will tell you the wheel forgot it.

## House rules

**Tests are offline by contract.** No test may reach Vertex, Grafana Cloud, the
network, a `mcp-grafana` binary, or your `.env`. Three autouse fixtures in
[`tests/conftest.py`](tests/conftest.py) enforce it; if one gets in your way, the
fix is an injected fake, not an exemption. [`tests/TESTPLAN.md`](tests/TESTPLAN.md)
is the full contract.

**The privacy invariants are not negotiable.** Artists appear only as salted
HMAC pseudonyms, no crew-load figure may describe a pool of fewer than three
people, and there is no per-person output metric. These live in
[`bridge/privacy.py`](bridge/privacy.py) and are tested. A change that weakens
one needs to argue for itself in the pull request, not just pass.

**Writes stay gated.** The approval gate allows a *known read* and asks a human
about everything else ([`agent/approval.py`](agent/approval.py)). If you wire a
new Grafana tool to an analyst, add it to `READ_TOOLS` — a contract test will
tell you if you forgot. Never widen the gate to make a tool convenient.

**The deployed runtime is Gemini/Vertex only.** No other AI SDK may appear on the
import path of anything that ships; a test enforces it. Coding assistants are a
development tool and are fine.

**Say why, not what.** The comments in this codebase explain decisions — the
measurement behind a default, the bug a guard was written after. Match that. A
comment restating the line below it is worse than none.

## Adding support for a tool

This is the most useful contribution and the smallest. The join needs a **shot
id** and, on the schedule side, a **task status** — nothing else.

- **A new job-name convention** (a farm we don't list): add it to
  `bridge/ontology.py` and a round-trip case to `tests/test_conventions.py`
  using a name your farm really emits.
- **A new tracker status vocabulary**: extend `TaskStatus.from_tracker` and test
  it against the short codes that tracker actually sends.
- **A live source adapter**: implement one of the Protocols in
  `bridge/sources.py`. `docs/SETUP.md` §6 has a worked Kitsu example.

Real dialects beat invented ones. If you have a job name from a production farm,
that fixture is worth more than the code around it.

## Pull requests

Keep them to one thing. Say what broke or what was missing, and how you know the
change fixes it — a failing test that now passes is the best answer. Run
`uv run pytest -q` and `uv run ruff check .` before you push; CI runs both, but
the loop is faster locally.

Security issues go to [`SECURITY.md`](SECURITY.md), not to a public issue.

## Licence

Contributions are accepted under [Apache-2.0](LICENSE), the project's licence.
