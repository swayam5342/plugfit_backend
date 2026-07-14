"""
Eval scorer — turns raw tool calls into outcomes and a 0-100 score.

Outcome classification:
  EXACT_MATCH  — first tool called matches expected_tool exactly
                 AND all expected_args keys are present
  NEAR_MISS    — first tool called is semantically similar (shared words)
                 OR right tool called but wrong/missing args
  WRONG_TOOL   — first tool called is unrelated to expected
  NO_CALL      — agent made no tool calls
  ERROR        — all tool calls returned isError=True

Trap scoring:
  Trap tasks expect the agent to make NO tool calls.
  If agent calls no tools → TRAP_CORRECT (bonus)
  If agent calls any tool  → WRONG_TOOL (penalised)

Final score formula:
  weighted_sum = Σ (base_score × outcome_weight × difficulty_weight × trap_multiplier)
  max_possible = Σ (difficulty_weight × trap_multiplier) for all tasks
  score_0_to_100 = (weighted_sum / max_possible) × 100
"""

from __future__ import annotations

import logging
import re

from .types import (
    DIFFICULTY_WEIGHTS, OUTCOME_WEIGHTS, TRAP_BONUS,
    Difficulty, EvalReport, Outcome, Task, TaskResult, TestSuite, ToolCall,
)

log = logging.getLogger("plugfit.eval.scorer")


# ── Outcome classifier ────────────────────────────────────────────────────────

def _words(name: str) -> set[str]:
    """Split a snake_case tool name into meaningful words."""
    return set(re.split(r"[_\-\s]+", name.lower())) - {"", "get", "the", "a", "an"}


def _is_near_miss(called: str, expected: str) -> bool:
    """True if the called tool shares meaningful words with the expected tool."""
    if not called or not expected:
        return False
    overlap = _words(called) & _words(expected)
    return len(overlap) >= 1


def classify_outcome(
    task: Task,
    tool_calls: list[ToolCall],
) -> tuple[Outcome, float]:
    """
    Classify the outcome of a task run.

    Returns:
        (Outcome, base_score 0.0–1.0)
    """
    # ── Trap tasks ────────────────────────────────────────────────────────────
    if task.is_trap:
        if not tool_calls:
            # Agent correctly said it couldn't help
            return Outcome.EXACT_MATCH, 1.0
        else:
            # Agent hallucinated a tool call for an instruction that had no tool
            return Outcome.WRONG_TOOL, 0.0

    # ── Real tasks ────────────────────────────────────────────────────────────
    if not tool_calls:
        return Outcome.NO_CALL, 0.0

    first = tool_calls[0]

    # All calls errored → ERROR
    if all(c.is_error for c in tool_calls):
        return Outcome.ERROR, 0.0

    # Check if the right tool was eventually called (not just first)
    right_call = next(
        (c for c in tool_calls if c.tool_name == task.expected_tool),
        None,
    )

    if right_call:
        # Right tool called — check if expected args are present
        missing_args = [
            k for k in task.expected_args
            if k not in right_call.args
        ]
        if not missing_args:
            return Outcome.EXACT_MATCH, 1.0
        else:
            # Right tool, missing some args → near miss
            present_ratio = 1.0 - len(missing_args) / max(len(task.expected_args), 1)
            return Outcome.NEAR_MISS, 0.3 + 0.1 * present_ratio

    # Right tool not called — check if first call is a near miss
    if _is_near_miss(first.tool_name, task.expected_tool):
        return Outcome.NEAR_MISS, 0.4

    return Outcome.WRONG_TOOL, 0.0


# ── Weighted score aggregation ────────────────────────────────────────────────

def compute_score(results: list[TaskResult], suite: TestSuite) -> float:
    """
    Aggregate TaskResults into a 0–100 score.

    Weighting:
      - Difficulty multiplier (easy=1, medium=1.5, hard=2)
      - Trap bonus (1.5×) for correctly handled trap tasks
      - Outcome weight (exact=1.0, near_miss=0.4, wrong/no_call=0.0)
    """
    if not results:
        return 0.0

    # Build task lookup for difficulty/trap info
    task_map: dict[str, Task] = {t.id: t for t in suite.tasks}

    weighted_sum = 0.0
    max_possible = 0.0

    for result in results:
        task = task_map.get(result.task_id)
        diff_w = DIFFICULTY_WEIGHTS.get(result.difficulty, 1.0)
        trap_w = TRAP_BONUS if result.is_trap else 1.0
        total_w = diff_w * trap_w

        weighted_sum += result.base_score * OUTCOME_WEIGHTS.get(result.outcome, 0.0) * total_w
        max_possible += total_w

    if max_possible == 0:
        return 0.0

    return round((weighted_sum / max_possible) * 100, 1)


# ── Report builder ────────────────────────────────────────────────────────────

def build_report(
    results: list[TaskResult],
    suite: TestSuite,
    manifest_title: str,
    is_before: bool,
    model_used: str = "gemini-2.5-flash",
) -> EvalReport:
    """Aggregate TaskResults into a full EvalReport."""
    score = compute_score(results, suite)

    report = EvalReport(
        manifest_title=manifest_title,
        is_before=is_before,
        task_results=results,
        score=score,
        model_used=model_used,
    )

    for r in results:
        match r.outcome:
            case Outcome.EXACT_MATCH: report.exact_matches += 1
            case Outcome.NEAR_MISS:   report.near_misses  += 1
            case Outcome.WRONG_TOOL:  report.wrong_tools  += 1
            case Outcome.NO_CALL:     report.no_calls     += 1
            case Outcome.ERROR:       report.errors       += 1

        if r.is_trap:
            report.trap_total += 1
            if r.outcome == Outcome.EXACT_MATCH:
                report.trap_correct += 1

    log.info(
        "EvalReport: score=%.1f  exact=%d  near=%d  wrong=%d  no_call=%d  traps=%d/%d",
        score, report.exact_matches, report.near_misses,
        report.wrong_tools, report.no_calls,
        report.trap_correct, report.trap_total,
    )
    return report
