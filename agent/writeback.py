"""Kitsu write-back, behind an interface.

The Remediator's job ends with a change proposed back into the *production*
tool, not just a note in Grafana: a comment on the task, or a suggested status.
There is no live Kitsu in this environment (PROJECT.md, "Deferred by decision"
-- no container runtime), so the real ``gazu`` path sits behind a Protocol and a
recording implementation stands in. The recorder does two useful things:

* appends every write to a JSONL audit log (see :func:`default_log_path`);
* drops a Grafana annotation so the write-back is visible on the same
  dashboards the evidence came from -- the loop closes on screen.

Swapping in a real studio Kitsu is one class, no caller changes.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from bridge.annotate import Annotator, Mark

_ACTIONS = ("note", "status")
_LOG_ENV = "TURNAROUND_WRITEBACK_LOG"

#: One lock and one counter for the process, not one per instance. Each ask
#: builds its own :class:`RecordingKitsu` through :func:`default_writeback`, and
#: ``TURNAROUND_CONCURRENT_ASKS`` defaults to 2 -- so per-instance state would
#: have let two runs mint the same ``ref`` (the timestamp is second-resolution)
#: and interleave two appends into one line. ``next()`` on an ``itertools.count``
#: is a single C call and needs no lock of its own.
_LOG_LOCK = threading.Lock()
_SEQ = itertools.count(1)


def default_log_path() -> Path:
    """Where the audit log goes when the caller does not say.

    It used to be ``Path(__file__).parent/"_writeback.jsonl"`` -- inside the
    package. From a checkout that is merely untidy; from a wheel it is
    site-packages, which is frequently read-only and is never somewhere a user
    would think to look for their own audit trail. On Cloud Run it is a
    container layer that vanishes with the instance.

    So: an explicit ``TURNAROUND_WRITEBACK_LOG`` wins, then the XDG state
    directory, which is the standard home for exactly this -- data a program
    keeps between runs that is not configuration and not a cache.
    """
    override = (os.environ.get(_LOG_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    state = (os.environ.get("XDG_STATE_HOME") or "").strip()
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return base / "turnaround" / "writeback.jsonl"


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    ok: bool
    ref: str
    detail: str

    def to_dict(self) -> dict:
        return {"ok": self.ok, "ref": self.ref, "detail": self.detail}


class KitsuWriteBack(Protocol):
    def write(self, *, action: str, shot_id: str, department: str, text: str) -> WriteReceipt: ...


@dataclass(slots=True)
class RecordingKitsu:
    """Records write-backs to a JSONL file and (if Grafana is configured) to an annotation."""

    log_path: Path = field(default_factory=default_log_path)
    annotator: Annotator | None = None

    def write(self, *, action: str, shot_id: str, department: str, text: str) -> WriteReceipt:
        if action not in _ACTIONS:
            return WriteReceipt(False, "", f"unknown action {action!r}; expected one of {_ACTIONS}")
        ref = f"local-{datetime.now(UTC):%Y%m%dT%H%M%S}-{next(_SEQ):02d}"
        record = {
            "ref": ref, "action": action, "shot_id": shot_id,
            "department": department, "text": " ".join(text.split()),
            "at": datetime.now(UTC).isoformat(),
        }
        try:
            with _LOG_LOCK:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            # A read-only install or a full disk must not take the run down --
            # the rest of this module goes to some trouble to return receipts
            # rather than raise, and an audit log is the last thing that should
            # be the exception. No annotation either: a Grafana mark asserts
            # that the write-back was recorded, and it was not.
            return WriteReceipt(
                False, ref,
                f"could not record the write-back at {self.log_path}: {exc}. "
                f"Set {_LOG_ENV} to a writable path.",
            )

        annotated = self._annotate(record)
        detail = f"recorded in {self.log_path}"
        return WriteReceipt(True, ref, detail + (" and annotated in Grafana" if annotated else ""))

    def _annotate(self, record: dict) -> bool:
        ann = self.annotator or Annotator()
        if not ann.enabled:
            return False
        verb = "note" if record["action"] == "note" else "status suggestion"
        mark = Mark(
            at=datetime.now(UTC),
            text=(f"Turnaround Remediator → Kitsu {verb} on "
                  f"{record['shot_id']} / {record['department']}: {record['text']}"),
            tags=("turnaround", "remediation", "write-back", record["shot_id"]),
        )
        try:
            ann.write([mark])
        except Exception:  # noqa: BLE001 -- annotation is a nicety, never fail the write-back on it
            return False
        return True


@dataclass(slots=True)
class GazuKitsu:
    """Real Kitsu via gazu. Drops in when a studio instance exists.

    Never raises into the agent: an unimplemented or unreachable write-back
    returns a failed receipt, which the model reports, rather than crashing the
    run.
    """

    def write(self, *, action: str, shot_id: str, department: str, text: str) -> WriteReceipt:
        del action, shot_id, department, text
        return WriteReceipt(  # pragma: no cover - needs a live Kitsu
            False, "",
            "gazu write-back is not implemented and no live Kitsu is configured. "
            "The real path (authenticate, resolve shot_id+department to a task, "
            "gazu.task.add_comment / set status) lands here once a studio instance "
            "exists; until then set TURNAROUND_KITSU_LIVE unset to use RecordingKitsu.",
        )


def _kitsu_is_live() -> bool:
    """A real studio Kitsu, not the .env.example placeholder.

    ``KITSU_URL`` ships in ``.env.example`` pointed at ``http://localhost/api``
    with no server behind it, so its mere presence means nothing. Require an
    explicit opt-in and a non-loopback URL.
    """
    if not os.environ.get("TURNAROUND_KITSU_LIVE"):
        return False
    url = os.environ.get("KITSU_URL", "")
    return bool(url) and "localhost" not in url and "127.0.0.1" not in url


def default_writeback() -> KitsuWriteBack:
    return GazuKitsu() if _kitsu_is_live() else RecordingKitsu()


def make_write_back_tool(client: KitsuWriteBack):
    """A plain function for ADK's FunctionTool. Name is significant: the approval
    gate matches on ``kitsu_write_back`` (see ``agent/approval.py``)."""

    def kitsu_write_back(action: str, shot_id: str, department: str, text: str) -> dict:
        """Propose a change back into Kitsu, the production tool.

        Args:
            action: "note" to add a task comment, or "status" to suggest a task status.
            shot_id: canonical shot id, e.g. "SEQ0420_SH0100".
            department: pipeline stage the write applies to, e.g. "comp".
            text: the comment body, or the suggested status plus a one-line reason.

        Returns:
            A receipt dict: ok, ref, detail.
        """
        return client.write(
            action=action, shot_id=shot_id, department=department, text=text,
        ).to_dict()

    return kitsu_write_back
