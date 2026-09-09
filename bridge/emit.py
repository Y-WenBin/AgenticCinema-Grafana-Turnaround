"""Export production events to Grafana Cloud over a single OTLP endpoint.

Two things make this harder than ordinary instrumentation, and both shape the
design:

1. **A shot's trace spans weeks.** You cannot hold a span open from previz to
   delivery, so spans are written retroactively with explicit start and end
   timestamps rather than being timed in-process.

2. **Trace ids must be derivable, not discovered.** A producer asks about
   "SEQ0420_SH0100", not about a 128-bit id. `DeterministicIdGenerator` derives
   the trace id from the shot id and the span id from (shot, department,
   iteration), which buys three things: the agent can compute a shot's trace id
   without a lookup; re-running the seeder overwrites rather than duplicates;
   and a shot's history is reproducible across rebuilds, which is what makes a
   scripted demo trustworthy.

Every attribute bag passes through `assert_no_pii` on the way out. That check is
deliberately on the export path rather than at the call sites, so a new caller
cannot forget it.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs._internal import LogRecord
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import Status, StatusCode

from bridge import timewarp
from bridge.ontology import Attr, Department, TaskStatus, sequence_of
from bridge.privacy import assert_no_pii

SERVICE_NAME = "turnaround-bridge"

# The generator reads these rather than taking arguments, because the OTel API
# gives an IdGenerator no access to the span being created.
_id_seed: ContextVar[tuple[str, str, int] | None] = ContextVar("_id_seed", default=None)

_INVALID_TRACE_ID = 0
_INVALID_SPAN_ID = 0


def _derive(material: str, *, nbytes: int) -> int:
    """Deterministic non-zero id of `nbytes` bytes, from arbitrary material."""
    digest = hashlib.sha256(material.encode()).digest()
    value = int.from_bytes(digest[:nbytes], "big")
    # A zero id is invalid in OTel; the odds are negligible but the failure
    # would be a silently dropped trace, so handle it rather than hope.
    return value or 1


class DeterministicIdGenerator(IdGenerator):
    """Derives ids from production identity so they are stable across runs.

    Falls back to the SDK's random generator whenever no seed is set, so the
    agent's own instrumentation -- which shares this process -- keeps normal
    random ids.
    """

    def __init__(self) -> None:
        from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

        self._random = RandomIdGenerator()

    def generate_trace_id(self) -> int:
        seed = _id_seed.get()
        if seed is None:
            return self._random.generate_trace_id()
        shot_id, _, _ = seed
        return _derive(f"shot:{shot_id}", nbytes=16)

    def generate_span_id(self) -> int:
        seed = _id_seed.get()
        if seed is None:
            return self._random.generate_span_id()
        shot_id, department, iteration = seed
        return _derive(f"span:{shot_id}:{department}:{iteration}", nbytes=8)


def span_id_for(shot_id: str, department: str, iteration: int) -> int:
    """The span id for one department pass, computed rather than looked up."""
    return _derive(f"span:{shot_id}:{department}:{iteration}", nbytes=8)


def trace_id_for_shot(shot_id: str) -> str:
    """The hex trace id for a shot, computable without querying anything.

    This is what lets the agent jump straight from a producer's question to a
    trace, and what the console uses to build Grafana deeplinks.
    """
    return format(_derive(f"shot:{shot_id}", nbytes=16), "032x")


@contextmanager
def _seeded(shot_id: str, department: str, iteration: int):
    token = _id_seed.set((shot_id, department, iteration))
    try:
        yield
    finally:
        _id_seed.reset(token)


def _ns(when: datetime) -> int:
    if when.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware; production data crosses facilities")
    when = timewarp.apply(when)
    return int(when.astimezone(UTC).timestamp() * 1_000_000_000)


@dataclass(frozen=True, slots=True)
class StageEvent:
    """One pass of one department over one shot.

    `iteration` is 1 for the first attempt. Anything above 1 is rework, which
    is the signal the whole product is built to see coming.
    """

    show: str
    shot_id: str
    department: Department
    artist: str  # pseudonym; see bridge.privacy.pseudonymize
    vendor: str
    pool: str
    status: TaskStatus
    iteration: int
    started_at: datetime
    ended_at: datetime
    due_date: str | None = None
    revision: int | None = None

    def __post_init__(self) -> None:
        # Checked here rather than at export: production data crosses
        # facilities, so a naive timestamp is a silent hours-out error, and it
        # should fail where it is introduced rather than deep in the exporter.
        for field, value in (("started_at", self.started_at), ("ended_at", self.ended_at)):
            if value.tzinfo is None:
                raise ValueError(
                    f"{self.shot_id}/{self.department.value}: {field} must be timezone-aware"
                )
        if self.ended_at < self.started_at:
            raise ValueError(f"{self.shot_id}/{self.department.value}: ends before it starts")
        if self.iteration < 1:
            raise ValueError("iteration is 1-based")

    @property
    def is_rework(self) -> bool:
        return self.iteration > 1

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


class Emitter:
    """Writes production history to Grafana Cloud.

    Configured entirely from the standard OTEL_* environment variables so the
    same code runs against a local collector or the Cloud OTLP gateway with no
    branching. Take the endpoint from your stack's OpenTelemetry tile -- the
    host varies by region and by when the stack was created.
    """

    def __init__(
        self,
        *,
        resource_attributes: dict[str, str] | None = None,
        span_exporter: SpanExporter | None = None,
        log_exporter: LogExporter | None = None,
    ) -> None:
        # An injected exporter means a test or a dry run; only the real OTLP
        # path needs the environment to be configured. Record the intent now,
        # before span_exporter is reassigned -- the log side keys off it too.
        real_otlp = span_exporter is None
        if real_otlp:
            if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
                raise RuntimeError(
                    "OTEL_EXPORTER_OTLP_ENDPOINT is unset. Copy .env.example and fill in the "
                    "OTLP gateway details from your Grafana Cloud stack's OpenTelemetry tile."
                )
            span_exporter = OTLPSpanExporter()
            processor = BatchSpanProcessor(span_exporter)
        else:
            # Synchronous, so a caller can assert on what was written.
            processor = SimpleSpanProcessor(span_exporter)

        resource = Resource.create(
            {"service.name": SERVICE_NAME, **(resource_attributes or {})}
        )
        self._provider = TracerProvider(
            resource=resource, id_generator=DeterministicIdGenerator()
        )
        self._provider.add_span_processor(processor)
        self._tracer = trace.get_tracer(SERVICE_NAME, tracer_provider=self._provider)

        # Every stage event is written as a span *and* a trace-shaped log line.
        # That is deliberate redundancy: TraceQL tools are not in mcp-grafana's
        # default tool set (they live on the separate Cloud Traces MCP
        # endpoint), whereas Loki's are. Carrying trace and span ids on the log
        # means the agent can reconstruct a shot's history through tools we
        # know exist, and correlate to the trace when it can reach one.
        if log_exporter is None and real_otlp:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import (
                OTLPLogExporter,
            )

            log_exporter = OTLPLogExporter()
        self._logs = LoggerProvider(resource=resource)
        if log_exporter is not None:
            self._logs.add_log_record_processor(
                BatchLogRecordProcessor(log_exporter)
                if real_otlp
                else SimpleLogRecordProcessor(log_exporter)
            )
        self._logger = self._logs.get_logger(SERVICE_NAME)

    def emit_stage(self, event: StageEvent) -> str:
        """Write one department pass as a span. Returns its trace id."""
        attributes = {
            Attr.SHOW: event.show,
            Attr.SHOT_ID: event.shot_id,
            Attr.SEQUENCE: sequence_of(event.shot_id),
            Attr.DEPARTMENT: event.department.value,
            Attr.ARTIST: event.artist,
            Attr.POOL: event.pool,
            Attr.VENDOR: event.vendor,
            Attr.STATUS: event.status.value,
            Attr.ITERATION: event.iteration,
        }
        if event.due_date:
            attributes[Attr.DUE_DATE] = event.due_date
        if event.revision is not None:
            attributes[Attr.REVISION] = event.revision

        assert_no_pii(attributes, where=f"span {event.shot_id}/{event.department.value}")

        with _seeded(event.shot_id, event.department.value, event.iteration):
            span = self._tracer.start_span(
                name=f"{event.department.value} {event.shot_id}",
                start_time=_ns(event.started_at),
                attributes=attributes,
            )
            # A retake is genuinely an error in the trace sense: work that had
            # to be done again. Recording it as one means Tempo's error-rate
            # tooling counts rework without any bespoke query.
            if event.status.is_rework:
                span.set_status(Status(StatusCode.ERROR, "retake"))
            span.end(end_time=_ns(event.ended_at))

        return trace_id_for_shot(event.shot_id)

    def emit_log(
        self,
        *,
        at: datetime,
        message: str,
        shot_id: str,
        department: str,
        iteration: int,
        attributes: dict[str, str] | None = None,
        severity: str = "INFO",
    ) -> None:
        """Write one trace-shaped log line, correlated to its span."""
        labels = {
            Attr.SHOT_ID: shot_id,
            Attr.SEQUENCE: sequence_of(shot_id),
            Attr.DEPARTMENT: department,
            Attr.ITERATION: iteration,
            **(attributes or {}),
        }
        assert_no_pii(labels, where=f"log {shot_id}/{department}")

        self._logger.emit(
            LogRecord(
                timestamp=_ns(at),
                observed_timestamp=_ns(at),
                trace_id=_derive(f"shot:{shot_id}", nbytes=16),
                span_id=span_id_for(shot_id, department, iteration),
                severity_text=severity,
                severity_number=(
                    SeverityNumber.ERROR if severity == "ERROR" else SeverityNumber.INFO
                ),
                body=message,
                attributes=labels,
            )
        )

    def flush(self, timeout_millis: int = 30_000) -> bool:
        """Block until everything is delivered. The seeder depends on this."""
        spans_ok = self._provider.force_flush(timeout_millis)
        logs_ok = self._logs.force_flush(timeout_millis)
        return bool(spans_ok and logs_ok)

    def shutdown(self) -> None:
        self._provider.shutdown()
        self._logs.shutdown()
