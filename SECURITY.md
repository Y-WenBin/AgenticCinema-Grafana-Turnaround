# Security

## Reporting a vulnerability

Please report privately, not in a public issue:

- **Preferred** — [open a private security advisory](https://github.com/Y-WenBin/AgenticCinema-Grafana-Turnaround/security/advisories/new)
  on this repository.

Include what you did, what happened, and what you expected. A proof of concept
helps; a stack trace and the commit you were on help more. You will get an
acknowledgement, and a fix or an explanation of why it is not one — if a report
turns out to be intended behaviour, the answer will say why rather than closing
quietly.

The hosted demo is **not** in this repository — the HTTP service, its rate
limiting and its deployment live in a separate web-application repository, and
issues in those belong there. Either way, please do not run scans, load tests or
automated exploitation against it: it is a single small service on a personal
billing account, and its rate limits are what stand between a curious visitor
and a real bill. If you need to test something aggressive, run it locally — the
whole stack installs in two commands.

## Supported versions

This is a pre-1.0 project. Fixes land on `main`; there are no maintained release
branches.

## What the project already does, and what it does not

Worth knowing before you report, because some of these are deliberate and
documented rather than oversights.

**Enforced in code, and tested:**

- **Writes need a human.** Analysts are read-only *by construction* — wired to a
  `mcp-grafana` started `--disable-write`. The Remediator is the only agent that
  can mutate anything, and the approval gate allows a *known read* and asks a
  human about everything else. A tool it does not recognise is gated, not passed
  through ([`agent/approval.py`](agent/approval.py)).
- **Privacy floor.** Artists are salted HMAC pseudonyms; no crew-load figure may
  describe a pool of fewer than three people, and the floor is written into the
  public board's own PromQL rather than applied afterwards. The exporter refuses
  to start on a guessable salt ([`bridge/privacy.py`](bridge/privacy.py)).
- **Security headers on every response**, errors included — a CSP with no
  `unsafe-inline` on `script-src`, `frame-ancestors 'none'`, `base-uri 'none'`,
  `form-action 'none'`.
- **No deployment identifiers in the repo.** A shape-based test keeps the
  Grafana stack hostname and the GCP project id out of every tracked file
  ([`tests/test_no_deployment_identifiers.py`](tests/test_no_deployment_identifiers.py)).
- **Least privilege.** The Grafana service account is Editor, not Admin, for the
  reasons in `docs/SETUP.md` §2b.

**Known and accepted:**

- **A run spends real money.** Every question is a sequence of Gemini calls.
  `TURNAROUND_MAX_LLM_CALLS` (default 40) is the per-run circuit breaker and is
  the control that bounds it; ADK's own default is 500. If you expose this
  library behind anything public, rate limiting is yours to add — it used to
  live here and moved out with the service.
- **Model output is not sanitised by this library.** `agent.run` prints answers
  and tool arguments to a terminal. Anything that renders them as markup is
  responsible for escaping them.

## Handling credentials

If you are running your own instance: `.env` is git-ignored and so is
`.secrets/`, and nothing here writes a credential to disk. Rotate `TURNAROUND_PSEUDONYM_SALT` per deployment and never reuse the one
from any example — a shared salt makes the pseudonyms reversible by anyone who
can list your crew, which defeats the entire point of them.
