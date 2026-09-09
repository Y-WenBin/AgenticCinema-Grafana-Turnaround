"""Cross-module contracts that no single module's tests can see.

Turnaround holds the same tool names in four places: the per-analyst
``tool_filter``, the approval gate's write set, the timeline's "is this a
Grafana MCP call" set, and the prompts. Each list is correct on its own; the
failure mode is *drift* between them, and it is silent. Adding a tool to an
analyst without teaching the timeline about it does not break a run -- it just
stops the ``*`` marker, the evidence digest and the judge's timeline from
counting that call as a Grafana call, which quietly weakens the one claim the
demo rests on.

These are the joins between those lists.
"""

from __future__ import annotations

import pytest

from agent import timeline as timeline_mod
from agent.approval import WRITE_TOOLS
from agent.mcp_grafana import (
    CRUNCH_TOOLS,
    FARM_TOOLS,
    REMEDIATOR_WRITE_TOOLS,
    SCHEDULE_TOOLS,
)
from agent.vocabulary import QUERY_RECIPES

ALL_ANALYST_TOOLS = {*SCHEDULE_TOOLS, *FARM_TOOLS, *CRUNCH_TOOLS}


def test_every_tool_an_agent_may_call_is_recognised_as_a_grafana_call():
    """Otherwise the timeline silently stops marking it, and the answer looks
    less grounded than it is."""
    unknown = sorted(
        (ALL_ANALYST_TOOLS | set(REMEDIATOR_WRITE_TOOLS))
        - timeline_mod._MCP_GRAFANA_TOOLS
    )
    assert not unknown, (
        f"{unknown} are in a tool_filter but not in agent/timeline.py's "
        "_MCP_GRAFANA_TOOLS, so calls to them will not be marked '*'"
    )


def test_every_write_tool_the_remediator_has_is_gated():
    """A write tool outside ``WRITE_TOOLS`` passes ``before_tool`` untouched --
    it would mutate Grafana with no human in the loop."""
    ungated = sorted(set(REMEDIATOR_WRITE_TOOLS) - WRITE_TOOLS)
    assert not ungated, f"{ungated} can be called without passing the approval gate"


def test_no_analyst_can_reach_a_write_tool():
    """The read/write split is the whole privilege boundary. In oss mode the
    ``--disable-write`` server enforces it too, but hosted mode has only this."""
    assert not (ALL_ANALYST_TOOLS & WRITE_TOOLS)


def test_only_the_farm_analyst_gets_tempo():
    """The split is deliberate: traces are the farm's evidence, and a narrower
    schema is a cheaper and less confusable prompt for the other two."""
    tempo = {t for t in ALL_ANALYST_TOOLS if t.startswith("tempo_")}
    assert tempo <= set(FARM_TOOLS)
    assert not tempo & set(SCHEDULE_TOOLS)
    assert not tempo & set(CRUNCH_TOOLS)


def test_each_filter_is_a_list_of_unique_names():
    for name, tools in (("SCHEDULE_TOOLS", SCHEDULE_TOOLS), ("FARM_TOOLS", FARM_TOOLS),
                        ("CRUNCH_TOOLS", CRUNCH_TOOLS),
                        ("REMEDIATOR_WRITE_TOOLS", REMEDIATOR_WRITE_TOOLS)):
        assert len(tools) == len(set(tools)), f"{name} repeats a tool name"


@pytest.mark.parametrize("recipe", list(QUERY_RECIPES.values()))
def test_every_prompt_recipe_survives_the_stale_read_wrapper(recipe):
    """R-time-base: the seeded history sits in a compressed window that has
    already ended, so a bare instant query returns nothing. Every recipe an
    analyst is told to run must therefore be wrapped ``last_over_time(...)``."""
    assert recipe.startswith("last_over_time(("), recipe
