"""Turnaround is not Kitsu+OpenCue-only: the join needs a shot id and a status,
and those can come from any of the trackers, render managers and NLEs a real
VFX / post pipeline uses. These tests pin the generalisation:

* ``TaskStatus.from_tracker`` -- ShotGrid / Flow Production Tracking / ftrack vocab
* ``parse_job_name`` -- Deadline / Tractor / Qube! / Royal Render / template
* ``ShotIdScheme`` -- studio-specific shot-id spellings
* ``bridge.editorial`` -- EDL (every NLE exports one)
* ``bridge.sources.SUPPORTED_TOOLS`` -- the honest coverage list
"""

from __future__ import annotations

import re

import pytest

from bridge.editorial import parse_edl, shot_ids_from_edl
from bridge.ontology import (
    SHOT_ID_SCHEMES,
    Department,
    ShotIdScheme,
    ShotRef,
    TaskStatus,
    department_from_token,
    parse_job_name,
    parse_opencue_job_name,
    sequence_of,
)
from bridge.sources import CsvScheduleSource, Support, supported

# --------------------------------------------------------------------------
# tracker status vocabularies
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    # ShotGrid / Flow Production Tracking short codes
    ("cbb", TaskStatus.RETAKE),
    ("rev", TaskStatus.WAITING_FOR_APPROVAL),
    ("apr", TaskStatus.DONE),
    ("fin", TaskStatus.DONE),
    ("ip", TaskStatus.WIP),
    ("hld", TaskStatus.TODO),
    ("rdy", TaskStatus.TODO),
    ("omt", TaskStatus.DONE),
    # ftrack phrases
    ("Changes Requested", TaskStatus.RETAKE),
    ("Awaiting Approval", TaskStatus.WAITING_FOR_APPROVAL),
    ("Not started", TaskStatus.TODO),
    # free text / other trackers
    ("Kickback", TaskStatus.RETAKE),
    ("Delivered", TaskStatus.DONE),
    ("On Hold", TaskStatus.TODO),
])
def test_from_tracker_normalises_across_vocabularies(name, expected):
    assert TaskStatus.from_tracker(name) is expected


def test_short_codes_do_not_match_as_substrings():
    # "ip" must not fire on "Shipping"; "rev" must not fire on "Approved".
    assert TaskStatus.from_tracker("Shipping") is TaskStatus.TODO
    assert TaskStatus.from_tracker("Approved") is TaskStatus.DONE


def test_from_kitsu_is_still_a_working_alias():
    assert TaskStatus.from_kitsu("Retake") is TaskStatus.RETAKE
    assert TaskStatus.from_kitsu.__func__ is TaskStatus.from_tracker.__func__


# --------------------------------------------------------------------------
# render-manager job-name conventions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("convention,job,dept,version", [
    ("deadline", "SEQ0420_SH0100 comp v6", Department.COMP, 6),
    ("deadline", "SEQ0420_SH0100-lighting-012", Department.LIGHTING, 12),
    ("tractor", "nightfall SEQ0420_SH0100 lighting v12", Department.LIGHTING, 12),
    ("tractor", "SEQ0420_SH0100 fx", Department.FX, None),
    ("qube", "SEQ0420_SH0100_cmp_v3", Department.COMP, 3),
    ("royalrender", "SEQ0420_SH0100.lgt.v9", Department.LIGHTING, 9),
])
def test_job_name_conventions_resolve_to_shot_and_stage(convention, job, dept, version):
    ref = parse_job_name(job, convention=convention)
    assert ref is not None
    assert ref.shot_id == "SEQ0420_SH0100"
    assert ref.sequence == "SEQ0420"
    assert ref.department is dept
    assert ref.version == version


@pytest.mark.parametrize("convention", ["deadline", "tractor", "qube", "royalrender"])
def test_non_shot_jobs_resolve_to_none(convention):
    for junk in ("nightly-cache-bake", "farm cleanup", "tooltest_scratch"):
        assert parse_job_name(junk, convention=convention) is None


def test_opencue_is_the_default_convention():
    assert parse_job_name("nightfall-SEQ0420_SH0100-a7f3c2d1_comp_v006") == \
        parse_opencue_job_name("nightfall-SEQ0420_SH0100-a7f3c2d1_comp_v006")


def test_template_convention_reads_a_studio_regex(monkeypatch):
    monkeypatch.setenv(
        "TURNAROUND_FARM_JOB_PATTERN",
        r"job::(?P<shot_id>SEQ\d{4}_SH\d{4})::(?P<dept>\w+)::v(?P<version>\d+)",
    )
    ref = parse_job_name("job::SEQ0420_SH0100::comp::v4", convention="template")
    assert ref is not None
    assert (ref.shot_id, ref.department, ref.version) == ("SEQ0420_SH0100", Department.COMP, 4)


def test_department_aliases_resolve_common_short_codes():
    assert department_from_token("lgt") is Department.LIGHTING
    assert department_from_token("CMP") is Department.COMP
    assert department_from_token("anm") is Department.ANIM
    assert department_from_token("matchmove") is None  # real stage, not one we model


# --------------------------------------------------------------------------
# studio-specific shot-id schemes
# --------------------------------------------------------------------------


def test_default_scheme_is_unchanged():
    assert sequence_of("SEQ0420_SH0100") == "SEQ0420"
    with pytest.raises(ValueError):
        sequence_of("0420_0100")  # not the default (seq_sh) spelling


@pytest.mark.parametrize("scheme_name,shot_id,sequence", [
    ("numeric", "0420_0100", "0420"),
    ("dash", "SQ042-SH0100", "SQ042"),
    ("loose", "ep02sq10_sh020", "ep02sq10"),
])
def test_alternate_schemes_parse_their_spelling(scheme_name, shot_id, sequence):
    scheme = SHOT_ID_SCHEMES[scheme_name]
    assert sequence_of(shot_id, scheme) == sequence
    ref = ShotRef.parse("show", shot_id, scheme)
    assert ref.sequence == sequence
    assert ref.shot_id == shot_id


def test_job_name_parse_respects_a_non_default_scheme():
    ref = parse_job_name("0420_0100 comp v2", convention="deadline",
                          scheme=SHOT_ID_SCHEMES["numeric"])
    assert ref is not None and ref.shot_id == "0420_0100" and ref.sequence == "0420"
    # the same job under the default scheme does not resolve
    assert parse_job_name("0420_0100 comp v2", convention="deadline") is None


# --------------------------------------------------------------------------
# editorial (EDL — every NLE exports one)
# --------------------------------------------------------------------------


_EDL = """TITLE: NIGHTFALL_R02_v14
FCM: NON-DROP FRAME

001  BL       V     C        00:00:00:00 00:00:02:00 01:00:00:00 01:00:02:00
* FROM CLIP NAME: BARS_AND_TONE

002  SEQ0420  V     C        01:00:00:00 01:00:04:12 01:00:02:00 01:00:06:12
* FROM CLIP NAME: SEQ0420_SH0100_comp_v006.mov

003  SEQ0420  V     D    012 01:00:04:12 01:00:07:00 01:00:06:12 01:00:09:00
* FROM CLIP NAME: SEQ0420_SH0110_comp_v002
"""


def test_edl_parse_extracts_the_cut_and_skips_non_shots():
    items = parse_edl(_EDL)
    assert [i.shot_id for i in items] == ["SEQ0420_SH0100", "SEQ0420_SH0110"]
    first = items[0]
    assert first.revision == 6
    assert first.duration_seconds == pytest.approx(4.5)
    assert first.record_in_seconds == pytest.approx(3602.0)


def test_shot_ids_from_edl_is_ordered_and_deduplicated():
    doubled = _EDL + (
        "\n004  SEQ0420  V     C        01:00:00:00 01:00:02:00 01:00:09:00 01:00:11:00\n"
        "* FROM CLIP NAME: SEQ0420_SH0100_comp_v007\n"
    )
    assert shot_ids_from_edl(doubled) == ["SEQ0420_SH0100", "SEQ0420_SH0110"]


def test_edl_maps_clips_with_a_non_default_scheme():
    edl = (
        "001  R1  V  C  01:00:00:00 01:00:02:00 01:00:00:00 01:00:02:00\n"
        "* FROM CLIP NAME: 0420_0100_comp_v1\n"
    )
    assert shot_ids_from_edl(edl, scheme=SHOT_ID_SCHEMES["numeric"]) == ["0420_0100"]


# --------------------------------------------------------------------------
# the CSV fallback + the support matrix
# --------------------------------------------------------------------------


def test_csv_schedule_source_normalises_any_tracker_export():
    csv_text = (
        "shot_id,department,status,pool,hours,at\n"
        "SEQ0420_SH0100,comp,cbb,comp-pool-1,52,2026-09-01T00:00:00\n"
        "SEQ0420_SH0110,comp,ip,comp-pool-1,44,2026-09-01T00:00:00\n"
    )
    src = CsvScheduleSource.from_csv(csv_text)
    events = list(src.iter_task_events())
    assert events[0].status is TaskStatus.RETAKE
    assert events[1].status is TaskStatus.WIP
    hours = list(src.iter_time_spent())
    assert sum(h.hours for h in hours) == pytest.approx(96.0)
    assert all(h.department is Department.COMP for h in hours)


def test_support_matrix_names_the_real_tools():
    assert "kitsu" in supported("schedule")
    assert {"shotgrid", "ftrack", "flow_production_tracking"} <= set(supported("schedule"))
    assert {"opencue", "deadline", "tractor", "qube", "royalrender"} <= set(supported("farm"))
    assert {"edl_cmx3600", "opentimelineio", "davinci_resolve",
            "avid_media_composer"} <= set(supported("editorial"))
    from bridge.sources import SUPPORTED_TOOLS

    kitsu = next(t for t in SUPPORTED_TOOLS["schedule"] if t.tool == "kitsu")
    assert kitsu.kind is Support.NATIVE


def test_scheme_regexes_have_the_required_groups():
    for scheme in SHOT_ID_SCHEMES.values():
        assert isinstance(scheme, ShotIdScheme)
        assert set(scheme.pattern.groupindex) >= {"sequence", "shot"}
        assert re.search(r"\{sequence\}.*\{shot\}", scheme.template)
