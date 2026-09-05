"""The emitter is where production identity becomes telemetry identity.

If trace ids are not stable, the agent cannot reach a shot's history without a
lookup table, reseeding duplicates every trace, and a scripted demo stops being
reproducible. So stability is tested, not assumed.
"""

from datetime import datetime, timedelta, timezone

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from bridge.emit import Emitter, StageEvent, trace_id_for_shot
from bridge.ontology import Attr, Department, TaskStatus
from bridge.privacy import PrivacyViolation

START = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def stage(**overrides) -> StageEvent:
    defaults = dict(
        show="nightfall",
        shot_id="SEQ0420_SH0100",
        department=Department.COMP,
        artist="a7f3c2d1",
        vendor="vendor-b",
        pool="comp-pool-2",
        status=TaskStatus.WIP,
        iteration=1,
        started_at=START,
        ended_at=START + timedelta(days=2),
    )
    return StageEvent(**{**defaults, **overrides})


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


@pytest.fixture
def emitter(exporter):
    e = Emitter(span_exporter=exporter)
    yield e
    e.shutdown()


class TestDeterministicIdentity:
    def test_trace_id_is_derivable_from_the_shot_id_alone(self, emitter, exporter):
        emitter.emit_stage(stage())
        (span,) = exporter.get_finished_spans()
        assert format(span.context.trace_id, "032x") == trace_id_for_shot("SEQ0420_SH0100")

    def test_all_stages_of_a_shot_share_one_trace(self, emitter, exporter):
        for dept in (Department.LIGHTING, Department.COMP, Department.DI):
            emitter.emit_stage(stage(department=dept))
        trace_ids = {s.context.trace_id for s in exporter.get_finished_spans()}
        assert len(trace_ids) == 1

    def test_different_shots_do_not_collide(self, emitter, exporter):
        emitter.emit_stage(stage(shot_id="SEQ0420_SH0100"))
        emitter.emit_stage(stage(shot_id="SEQ0420_SH0110"))
        a, b = exporter.get_finished_spans()
        assert a.context.trace_id != b.context.trace_id

    def test_reseeding_is_idempotent_not_duplicative(self, exporter):
        """Re-running the seeder must overwrite, not accumulate."""
        ids = []
        for _ in range(2):
            e = Emitter(span_exporter=exporter)
            e.emit_stage(stage())
            e.shutdown()
            ids.append(exporter.get_finished_spans()[-1].context.span_id)
        assert ids[0] == ids[1]

    def test_iterations_are_distinct_spans_within_one_trace(self, emitter, exporter):
        emitter.emit_stage(stage(iteration=1))
        emitter.emit_stage(stage(iteration=2, status=TaskStatus.RETAKE))
        first, second = exporter.get_finished_spans()
        assert first.context.trace_id == second.context.trace_id
        assert first.context.span_id != second.context.span_id


class TestReworkIsATraceError:
    def test_retake_records_an_error_status(self, emitter, exporter):
        emitter.emit_stage(stage(status=TaskStatus.RETAKE, iteration=3))
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code is StatusCode.ERROR
        assert span.attributes[Attr.ITERATION] == 3

    def test_ordinary_work_is_not_an_error(self, emitter, exporter):
        emitter.emit_stage(stage(status=TaskStatus.WIP))
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code is not StatusCode.ERROR


class TestBackfilledTimestamps:
    def test_span_carries_the_historical_window_not_wall_clock(self, emitter, exporter):
        ended = START + timedelta(days=9)
        emitter.emit_stage(stage(ended_at=ended))
        (span,) = exporter.get_finished_spans()
        assert span.start_time == int(START.timestamp() * 1e9)
        assert span.end_time == int(ended.timestamp() * 1e9)

    def test_naive_timestamps_are_refused(self, emitter):
        # Production data crosses facilities; a naive timestamp is a silent bug.
        with pytest.raises(ValueError, match="timezone-aware"):
            emitter.emit_stage(stage(started_at=datetime(2026, 9, 1, 9, 0)))

    def test_backwards_window_is_refused(self):
        with pytest.raises(ValueError, match="ends before it starts"):
            stage(ended_at=START - timedelta(days=1))


class TestPrivacyIsEnforcedOnTheExportPath:
    def test_a_real_name_cannot_be_emitted(self, emitter):
        with pytest.raises(PrivacyViolation):
            emitter.emit_stage(stage(artist="jane.doe@studio.com"))

    def test_a_raw_kitsu_uuid_cannot_be_emitted(self, emitter):
        with pytest.raises(PrivacyViolation, match="raw source id"):
            emitter.emit_stage(stage(artist="3f2504e0-4f89-11d3-9a0c-0305e82c3301"))
