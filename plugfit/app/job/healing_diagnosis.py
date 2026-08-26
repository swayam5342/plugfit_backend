from __future__ import annotations

import json
import logging
import re
from typing import Iterable

from plugfit.app.config import settings
from plugfit.app.eval.types import TaskResult
from plugfit.app.job.healing import HealingInvariantViolation

log = logging.getLogger("plugfit.job.healing")


class DiagnosisError(Exception):
    """
    Raised when the diagnosis step cannot produce a usable description —
    no failures to diagnose from, the LLM call failed, or its response
    didn't parse. Callers should treat this the same as "no candidate found
    this iteration" and revert, not crash the loop.
    """


def _get_client():
    from google import genai

    api_key = settings.GEMINI_API_KEY
    if not api_key:
        raise DiagnosisError("GEMINI_API_KEY not set")
    return genai.Client(api_key=api_key)


def _call_gemini(prompt: str) -> str:
    """Single Gemini call boundary — the one thing tests stub."""
    client = _get_client()
    response = client.models.generate_content(
        model=settings.AI_MODEL_NAME,
        contents=prompt,
        config={"temperature": 0.2, "max_output_tokens": 2048},
    )
    text = response.text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


_DIAGNOSIS_SYSTEM = """You are diagnosing why an AI agent failed to correctly use ONE specific MCP tool.

You will see the tool's current name, description, and parameters, plus one \
or more tasks where an agent using this tool set failed to call it \
correctly (wrong tool, no call, or an error).

Diagnose the likely cause (unclear wording, ambiguity vs a similar tool, \
unclear parameters, etc.) and propose a rewritten description that:
- Keeps the same meaning and behavior — you are NOT proposing a different \
tool name, parameters, method, or path, only better wording.
- Is more specific, action-oriented, and disambiguates it from similar tools.
- Does not invent capabilities the tool doesn't have.

Return ONLY a JSON object of the form:
{"diagnosis": "one sentence on why the agent likely failed", "description": "the rewritten description"}
No markdown, no code fences, no explanation outside the JSON."""


def _failure_summary(result: TaskResult) -> dict:
    return {
        "task_id": result.task_id,
        "instruction": result.instruction,
        "expected_tool": result.expected_tool,
        "outcome": result.outcome.value,
        "actual_tools_called": [c.tool_name for c in result.actual_calls],
        "agent_response": result.agent_response[:300],
    }


def diagnose_tool(
    tool: dict,
    failures: list[TaskResult],
    *,
    held_out_task_ids: Iterable[str] = (),
) -> str:
    held_out = set(held_out_task_ids)
    leaked = [f.task_id for f in failures if f.task_id in held_out]
    if leaked:
        raise HealingInvariantViolation(
            f"Diagnosis was given held-out task(s) {leaked} — held-out "
            "tasks must never be visible to the diagnosis step, only to "
            "the final held-out confirmation eval."
        )

    if not failures:
        raise DiagnosisError("No failures given to diagnose")

    prompt = f"""{_DIAGNOSIS_SYSTEM}

Tool:
{
        json.dumps(
            {
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                "parameters": list(tool.get("parameters", {}).keys()),
                "http_method": tool.get("http_method", ""),
                "http_path": tool.get("http_path", ""),
            },
            indent=2,
        )
    }

Failed tasks:
{json.dumps([_failure_summary(f) for f in failures], indent=2)}"""

    try:
        raw = _call_gemini(prompt)
    except DiagnosisError:
        raise
    except Exception as exc:
        raise DiagnosisError(f"Diagnosis call failed: {exc}") from exc

    try:
        data = json.loads(raw)
        description = data.get("description")
    except Exception as exc:
        raise DiagnosisError(f"Malformed diagnosis response: {raw!r}") from exc

    if not description or not isinstance(description, str):
        raise DiagnosisError(f"Malformed diagnosis response: {raw!r}")

    return description.strip()
