"""
AI Test Generation — Gemini reads the manifest and writes the test suite.

This is the unlock that makes PlugFit work on any server with zero manual setup.
No YAML. No hand-written scenarios. Gemini reads the tool list and generates:

  1. Real tasks  — what a genuine user would ask ("list all my unwatched movies")
     mapped to the correct tool + required args
  2. Medium tasks — where two tools could plausibly both fit; tests whether
     descriptions are distinct enough for the agent to pick correctly
  3. Trap tasks  — instructions that sound tool-shaped but don't match any tool
     ("what's the weather today") — agents with bad MCPs hallucinate calls;
     good ones say they can't help

Output is a TestSuite with Task objects ready to hand to EvalRunner.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid

from .types import Difficulty, Task, TestSuite

log = logging.getLogger("plugfit.eval.test_gen")


# ── Gemini call ───────────────────────────────────────────────────────────────

def _gemini(prompt: str, api_key: str | None = None) -> str:
    from google import genai
    key = api_key or os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"temperature": 0.4, "max_output_tokens": 8192},
    )
    text = resp.text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ── Prompt ────────────────────────────────────────────────────────────────────

_SYSTEM = """You are designing an evaluation test suite for an MCP tool server.

Your job: given a list of tools, generate test tasks that will reveal whether
an AI agent can correctly select and call the right tool for each task.

Generate tasks in three categories:

EASY tasks (difficulty: "easy"):
  - Single unambiguous instruction that maps to exactly one tool
  - No confusion possible if the description is clear
  - Generate 1-2 per tool (skip tools with no parameters if boring)

MEDIUM tasks (difficulty: "medium"):
  - Instruction where 2+ tools are plausible but only one is correct
  - Tests whether descriptions distinguish similar tools
  - Generate 2-3 total (pick the most similar-looking tool pairs)

TRAP tasks (is_trap: true, difficulty: "medium"):
  - Instructions that sound like they need a tool but don't match any
  - OR instructions that are ambiguous enough the agent should ask for clarification
  - Generate exactly 2
  - expected_tool must be "" (empty string)

Rules:
- Instructions must sound natural, like a real user typed them
- expected_args: list ONLY the param names the agent must include (values null)
  If a tool takes no required params, use {}
- rationale: one sentence explaining what this task is testing
- Return ONLY a valid JSON array of task objects, no markdown

JSON schema for each task:
{
  "instruction": "string",
  "expected_tool": "string (tool name, or empty string for traps)",
  "expected_args": {"param_name": null, ...},
  "is_trap": false,
  "difficulty": "easy|medium|hard",
  "rationale": "string"
}"""


# ── Main generator ────────────────────────────────────────────────────────────

def generate_test_suite(
    manifest: dict,
    n_tasks: int = 12,
    min_tasks: int | None = None,
    trap_count: int = 2,
    gemini_api_key: str | None = None,
) -> TestSuite:
    """
    Generate a TestSuite from a ToolManifest dict.

    Args:
        manifest:  ToolManifest.to_dict() output
        n_tasks:   Approximate number of tasks to generate (Gemini decides final count)

    Returns:
        TestSuite ready to hand to EvalRunner
    """
    tools = manifest.get("tools", [])
    if not tools:
        raise ValueError("Manifest has no tools — cannot generate test suite")

    title = manifest.get("title", "unknown")
    task_target = min_tasks or n_tasks

    # Build a compact tool summary for the prompt
    tool_list = []
    for t in tools:
        params = {
            k: v.get("description", v.get("type", ""))
            for k, v in t.get("parameters", {}).items()
        }
        tool_list.append({
            "name":        t["name"],
            "description": t.get("description", ""),
            "parameters":  params,
        })

    prompt = f"""{_SYSTEM}

Here are the tools in the MCP server called "{title}":

{json.dumps(tool_list, indent=2)}

Generate approximately {task_target} tasks total following the rules above.
Make sure you include exactly {trap_count} trap tasks.
Return only the JSON array."""

    log.info("Generating test suite for '%s' (%d tools)...", title, len(tools))

    try:
        raw = _gemini(prompt, api_key=gemini_api_key)
        task_dicts = json.loads(raw)
        if not isinstance(task_dicts, list):
            raise ValueError("Expected JSON array")
    except Exception as e:
        log.warning("Gemini test gen failed: %s — using fallback suite", e)
        task_dicts = _fallback_tasks(tools)

    tasks = []
    for td in task_dicts:
        try:
            task = Task(
                id=str(uuid.uuid4())[:8],
                instruction=td["instruction"],
                expected_tool=td.get("expected_tool", ""),
                expected_args=td.get("expected_args") or {},
                is_trap=bool(td.get("is_trap", False)),
                difficulty=Difficulty(td.get("difficulty", "easy")),
                rationale=td.get("rationale", ""),
            )
            tasks.append(task)
        except Exception as e:
            log.warning("Skipping malformed task dict %s: %s", td, e)

    log.info(
        "Generated %d tasks (%d real, %d traps) for '%s'",
        len(tasks),
        sum(1 for t in tasks if not t.is_trap),
        sum(1 for t in tasks if t.is_trap),
        title,
    )

    return TestSuite(manifest_title=title, tasks=tasks)


# ── Fallback (no Gemini) ──────────────────────────────────────────────────────

def _fallback_tasks(tools: list[dict]) -> list[dict]:
    """
    Generate minimal deterministic tasks when Gemini is unavailable.
    One task per tool + two generic traps.
    """
    tasks = []
    for t in tools[:8]:  # cap at 8 to keep eval fast
        name = t["name"]
        params = t.get("parameters", {})
        req_params = {
            k: None for k, v in params.items()
            if isinstance(v, dict) and v.get("required")
        }
        # Build a minimal natural-language instruction from the description
        desc = t.get("description", f"use the {name} tool")
        instruction = desc.split(".")[0].rstrip(",").strip()
        if not instruction:
            instruction = f"Call {name}"

        tasks.append({
            "instruction":   instruction,
            "expected_tool": name,
            "expected_args": req_params,
            "is_trap":       False,
            "difficulty":    "easy",
            "rationale":     f"Direct call to {name}",
        })

    # Two traps
    tasks.append({
        "instruction":   "What's the weather like in Mumbai right now?",
        "expected_tool": "",
        "expected_args": {},
        "is_trap":       True,
        "difficulty":    "medium",
        "rationale":     "No weather tool exists — agent should decline",
    })
    tasks.append({
        "instruction":   "Send me a reminder in 30 minutes",
        "expected_tool": "",
        "expected_args": {},
        "is_trap":       True,
        "difficulty":    "medium",
        "rationale":     "No reminder/scheduling tool exists",
    })

    return tasks
