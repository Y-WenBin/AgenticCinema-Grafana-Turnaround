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

Please do not run scans, load tests or automated exploitation against the hosted
demo. It is a single small Cloud Run service on a personal billing account, and
its rate limits are what stand between a curious visitor and a real bill. If you
need to test something aggressive, run it locally — the whole stack installs in
two commands.

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
- **The public endpoint cannot write.** `agent/serve.py` runs
  `AutoApprover(approve=False)` unconditionally.
- **No parameters reach a query.** `/api/backend` is a fixed board; the browser
  sends nothing that reaches PromQL or LogQL ([`agent/backend.py`](agent/backend.py)).
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
- **Least privilege on the deployment.** The Cloud Run runtime service account
  holds `roles/aiplatform.user` and nothing else; the Grafana service account is
  Editor, not Admin, for the reasons in `docs/SETUP.md` §2b.

**Known and accepted, for a demo:**

- **Rate limits are per instance.** The counters in
  [`agent/limits.py`](agent/limits.py) live in process memory, so with
  `--max-instances N` the real ceiling is `N ×` what is configured, and a cold
  start forgets the window early. Exact global limits need shared state; the
  module says so rather than implying otherwise. Not a vulnerability report.
- **`X-Forwarded-For` is spoofable.** The per-visitor window is politeness for
  honest traffic. The daily budget is the control that actually bounds spend,
  and it does not depend on caller identity.
- **`/docs` is public by default.** Useful for a reviewer poking at a demo; turn
  it off with `TURNAROUND_PUBLIC_DOCS=0`.
- **Model output reaches the page.** Answers and tool arguments are rendered by
  the playground. Every interpolation goes through `esc()`, and the CSP is the
  second wall behind it. A way past *both* is very much a report worth making.

## Handling credentials

If you are running your own instance: `.env` is git-ignored and so is
`.secrets/`, no key file is baked into the image, and `deploy/deploy.sh` pushes
the Grafana and OTLP credentials to Secret Manager rather than into the service
YAML. Rotate `TURNAROUND_PSEUDONYM_SALT` per deployment and never reuse the one
from any example — a shared salt makes the pseudonyms reversible by anyone who
can list your crew, which defeats the entire point of them.
