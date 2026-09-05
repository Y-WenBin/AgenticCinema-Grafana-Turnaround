"""The join is the product. If these fail, nothing downstream is trustworthy."""

import pytest

from bridge.ontology import (
    Department,
    JobRef,
    ShotRef,
    TaskStatus,
    format_opencue_job_name,
    parse_opencue_job_name,
    sequence_of,
)


class TestShotIdentity:
    def test_shot_ref_round_trip(self):
        ref = ShotRef.parse("nightfall", "SEQ0420_SH0100")
        assert ref.sequence == "SEQ0420"
        assert ref.shot == "SH0100"
        assert ref.shot_id == "SEQ0420_SH0100"

    def test_sequence_derivable_without_lookup(self):
        assert sequence_of("SEQ0420_SH0100") == "SEQ0420"

    @pytest.mark.parametrize("bad", ["SH0100", "SEQ420_SH0100", "seq0420_sh0100", ""])
    def test_rejects_non_canonical_ids(self, bad):
        with pytest.raises(ValueError):
            sequence_of(bad)


class TestOpenCueJoin:
    """OpenCue names jobs '<show>-<shot>-<user>_<name>', so farm telemetry
    already carries the shot id. These tests pin that assumption down."""

    def test_round_trip_through_the_formatter(self):
        name = format_opencue_job_name(
            "nightfall", "SEQ0420_SH0100", "a7f3c2d1", Department.COMP, 6
        )
        assert name == "nightfall-SEQ0420_SH0100-a7f3c2d1_comp_v006"

        job = parse_opencue_job_name(name)
        assert job == JobRef(
            show="nightfall",
            shot_id="SEQ0420_SH0100",
            user="a7f3c2d1",
            name="comp_v006",
            department=Department.COMP,
            version=6,
        )
        assert job.sequence == "SEQ0420"

    def test_show_slug_may_contain_hyphens(self):
        # Anchoring on the shot id keeps studio-specific show slugs working.
        job = parse_opencue_job_name("night-fall-s2-SEQ0011_SH0020-b1c2d3e4_lighting_v012")
        assert job is not None
        assert job.show == "night-fall-s2"
        assert job.shot_id == "SEQ0011_SH0020"
        assert job.department is Department.LIGHTING
        assert job.version == 12

    def test_unattributable_jobs_return_none_rather_than_raising(self):
        # Farm maintenance and ad-hoc tool jobs are real; they must not crash a tap.
        assert parse_opencue_job_name("farm-maintenance-cleanup") is None
        assert parse_opencue_job_name("testing-default-jdoe_myjob") is None

    def test_unknown_department_still_resolves_the_shot(self):
        # A studio-specific stage we don't model shouldn't cost us the join.
        job = parse_opencue_job_name("nightfall-SEQ0420_SH0100-a7f3c2d1_matchmove_v001")
        assert job is not None
        assert job.shot_id == "SEQ0420_SH0100"
        assert job.department is None
        assert job.version == 1


class TestPipelineOrder:
    def test_upstream_is_dependency_order(self):
        assert Department.COMP.upstream() == (
            Department.PREVIZ,
            Department.LAYOUT,
            Department.ANIM,
            Department.FX,
            Department.LIGHTING,
        )
        assert Department.PREVIZ.upstream() == ()


class TestTaskStatus:
    @pytest.mark.parametrize(
        "kitsu_name,expected",
        [
            ("Retake", TaskStatus.RETAKE),
            ("rejected", TaskStatus.RETAKE),
            ("Waiting For Approval", TaskStatus.WAITING_FOR_APPROVAL),
            ("WFA", TaskStatus.WAITING_FOR_APPROVAL),
            ("Approved", TaskStatus.DONE),
            ("WIP", TaskStatus.WIP),
            ("Ready To Start", TaskStatus.TODO),
        ],
    )
    def test_studio_vocabulary_is_matched_loosely(self, kitsu_name, expected):
        assert TaskStatus.from_kitsu(kitsu_name) is expected

    def test_rework_is_the_signal_that_matters(self):
        assert TaskStatus.RETAKE.is_rework
        assert not TaskStatus.WIP.is_rework
