"""Push everything in grafana/ to the stack in one shot: a folder, the
dashboards, the alert rules, the ML jobs. Idempotent -- safe to re-run.

Needs GRAFANA_URL and GRAFANA_SERVICE_ACCOUNT_TOKEN (Editor or above).

    uv run python -m grafana.provision              # dashboards + alerts + ml
    uv run python -m grafana.provision --only dashboards
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

from bridge.dotenv import load_env

HERE = Path(__file__).parent
FOLDER_UID = "turnaround"
FOLDER_TITLE = "Turnaround"


def _client() -> tuple[requests.Session, str]:
    url = os.environ.get("GRAFANA_URL", "").rstrip("/")
    token = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "")
    if not (url and token):
        sys.exit("GRAFANA_URL / GRAFANA_SERVICE_ACCOUNT_TOKEN unset; see .env.example")
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    return s, url


def ensure_folder(s: requests.Session, url: str) -> None:
    r = s.get(f"{url}/api/folders/{FOLDER_UID}", timeout=15)
    if r.status_code == 200:
        return
    r = s.post(f"{url}/api/folders", json={"uid": FOLDER_UID, "title": FOLDER_TITLE}, timeout=15)
    r.raise_for_status()
    print(f"  folder {FOLDER_TITLE!r} created")


def push_dashboards(s: requests.Session, url: str) -> None:
    for path in sorted((HERE / "dashboards").glob("*.json")):
        dash = json.loads(path.read_text())
        dash["id"] = None
        body = {"dashboard": dash, "folderUid": FOLDER_UID, "overwrite": True,
                "message": "provisioned by grafana/provision.py"}
        r = s.post(f"{url}/api/dashboards/db", json=body, timeout=30)
        r.raise_for_status()
        print(f"  dashboard {dash['uid']:<20} -> {r.json().get('status')}  {url}{r.json().get('url','')}")


def push_alert_rules(s: requests.Session, url: str) -> None:
    path = HERE / "alerts" / "rules.json"
    if not path.exists():
        print("  (no alerts/rules.json)")
        return
    groups = json.loads(path.read_text())
    for group in groups:
        folder = group.get("folderUid", FOLDER_UID)
        name = group["title"]
        r = s.put(
            f"{url}/api/v1/provisioning/folder/{folder}/rule-groups/{name}",
            json=group,
            headers={"X-Disable-Provenance": "true"},
            timeout=30,
        )
        if not r.ok:
            sys.exit(f"  alert group {name!r} -> {r.status_code} {r.text[:400]}")
        print(f"  alert group {name!r} -> {len(group.get('rules', []))} rule(s)")


def push_ml(s: requests.Session, url: str) -> None:
    """Build the ML jobs against *this* stack and push them.

    Built here rather than read from a committed ``ml/jobs.json``: a written job
    carries the stack's own hostname and the numeric datasource id, both of
    which differ per stack. A checked-in copy was therefore two things at once
    -- a fork that could not provision, and our hostname published in a public
    repo.
    """
    from grafana.ml.build import jobs

    base = f"{url}/api/plugins/grafana-ml-app/resources/manage/api/v1"
    existing: dict[str, str] = {}
    for route in ("jobs", "outliers"):
        for j in s.get(f"{base}/{route}", timeout=20).json().get("data", []):
            existing[j["name"]] = j["id"]
    for job in jobs():
        kind = job.pop("_kind", "forecast")
        route = "outliers" if kind == "outlier" else "jobs"
        if job["name"] in existing:
            r = s.post(f"{base}/{route}/{existing[job['name']]}", json=job, timeout=30)
        else:
            r = s.post(f"{base}/{route}", json=job, timeout=30)
        if not r.ok:
            sys.exit(f"  ml {kind} {job['name']!r} -> {r.status_code} {r.text[:400]}")
        print(f"  ml {kind:<8} {job['name']:<32} -> {r.status_code}")


STEPS = {"dashboards": push_dashboards, "alerts": push_alert_rules, "ml": push_ml}


def main() -> None:
    # Load `.env` first, so this entry point needs no `set -a && source .env`
    # incantation. A real exported variable still wins (bridge/dotenv.py).
    load_env()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=STEPS, action="append", help="run only these steps")
    args = ap.parse_args()
    s, url = _client()
    ensure_folder(s, url)
    for name, fn in STEPS.items():
        if args.only and name not in args.only:
            continue
        print(f"{name}:")
        fn(s, url)
    print(f"\ndone. {url}/dashboards/f/{FOLDER_UID}")


if __name__ == "__main__":
    main()
