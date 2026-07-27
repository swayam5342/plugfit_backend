"""
Eval pipeline — the thing that produces the "41 → 88" number.

Runs the full before/after evaluation:
  1. Generate test tasks from the raw manifest (Gemini or heuristic)
  2. Eval the raw manifest    → before_report
  3. Eval the cleaned manifest → after_report
  4. Return EvalComparison with both reports + delta

This is called from:
  - The Celery pipeline task (after cleaning stage)
  - The CLI for manual testing
  - The test suite (with mocked agent)
"""

import logging
from dataclasses import dataclass

from plugfit.app.config import settings

from .mcp_client import InlineMCPClient
from .runner import EvalRunner
from .test_gen import generate_test_suite
from .types import EvalReport, Task

log = logging.getLogger("plugfit.eval.pipeline")


@dataclass
class EvalComparison:
    before: EvalReport
    after: EvalReport

    @property
    def delta(self) -> float:
        return round(self.after.score - self.before.score, 1)

    @property
    def improved(self) -> bool:
        return self.delta > 0

    def summary(self) -> str:
        lines = [
            "── Eval Comparison ──────────────────────────────",
            f"  Score:  {self.before.score:.1f}  →  {self.after.score:.1f}  "
            f"(Δ {self.delta:+.1f})",
            f"  Tasks:  {self.before.task_count}",
            "",
            "  BEFORE",
            *["    " + l for l in self.before.summary_lines()],
            "",
            "  AFTER",
            *["    " + l for l in self.after.summary_lines()],
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "delta": self.delta,
            "improved": self.improved,
        }


def run_eval_pipeline(
    raw_manifest: dict,
    cleaned_manifest: dict,
    gemini_api_key: str | None = None,
    model: str = settings.AI_MODEL_NAME,
    runs_per_task: int = 3,
    min_tasks: int = 8,
    trap_count: int = 3,
    proxy_fn=None,  # optional (tool_name, args) -> str for real calls
    on_progress=None,  # optional progress callback
) -> EvalComparison:
    """
    Full before/after eval pipeline.

    Args:
        raw_manifest:      ToolManifest.to_dict() before cleaning
        cleaned_manifest:  ToolManifest.to_dict() after cleaning
        gemini_api_key:    for running the agent and generating tasks
                            (falls back to GEMINI_API_KEY env)
        model:             Gemini model for the agent
        runs_per_task:     majority vote across N runs (3 recommended, 1 for speed)
        min_tasks:         minimum tasks to generate
        trap_count:        how many trap tasks to generate
        proxy_fn:          if set, tool calls hit the real upstream
        on_progress:       callback(phase, task_idx, total, task, result)

    Returns:
        EvalComparison with before and after EvalReport
    """
    # ── Step 1: Generate tasks from the cleaned manifest ─────────────────────
    # We generate from the CLEANED manifest so task instructions use clean
    # tool concepts. The before eval tests whether raw descriptions confuse
    # the agent on the same tasks.
    log.info("Generating test suite from manifest...")
    suite = generate_test_suite(
        manifest=cleaned_manifest,
        min_tasks=min_tasks,
        trap_count=trap_count,
        gemini_api_key=gemini_api_key,
    )
    tasks: list[Task] = suite.tasks
    log.info(
        "Generated %d tasks (%d traps)", len(tasks), sum(1 for t in tasks if t.is_trap)
    )

    if not tasks:
        raise RuntimeError("Test suite generation produced no tasks")

    # ── Step 2: Eval BEFORE ───────────────────────────────────────────────────
    log.info("Running BEFORE eval (raw manifest)...")
    before_client = InlineMCPClient(raw_manifest, proxy_fn=proxy_fn)
    before_runner = EvalRunner(
        manifest=raw_manifest,
        mcp_client=before_client,
        model=model,
        gemini_api_key=gemini_api_key,
    )

    def _before_progress(idx, total, task, result):
        if on_progress:
            on_progress("before", idx, total, task, result)

    before_report = before_runner.run(
        tasks, runs_per_task=runs_per_task, on_progress=_before_progress, is_before=True
    )
    log.info("BEFORE score: %.1f", before_report.score)

    # ── Step 3: Eval AFTER ────────────────────────────────────────────────────
    log.info("Running AFTER eval (cleaned manifest)...")
    after_client = InlineMCPClient(cleaned_manifest, proxy_fn=proxy_fn)
    after_runner = EvalRunner(
        manifest=cleaned_manifest,
        mcp_client=after_client,
        model=model,
        gemini_api_key=gemini_api_key,
    )

    def _after_progress(idx, total, task, result):
        if on_progress:
            on_progress("after", idx, total, task, result)

    after_report = after_runner.run(
        tasks, runs_per_task=runs_per_task, on_progress=_after_progress, is_before=False
    )
    log.info("AFTER score: %.1f", after_report.score)

    comparison = EvalComparison(before=before_report, after=after_report)
    log.info(
        "Eval complete: %.1f → %.1f (Δ %+.1f)",
        before_report.score,
        after_report.score,
        comparison.delta,
    )

    return comparison
