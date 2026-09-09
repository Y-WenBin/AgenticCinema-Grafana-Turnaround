"""Agent-tier observability: make Turnaround's own agents legible in Grafana.

``bridge/`` makes a *shot* a trace. This package makes an *agent run* a trace,
following the OpenTelemetry GenAI semantic conventions, so the agent's token
cost, latency, tool fan-out and evaluation scores show up in the same Grafana
Cloud stack as the production data it reasons over.

One call wires it up::

    from observability import instrument
    obs = instrument()                       # reads OTEL_* like the seeder
    runner = Runner(..., plugins=[obs.plugin])
    ...
    obs.telemetry.flush()

Kept import-clean of ``agent/`` and ``bridge/`` so it can move to its own
package later; the PII guard is injected, not imported.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from observability.adk import GenAiObservabilityPlugin
from observability.genai import GenAiTelemetry
from observability.providers import DEFAULT_SERVICE_NAME, build_providers

__all__ = ["TURNAROUND_GUARD", "GenAiObservabilityPlugin", "GenAiTelemetry",
           "Instrumentation", "instrument"]

_CAPTURE_ENV = "TURNAROUND_CAPTURE_CONTENT"

#: sentinel for "use Turnaround's own PII guard"; pass an explicit callable to
#: substitute one, or ``None`` to run with no guard at all
TURNAROUND_GUARD = object()


@dataclass(slots=True)
class Instrumentation:
    telemetry: GenAiTelemetry
    plugin: GenAiObservabilityPlugin

    def flush(self, timeout_millis: int = 30_000) -> bool:
        return self.telemetry.flush(timeout_millis)

    def shutdown(self) -> None:
        self.telemetry.shutdown()


def _default_guard():
    """Turnaround's PII guard, if importable. External callers pass their own."""
    try:
        from bridge.privacy import assert_no_pii

        return assert_no_pii
    except Exception:  # noqa: BLE001
        return None


def instrument(
    *,
    service_name: str = DEFAULT_SERVICE_NAME,
    capture_content: bool | None = None,
    content_guard=TURNAROUND_GUARD,
    resource_attributes: dict[str, str] | None = None,
) -> Instrumentation:
    """Stand up the GenAI telemetry and its ADK plugin against the OTLP gateway.

    ``capture_content`` defaults to the ``TURNAROUND_CAPTURE_CONTENT`` env flag
    (off unless set to ``1``/``true``). When on, prompt and completion content is
    attached to ``chat`` spans -- every payload first passes ``content_guard``
    (``bridge.privacy.assert_no_pii`` by default) so a crew name in a prompt is a
    hard failure, not a silent leak.
    """
    if capture_content is None:
        capture_content = os.environ.get(_CAPTURE_ENV, "").lower() in ("1", "true", "yes")
    guard = _default_guard() if content_guard is TURNAROUND_GUARD else content_guard

    providers = build_providers(service_name=service_name,
                                resource_attributes=resource_attributes)
    telemetry = GenAiTelemetry(
        tracer_provider=providers.tracer_provider,
        logger_provider=providers.logger_provider,
        meter_provider=providers.meter_provider,
        capture_content=bool(capture_content),
        content_guard=guard,
    )
    return Instrumentation(telemetry=telemetry, plugin=GenAiObservabilityPlugin(telemetry))
