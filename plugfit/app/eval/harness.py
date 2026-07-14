"""
EvalHarness — top-level orchestrator.

Usage:
    from plugfit.eval.harness import EvalHarness

    harness = EvalHarness(raw_manifest, cleaned_manifest)
    result = harness.run()
    print(result.before_report.score, "→", result.after_report.score)

What it does:
  1. Generates a shared TestSuite from the cleaned manifest (descriptions are
     better, so the tasks are more meaningful — but we run both manifests against
     the SAME tasks for a fair comparison)
  2. Runs each task against the raw manifest (before score)
  3. Runs each task against the cleaned manifest (after score)
  4. Returns EvalResult with both reports and the delta

Runs each task N_RUNS times and takes the best outcome (agent non-determinism).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .mock_server import MockMCPServer
from .runner import run_task
from .scorer import build_report, classify_outcome
from .test_gen import TestSuite, generate_test_suite
from .types import (
    Difficulty, EvalReport, Outcome, Task, TaskResult, ToolCall,
)

log = logging.getLogger("plugfit.eval.harness")

N_RUNS = 2          # runs per task (majority vote; 2 = take better of 2)
MODEL  = "gemini-2.5-flash"


# ── Result container ──────────────────────────────────────────────────────────

@dataclass
class EvalResult:
    before_report: EvalReport
    after_report:  EvalReport
    suite:         TestSuite

    @property
    def delta(self) -> float:
        return round(self.after_report.score - self.before_report.score, 1)

    def summary(self) -> str:
        b = self.before_report
        a = self.after_report
        lines = [
            f"  Score:  {b.score:.1f} → {a.score:.1f}  (Δ {self.delta:+.1f})",
            f"  Tasks:  {len(b.task_results)} tasks, {self.suite.trap_count} traps",
            f"  Before: {b.exact_matches} exact, {b.near_misses} near, "
            f"{b.wrong_tools} wrong, {b.no_calls} no_call",
            f"  After:  {a.exact_matches} exact, {a.near_misses} near, "
            f"{a.wrong_tools} wrong, {a.no_calls} no_call",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "before": self.before_report.to_dict(),
            "after":  self.after_report.to_dict(),
            "delta":  self.delta,
            "suite":  self.suite.to_dict(),
        }


# ── Single-task eval (with retries) ──────────────────────────────────────────

def _run_task_with_retries(
    task: Task,
    manifest: dict,
    server_url: str,
    n_runs: int = N_RUNS,
) -> TaskResult:
    """
    Run a task up to n_runs times against a live mock server.
    Takes the best outcome across runs (higher base_score wins).
    """
    best: TaskResult | None = None

    for attempt in range(n_runs):
        try:
            tool_calls, agent_response = run_task(
                task.instruction, manifest, server_url
            )
        except Exception as e:
            log.warning("Task %s attempt %d failed: %s", task.id, attempt, e)
            tool_calls = []
            agent_response = str(e)

        outcome, base_score = classify_outcome(task, tool_calls)
        diff_w  = {Difficulty.EASY: 1.0, Difficulty.MEDIUM: 1.5, Difficulty.HARD: 2.0}
        from .types import TRAP_BONUS, OUTCOME_WEIGHTS
        trap_w  = TRAP_BONUS if task.is_trap else 1.0
        w_score = base_score * OUTCOME_WEIGHTS.get(outcome, 0.0) * diff_w.get(task.difficulty, 1.0) * trap_w

        result = TaskResult(
            task_id=task.id,
            instruction=task.instruction,
            expected_tool=task.expected_tool,
            is_trap=task.is_trap,
            difficulty=task.difficulty,
            actual_calls=tool_calls,
            outcome=outcome,
            base_score=base_score,
            weighted_score=w_score,
            agent_response=agent_response,
        )

        if best is None or base_score > best.base_score:
            best = result

        # Perfect score — no need to retry
        if base_score >= 1.0:
            break

    assert best is not None
    log.info(
        "Task %s [%s]: expected=%s actual=%s outcome=%s score=%.2f",
        task.id, task.difficulty.value,
        task.expected_tool or "∅",
        best.first_tool or "∅",
        best.outcome.value,
        best.base_score,
    )
    return best


# ── Manifest evaluator ────────────────────────────────────────────────────────

def _eval_manifest(
    manifest: dict,
    suite: TestSuite,
    is_before: bool,
) -> EvalReport:
    """Run the full test suite against one manifest. Returns an EvalReport."""
    label = "raw" if is_before else "cleaned"
    log.info("Evaluating %s manifest '%s' (%d tasks)...",
             label, manifest.get("title", "?"), len(suite.tasks))

    results: list[TaskResult] = []

    with MockMCPServer(manifest) as server:
        for task in suite.tasks:
            result = _run_task_with_retries(task, manifest, server.url)
            results.append(result)

    return build_report(
        results=results,
        suite=suite,
        manifest_title=manifest.get("title", "unknown"),
        is_before=is_before,
        model_used=MODEL,
    )


# ── Main harness ──────────────────────────────────────────────────────────────

class EvalHarness:
    """
    Runs the full before/after eval for one server.

    Args:
        raw_manifest:     ToolManifest.to_dict() before cleaning
        cleaned_manifest: ToolManifest.to_dict() after cleaning
        suite:            Optional pre-generated TestSuite (if None, generates one)
    """

    def __init__(
        self,
        raw_manifest: dict,
        cleaned_manifest: dict,
        suite: TestSuite | None = None,
    ):
        self.raw      = raw_manifest
        self.cleaned  = cleaned_manifest
        self._suite   = suite

    def generate_suite(self) -> TestSuite:
        """Generate (or return cached) test suite from the cleaned manifest."""
        if self._suite is None:
            # Use cleaned manifest for task generation — better descriptions
            # mean Gemini writes better tasks — but we run BOTH manifests
            # against the same tasks for a fair comparison
            self._suite = generate_test_suite(self.cleaned)
        return self._suite

    def run(self) -> EvalResult:
        """
        Run the full before/after evaluation.
        Returns EvalResult with both reports and delta.
        """
        if not os.getenv("GEMINI_API_KEY"):
            raise RuntimeError(
                "GEMINI_API_KEY environment variable is not set. "
                "Get one at https://aistudio.google.com/apikey"
            )

        suite = self.generate_suite()

        log.info("=== Starting eval: %d tasks, %d traps ===",
                 len(suite.tasks), suite.trap_count)

        before_report = _eval_manifest(self.raw,     suite, is_before=True)
        after_report  = _eval_manifest(self.cleaned, suite, is_before=False)

        return EvalResult(
            before_report=before_report,
            after_report=after_report,
            suite=suite,
        )
