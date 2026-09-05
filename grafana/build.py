"""Build the Turnaround dashboards as plain dicts and write them to
``grafana/dashboards/*.json``.

Hand-writing Grafana dashboard JSON is a keying-error factory, so the panels
are assembled here from a few helpers and the rendered JSON is committed next
to this file for review and for ``provision.py`` to push.

Every query is PromQL against the ``turnaround_*`` series and is written in the
*compressed* time base (see ``bridge/timewarp.py``): a range that would read
``[14d]`` over real history reads ``[5m]`` here. The mapping is
``real_span * (window / real_span)`` -- with the default 45-minute window and
~154 days of history, one real day is about 17.6 wall-clock seconds.

    uv run python -m grafana.build        # rewrite grafana/dashboards/*.json
"""

from __future__ import annotations

import json
from pathlib import Path

PROM = "grafanacloud-prom"
LOKI = "grafanacloud-logs"
OUT = Path(__file__).parent / "dashboards"

# The join reads cleanest as a ratio of cumulative counters rather than a
# rolling increase(): on the compressed time base the points are sparse enough
# that a short increase() window catches a different number of whole iterations
# per sequence and the ratio jumps around. Cumulative core-hours over cumulative
# iterations is stable and still says exactly what the thesis claims.
W_SINCE_NOTE = "6m"   # ~= 18 days, the span since the director note
FLOOR = 3             # aggregation floor: pools smaller than this are not alertable


def _prom(expr: str, legend: str = "", instant: bool = False) -> dict:
    return {
        "datasource": {"type": "prometheus", "uid": PROM},
        "editorMode": "code",
        "expr": expr,
        "legendFormat": legend or "__auto",
        "range": not instant,
        "instant": instant,
        "refId": "A",
    }


def _grid(x: int, y: int, w: int, h: int) -> dict:
    return {"h": h, "w": w, "x": x, "y": y}


def _timeseries(title, targets, *, unit="none", thresholds=None, y=0, x=0, w=24, h=8, desc="") -> dict:
    steps = [{"color": "green", "value": None}]
    if thresholds:
        steps += [{"color": c, "value": v} for v, c in thresholds]
    return {
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": {"type": "prometheus", "uid": PROM},
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 8, "showPoints": "never"},
                "thresholds": {"mode": "absolute", "steps": steps},
                **({"color": {"mode": "thresholds"}} if thresholds else {}),
            },
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "targets": targets,
    }


def _stat(title, target, *, unit="none", thresholds=None, y=0, x=0, w=6, h=8, desc="") -> dict:
    steps = [{"color": "green", "value": None}]
    if thresholds:
        steps += [{"color": c, "value": v} for v, c in thresholds]
    return {
        "type": "stat",
        "title": title,
        "description": desc,
        "datasource": {"type": "prometheus", "uid": PROM},
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": {"defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps},
                                     "color": {"mode": "thresholds"}}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "area", "textMode": "auto"},
        "targets": [target],
    }


def _table(title, targets, *, y=0, x=0, w=24, h=8, desc="", overrides=None) -> dict:
    return {
        "type": "table",
        "title": title,
        "description": desc,
        "datasource": {"type": "prometheus", "uid": PROM},
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": {"defaults": {"custom": {"filterable": True}}, "overrides": overrides or []},
        "options": {"showHeader": True, "sortBy": [{"displayName": "Value", "desc": True}]},
        "transformations": [{"id": "merge", "options": {}}],
        "targets": [{**t, "format": "table", "instant": True, "range": False} for t in targets],
    }


def _text(title, content, *, y=0, x=0, w=24, h=4) -> dict:
    return {"type": "text", "title": title, "gridPos": _grid(x, y, w, h),
            "options": {"mode": "markdown", "content": content}}


def _dashboard(uid, title, panels, *, tags, description) -> dict:
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["turnaround", *tags],
        "timezone": "utc",
        "schemaVersion": 39,
        "editable": True,
        "graphTooltip": 1,
        "time": {"from": "now-6h", "to": "now"},
        "refresh": "",
        "annotations": {"list": [
            {"name": "Director notes", "enable": True, "iconColor": "yellow",
             "datasource": {"type": "prometheus", "uid": "-- Grafana --"},
             "target": {"type": "tags", "tags": ["director-note"], "limit": 100, "matchAny": True}},
        ]},
        "templating": {"list": [
            {"name": "sequence", "type": "query", "datasource": {"type": "prometheus", "uid": PROM},
             "query": {"query": "label_values(turnaround_task_iterations_total, sequence)", "refId": "A"},
             "includeAll": True, "multi": True, "current": {"text": "All", "value": "$__all"}},
        ]},
        "panels": panels,
    }


# --------------------------------------------------------------------------- #

def crew_load() -> dict:
    hours = "turnaround_artist_hours_logged"
    panels = [
        _text("", (
            "**Hours per rostered artist per week, by pool.** The forecastable "
            "half of crunch. `di-pool-1` is two people — below the aggregation "
            "floor of 3 — so it is shown here for context but is excluded from "
            "every alert by `and on(pool) (turnaround_pool_headcount >= "
            f"{FLOOR}))`."
        ), h=3),
        _stat("comp-pool-2 — hours per rostered artist", _prom(
            f"max({hours}{{pool=\"comp-pool-2\"}})", "comp-pool-2", instant=True),
            unit="h", thresholds=[(40, "yellow"), (60, "red")], y=3, x=0, w=6, h=8,
            desc="The pool the show is about. Sustained >60 h/week is crunch. "
                 "Grafana ML job turnaround-comp-pool-2-hours forecasts this series."),
        _timeseries("Weekly hours by pool", [
            _prom(f"max by (pool) ({hours})", "{{pool}}")],
            unit="h", thresholds=[(40, "yellow"), (60, "red")], y=3, x=6, w=18, h=8,
            desc="A 40 h line is the plan; 60 h is unsustainable by the studio's own account."),
        _table("Pools over 55 h that an alert can fire on", [
            _prom(
                f"(max by (pool, department) ({hours} > 55)) "
                f"and on(pool) (turnaround_pool_headcount >= {FLOOR})",
                instant=True)],
            y=11, x=0, w=12, h=8,
            desc="The floor join in action. di-pool-1 cannot appear here even when its two people are buried."),
        _table("Rostered headcount", [
            _prom("max by (pool) (turnaround_pool_headcount)", instant=True)],
            y=11, x=12, w=12, h=8,
            desc="Join key for the floor. Not a load signal."),
    ]
    return _dashboard("turnaround-crew", "Turnaround · Crew Load", panels,
                      tags=["crew", "hero"],
                      description="Weekly hours per rostered artist, per pool, with the aggregation floor visible.")


def the_join() -> dict:
    panels = [
        _text("", (
            "**The creative plane joined to the compute plane on `sequence`.** "
            "OpenCue core-hours over Kitsu iterations — a number no scheduling "
            "tool and no farm dashboard can produce alone. SEQ0420 runs about "
            "4× every other sequence, and has since before the note."
        ), h=3),
        _timeseries("Render core-hours per comp iteration, by sequence", [
            _prom(
                "sum by (sequence) (turnaround_render_core_hours_total{department=\"comp\"}) "
                "/ sum by (sequence) (turnaround_task_iterations_total{department=\"comp\"})",
                "{{sequence}}")],
            unit="h", thresholds=[(4, "orange"), (7, "red")], y=3, x=0, w=24, h=9,
            desc="Cumulative farm core-hours over cumulative comp passes. SEQ0420 sits well clear of every other sequence."),
        {
            "type": "bargauge", "title": "Wasted render core-hours by sequence",
            "description": "Farm time that produced no accepted frame, attributed to the shot that caused it.",
            "datasource": {"type": "prometheus", "uid": PROM},
            "gridPos": _grid(0, 12, 12, 8),
            "fieldConfig": {"defaults": {"unit": "h", "thresholds": {"mode": "absolute", "steps": [
                {"color": "green", "value": None}, {"color": "orange", "value": 20}, {"color": "red", "value": 50}]},
                "color": {"mode": "thresholds"}}, "overrides": []},
            "options": {"displayMode": "gradient", "orientation": "horizontal",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
            "targets": [_prom("sum by (sequence) (turnaround_render_waste_core_hours_total)", "{{sequence}}", instant=True)],
        },
        _timeseries("Frame-failure rate by sequence", [
            _prom(
                "sum by (sequence) (turnaround_render_frames_failed_total) "
                "/ clamp_min(sum by (sequence) (turnaround_render_frames_total), 1)",
                "{{sequence}}")],
            unit="percentunit", thresholds=[(0.05, "orange"), (0.1, "red")], y=12, x=12, w=12, h=8,
            desc="A healthy farm loses ~2% of frames. Frame 118 of every SEQ0420 comp render is the regression."),
    ]
    return _dashboard("turnaround-join", "Turnaround · The Join", panels,
                      tags=["join", "render"],
                      description="Render effort per creative iteration, and the render waste it hides, keyed on sequence.")


def delivery() -> dict:
    panels = [
        _text("", (
            "**Where the schedule is, and how hard it is working to get there.** "
            "Burndown alone never predicts crunch — that is what the iteration "
            "and hours views are for — but it frames everything else."
        ), h=3),
        _timeseries("Shots signed off through DI", [
            _prom("sum(turnaround_shots_approved_total)", "delivered"),
            {**_prom("vector(200)", "all 200 shots"), "refId": "C"}],
            unit="short", y=3, x=0, w=12, h=8,
            desc="Cumulative deliveries against the 200-shot line. Grafana ML job "
                 "turnaround-delivery-burndown forecasts this series with Prophet; the gap to "
                 "the line at the planned date is the slip."),
        _timeseries("Department passes per sequence (cumulative, incl. rework)", [
            _prom(
                "sum by (sequence) (turnaround_task_iterations_total{sequence=~\"$sequence\"})",
                "{{sequence}}")],
            unit="short", y=3, x=12, w=12, h=8,
            desc="The leading indicator of a slip — still barely moving on SEQ0420 this soon after a note, which is the point."),
        _table("Vendor turnaround — seconds per department pass", [
            _prom("avg by (vendor, department) (turnaround_vendor_turnaround_seconds)", instant=True)],
            y=11, x=0, w=24, h=8,
            desc="Wall-clock per pass by vendor. vendor-b is the outlier the ML detector flags."),
    ]
    return _dashboard("turnaround-delivery", "Turnaround · Delivery", panels,
                      tags=["delivery"],
                      description="Burndown, iteration load and vendor turnaround.")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for build in (crew_load, the_join, delivery):
        dash = build()
        path = OUT / f"{dash['uid']}.json"
        path.write_text(json.dumps(dash, indent=2) + "\n")
        print(f"wrote {path.relative_to(Path(__file__).parent.parent)}  ({len(dash['panels'])} panels)")


if __name__ == "__main__":
    main()
