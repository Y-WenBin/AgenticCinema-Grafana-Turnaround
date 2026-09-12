"""Kitsu write-back: the recorder is an audit log plus an optional Grafana
annotation, and the real gazu path stays behind the interface."""

from __future__ import annotations

import json
from pathlib import Path

from agent.writeback import (
    GazuKitsu,
    RecordingKitsu,
    WriteReceipt,
    default_log_path,
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


class TestTheAuditLogLivesSomewhereWritable:
    """It used to live inside the package, and the append was unguarded.

    From a checkout that was untidy. From a wheel it is site-packages -- often
    read-only, never where a user would look for their own audit trail -- and an
    ``OSError`` from the append would have escaped into the agent run, which
    every other path in this module goes out of its way to prevent.
    """

    def test_the_default_is_not_inside_the_installed_package(self, monkeypatch):
        monkeypatch.delenv("TURNAROUND_WRITEBACK_LOG", raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        import agent

        package = Path(agent.__file__).resolve().parent
        assert package not in default_log_path().resolve().parents

    def test_xdg_state_home_is_honoured(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TURNAROUND_WRITEBACK_LOG", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert default_log_path() == tmp_path / "turnaround" / "writeback.jsonl"

    def test_an_explicit_path_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ignored"))
        monkeypatch.setenv("TURNAROUND_WRITEBACK_LOG", str(tmp_path / "mine.jsonl"))
        assert default_log_path() == tmp_path / "mine.jsonl"

    def test_a_missing_directory_is_created(self, tmp_path):
        log = tmp_path / "deep" / "deeper" / "wb.jsonl"
        client = RecordingKitsu(log_path=log, annotator=_NoAnnotator())
        assert client.write(action="note", shot_id="SEQ0420_SH0100",
                            department="comp", text="x").ok
        assert log.is_file()

    def test_an_unwritable_log_returns_a_receipt_rather_than_raising(self, tmp_path):
        """The run keeps its answer. The receipt says what went wrong and how to
        fix it, because the model reads the receipt out to the user."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        client = RecordingKitsu(log_path=blocker / "wb.jsonl", annotator=_NoAnnotator())
        receipt = client.write(action="note", shot_id="SEQ0420_SH0100",
                               department="comp", text="x")
        assert not receipt.ok
        assert "TURNAROUND_WRITEBACK_LOG" in receipt.detail

    def test_an_unrecorded_write_is_not_annotated_in_grafana(self, tmp_path):
        """An annotation asserts the write-back was recorded. If it was not,
        leaving a mark on the dashboard would be the one dishonest outcome."""
        marks = []

        class _Counting:
            enabled = True

            def write(self, batch):
                marks.extend(batch)

        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        RecordingKitsu(log_path=blocker / "wb.jsonl", annotator=_Counting()).write(
            action="note", shot_id="SEQ0420_SH0100", department="comp", text="x")
        assert marks == []

    def test_two_recorders_in_one_process_do_not_collide(self, tmp_path):
        """``TURNAROUND_CONCURRENT_ASKS`` defaults to 2 and each ask builds its
        own recorder, so the sequence has to be shared rather than per-instance
        -- the timestamp in a ref is only second-resolution."""
        log = tmp_path / "wb.jsonl"
        refs = {
            RecordingKitsu(log_path=log, annotator=_NoAnnotator()).write(
                action="note", shot_id="SEQ0420_SH0100", department="comp", text="x"
            ).ref
            for _ in range(5)
        }
        assert len(refs) == 5
        assert len(log.read_text().strip().splitlines()) == 5
