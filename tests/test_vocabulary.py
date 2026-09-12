"""The prompt vocabulary is generated from the ontology, so a renamed metric
breaks a test here rather than a demo."""

from __future__ import annotations

import re

import pytest

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


class TestFiguresAreRoundedWhereTheyAreProduced:
    """A producer-facing answer once read ``lighting-pool-2 at 63.16190476190476h``.

    The prompt could not have stopped it. Analysts pass tool results through as
    text and the synthesis agent is forbidden from deriving new numbers, which
    forbids it from rounding them -- so the rounding has to happen in the query.
    """

    @pytest.mark.parametrize("title,expr", list(vocab.QUERY_RECIPES.items()))
    def test_every_recipe_rounds_before_a_model_reads_it(self, title, expr):
        assert "round(" in expr, title

    @pytest.mark.parametrize("title,expr", list(vocab.QUERY_RECIPES.items()))
    def test_rounding_does_not_break_the_stale_read_wrapper(self, title, expr):
        """Rounding goes *inside* the subquery, so the compressed time base
        still works. Belt to ``tests/test_contracts.py``'s braces: that test
        would pass on a recipe wrapped ``last_over_time`` around a raw float."""
        assert expr.startswith("last_over_time((round("), title

    def test_a_rate_keeps_more_precision_than_an_hour(self):
        """Frame-failure rate lives near 0.05. Rounding it to a tenth like an
        hours figure would erase the signal the demo is built on."""
        assert vocab.RATIO < vocab.HOURS
        rate = vocab.QUERY_RECIPES["Frame-failure rate by sequence"]
        assert f", {vocab.RATIO})" in rate

    def test_counted_things_are_not_given_decimals(self):
        shots = vocab.QUERY_RECIPES[
            "Delivery burndown (shots signed off; 200 is the whole show)"]
        assert f", {vocab.COUNT})" in shots

    def test_the_instruction_covers_promql_the_model_writes_itself(self):
        """Rounding the recipes cannot help a query the model composes on the
        spot, so the rule is in the shared context as well."""
        text = vocab.shared_context()
        assert "round(" in vocab.NUMBER_RULE
        assert vocab.NUMBER_RULE in text
