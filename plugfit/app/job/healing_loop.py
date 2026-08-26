from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable

from plugfit.app.config import settings
from plugfit.app.eval.types import DIFFICULTY_WEIGHTS, Outcome, Task, TaskResult
from plugfit.app.job.healing import (
    StopReason,
    StoppingState,
    assert_descriptions_only_diff,
    evaluate_stop,
    record_iteration,
    should_accept,
)
from plugfit.app.job.healing_diagnosis import DiagnosisError, diagnose_tool

_FAILURE_OUTCOMES = {
    Outcome.WRONG_TOOL,
    Outcome.NO_CALL,
    Outcome.ERROR,
    Outcome.NEAR_MISS,
}

EvalFn = Callable[[dict, list[Task]], float]
DiagnoseFn = Callable[[dict, list[TaskResult]], str]


@dataclass
class IterationRecord:
    """One iteration's full trajectory — logged verbatim by the caller."""

    iteration: int
    tool_id: str
    tool_name: str
    old_description: str
    new_description: str | None
    baseline_score: float | None
    candidate_score: float | None
    accepted: bool
    single_sample: bool = True
    reason: str = ""


@dataclass
class HealingRunResult:
    final_manifest: dict
    iterations: list[IterationRecord] = field(default_factory=list)
    stop_reason: StopReason = StopReason.CONTINUE
    tools_attempted: list[str] = field(default_factory=list)


def _tool_id_for_name(manifest: dict, name: str) -> str:
    for t in manifest.get("tools", []):
        if t.get("name") == name:
            return t.get("tool_id") or name
    return name


def _find_tool(manifest: dict, tool_id: str) -> dict:
    for t in manifest.get("tools", []):
        if (t.get("tool_id") or t.get("name")) == tool_id:
            return t
    raise KeyError(f"tool_id {tool_id!r} not found in manifest")


def _apply_description(manifest: dict, tool_id: str, new_description: str) -> dict:
    candidate = copy.deepcopy(manifest)
    for t in candidate.get("tools", []):
        if (t.get("tool_id") or t.get("name")) == tool_id:
            t["description"] = new_description
            break
    return candidate


def rank_candidate_tools(manifest: dict, task_results: list[TaskResult]) -> list[str]:
    weights: dict[str, float] = {}
    for r in task_results:
        if r.is_trap or not r.expected_tool:
            continue
        if r.outcome not in _FAILURE_OUTCOMES:
            continue
        tool_id = _tool_id_for_name(manifest, r.expected_tool)
        weights[tool_id] = weights.get(tool_id, 0.0) + DIFFICULTY_WEIGHTS[r.difficulty]
    return sorted(weights.keys(), key=lambda tid: (-weights[tid], tid))


def _subset_for_tool(
    manifest: dict,
    healing_tasks: list[Task],
    stable_sample_tasks: list[Task],
    tool_id: str,
) -> list[Task]:
    target_name = None
    for t in manifest.get("tools", []):
        if (t.get("tool_id") or t.get("name")) == tool_id:
            target_name = t.get("name")
            break

    targeted = [t for t in healing_tasks if t.expected_tool == target_name]
    targeted_ids = {t.id for t in targeted}
    extra = [t for t in stable_sample_tasks if t.id not in targeted_ids]
    return targeted + extra


def _default_diagnose(tool: dict, failures: list[TaskResult]) -> str:
    return diagnose_tool(tool, failures)


def run_healing_loop(
    manifest: dict,
    healing_tasks: list[Task],
    initial_task_results: list[TaskResult],
    stable_sample_tasks: list[Task],
    *,
    eval_fn: EvalFn,
    diagnose_fn: DiagnoseFn = _default_diagnose,
    max_iterations: int = settings.HEAL_MAX_ITERATIONS,
    accept_threshold: float = settings.HEAL_ACCEPT_THRESHOLD,
    non_improving_stop: int = settings.HEAL_NON_IMPROVING_STOP,
) -> HealingRunResult:
    candidate_tool_ids = rank_candidate_tools(manifest, initial_task_results)
    attempted: set[str] = set()
    current_manifest = copy.deepcopy(manifest)
    state = StoppingState(iteration=0, consecutive_non_improving=0, tools_remaining=0)
    iterations: list[IterationRecord] = []
    stop_reason = StopReason.CONTINUE

    while True:
        state.tools_remaining = sum(
            1 for tid in candidate_tool_ids if tid not in attempted
        )
        stop_reason = evaluate_stop(state, max_iterations, non_improving_stop)
        if stop_reason != StopReason.CONTINUE:
            break

        next_tool_id = next(tid for tid in candidate_tool_ids if tid not in attempted)
        attempted.add(next_tool_id)

        tool = _find_tool(current_manifest, next_tool_id)
        failures = [
            r
            for r in initial_task_results
            if not r.is_trap
            and r.expected_tool == tool.get("name")
            and r.outcome in _FAILURE_OUTCOMES
        ]

        try:
            new_description = diagnose_fn(tool, failures)
        except DiagnosisError as exc:
            iterations.append(
                IterationRecord(
                    iteration=state.iteration + 1,
                    tool_id=next_tool_id,
                    tool_name=tool.get("name", ""),
                    old_description=tool.get("description", ""),
                    new_description=None,
                    baseline_score=None,
                    candidate_score=None,
                    accepted=False,
                    reason=f"diagnosis_failed: {exc}",
                )
            )
            record_iteration(state, accepted=False)
            continue

        candidate_manifest = _apply_description(
            current_manifest, next_tool_id, new_description
        )
        assert_descriptions_only_diff(current_manifest, candidate_manifest)

        subset = _subset_for_tool(
            current_manifest, healing_tasks, stable_sample_tasks, next_tool_id
        )
        baseline_score = eval_fn(current_manifest, subset)
        candidate_score = eval_fn(candidate_manifest, subset)
        accepted = should_accept(candidate_score, baseline_score, accept_threshold)

        iterations.append(
            IterationRecord(
                iteration=state.iteration + 1,
                tool_id=next_tool_id,
                tool_name=tool.get("name", ""),
                old_description=tool.get("description", ""),
                new_description=new_description,
                baseline_score=baseline_score,
                candidate_score=candidate_score,
                accepted=accepted,
            )
        )

        if accepted:
            current_manifest = candidate_manifest

        record_iteration(state, accepted=accepted)

    return HealingRunResult(
        final_manifest=current_manifest,
        iterations=iterations,
        stop_reason=stop_reason,
        tools_attempted=sorted(attempted),
    )
