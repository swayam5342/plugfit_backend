import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("plugfit.eval.agent")

MAX_TURNS = 6  # max back-and-forth before we declare no_call
MAX_TOKENS = 1024  # keep costs low — we just need tool selection + brief answer
DEFAULT_MODEL = "gemini-2.5-flash"


@dataclass
class ToolCallRecord:
    """One tool invocation by the agent."""

    tool_name: str
    arguments: dict
    response: str = ""
    is_error: bool = False


@dataclass
class AgentRun:
    """Full result of one agent run on one task."""

    task_instruction: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    final_answer: str = ""
    error: str = ""
    turn_count: int = 0

    @property
    def first_tool(self) -> str:
        return self.tool_calls[0].tool_name if self.tool_calls else ""

    @property
    def first_args(self) -> dict:
        return self.tool_calls[0].arguments if self.tool_calls else {}


def run_agent(
    instruction: str,
    mcp_client,  # HttpMCPClient or InlineMCPClient
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
) -> AgentRun:
    """
    Run Gemini on a single instruction with the MCP tools available.

    Uses gemini-2.5-flash by default — fast and cheap for eval.

    Returns AgentRun with the full tool call trace.
    """
    key = api_key or os.getenv("GEMINI_API_KEY", "")
    if not key:
        return AgentRun(
            task_instruction=instruction,
            error="GEMINI_API_KEY not set",
        )

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key)
    try:
        mcp_client.initialize()
        mcp_tools = mcp_client.list_tools()
    except Exception as e:
        return AgentRun(task_instruction=instruction, error=f"MCP init failed: {e}")

    if not mcp_tools:
        return AgentRun(
            task_instruction=instruction, error="MCP server returned no tools"
        )

    # Convert MCP tool schemas to Gemini tool format
    gemini_tools = _mcp_to_gemini_tools(mcp_tools)

    system = (
        "You are a helpful assistant with access to a set of tools. "
        "Use the most appropriate tool to answer the user's request. "
        "Be direct — call the tool immediately without preamble. "
        "If no tool is appropriate, say so briefly."
    )

    config = types.GenerateContentConfig(
        system_instruction=system,
        tools=gemini_tools,
        max_output_tokens=MAX_TOKENS,
    )

    contents: list = [
        types.Content(role="user", parts=[types.Part(text=instruction)])
    ]
    run = AgentRun(task_instruction=instruction)

    for turn in range(MAX_TURNS):
        run.turn_count = turn + 1

        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as e:
            run.error = str(e)
            log.warning("Gemini API error on turn %d: %s", turn, e)
            break

        candidates = response.candidates or []
        if not candidates or not candidates[0].content:
            break

        model_content = candidates[0].content
        parts = model_content.parts or []

        text_parts = []
        function_call_parts = []

        for part in parts:
            if part.text:
                text_parts.append(part.text)
            elif part.function_call:
                function_call_parts.append(part)

        if text_parts:
            run.final_answer = " ".join(text_parts).strip()

        # No tool calls → agent is done
        if not function_call_parts:
            break

        contents.append(model_content)

        # Execute each tool call through the MCP client
        response_parts = []
        for part in function_call_parts:
            fc = part.function_call
            record = ToolCallRecord(
                tool_name=fc.name,
                arguments=dict(fc.args) if fc.args else {},
            )

            try:
                resp_text, is_error = mcp_client.call_tool(fc.name, record.arguments)
                record.response = resp_text
                record.is_error = is_error
            except Exception as e:
                record.response = str(e)
                record.is_error = True

            run.tool_calls.append(record)
            log.debug(
                "Tool call: %s(%s) → %s",
                fc.name,
                list(record.arguments.keys()),
                "error" if record.is_error else "ok",
            )

            response_parts.append(
                types.Part.from_function_response(
                    name=fc.name,
                    response={
                        "error" if record.is_error else "result": record.response
                    },
                )
            )

        # Continue the conversation with tool results
        contents.append(types.Content(role="user", parts=response_parts))

    return run


def _mcp_to_gemini_tools(mcp_tools: list[dict]):
    """
    Convert MCP tool schemas → Gemini tool definitions.

    MCP:
      {"name": "...", "description": "...", "inputSchema": {"type": "object", ...}}

    Gemini:
      types.Tool(function_declarations=[types.FunctionDeclaration(
          name=..., description=..., parameters_json_schema={"type": "object", ...}
      )])
    """
    from google.genai import types

    declarations = []
    for t in mcp_tools:
        schema = t.get(
            "inputSchema", t.get("input_schema", {"type": "object", "properties": {}})
        )
        declarations.append(
            types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters_json_schema=schema,
            )
        )
    return [types.Tool(function_declarations=declarations)]
