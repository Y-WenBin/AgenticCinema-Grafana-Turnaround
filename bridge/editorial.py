"""Editorial ingest: pull the shot list out of a cut.

The creative-schedule side of the join is usually a tracker (``bridge.sources``),
but the *authoritative* statement of what is in the show right now is the
editor's cut. Turnaround reads a cut through one of two interchange formats, so
it does not care which NLE produced it:

* **CMX3600 EDL** -- ``parse_edl`` / ``shot_ids_from_edl``, pure stdlib. DaVinci
  Resolve, Premiere Pro, Avid Media Composer, Shotcut and Flame all export one;
  an online/conform EDL that carries the shot only in the reel column works too.
* **OpenTimelineIO** -- ``iter_otio_cut``, needs ``opentimelineio`` (``pip
  install 'turnaround[editorial]'``). DaVinci Resolve 18+, Blender's VSE and
  Kdenlive export ``.otio`` directly; Final Cut Pro (FCPXML), Premiere (FCP7
  XML) and Avid (AAF) reach it by also installing the matching
  ``otio-*-adapter`` package.

Every clip name (or reel) is mapped to a shot id with the active
``ShotIdScheme``, so a recut -- the perturbation the seed models as a director
note -- shows up as a change in the cut, not only as a tracker status.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from bridge.ontology import DEFAULT_SHOT_ID_SCHEME, ShotIdScheme
from bridge.sources import CutItem

# A CMX3600 event line: event number, reel/tape, track, edit code, then four
# timecodes (src in/out, rec in/out). A dissolve/wipe inserts a
# transition-duration column before the timecodes, so instead of pinning the
# column count we read the reel from the head and take the last four
# timecode-shaped tokens on the line.
#   001  SEQ0420_SH0100 V     C        01:00:00:00 01:00:04:12 00:59:58:00 01:00:02:12
#   003  AX             V     D    012 01:00:04:12 01:00:07:00 01:00:06:12 01:00:09:00
_EVENT_HEAD_RE = re.compile(r"^\s*(?P<num>\d+)\s+(?P<reel>\S+)\s+(?P<track>\S+)\s+(?P<edit>\S+)\s")
_TC_RE = re.compile(r"\b\d{1,2}[:;]\d{2}[:;]\d{2}[:;]\d{2,3}\b")
# The comment that names the source. Different tools use different keys; any of
# these carries the clip name or file path we can pull a shot id from.
_CLIP_NAME_RE = re.compile(
    r"^\*\s*(?:FROM\s+CLIP\s+NAME|SOURCE\s+FILE|FROM\s+FILE|CLIP\s+NAME)\s*:\s*(?P<name>.+?)\s*$",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"[_.\-]v(?P<version>\d{1,4})\b", re.IGNORECASE)
_NON_SHOT_REELS = {"AX", "BL", "BLACK", "NONE", "TAPE", "TBD"}
_FPS_DEFAULT = 24.0


def _tc_to_seconds(tc: str, fps: float = _FPS_DEFAULT) -> float:
    """CMX timecode HH:MM:SS:FF (or ';' for drop-frame) to seconds."""
    parts = re.split(r"[:;]", tc)
    if len(parts) != 4:
        return 0.0
    h, m, s, f = (int(p) for p in parts)
    return h * 3600 + m * 60 + s + f / fps


def _unanchored(pattern: re.Pattern[str]) -> re.Pattern[str]:
    """The scheme's regex with its ^ / $ anchors removed, for substring search."""
    body = pattern.pattern.removeprefix("^").removesuffix("$")
    return re.compile(body, pattern.flags & ~re.DOTALL)


def _shot_id_from_clip(name: str, scheme: ShotIdScheme) -> str | None:
    """Resolve a shot id from a clip name, file name or full path.

    Handles '<shot>[_<dept>][_v###][.ext]' and both '/' and '\\' path
    separators: peel the directory, the extension and any trailing
    '_dept' / '_v###' tokens until what remains is a valid shot id; failing
    that, search for a scheme-shaped id anywhere in the string.
    """
    base = re.split(r"[\\/]", name.strip())[-1]
    stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", base)
    if scheme.sequence(stem) is not None:
        return stem
    tokens = re.split(r"[_\s]+", stem)
    for cut in range(len(tokens), 0, -1):
        candidate = "_".join(tokens[:cut])
        if scheme.sequence(candidate) is not None:
            return candidate
    m = _unanchored(scheme.pattern).search(name)
    return m.group(0) if m else None


def _reel_shot_id(reel: str, scheme: ShotIdScheme) -> str | None:
    """A conform/online EDL often carries the shot only in the reel column."""
    if not reel or reel.upper() in _NON_SHOT_REELS:
        return None
    return _shot_id_from_clip(reel, scheme)


def parse_edl(text: str, *, fps: float = _FPS_DEFAULT,
              scheme: ShotIdScheme | None = None) -> list[CutItem]:
    """Parse a CMX3600 EDL into the cut's shots, in record order.

    The shot id comes from a ``FROM CLIP NAME`` / ``SOURCE FILE`` comment when
    there is one, otherwise from the reel column. Events that resolve to no shot
    id (bars, tone, slugs, black) are skipped.
    """
    scheme = scheme or DEFAULT_SHOT_ID_SCHEME
    items: list[CutItem] = []
    reel: str | None = None
    rec: tuple[float, float] | None = None
    clip_name: str | None = None

    def flush() -> None:
        nonlocal reel, rec, clip_name
        if rec is not None:
            shot_id = (_shot_id_from_clip(clip_name, scheme) if clip_name
                       else None) or _reel_shot_id(reel or "", scheme)
            if shot_id is not None:
                src = clip_name or reel or shot_id
                ver = _VERSION_RE.search(src)
                items.append(CutItem(
                    shot_id=shot_id,
                    source_name=src,
                    record_in_seconds=rec[0],
                    duration_seconds=max(0.0, rec[1] - rec[0]),
                    revision=int(ver["version"]) if ver else None,
                ))
        reel = rec = clip_name = None

    for raw in text.splitlines():
        line = raw.rstrip()
        head = _EVENT_HEAD_RE.match(line)
        if head:
            flush()
            reel = head["reel"]
            tcs = _TC_RE.findall(line)
            rec = ((_tc_to_seconds(tcs[-2], fps), _tc_to_seconds(tcs[-1], fps))
                   if len(tcs) >= 4 else None)
            continue
        comment = _CLIP_NAME_RE.match(line)
        if comment and clip_name is None:
            clip_name = comment["name"]
    flush()
    return items


def shot_ids_from_edl(text: str, *, scheme: ShotIdScheme | None = None) -> list[str]:
    """Just the ordered, de-duplicated shot ids in a cut."""
    seen: dict[str, None] = {}
    for item in parse_edl(text, scheme=scheme):
        seen.setdefault(item.shot_id, None)
    return list(seen)


def _otio_clips(timeline: object) -> Iterator[object]:
    """``find_clips`` on OTIO >= 0.15, ``each_clip`` on older releases."""
    for attr in ("find_clips", "each_clip"):
        fn = getattr(timeline, attr, None)
        if callable(fn):
            yield from fn()
            return


def _otio_name(clip: object) -> str:
    name = getattr(clip, "name", "") or ""
    if name:
        return name
    ref = getattr(clip, "media_reference", None)
    return getattr(ref, "target_url", "") or ""


def iter_otio_cut(path: str, *, scheme: ShotIdScheme | None = None) -> Iterator[CutItem]:
    """Yield CutItems from an OpenTimelineIO-readable file.

    ``.otio`` / ``.otiod`` / ``.otioz`` (DaVinci Resolve 18+, Blender VSE and
    Kdenlive all export ``.otio``) need only ``opentimelineio``. ``.fcpxml``
    (Final Cut), FCP7 ``.xml`` (Premiere) and ``.aaf`` (Avid) additionally need
    the matching ``otio-*-adapter`` package. For a plain EDL from any tool, use
    ``parse_edl`` -- it needs nothing extra.
    """
    try:
        import opentimelineio as otio
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "iter_otio_cut needs opentimelineio: pip install 'turnaround[editorial]'"
        ) from exc

    scheme = scheme or DEFAULT_SHOT_ID_SCHEME
    timeline = otio.adapters.read_from_file(path)
    for clip in _otio_clips(timeline):
        name = _otio_name(clip)
        shot_id = _shot_id_from_clip(name, scheme) if name else None
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
