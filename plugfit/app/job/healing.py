from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class HealingInvariantViolation(Exception):
    """
    Raised when a candidate manifest changes anything other than tool
    descriptions relative to its baseline — names, parameters, http method/
    path, tool identity, or tool count must never change during healing.
    This is enforced structurally (an exception the orchestrator cannot
    ignore), not left to prompt discipline alone.
    """


def should_accept(candidate_score: float, best_score: float, threshold: float) -> bool:
    return (candidate_score - best_score) > threshold


class StopReason(str, Enum):
    MAX_ITERATIONS = "max_iterations"
    NON_IMPROVING = "non_improving"
    TOOLS_EXHAUSTED = "tools_exhausted"
    CONTINUE = "continue"


@dataclass
class StoppingState:
    """Mutable loop state the stopping-rule evaluator reads each iteration."""

    iteration: int = 0
    consecutive_non_improving: int = 0
    tools_remaining: int = 0


def evaluate_stop(
    state: StoppingState,
    max_iterations: int,
    non_improving_stop: int,
) -> StopReason:
    if state.iteration >= max_iterations:
        return StopReason.MAX_ITERATIONS
    if state.consecutive_non_improving >= non_improving_stop:
        return StopReason.NON_IMPROVING
    if state.tools_remaining <= 0:
        return StopReason.TOOLS_EXHAUSTED
    return StopReason.CONTINUE


def record_iteration(state: StoppingState, *, accepted: bool) -> StoppingState:
    state.iteration += 1
    if accepted:
        state.consecutive_non_improving = 0
    else:
        state.consecutive_non_improving += 1
    return state


def _tool_key(tool: dict) -> str:
    return tool.get("tool_id") or tool.get("name", "")


def assert_descriptions_only_diff(before: dict, after: dict) -> None:
    before_tools = before.get("tools", [])
    after_tools = after.get("tools", [])

    if len(before_tools) != len(after_tools):
        raise HealingInvariantViolation(
            f"Tool count changed: {len(before_tools)} -> {len(after_tools)}"
        )

    before_by_key = {_tool_key(t): t for t in before_tools}
    after_by_key = {_tool_key(t): t for t in after_tools}

    if before_by_key.keys() != after_by_key.keys():
        missing = before_by_key.keys() - after_by_key.keys()
        added = after_by_key.keys() - before_by_key.keys()
        raise HealingInvariantViolation(
            f"Tool identities changed: missing={sorted(missing)} added={sorted(added)}"
        )

    for key, before_tool in before_by_key.items():
        after_tool = after_by_key[key]
        if before_tool.get("name") != after_tool.get("name"):
            raise HealingInvariantViolation(f"Tool {key!r}: name changed")
        if before_tool.get("parameters") != after_tool.get("parameters"):
            raise HealingInvariantViolation(f"Tool {key!r}: parameters changed")
        if before_tool.get("http_method") != after_tool.get("http_method"):
            raise HealingInvariantViolation(f"Tool {key!r}: http_method changed")
        if before_tool.get("http_path") != after_tool.get("http_path"):
            raise HealingInvariantViolation(f"Tool {key!r}: http_path changed")
