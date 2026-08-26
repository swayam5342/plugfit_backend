from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from plugfit.app.config import settings
from plugfit.app.eval.types import Task

EvalFn = Callable[[dict, list[Task]], float]


def _task_hash_bucket(task_id: str) -> int:
    return int(hashlib.sha256(task_id.encode()).hexdigest(), 16) % 100


def split_holdout(
    tasks: list[Task],
    ratio: float = settings.HEAL_HOLDOUT_RATIO,
    min_holdout: int = settings.HEAL_MIN_HOLDOUT,
) -> tuple[list[Task], list[Task]]:
    threshold = ratio * 100
    held_out: list[Task] = []
    healing: list[Task] = []
    for t in tasks:
        (held_out if _task_hash_bucket(t.id) < threshold else healing).append(t)

    non_trap_held_out = [t for t in held_out if not t.is_trap]
    if len(non_trap_held_out) < min_holdout:
        needed = min_holdout - len(non_trap_held_out)
        promotable = sorted(
            (t for t in healing if not t.is_trap),
            key=lambda t: _task_hash_bucket(t.id),
        )
        promoted = promotable[:needed]
        promoted_ids = {t.id for t in promoted}
        healing = [t for t in healing if t.id not in promoted_ids]
        held_out.extend(promoted)

    return healing, held_out


def select_stable_sample(healing_tasks: list[Task], n: int = 2) -> list[Task]:
    non_trap = [t for t in healing_tasks if not t.is_trap]
    ordered = sorted(non_trap, key=lambda t: _task_hash_bucket(t.id))
    return ordered[:n]


@dataclass
class OverfitCheckResult:
    held_out_score_before: float
    held_out_score_after: float
    overfit: bool
    verdict: str  # "improved" | "overfit — low confidence" | "no held-out tasks"


def check_overfit(
    baseline_manifest: dict,
    healed_manifest: dict,
    held_out_tasks: list[Task],
    *,
    eval_fn: EvalFn,
) -> OverfitCheckResult:
    if not held_out_tasks:
        return OverfitCheckResult(
            held_out_score_before=0.0,
            held_out_score_after=0.0,
            overfit=False,
            verdict="no held-out tasks",
        )

    before = eval_fn(baseline_manifest, held_out_tasks)
    after = eval_fn(healed_manifest, held_out_tasks)
    overfit = after <= before
    return OverfitCheckResult(
        held_out_score_before=before,
        held_out_score_after=after,
        overfit=overfit,
        verdict="overfit — low confidence" if overfit else "improved",
    )
