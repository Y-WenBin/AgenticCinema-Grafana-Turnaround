"""Kitsu write-back, behind an interface.

The Remediator's job ends with a change proposed back into the *production*
tool, not just a note in Grafana: a comment on the task, or a suggested status.
There is no live Kitsu in this environment (PROJECT.md, "Deferred by decision"
-- no container runtime), so the real ``gazu`` path sits behind a Protocol and a
recording implementation stands in. The recorder does two useful things:

* appends every write to ``agent/_writeback.jsonl`` (gitignored) as an audit log;
* drops a Grafana annotation so the write-back is visible on the same
  dashboards the evidence came from -- the loop closes on screen.

Swapping in a real studio Kitsu is one class, no caller changes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from bridge.annotate import Annotator, Mark

_LOG_PATH = Path(__file__).resolve().parent / "_writeback.jsonl"
_ACTIONS = ("note", "status")


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

    log_path: Path = _LOG_PATH
    annotator: Annotator | None = None
    _seq: int = 0

    def write(self, *, action: str, shot_id: str, department: str, text: str) -> WriteReceipt:
        if action not in _ACTIONS:
            return WriteReceipt(False, "", f"unknown action {action!r}; expected one of {_ACTIONS}")
        self._seq += 1
        ref = f"local-{datetime.now(UTC):%Y%m%dT%H%M%S}-{self._seq:02d}"
        record = {
            "ref": ref, "action": action, "shot_id": shot_id,
            "department": department, "text": " ".join(text.split()),
            "at": datetime.now(UTC).isoformat(),
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

        annotated = self._annotate(record)
        detail = "recorded" + (" and annotated in Grafana" if annotated else "")
        return WriteReceipt(True, ref, detail)

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
