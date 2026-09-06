"""Editorial ingest against the shapes real NLEs actually export.

Turnaround reads a cut through two interchange formats, so it does not care
which editor produced it:

* **CMX3600 EDL** -- DaVinci Resolve, Premiere Pro, Avid, Shotcut, Flame, plus
  online/conform EDLs that carry the shot only in the reel column.
* **OpenTimelineIO** -- Resolve 18+, Blender VSE and Kdenlive export ``.otio``
  directly.

Each fixture below mirrors one tool's real output (reel conventions, comment
keys, drop-frame timecode, dissolve columns, file paths).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bridge.editorial import iter_otio_cut, parse_edl, shot_ids_from_edl
from bridge.ontology import SHOT_ID_SCHEMES

otio = pytest.importorskip("opentimelineio")


# --------------------------------------------------------------------------
# EDL -- one fixture per tool / dialect
# --------------------------------------------------------------------------

RESOLVE_EDL = """TITLE: SEQ0420_conform_v012
FCM: DROP FRAME

001  SEQ0420_SH0100_comp_v006 V     C        01:00:04;00 01:00:09;12 01:00:00;00 01:00:05;12
* FROM CLIP NAME: SEQ0420_SH0100_comp_v006.mov
* SOURCE FILE: SEQ0420_SH0100_comp_v006.mov

002  SEQ0420_SH0110_comp_v002 V     C        01:00:00;00 01:00:03;00 01:00:05;12 01:00:08;12
* FROM CLIP NAME: SEQ0420_SH0110_comp_v002.mov
* SOURCE FILE: SEQ0420_SH0110_comp_v002.mov
"""

PREMIERE_EDL = """TITLE: SEQ0420 EDIT
FCM: NON-DROP FRAME

001  AX       V     C        00:00:00:00 00:00:04:00 01:00:00:00 01:00:04:00
* FROM CLIP NAME: SEQ0420_SH0100_v3.mov
* COMMENT:
002  AX       V     C        00:00:00:00 00:00:03:12 01:00:04:00 01:00:07:12
* FROM CLIP NAME: SEQ0420_SH0120_v1.mov
"""

SHOTCUT_EDL = """TITLE: timeline

001  AX       V     C        00:00:00:00 00:00:05:00 00:00:00:00 00:00:05:00
* FROM CLIP NAME: SEQ0420_SH0100_comp.mp4
002  AX       V     C        00:00:00:00 00:00:02:12 00:00:05:00 00:00:07:12
* FROM CLIP NAME: SEQ0420_SH0130_comp.mp4
"""

AVID_EDL = """TITLE: SEQ0420_v4
FCM: NON-DROP FRAME
001  001      V     C        01:00:00:00 01:00:04:00 01:00:00:00 01:00:04:00
* FROM CLIP NAME:  SEQ0420_SH0100_comp_v6
* SOURCE FILE: SEQ0420_SH0100_COMP_V6.MXF
"""

# An online/conform EDL: the shot id is only in the reel/tape column, and there
# are slug events (BL = black) that must be skipped.
CONFORM_EDL = """TITLE: SEQ0420 ONLINE
FCM: NON-DROP FRAME
001  SEQ0420_SH0100  V     C        01:00:00:00 01:00:04:00 01:00:00:00 01:00:04:00
002  SEQ0420_SH0140  V     C        01:00:00:00 01:00:02:00 01:00:04:00 01:00:06:00
003  BL              V     C        01:00:00:00 01:00:01:00 01:00:06:00 01:00:07:00
"""

# A dissolve inserts a transition-duration column before the timecodes.
DISSOLVE_EDL = """001  BL     V  C        00:00:00:00 00:00:00:00 01:00:10:00 01:00:10:00
001  AX     V  D    025 01:00:00:00 01:00:05:00 01:00:10:00 01:00:15:00
* FROM CLIP NAME: SEQ0420_SH0170_comp_v1.mov
"""


@pytest.mark.parametrize("label,edl,expected", [
    ("resolve", RESOLVE_EDL, ["SEQ0420_SH0100", "SEQ0420_SH0110"]),
    ("premiere", PREMIERE_EDL, ["SEQ0420_SH0100", "SEQ0420_SH0120"]),
    ("shotcut", SHOTCUT_EDL, ["SEQ0420_SH0100", "SEQ0420_SH0130"]),
    ("avid", AVID_EDL, ["SEQ0420_SH0100"]),
    ("conform-reel-only", CONFORM_EDL, ["SEQ0420_SH0100", "SEQ0420_SH0140"]),
    ("dissolve", DISSOLVE_EDL, ["SEQ0420_SH0170"]),
])
def test_edl_dialects_resolve_to_the_same_shot_ids(label, edl, expected):
    assert shot_ids_from_edl(edl) == expected


def test_resolve_edl_keeps_record_timing_and_version():
    first = parse_edl(RESOLVE_EDL)[0]
    assert first.shot_id == "SEQ0420_SH0100"
    assert first.revision == 6
    # 01:00:00;12 - 01:00:00;00 record range at ~24fps drop-frame
    assert first.duration_seconds == pytest.approx(5.5, abs=0.1)


@pytest.mark.parametrize("clip_line", [
    "* FROM CLIP NAME: /mnt/prod/nightfall/SEQ0420/SH0150/SEQ0420_SH0150_comp_v9.exr",
    "* SOURCE FILE: X:\\prod\\SEQ0420_SH0150_comp_v9.mov",
    "* FROM FILE: SEQ0420_SH0150.mov",
])
def test_edl_pulls_the_shot_from_a_path_or_alternate_comment_key(clip_line):
    edl = ("001  AX  V  C  01:00:00:00 01:00:02:00 01:00:00:00 01:00:02:00\n"
           + clip_line + "\n")
    assert shot_ids_from_edl(edl) == ["SEQ0420_SH0150"]


def test_edl_skips_bars_tone_and_black():
    edl = (
        "001  BL  V  C  00:00:00:00 00:00:02:00 01:00:00:00 01:00:02:00\n"
        "* FROM CLIP NAME: BARS_AND_TONE\n"
        "002  AX  V  C  00:00:00:00 00:00:03:00 01:00:02:00 01:00:05:00\n"
        "* FROM CLIP NAME: SEQ0420_SH0100_comp_v1\n"
    )
    assert shot_ids_from_edl(edl) == ["SEQ0420_SH0100"]


def test_edl_is_ordered_and_deduplicated():
    doubled = RESOLVE_EDL + (
        "\n003  AX  V  C  01:00:00;00 01:00:02;00 01:00:09;00 01:00:11;00\n"
        "* FROM CLIP NAME: SEQ0420_SH0100_comp_v007\n"
    )
    assert shot_ids_from_edl(doubled) == ["SEQ0420_SH0100", "SEQ0420_SH0110"]


def test_edl_honours_a_non_default_shot_id_scheme():
    edl = (
        "001  R1  V  C  01:00:00:00 01:00:02:00 01:00:00:00 01:00:02:00\n"
        "* FROM CLIP NAME: 0420_0100_comp_v1\n"
    )
    assert shot_ids_from_edl(edl, scheme=SHOT_ID_SCHEMES["numeric"]) == ["0420_0100"]
    assert shot_ids_from_edl(edl) == []  # not the default (seq_sh) spelling


# --------------------------------------------------------------------------
# OpenTimelineIO -- Resolve / Blender VSE / Kdenlive export .otio
# --------------------------------------------------------------------------


def _write_timeline(clips: list[tuple[str, int, int, str | None]]) -> str:
    tl = otio.schema.Timeline(name="SEQ0420_reel")
    track = otio.schema.Track(name="V1")
    tl.tracks.append(track)
    for name, start, dur, url in clips:
        ref = (otio.schema.ExternalReference(target_url=url) if url
               else otio.schema.MissingReference())
        track.append(otio.schema.Clip(
            name=name, media_reference=ref,
            source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(start, 24),
                otio.opentime.RationalTime(dur, 24))))
    path = Path(tempfile.mkdtemp()) / "cut.otio"
    otio.adapters.write_to_file(tl, str(path))
    return str(path)


def test_otio_timeline_resolves_named_clips_and_skips_non_shots():
    path = _write_timeline([
        ("SEQ0420_SH0100_comp_v6", 0, 120, None),
        ("bars", 0, 24, None),
        ("SEQ0420_SH0180", 120, 60, None),
    ])
    items = list(iter_otio_cut(path))
    assert [i.shot_id for i in items] == ["SEQ0420_SH0100", "SEQ0420_SH0180"]
    assert items[0].revision == 6
    assert items[0].duration_seconds == pytest.approx(5.0)


def test_otio_falls_back_to_the_media_path_when_the_clip_name_is_blank():
    # Premiere / AAF via OTIO frequently leave clip.name empty.
    path = _write_timeline([
        ("", 0, 48, "file:///mnt/prod/SEQ0420_SH0200_comp_v2.mov"),
    ])
    items = list(iter_otio_cut(path))
    assert [i.shot_id for i in items] == ["SEQ0420_SH0200"]
    assert items[0].revision == 2


def test_otio_honours_a_non_default_scheme():
    path = _write_timeline([("0420_0100_comp_v1", 0, 24, None)])
    assert [i.shot_id for i in iter_otio_cut(path, scheme=SHOT_ID_SCHEMES["numeric"])] \
        == ["0420_0100"]
