"""
EvalRunner — runs one Task through a real Gemini agent.

For each task:
  1. Start with the manifest tools visible to the agent
  2. Give the agent the task instruction
  3. Intercept every tool call via Gemini function_call parts
  4. Return the actual tool calls made + the agent's final response

The agent is Gemini Flash (cheap, fast, still good at tool use).
We use the Gemini API directly — no MCP client SDK needed —
because we need to intercept tool calls at the message level.

The mock server runs alongside so tool calls return real (stub) responses,
which the agent can use to formulate a final answer.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from plugfit.app.config import settings

from .types import Difficulty, Outcome, TaskResult, ToolCall

log = logging.getLogger("plugfit.eval.runner")

MODEL = settings.AI_MODEL_NAME
MAX_TURNS = 6   # max agentic turns before we stop (prevents infinite loops)


# ── Tool schema builder ───────────────────────────────────────────────────────

def _make_tool_schemas(manifest: dict):
    """Convert manifest tools into Gemini tool schema format."""
    from google.genai import types

    declarations = []
    for t in manifest.get("tools", []):
        props: dict[str, Any] = {}
        required: list[str] = []
        for pname, p in t.get("parameters", {}).items():
            if not isinstance(p, dict):
                continue
            prop: dict = {"type": p.get("type", "string")}
            if p.get("description"):
                prop["description"] = p["description"]
            if p.get("enum"):
                prop["enum"] = p["enum"]
            props[pname] = prop
            if p.get("required"):
                required.append(pname)

        declarations.append(types.FunctionDeclaration(
            name=t["name"],
            description=t.get("description", ""),
            parameters_json_schema={
                "type": "object",
                "properties": props,
                **({"required": required} if required else {}),
            },
        ))
    return [types.Tool(function_declarations=declarations)]


# ── Single-task runner ────────────────────────────────────────────────────────

def run_task(
    task_instruction: str,
    manifest: dict,
    mock_server_url: str,
    timeout: int = 30,
) -> tuple[list[ToolCall], str]:
    """
    Run one task through Gemini with the manifest tools available.

    Returns:
        (tool_calls_made, agent_final_response)
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    tools = _make_tool_schemas(manifest)

    system = (
        "You are a helpful assistant with access to a set of tools. "
        "Use the tools to answer the user's request. "
        "If no tool is appropriate, say so clearly — do not make up tool calls. "
        "Be direct and concise."
    )
    config = types.GenerateContentConfig(
        system_instruction=system,
        tools=tools,
        max_output_tokens=1024,
    )

    contents: list = [types.Content(role="user", parts=[types.Part(text=task_instruction)])]
    tool_calls_made: list[ToolCall] = []
    final_response = ""

    for turn in range(MAX_TURNS):
        response = client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=config,
        )

        candidates = response.candidates or []
        if not candidates or not candidates[0].content:
            break

        model_content = candidates[0].content
        parts = model_content.parts or []

        # Collect text from this turn
        text_parts = []
        function_call_parts = []

        for part in parts:
            if part.text:
                text_parts.append(part.text)
            elif part.function_call:
                function_call_parts.append(part)

        if text_parts:
            final_response = " ".join(text_parts)

        # No tool calls → agent is done
        if not function_call_parts:
            break

        contents.append(model_content)

        # Execute each tool call against the mock server
        response_parts = []
        for part in function_call_parts:
            fc = part.function_call
            tool_name = fc.name
            tool_args = dict(fc.args) if fc.args else {}

            # Call the mock MCP server
            call_result, is_error = _call_mock_tool(
                mock_server_url, tool_name, tool_args
            )
            tool_calls_made.append(ToolCall(
                tool_name=tool_name,
                args=tool_args,
                result=call_result,
                is_error=is_error,
            ))

            response_parts.append(types.Part.from_function_response(
                name=tool_name,
                response={"error" if is_error else "result": call_result},
            ))

        # Append model turn + tool results to message history
        contents.append(types.Content(role="user", parts=response_parts))

    return tool_calls_made, final_response


class EvalRunner:
    """
    Runs a list of Task objects through the agent and returns an EvalReport.

    Uses run_agent from agent.py under the hood so the same agent loop
    is shared by both the old harness (MockMCPServer) and the new pipeline
    (InlineMCPClient / HttpMCPClient).
    """

    def __init__(
        self,
        manifest: dict,
        mcp_client,
        model: str = settings.AI_MODEL_NAME,
        gemini_api_key: str | None = None,
    ):
        self._manifest  = manifest
        self._client    = mcp_client
        self._model     = model
        self._api_key   = gemini_api_key

    def run(
        self,
        tasks: list,
        runs_per_task: int = 3,
        on_progress=None,
        is_before: bool = False,
    ):
        from .agent import run_agent
        from .scorer import build_report, classify_outcome
        from .types import (
            DIFFICULTY_WEIGHTS, OUTCOME_WEIGHTS, TRAP_BONUS,
            TaskResult, TestSuite, ToolCall,
        )

        suite = TestSuite(
            manifest_title=self._manifest.get("title", ""),
            tasks=list(tasks),
        )
        results: list[TaskResult] = []

        # Map this manifest's tool names → stable ids so classify_outcome
        # can match expected_tool_id even when names differ (raw vs cleaned).
        name_to_id = {
            t["name"]: (t.get("tool_id") or t["name"])
            for t in self._manifest.get("tools", [])
        }

        for idx, task in enumerate(tasks):
            best: TaskResult | None = None

            for _ in range(runs_per_task):
                agent_run = run_agent(
                    instruction=task.instruction,
                    mcp_client=self._client,
                    model=self._model,
                    api_key=self._api_key,
                )

                tool_calls = [
                    ToolCall(
                        tool_name=r.tool_name,
                        args=r.arguments,
                        result=r.response,
                        is_error=r.is_error,
                    )
                    for r in agent_run.tool_calls
                ]

                outcome, base_score = classify_outcome(
                    task, tool_calls, name_to_id=name_to_id
                )
                diff_w = DIFFICULTY_WEIGHTS.get(task.difficulty, 1.0)
                trap_w = TRAP_BONUS if task.is_trap else 1.0
                w_score = base_score * OUTCOME_WEIGHTS.get(outcome, 0.0) * diff_w * trap_w

                candidate = TaskResult(
                    task_id=task.id,
                    instruction=task.instruction,
                    expected_tool=task.expected_tool,
                    is_trap=task.is_trap,
                    difficulty=task.difficulty,
                    actual_calls=tool_calls,
                    outcome=outcome,
                    base_score=base_score,
                    weighted_score=w_score,
                    agent_response=agent_run.final_answer,
                    error=agent_run.error,
                )

                if best is None or base_score > best.base_score:
                    best = candidate

                if base_score >= 1.0:
                    break

            assert best is not None
            results.append(best)
            log.info(
                "Task %s [%s]: expected=%s actual=%s outcome=%s score=%.2f",
                task.id, task.difficulty.value,
                task.expected_tool or "∅",
                best.first_tool or "∅",
                best.outcome.value,
                best.base_score,
            )

            if on_progress:
                on_progress(idx + 1, len(tasks), task, best)

        return build_report(
            results=results,
            suite=suite,
            manifest_title=self._manifest.get("title", ""),
            is_before=is_before,
            model_used=self._model,
        )


def _call_mock_tool(server_url: str, tool_name: str, args: dict) -> tuple[str, bool]:
    """Call the mock MCP server synchronously. Returns (result_text, is_error)."""
    import urllib.error
    import urllib.request

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args},
    }
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        server_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            if "error" in body:
                return json.dumps(body["error"]), True
            result = body.get("result", {})
            content = result.get("content", [])
            text = content[0]["text"] if content else json.dumps(result)
            return text, result.get("isError", False)
    except Exception as e:
        return str(e), True
