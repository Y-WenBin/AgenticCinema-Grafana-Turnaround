"""Editorial ingest: pull the shot list out of a cut.

The creative-schedule side of the join is usually a tracker (``bridge.sources``),
but the *authoritative* statement of what is in the show right now is the editor's
cut. Every NLE -- Avid Media Composer, Premiere, DaVinci Resolve, Final Cut --
exports a CMX3600 EDL, and most also export OpenTimelineIO. This module reads
either and maps each clip to a shot id via the active ``ShotIdScheme``, so a
recut (the exact perturbation the seed models as a director note) is visible as a
change in the cut, not just as a tracker status.

``parse_edl`` is pure stdlib. ``iter_otio_cut`` needs ``opentimelineio`` and is
guarded -- install it with ``pip install turnaround[editorial]``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from bridge.ontology import DEFAULT_SHOT_ID_SCHEME, ShotIdScheme
from bridge.sources import CutItem

# CMX3600 event lines start with an event number, a reel, a track and an edit
# code, then four timecodes (src in/out, rec in/out). A dissolve/wipe adds a
# transition-duration column before the timecodes, so rather than pin the column
# count, take the last four timecode-shaped tokens on the line.
#   001  R075_SH0100 V     C        01:00:00:00 01:00:04:12 00:59:58:00 01:00:02:12
#   003  R075_SH0100 V     D    012 01:00:04:12 01:00:07:00 01:00:06:12 01:00:09:00
# The useful comment line that follows is
#   * FROM CLIP NAME: R075_SH0100_comp_v006
_EVENT_HEAD_RE = re.compile(r"^(?P<num>\d+)\s+(?P<reel>\S+)\s+(?P<track>\S+)\s+(?P<edit>\S+)\s")
_TC_RE = re.compile(r"\b\d{1,2}[:;]\d{2}[:;]\d{2}[:;]\d{2,3}\b")
_CLIP_NAME_RE = re.compile(r"^\*\s*FROM CLIP NAME:\s*(?P<name>.+?)\s*$", re.IGNORECASE)
_VERSION_RE = re.compile(r"[_.\-]v(?P<version>\d{1,4})\b", re.IGNORECASE)
_FPS_DEFAULT = 24.0


def _tc_to_seconds(tc: str, fps: float = _FPS_DEFAULT) -> float:
    """CMX timecode HH:MM:SS:FF (or ';' for drop-frame) to seconds."""
    parts = re.split(r"[:;]", tc)
    if len(parts) != 4:
        return 0.0
    h, m, s, f = (int(p) for p in parts)
    return h * 3600 + m * 60 + s + f / fps


def _shot_id_from_clip(name: str, scheme: ShotIdScheme) -> str | None:
    """A clip name is '<shot>[_<dept>][_v###][.ext]'. Peel the extension and any
    trailing '_dept'/'_v###' tokens until what remains is a valid shot id."""
    stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name.strip())
    if scheme.sequence(stem) is not None:
        return stem
    tokens = re.split(r"[_\s]+", stem)
    for cut in range(len(tokens), 0, -1):
        candidate = "_".join(tokens[:cut])
        if scheme.sequence(candidate) is not None:
            return candidate
    # last resort: a scheme-shaped substring anywhere in the name
    m = scheme.pattern.search(name) if scheme.pattern.groups else None
    return m.group(0) if m else None


def parse_edl(text: str, *, fps: float = _FPS_DEFAULT,
              scheme: ShotIdScheme | None = None) -> list[CutItem]:
    """Parse a CMX3600 EDL into the cut's shots, in record order.

    Events whose clip name does not resolve to a shot id (slugs, colour bars,
    tone) are skipped -- they are real but not shots.
    """
    scheme = scheme or DEFAULT_SHOT_ID_SCHEME
    items: list[CutItem] = []
    pending: tuple[float, float] | None = None  # (rec_in, rec_out) seconds

    for raw in text.splitlines():
        line = raw.rstrip()
        if _EVENT_HEAD_RE.match(line):
            tcs = _TC_RE.findall(line)
            if len(tcs) >= 4:
                pending = (_tc_to_seconds(tcs[-2], fps), _tc_to_seconds(tcs[-1], fps))
            else:
                pending = None
            continue
        clip = _CLIP_NAME_RE.match(line)
        if clip and pending is not None:
            name = clip["name"]
            shot_id = _shot_id_from_clip(name, scheme)
            if shot_id is not None:
                ver = _VERSION_RE.search(name)
                rec_in, rec_out = pending
                items.append(CutItem(
                    shot_id=shot_id,
                    source_name=name,
                    record_in_seconds=rec_in,
                    duration_seconds=max(0.0, rec_out - rec_in),
                    revision=int(ver["version"]) if ver else None,
                ))
            pending = None
    return items


def shot_ids_from_edl(text: str, *, scheme: ShotIdScheme | None = None) -> list[str]:
    """Just the ordered, de-duplicated shot ids in a cut."""
    seen: dict[str, None] = {}
    for item in parse_edl(text, scheme=scheme):
        seen.setdefault(item.shot_id, None)
    return list(seen)


def iter_otio_cut(path: str, *, scheme: ShotIdScheme | None = None) -> Iterator[CutItem]:
    """Yield CutItems from any OpenTimelineIO-readable file (.otio, .edl, .aaf
    with the AAF adapter, .fcpxml, ...). Requires ``opentimelineio``."""
    try:
        import opentimelineio as otio
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "iter_otio_cut needs opentimelineio: pip install 'turnaround[editorial]'"
        ) from exc

    scheme = scheme or DEFAULT_SHOT_ID_SCHEME
    timeline = otio.adapters.read_from_file(path)
    for clip in timeline.each_clip():
        name = getattr(clip, "name", "") or ""
        shot_id = _shot_id_from_clip(name, scheme)
        if shot_id is None:
            continue
        try:
            trimmed = clip.trimmed_range_in_parent()
            rec_in = trimmed.start_time.to_seconds()
            dur = trimmed.duration.to_seconds()
        except Exception:  # noqa: BLE001 - OTIO range math varies by adapter
            rec_in = dur = 0.0
        ver = _VERSION_RE.search(name)
        yield CutItem(
            shot_id=shot_id,
            source_name=name,
            record_in_seconds=rec_in,
            duration_seconds=dur,
            revision=int(ver["version"]) if ver else None,
        )
