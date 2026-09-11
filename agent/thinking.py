"""One place that decides how much Gemini 2.5 is allowed to think.

Every agent in the pipeline gets the same budget, resolved from
``Settings.thinking_budget`` (``TURNAROUND_THINKING_BUDGET``). See
``agent/config.py::DEFAULT_THINKING_BUDGET`` for *why* the default is zero --
the short version is that this pipeline retrieves and formats, it does not
reason, and dynamic thinking was measured at ~4x the wall-clock for no change
in the scorecard.

Two shapes, because ADK and raw ``google-genai`` want different things:

* :func:`planner` -- for an ``LlmAgent``. ADK owns the request config, so the
  budget has to arrive as a ``BuiltInPlanner``.
* :func:`generate_config` -- for the LLM judge in ``agent/evaluation.py``,
  which calls ``client.models.generate_content`` directly.

Both return ``None`` for a *dynamic* budget (-1), which is Gemini's own default:
passing nothing and passing "decide for yourself" mean the same thing, and
``None`` keeps the un-tuned path byte-identical to what shipped before.
"""

from __future__ import annotations

from google.adk.planners import BuiltInPlanner
from google.genai import types

#: "let the model decide" -- the Gemini default, expressed as a budget
DYNAMIC = -1


def thinking_config(budget: int) -> types.ThinkingConfig | None:
    """A ``ThinkingConfig`` for ``budget``, or None when it is dynamic."""
    if budget == DYNAMIC:
        return None
    return types.ThinkingConfig(thinking_budget=max(budget, 0), include_thoughts=False)


def planner(budget: int) -> BuiltInPlanner | None:
    """The ``planner=`` for an ``LlmAgent``, or None to leave ADK's default."""
    config = thinking_config(budget)
    return None if config is None else BuiltInPlanner(thinking_config=config)


def generate_config(budget: int) -> types.GenerateContentConfig | None:
    """The ``config=`` for a direct ``generate_content`` call, or None."""
    config = thinking_config(budget)
    return None if config is None else types.GenerateContentConfig(thinking_config=config)


__all__ = ["DYNAMIC", "generate_config", "planner", "thinking_config"]
