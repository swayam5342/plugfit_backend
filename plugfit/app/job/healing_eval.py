"""
Self-healing loop — real eval wiring
"""

from __future__ import annotations

from plugfit.app.config import settings
from plugfit.app.eval.mcp_client import InlineMCPClient
from plugfit.app.eval.runner import EvalRunner
from plugfit.app.eval.types import EvalReport, Task


def run_eval_report(
    manifest: dict, tasks: list[Task], runs_per_task: int = 1
) -> EvalReport:
    if not tasks:
        return EvalReport(manifest_title=manifest.get("title", ""), is_before=False)

    client = InlineMCPClient(manifest)
    runner = EvalRunner(
        manifest=manifest,
        mcp_client=client,
        model=settings.AI_MODEL_NAME,
        gemini_api_key=settings.GEMINI_API_KEY,
    )
    return runner.run(tasks, runs_per_task=runs_per_task)


def make_eval_fn(runs_per_task: int = 1):
    def eval_fn(manifest: dict, tasks: list[Task]) -> float:
        return run_eval_report(manifest, tasks, runs_per_task=runs_per_task).score

    return eval_fn
