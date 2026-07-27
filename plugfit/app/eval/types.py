"""
Eval harness types.

  TestSuite  — tasks generated from one manifest
  Task       — one eval task: instruction + expected tool + expected args
  ToolCall   — one tool call the agent made
  TaskResult — outcome of one task
  EvalReport — aggregated score across the full suite
"""
from __future__ import annotations
import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from plugfit.app.config import settings


class Difficulty(str, enum.Enum):
    EASY   = "easy"    # single obvious tool, no ambiguity
    MEDIUM = "medium"  # choose between 2 similar tools
    HARD   = "hard"    # multi-step or genuinely ambiguous


class Outcome(str, enum.Enum):
    EXACT_MATCH = "exact_match"  # right tool, valid args
    NEAR_MISS   = "near_miss"    # related tool (partial credit)
    WRONG_TOOL  = "wrong_tool"   # completely wrong tool
    NO_CALL     = "no_call"      # agent gave up
    ERROR       = "error"        # tool errored (bad args / server)


OUTCOME_WEIGHTS: dict[Outcome, float] = {
    Outcome.EXACT_MATCH: 1.0,
    Outcome.NEAR_MISS:   0.4,
    Outcome.WRONG_TOOL:  0.0,
    Outcome.NO_CALL:     0.0,
    Outcome.ERROR:       0.0,
}

DIFFICULTY_WEIGHTS = {
    Difficulty.EASY:   1.0,
    Difficulty.MEDIUM: 1.5,
    Difficulty.HARD:   2.0,
}

TRAP_BONUS = 1.5  # multiplier when agent correctly avoids a trap


@dataclass
class Task:
    """
    One evaluation task.

    instruction    — natural language prompt given to the agent
    expected_tool  — tool name it should call ("" for trap tasks)
    expected_args  — param names that must appear in the call
                     (values can be None — we only check presence)
    is_trap        — agent should recognise no tool fits; penalised
                     if it calls something anyway
    difficulty     — affects score weighting
    rationale      — why this tests what it tests (for debugging)
    """
    id:            str
    instruction:   str
    expected_tool: str
    expected_args: dict[str, Any] = field(default_factory=dict)
    is_trap:       bool = False
    difficulty:    Difficulty = Difficulty.EASY
    rationale:     str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "instruction": self.instruction,
            "expected_tool": self.expected_tool,
            "expected_args": self.expected_args,
            "is_trap": self.is_trap,
            "difficulty": self.difficulty.value,
            "rationale": self.rationale,
        }


@dataclass
class TestSuite:
    manifest_title: str
    tasks: list[Task] = field(default_factory=list)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def trap_count(self) -> int:
        return sum(1 for t in self.tasks if t.is_trap)

    def to_dict(self) -> dict:
        return {
            "manifest_title": self.manifest_title,
            "generated_at": self.generated_at,
            "task_count": len(self.tasks),
            "trap_count": self.trap_count,
            "tasks": [t.to_dict() for t in self.tasks],
        }


@dataclass
class ToolCall:
    tool_name: str
    args:      dict[str, Any]
    result:    str
    is_error:  bool = False


@dataclass
class TaskResult:
    task_id:        str
    instruction:    str
    expected_tool:  str
    is_trap:        bool = False
    difficulty:     Difficulty = Difficulty.EASY
    actual_calls:   list[ToolCall] = field(default_factory=list)
    outcome:        Outcome = Outcome.NO_CALL
    base_score:     float = 0.0   # 0–1 before weighting
    weighted_score: float = 0.0   # after difficulty + trap weighting
    agent_response: str = ""
    error:          str = ""

    @property
    def first_tool(self) -> str:
        return self.actual_calls[0].tool_name if self.actual_calls else ""

    def to_dict(self) -> dict:
        return {
            "task_id":        self.task_id,
            "instruction":    self.instruction,
            "expected_tool":  self.expected_tool,
            "actual_tools":   [c.tool_name for c in self.actual_calls],
            "outcome":        self.outcome.value,
            "base_score":     round(self.base_score, 3),
            "weighted_score": round(self.weighted_score, 3),
            "is_trap":        self.is_trap,
            "difficulty":     self.difficulty.value,
            "agent_response": self.agent_response[:400],
        }


@dataclass
class EvalReport:
    """The headline number + full breakdown. Stored in DB, shown in dashboard."""
    manifest_title: str
    is_before:      bool          # True = raw manifest, False = cleaned

    task_results:   list[TaskResult] = field(default_factory=list)

    score:          float = 0.0   # 0–100
    exact_matches:  int = 0
    near_misses:    int = 0
    wrong_tools:    int = 0
    no_calls:       int = 0
    errors:         int = 0
    trap_correct:   int = 0
    trap_total:     int = 0

    model_used:     str = settings.AI_MODEL_NAME
    evaluated_at:   str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def task_count(self) -> int:
        return len(self.task_results)

    def summary_lines(self) -> list[str]:
        return [
            f"exact={self.exact_matches}  near={self.near_misses}  "
            f"wrong={self.wrong_tools}  no_call={self.no_calls}  err={self.errors}",
            f"traps={self.trap_correct}/{self.trap_total}  score={self.score:.1f}",
        ]

    def to_dict(self) -> dict:
        return {
            "manifest_title": self.manifest_title,
            "is_before":      self.is_before,
            "score":          round(self.score, 1),
            "model_used":     self.model_used,
            "evaluated_at":   self.evaluated_at,
            "task_count":     self.task_count,
            "exact_matches":  self.exact_matches,
            "near_misses":    self.near_misses,
            "wrong_tools":    self.wrong_tools,
            "no_calls":       self.no_calls,
            "errors":         self.errors,
            "trap_correct":   self.trap_correct,
            "trap_total":     self.trap_total,
            "task_results":   [r.to_dict() for r in self.task_results],
        }
