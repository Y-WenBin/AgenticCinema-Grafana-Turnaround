"""In-memory exporters, shipped with the package rather than kept in tests.

`seed.populate --dry-run` needs them too: exercising the full write path
without sending anything is the only way to catch a mis-accumulated counter
or a wrong timestamp unit before the data is in the stack and awkward to
remove.
"""

from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult


class CollectingMetricExporter(MetricExporter):
    """Keeps exported metrics in memory instead of sending them anywhere."""

    def __init__(self):
        super().__init__()
        self.batches: list[list] = []

    def export(self, metrics_data, timeout_millis=10_000, **kwargs):
        for resource_metrics in metrics_data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                self.batches.append(list(scope_metrics.metrics))
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis=10_000):
        return True

    def shutdown(self, timeout_millis=30_000, **kwargs):
        return None

    @property
    def metrics(self):
        return [m for batch in self.batches for m in batch]

    def points(self, name):
        return [p for m in self.metrics if m.name == name for p in m.data.data_points]
