"""Kitsu write-back: the recorder is an audit log plus an optional Grafana
annotation, and the real gazu path stays behind the interface."""

from __future__ import annotations

import json

from agent.writeback import (
    GazuKitsu,
    RecordingKitsu,
    WriteReceipt,
    default_writeback,
    make_write_back_tool,
)


def test_recording_kitsu_appends_one_jsonl_line_per_write(tmp_path):
    log = tmp_path / "wb.jsonl"
    client = RecordingKitsu(log_path=log, annotator=_NoAnnotator())
    r1 = client.write(action="note", shot_id="SEQ0420_SH0100", department="comp", text="  fix   the   cache ")
    r2 = client.write(action="status", shot_id="SEQ0420_SH0100", department="comp", text="retake -> wip")

    assert r1.ok and r2.ok and r1.ref != r2.ref
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["action"] == "note"
    assert first["text"] == "fix the cache"  # whitespace collapsed
    assert first["shot_id"] == "SEQ0420_SH0100"


def test_unknown_action_is_rejected_without_writing(tmp_path):
    log = tmp_path / "wb.jsonl"
    client = RecordingKitsu(log_path=log, annotator=_NoAnnotator())
    r = client.write(action="delete", shot_id="SEQ0420_SH0100", department="comp", text="x")
    assert not r.ok and "unknown action" in r.detail
    assert not log.exists()


def test_annotation_is_best_effort(tmp_path):
    log = tmp_path / "wb.jsonl"
    client = RecordingKitsu(log_path=log, annotator=_BoomAnnotator())
    r = client.write(action="note", shot_id="SEQ0420_SH0100", department="comp", text="x")
    assert r.ok  # write-back still succeeds even though the annotation raised
    assert "annotated" not in r.detail


def test_gazu_kitsu_returns_a_failed_receipt_never_raises():
    r = GazuKitsu().write(action="note", shot_id="SEQ0420_SH0100", department="comp", text="x")
    assert not r.ok and r.ref == "" and "not implemented" in r.detail


def test_default_writeback_selects_recorder_unless_kitsu_is_live(monkeypatch):
    monkeypatch.delenv("TURNAROUND_KITSU_LIVE", raising=False)
    monkeypatch.setenv("KITSU_URL", "http://localhost/api")  # the .env.example placeholder
    assert isinstance(default_writeback(), RecordingKitsu)

    monkeypatch.setenv("TURNAROUND_KITSU_LIVE", "1")
    monkeypatch.setenv("KITSU_URL", "http://localhost/api")  # loopback still does not count
    assert isinstance(default_writeback(), RecordingKitsu)

    monkeypatch.setenv("KITSU_URL", "https://kitsu.studio.example/api")
    assert isinstance(default_writeback(), GazuKitsu)


def test_tool_wrapper_returns_receipt_dict():
    calls = []

    class Fake:
        def write(self, **kw):
            calls.append(kw)
            return WriteReceipt(True, "ref-1", "recorded")

    fn = make_write_back_tool(Fake())
    out = fn(action="note", shot_id="SEQ0420_SH0100", department="comp", text="fix")
    assert out == {"ok": True, "ref": "ref-1", "detail": "recorded"}
    assert calls[0]["shot_id"] == "SEQ0420_SH0100"
    assert fn.__name__ == "kitsu_write_back"  # the approval gate matches on this


class _NoAnnotator:
    enabled = False


class _BoomAnnotator:
    enabled = True

    def write(self, _marks):
        raise RuntimeError("grafana down")
