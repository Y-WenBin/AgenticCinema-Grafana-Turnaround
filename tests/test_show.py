"""The generated show is the demo. If it drifts, the demo stops being true.

These assert *relationships* rather than exact figures -- the simulation is
stochastic and pinning exact means would make every tuning change a test
rewrite. They are still tight enough that removing a story beat, breaking the
crunch mechanism, or losing the OpenCue join all fail loudly.
"""

import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import pytest

from bridge.ontology import Department, TaskStatus, parse_opencue_job_name
from bridge.privacy import MIN_POOL_SIZE, may_report_crew_load
from seed.model import ShowSimulation

NOW = datetime(2026, 9, 6, tzinfo=UTC)
ACT_THREE = "SEQ0420"


@pytest.fixture(scope="module", autouse=True)
def _salt():
    import os

    os.environ["TURNAROUND_PSEUDONYM_SALT"] = "test-salt"


@pytest.fixture(scope="module")
def history():
    return ShowSimulation.load(now=NOW).run()


@pytest.fixture(scope="module")
def sim():
    return ShowSimulation.load(now=NOW)


def peak_iterations(history, department: Department) -> dict[str, int]:
    peak: dict[str, int] = defaultdict(int)
    for stage in history.stages:
        if stage.department is department:
            peak[stage.shot_id] = max(peak[stage.shot_id], stage.iteration)
    return peak


class TestReproducibility:
    def test_two_runs_are_identical(self):
        a = ShowSimulation.load(now=NOW).run()
        b = ShowSimulation.load(now=NOW).run()
        assert len(a.stages) == len(b.stages)
        assert [s.shot_id for s in a.stages] == [s.shot_id for s in b.stages]
        assert [s.iteration for s in a.stages] == [s.iteration for s in b.stages]
        assert [r.job_name for r in a.renders] == [r.job_name for r in b.renders]

    def test_the_show_is_anchored_to_now_not_to_fixed_dates(self):
        later = ShowSimulation.load(now=NOW + timedelta(days=30))
        assert later.delivery > ShowSimulation.load(now=NOW).delivery


class TestTheShowIsMidFlight:
    def test_nothing_happens_in_the_future(self, history):
        assert not [s for s in history.stages if s.ended_at > NOW]
        assert not [r for r in history.renders if r.ended_at > NOW]

    def test_most_but_not_all_of_the_show_is_delivered(self, history):
        delivered = {
            s.shot_id
            for s in history.stages
            if s.department is Department.DI and s.status is TaskStatus.DONE
        }
        # Too complete and there is no remaining work for a forecast to be
        # about; too little and act three has not reached comp.
        assert 0.70 <= len(delivered) / 200 <= 0.90

    def test_work_is_still_in_progress(self, history):
        assert any(s.status is TaskStatus.WIP for s in history.stages)

    def test_every_department_has_recent_work(self, history):
        """A show moving in lockstep waves leaves departments idle, which is
        not what a studio looks like and makes every baseline meaningless."""
        recent = NOW - timedelta(days=42)
        active = {s.department for s in history.stages if s.ended_at >= recent}
        assert Department.COMP in active and Department.LIGHTING in active


class TestTheOpenCueJoin:
    def test_every_render_job_resolves_back_to_its_shot(self, history):
        for render in history.renders:
            job = parse_opencue_job_name(render.job_name)
            assert job is not None, render.job_name
            assert job.shot_id == render.shot_id
            assert job.department is render.department

    def test_the_join_recovers_the_sequence(self, history):
        job = parse_opencue_job_name(history.renders[0].job_name)
        assert job.sequence == history.renders[0].shot_id.split("_")[0]


class TestTheStoryIsPresent:
    def test_the_director_note_is_annotated(self, history):
        assert history.annotations
        note = history.annotations[0]
        assert ACT_THREE in note.tags
        assert note.at < NOW

    def test_the_cache_regression_leaves_a_log_signature(self, history):
        errors = [line for line in history.log_lines if line.labels.get("level") == "error"]
        assert errors, "the farm regression produced no evidence"
        assert all(line.shot_id.startswith(ACT_THREE) for line in errors)
        assert "frame 118" in errors[0].message

    def test_render_waste_concentrates_on_act_three(self, history):
        waste: dict[str, float] = defaultdict(float)
        for render in history.renders:
            waste[render.shot_id.split("_")[0]] += render.wasted_core_hours
        ranked = sorted(waste.items(), key=lambda kv: -kv[1])
        assert ranked[0][0] == ACT_THREE
        # The signal only counts if it stands out from the rest of the show.
        assert ranked[0][1] > 5 * ranked[1][1]

    def test_act_three_comp_costs_multiples_of_a_normal_iteration(self, history):
        def mean_hours(in_act_three: bool) -> float:
            rs = [
                r
                for r in history.renders
                if r.department is Department.COMP
                and r.shot_id.startswith(ACT_THREE) is in_act_three
            ]
            return statistics.mean(r.core_hours for r in rs)

        # The loud signal, and the one no producer can currently see.
        assert mean_hours(True) > 3 * mean_hours(False)

    def test_the_rework_signal_is_present_but_still_early(self, history):
        """Eighteen days after a note, with a six-day comp pass, barely two
        iterations have had time to happen. A weak signal here is the honest
        outcome -- and precisely why a human misses it."""
        peak = peak_iterations(history, Department.COMP)
        act_three = [v for k, v in peak.items() if k.startswith(ACT_THREE)]
        rest = [v for k, v in peak.items() if not k.startswith(ACT_THREE)]
        assert statistics.mean(act_three) > statistics.mean(rest)
        assert max(act_three) >= 3

    def test_crew_load_spikes_in_the_current_week(self, history):
        weekly = history.weekly_hours_by_pool()
        weeks = sorted({w for _, w in weekly})[-5:]
        series = [weekly.get(("comp-pool-2", w), 0.0) for w in weeks]
        baseline = statistics.mean(series[:-1])
        assert series[-1] > 1.7 * baseline, series


class TestBaselineIsHabitable:
    def test_median_pool_load_is_a_normal_working_week(self, history):
        """If the baseline is already 80 hours there is no crunch to detect --
        the forecast would be predicting a constant."""
        weekly = history.weekly_hours_by_pool()
        weeks = sorted({w for _, w in weekly})[-8:]
        values = [
            v
            for pool in history.pool_headcount
            for w in weeks
            if (v := weekly.get((pool, w), 0.0)) > 1
        ]
        assert 28 <= statistics.median(values) <= 45


class TestPrivacyHoldsOnGeneratedData:
    def test_the_small_pool_exists_and_is_suppressed(self, sim, history):
        """di-pool-1 is deliberately two people, so the demo can show the
        aggregation floor working rather than merely claiming it."""
        assert history.pool_headcount["di-pool-1"] < MIN_POOL_SIZE
        roster = {log.artist for log in history.time_logs if log.pool == "di-pool-1"}
        assert not may_report_crew_load(roster)

    def test_reportable_pools_clear_the_floor(self, history):
        for pool in ("comp-pool-1", "comp-pool-2", "lighting-pool-2"):
            assert history.pool_headcount[pool] >= MIN_POOL_SIZE

    def test_no_real_identity_reaches_the_history(self, history):
        for stage in history.stages:
            assert "@" not in stage.artist
            assert len(stage.artist) == 8  # pseudonym, not a name or a uuid
