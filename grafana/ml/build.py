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

Training windows are 1 hour: on the compressed time base (``bridge/timewarp``)
that is most of the ~45-minute history, and it means the jobs stay trainable as
long as the seed is refreshed (``seed/refresh.py``, or the Cloud Run job).

    uv run python -m grafana.ml.build
"""

from __future__ import annotations

import json
import os
from pathlib import Path

OUT = Path(__file__).parent / "jobs.json"
DS_UID = "grafanacloud-prom"
DS_ID = 8  # grafanacloud-your-stack-prom; stable for this stack


def _forecast(name, metric, expr, description) -> dict:
    return {
        "_kind": "forecast",
        "name": name,
        "metric": metric,
        "description": description,
        "grafanaUrl": os.environ.get("GRAFANA_URL", "").rstrip("/"),
        "datasourceType": "prometheus",
        "datasourceId": DS_ID,
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
        "grafanaUrl": os.environ.get("GRAFANA_URL", "").rstrip("/"),
        "datasourceType": "prometheus",
        "datasourceId": DS_ID,
        "datasourceUid": DS_UID,
        "queryParams": {"expr": expr, "range": True},
        "interval": 60,
        "algorithm": {"name": "mad", "sensitivity": 0.5},
    }


JOBS = [
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
    OUT.write_text(json.dumps(JOBS, indent=2) + "\n")
    kinds = ", ".join(sorted({j["_kind"] for j in JOBS}))
    print(f"wrote {OUT.relative_to(Path(__file__).parents[2])}  ({len(JOBS)} jobs: {kinds})")


if __name__ == "__main__":
    main()
