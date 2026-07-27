import json
import logging
import re
from plugfit.app.config import settings

log = logging.getLogger("plugfit.scorer")


_ACTION_VERBS = {
    "returns",
    "retrieves",
    "creates",
    "adds",
    "deletes",
    "updates",
    "toggles",
    "searches",
    "lists",
    "fetches",
    "sends",
    "gets",
    "marks",
    "removes",
    "checks",
    "reads",
    "writes",
    "generates",
    "calculates",
    "finds",
    "filters",
    "downloads",
    "uploads",
    "validates",
    "enables",
    "disables",
    "triggers",
    "starts",
    "stops",
}


def heuristic_score(manifest: dict) -> float:
    """
    Fast description-quality score. No LLM required.
    Returns 0.0 – 100.0.
    """
    tools = manifest.get("tools", [])
    if not tools:
        return 0.0

    total = 0.0
    for t in tools:
        desc = t.get("description", "")
        name = t.get("name", "")
        pts = 0.0

        # Description length
        if len(desc) > 80:
            pts += 40
        elif len(desc) > 40:
            pts += 25
        elif len(desc) > 15:
            pts += 10

        # Starts with action verb
        first = desc.split()[0].lower() if desc else ""
        if first in _ACTION_VERBS:
            pts += 30

        # Clean tool name (no double underscores, no method suffixes)
        if "__" not in name and not name.endswith(("_get", "_post", "_put", "_delete")):
            pts += 30

        total += pts

    return round(min(total / len(tools), 100), 1)


# ── Gemini scorer ─────────────────────────────────────────────────────────────

_SCORE_SYSTEM = """You are an AI agent evaluating how well a set of MCP tool descriptions would help you (the agent) do your job.

For each tool, score it from 0 to 10 on THREE criteria:
- clarity: Do you immediately understand what the tool does and what it returns?
- selectability: If multiple similar tools exist, would you pick the right one?  
- param_clarity: Are the parameter names and types clear enough to call without guessing?

Also give a one-line "issue" describing the worst problem (or "none" if perfect).

Return ONLY a JSON array — no markdown, no code fences:
[
  {
    "name": "tool_name",
    "clarity": 8,
    "selectability": 6,
    "param_clarity": 9,
    "issue": "Unclear when to use this vs get_movies"
  },
  ...
]"""


def gemini_score(manifest: dict) -> tuple[float, list[dict]]:
    """
    LLM-based quality score using Gemini.

    Returns:
        (score_0_to_100, per_tool_feedback_list)

    Falls back to heuristic_score if Gemini is unavailable.
    """
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        log.warning("GEMINI_API_KEY not set — using heuristic scorer")
        return heuristic_score(manifest), []

    tools = manifest.get("tools", [])
    if not tools:
        return 0.0, []

    # Build prompt payload — only what the agent sees
    tool_descriptions = [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": list(t.get("parameters", {}).keys()),
        }
        for t in tools
    ]

    prompt = f"""{_SCORE_SYSTEM}

Here are the tools to score:
{json.dumps(tool_descriptions, indent=2)}"""

    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=settings.AI_MODEL_NAME,
            contents=prompt,
            config={"temperature": 0.1, "max_output_tokens": 4096},
        )
        raw = response.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        feedback = json.loads(raw)
        if not isinstance(feedback, list):
            raise ValueError("Expected JSON array")
        scores = []
        for item in feedback:
            avg = (
                item.get("clarity", 5)
                + item.get("selectability", 5)
                + item.get("param_clarity", 5)
            ) / 3.0
            scores.append(avg)

        if not scores:
            return heuristic_score(manifest), feedback

        overall = round((sum(scores) / len(scores)) * 10, 1)
        log.info("Gemini score: %.1f/100 across %d tools", overall, len(scores))
        return overall, feedback

    except Exception as e:
        log.warning("Gemini scoring failed: %s — falling back to heuristic", e)
        return heuristic_score(manifest), []
