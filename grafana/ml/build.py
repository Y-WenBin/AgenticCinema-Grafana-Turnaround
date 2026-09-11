"""Build the Grafana ML jobs and write grafana/ml/jobs.json.

Two forecasts and one outlier detector, all over the ``turnaround_*`` series:

* **Delivery burndown forecast** -- projects `sum(shots_approved)` forward.
  Compared against the planned slope, the gap *is* the delivery slip.
* **comp-pool-2 hours forecast** -- projects the crunch pool's weekly hours.
  Crossing 40 in the forecast, before it is crossed in fact, is the point of
  the whole product.
* **Vendor turnaround outlier** -- MAD over `vendor_turnaround_seconds` grouped
  by vendor. Flags the vendor whose behaviour is unlike the rest without being
  told which one to suspect.

Nothing here is a literal pointing at one stack. ``grafanaUrl`` comes from
``GRAFANA_URL`` and the numeric ``datasourceId`` is looked up from the uid at
build time, so a fork provisions against its own stack with no edit -- and this
repo does not publish which stack is ours.

Training windows are 1 hour: on the compressed time base (``bridge/timewarp``)
that is most of the ~45-minute history, and it means the jobs stay trainable as
long as the seed is refreshed (``seed/refresh.py``, or the Cloud Run job).

    uv run python -m grafana.ml.build
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

OUT = Path(__file__).parent / "jobs.json"
DS_UID = os.environ.get("GRAFANA_DS_PROM_UID", "grafanacloud-prom")

#: Grafana's ML API wants the *numeric* datasource id, not the uid, and that
#: number is assigned per stack -- it is 8 on one and something else on the
#: next. Hard-coding it made `grafana.provision` silently target a datasource
#: that does not exist on anybody else's stack, so it is resolved at build time
#: from the uid instead: see :func:`datasource_id`. `GRAFANA_DS_PROM_ID`
#: short-circuits the lookup for an offline build.


def datasource_id(uid: str = "") -> int:
    """The numeric id of the Prometheus datasource, resolved from its uid.

    ``GRAFANA_DS_PROM_ID`` wins if set. Otherwise this asks the stack named by
    ``GRAFANA_URL``, which is the only place the answer actually lives.
    """
    override = os.environ.get("GRAFANA_DS_PROM_ID", "").strip()
    if override.isdigit():
        return int(override)

    import requests

    url = os.environ.get("GRAFANA_URL", "").rstrip("/")
    token = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "")
    if not (url and token):
        sys.exit("GRAFANA_URL / GRAFANA_SERVICE_ACCOUNT_TOKEN unset -- needed to "
                 "resolve the datasource id. See .env.example, or set "
                 "GRAFANA_DS_PROM_ID directly.")
    want = uid or DS_UID
    r = requests.get(f"{url}/api/datasources/uid/{want}",
                     headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if r.status_code in (401, 403):
        # Reading a datasource needs `datasources:read`, which not every
        # service-account role carries. That is a permissions problem, not a
        # wrong uid, and the fix is the override rather than a broader token.
        sys.exit(f"the service account may not read datasource {want!r} "
                 f"({r.status_code}). Either grant it `datasources:read`, or "
                 "set GRAFANA_DS_PROM_ID to the numeric id shown in the "
                 "datasource's URL in the Grafana UI.")
    if not r.ok:
        sys.exit(f"no datasource with uid {want!r} on this stack "
                 f"({r.status_code}). Set GRAFANA_DS_PROM_UID to yours -- "
                 "docs/SETUP.md part 2c lists them.")
    return int(r.json()["id"])


def grafana_url() -> str:
    """The stack the jobs will run against, from the environment.

    Never a literal. A resolved hostname committed to a public repo is free
    reconnaissance -- it points at a login page and an OTLP endpoint -- and it
    is also just wrong for anyone who forked this.
    """
    return os.environ.get("GRAFANA_URL", "").rstrip("/")


def _forecast(name, metric, expr, description) -> dict:
    return {
        "_kind": "forecast",
        "name": name,
        "metric": metric,
        "description": description,
        "grafanaUrl": grafana_url(),
        "datasourceType": "prometheus",
        "datasourceId": datasource_id(),
        "datasourceUid": DS_UID,
        "queryParams": {"expr": expr, "range": True},
        # 60 s resolution over a 3 h window: Prophet needs >=100 points, and on
        # the compressed time base that many only accumulate once the refresh
        # loop (seed/refresh.py) has been seeding for a couple of hours -- which
        # it always has in the deployed environment.
        "interval": 60,
        "algorithm": "grafana_prophet_1_0_1",
        "trainingWindow": 10800,
        "trainingFrequency": 3600,
    }


def _outlier(name, metric, expr, description) -> dict:
    return {
        "_kind": "outlier",
        "name": name,
        "metric": metric,
        "description": description,
        "grafanaUrl": grafana_url(),
        "datasourceType": "prometheus",
        "datasourceId": datasource_id(),
        "datasourceUid": DS_UID,
        "queryParams": {"expr": expr, "range": True},
        "interval": 60,
        "algorithm": {"name": "mad", "sensitivity": 0.5},
    }


def jobs() -> list[dict]:
    """The three ML jobs, resolved against the stack in the environment.

    A function, not a module-level list: it reaches the network (for the
    datasource id) and reads ``GRAFANA_URL``, and a module-level constant would
    do both at *import* time -- before ``.env`` is loaded, and in every offline
    test that so much as imports this package. The repo's config invariant
    (AGENTS.md) says the same thing in general terms.
    """
    return [
        _forecast(
            "turnaround-delivery-burndown",
            "turnaround_forecast_shots_approved",
            "sum(turnaround_shots_approved_total)",
            "Projected shots signed off through DI. The gap to the planned slope is the delivery slip.",
        ),
        _forecast(
            "turnaround-comp-pool-2-hours",
            "turnaround_forecast_comp_pool_2_hours",
            'max(turnaround_artist_hours_logged{pool="comp-pool-2"})',
            "Projected weekly hours per rostered artist in the crunch pool. Crossing 40 in the "
            "forecast is the early warning the crew-load alert only gives after the fact.",
        ),
        _outlier(
            "turnaround-vendor-turnaround",
            "turnaround_outlier_vendor_turnaround",
            "avg by (vendor) (turnaround_vendor_turnaround_seconds)",
            "Which vendor's turnaround behaves unlike the others, discovered rather than asserted.",
        ),
    ]


def main() -> None:
    """Write ``jobs.json`` for inspection.

    ``grafana/provision.py`` calls :func:`jobs` directly and does not read this
    file -- it is a debugging artifact, and it is git-ignored, because a written
    copy has the stack's own hostname resolved into it.
    """
    built = jobs()
    OUT.write_text(json.dumps(built, indent=2) + "\n")
    kinds = ", ".join(sorted({j["_kind"] for j in built}))
    print(f"wrote {OUT.relative_to(Path(__file__).parents[2])}  ({len(built)} jobs: {kinds})")
    print("(git-ignored: it has your stack's hostname in it)")


if __name__ == "__main__":
    main()
