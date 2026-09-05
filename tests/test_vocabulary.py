"""The prompt vocabulary is generated from the ontology, so a renamed metric
breaks a test here rather than a demo."""

from __future__ import annotations

import re

from agent import vocabulary as vocab
from bridge.ontology import Metric

ALL_METRICS = {getattr(Metric, name) for name in dir(Metric) if name.isupper()}


def test_catalogue_covers_every_ontology_metric_and_nothing_else():
    assert set(vocab.METRIC_MEANINGS) == ALL_METRICS


def test_every_recipe_references_only_real_metric_names():
    tokens = set(re.findall(r"turnaround_[a-z_]+", vocab.query_recipes_text()))
    assert tokens, "no turnaround_* series found in the recipes"
    assert tokens <= ALL_METRICS


def test_the_join_spans_both_planes_on_sequence():
    j = vocab.THE_JOIN
    assert Metric.RENDER_CORE_HOURS in j  # compute plane
    assert Metric.TASK_ITERATIONS in j    # creative plane
    assert "by (sequence)" in j           # the shared key
    assert 'department="comp"' in j
    assert vocab.THE_JOIN in vocab.QUERY_RECIPES.values()


def test_shared_context_carries_the_floor_and_names_the_protected_pool():
    text = vocab.shared_context()
    assert "di-pool-1" in text
    assert str(vocab.FLOOR) in text
    assert Metric.POOL_HEADCOUNT in text
    # the time base must be explained or every range in the recipes is wrong
    assert "compressed" in text.lower()


def test_pipeline_order_is_dependency_order():
    assert vocab.DEPARTMENT_ORDER.startswith("previz")
    assert vocab.DEPARTMENT_ORDER.endswith("di")
    assert "comp -> di" in vocab.DEPARTMENT_ORDER
