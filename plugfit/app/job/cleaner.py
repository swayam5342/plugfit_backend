import copy
import json
import logging
import re
from typing import Any

from google.genai.types import GenerateContentResponse

from plugfit.app.config import settings

log = logging.getLogger(__name__)


def _get_client():
    from google import genai

    api_key = settings.GEMINI_API_KEY
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is not set. "
            "Get one at https://aistudio.google.com/apikey"
        )
    return genai.Client(api_key=api_key)


def _call_gemini(prompt: str, expect_json: bool = True) -> str:
    """Single Gemini call. Returns the text content of the response."""
    client = _get_client()
    response: GenerateContentResponse = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={
            "temperature": 0.2,
            "max_output_tokens": 8192,
        },
    )
    text: str = response.text.strip()
    if expect_json:
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


_GENERIC_ID_WORDS = {"id", "uuid", "slug", "key", "pk", "ref", "guid", "code"}


def _variants(w: str) -> set[str]:
    return {w, w + "s", w + "es", w.rstrip("s"), w.rstrip("es")}


def _is_generic_id_word(w: str) -> bool:
    if w in _GENERIC_ID_WORDS:
        return True
    for suffix in _GENERIC_ID_WORDS:
        if w.endswith(suffix) and len(w) > len(suffix):
            return True
    return False


def _path_resource_words(http_path: str) -> set[str]:
    if not http_path:
        return set()
    words: set[str] = set()
    for seg in http_path.split("/"):
        seg = seg.strip()
        if not seg or seg.startswith("{"):
            continue
        seg = re.sub(r"[-.]", "_", seg)
        for w in re.findall(r"[A-Za-z0-9]+", seg):
            words.add(w.lower())
    return words


def _normalise_name(name: str, http_path: str = "") -> str:
    http_methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    parts = [p for p in name.split("_") if p]
    while parts and parts[-1] in http_methods:
        parts.pop()

    if not parts:
        return name

    path_words = _path_resource_words(http_path)

    seen: set[str] = set()
    cut = len(parts)
    for i, seg in enumerate(parts):
        variants = _variants(seg)
        if i > 0 and variants & seen:
            cut = i
            break
        if i >= 2 and _is_generic_id_word(seg):
            cut = i
            break
        if i >= 2 and (variants & path_words) and seg not in seen:
            cut = i
            break

        seen.add(seg)

    result_parts = parts[:cut]
    while len(result_parts) > 2:
        last = result_parts[-1]
        earlier = result_parts[:-1]
        is_name_echo = any(
            last in _variants(p) or p in _variants(last) for p in earlier
        )
        is_path_echo = bool(_variants(last) & path_words)
        if is_name_echo or is_path_echo:
            result_parts.pop()
        else:
            break
    return "_".join(result_parts) or name


def _is_dead(tool: dict) -> bool:
    no_desc = not tool.get("description", "").strip()
    no_params = not tool.get("parameters")
    path = tool.get("http_path", "")
    is_root = path in ("/", "")
    return (no_desc and no_params) or is_root


_REWRITE_SYSTEM = """You are an expert at writing MCP tool descriptions that AI agents can use reliably.

Your task: rewrite the tool descriptions in the given manifest so that an AI agent can:
1. Instantly understand WHAT each tool does
2. Know WHEN to call it (vs similar tools)
3. Know WHAT ARGS to pass

Rules:
- Start every description with an action verb (Returns, Creates, Searches, Toggles, Deletes, Lists, Adds, Fetches)
- Be specific: name the entity being acted on
- If two tools are similar, clearly differentiate them in each description
- Max 2 sentences per description
- NEVER change tool names, parameters, or any field other than "description"
- Return ONLY a valid JSON array of objects with "name" and "description" fields
- No markdown, no code fences, no explanation — just the JSON array"""


def _rewrite_descriptions(tools: list[dict]) -> dict[str, str]:
    tool_summaries = []
    for t in tools:
        params = list(t.get("parameters", {}).keys())
        tool_summaries.append(
            {
                "name": t["name"],
                "current_description": t.get("description", ""),
                "http_method": t.get("http_method", ""),
                "http_path": t.get("http_path", ""),
                "parameters": params,
            }
        )

    prompt = f"""{_REWRITE_SYSTEM}

Here are the tools to rewrite:

{json.dumps(tool_summaries, indent=2)}

Return a JSON array like:
[
  {{"name": "tool_name", "description": "Rewritten description here."}},
  ...
]"""

    try:
        raw = _call_gemini(prompt, expect_json=True)
        rewrites = json.loads(raw)
        if not isinstance(rewrites, list):
            raise ValueError("Expected JSON array")
        return {
            r["name"]: r["description"]
            for r in rewrites
            if "name" in r and "description" in r
        }
    except Exception as e:
        log.warning("Description rewrite failed: %s — using originals", e)
        return {}


_DEDUP_SYSTEM = """You are analysing a list of MCP tools for duplicates.

Two tools are duplicates if they perform the same operation on the same entity.
Examples of duplicates:
  - list_users and get_all_users
  - get_movie and fetch_movie_by_id (if both take an id param)

Return a JSON object:
{
  "duplicates": [
    {"keep": "tool_name_to_keep", "drop": "tool_name_to_drop", "reason": "one sentence"}
  ]
}

If there are no duplicates, return: {"duplicates": []}
Return ONLY the JSON object, no markdown."""


def _find_duplicates(tools: list[dict]) -> list[dict]:
    """
    Ask Gemini to identify duplicate tools.
    Returns list of {keep, drop, reason} dicts.
    """
    if len(tools) < 2:
        return []

    summaries = [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "method": t.get("http_method", ""),
            "path": t.get("http_path", ""),
            "params": list(t.get("parameters", {}).keys()),
        }
        for t in tools
    ]

    prompt = f"""{_DEDUP_SYSTEM}

Tools to analyse:
{json.dumps(summaries, indent=2)}"""

    try:
        raw = _call_gemini(prompt, expect_json=True)
        data = json.loads(raw)
        return data.get("duplicates", [])
    except Exception as e:
        log.warning("Duplicate detection failed: %s — skipping dedup", e)
        return []


_HEAL_SYSTEM = """You are repairing MCP tool descriptions for an AI agent.

Improve the descriptions for the listed tools only.
Rules:
- Keep tool names, parameters, methods, and paths unchanged.
- Make each description more specific, action-oriented, and easier to choose correctly.
- Explain when to use the tool versus similar tools.
- Return ONLY a JSON object of the form {"replacements": [{"name": "tool_name", "description": "..."}]}
- No markdown, no code fences, no explanation."""


def _repair_descriptions(
    manifest: dict, tool_names: list[str], feedback: list[dict] | None = None
) -> dict[str, str]:
    tools = [
        tool for tool in manifest.get("tools", []) if tool.get("name") in tool_names
    ]
    if not tools:
        return {}

    feedback_map = {
        str(item.get("name")): str(item.get("issue", "")).strip()
        for item in feedback or []
        if isinstance(item, dict) and item.get("name")
    }

    tool_summaries = []
    for tool in tools:
        tool_summaries.append(
            {
                "name": tool.get("name"),
                "current_description": tool.get("description", ""),
                "http_method": tool.get("http_method", ""),
                "http_path": tool.get("http_path", ""),
                "parameters": list(tool.get("parameters", {}).keys()),
                "feedback": feedback_map.get(tool.get("name", ""), ""),
            }
        )

    prompt = f"""{_HEAL_SYSTEM}

Tools to repair:
{json.dumps(tool_summaries, indent=2)}"""

    try:
        raw = _call_gemini(prompt, expect_json=True)
        data = json.loads(raw)
        replacements = data.get("replacements", [])
        return {
            r["name"]: r["description"]
            for r in replacements
            if isinstance(r, dict) and "name" in r and "description" in r
        }
    except Exception as exc:
        log.warning("Description repair failed: %s — leaving manifest unchanged", exc)
        return {}


def heal_manifest(
    manifest: dict,
    feedback: list[dict] | None = None,
    initial_score: float | None = None,
    max_attempts: int = 3,
) -> tuple[dict, float, list[dict]]:
    """
    Iteratively improve low-quality tool descriptions using Gemini feedback.
    Stops early when the score no longer improves materially.
    Returns (manifest, final_score, feedback).
    """
    current = copy.deepcopy(manifest)
    tools = current.get("tools", [])
    if not tools:
        return current, float(initial_score or 0.0), list(feedback or [])

    from .scorer import gemini_score

    if initial_score is None:
        initial_score, initial_feedback = gemini_score(current)
    else:
        initial_feedback = feedback or []

    best_manifest = copy.deepcopy(current)
    best_score = float(initial_score if initial_score is not None else 0.0)
    best_feedback = list(initial_feedback or [])
    history: list[dict[str, Any]] = []

    target_names = [
        str(item.get("name"))
        for item in (feedback or [])
        if isinstance(item, dict)
        and item.get("name")
        and (
            (item.get("clarity", 10) < 7)
            or (item.get("selectability", 10) < 7)
            or (item.get("param_clarity", 10) < 7)
            or str(item.get("issue", "")).strip().lower() not in {"", "none"}
        )
    ]
    if not target_names:
        target_names = [tool.get("name", "") for tool in tools if tool.get("name")]

    for attempt in range(1, max(1, max_attempts) + 1):
        if not target_names:
            break

        replacements = _repair_descriptions(current, target_names, feedback)
        if not replacements:
            break

        for tool in current.get("tools", []):
            if tool.get("name") in replacements:
                tool["description"] = replacements[tool.get("name")]

        healed_score, healed_feedback = gemini_score(current)
        improvement = healed_score - best_score
        history.append(
            {
                "attempt": attempt,
                "score": round(healed_score, 1),
                "improvement": round(improvement, 1),
                "targeted_tools": target_names,
            }
        )

        if improvement >= 2.0:
            best_manifest = copy.deepcopy(current)
            best_score = healed_score
            best_feedback = healed_feedback
            target_names = [
                str(item.get("name"))
                for item in healed_feedback
                if isinstance(item, dict)
                and item.get("name")
                and (
                    (item.get("clarity", 10) < 7)
                    or (item.get("selectability", 10) < 7)
                    or (item.get("param_clarity", 10) < 7)
                    or str(item.get("issue", "")).strip().lower() not in {"", "none"}
                )
            ]
            if healed_score >= 90:
                break
            continue

        break

    best_manifest["_healing_meta"] = {
        "attempts": len(history),
        "initial_score": round(
            float(initial_score if initial_score is not None else best_score), 1
        ),
        "final_score": round(float(best_score), 1),
        "improved": best_score > float(initial_score if initial_score is not None else best_score),
        "history": history,
    }
    if best_feedback:
        best_manifest["_score_feedback"] = best_feedback
    return best_manifest, round(float(best_score), 1), list(best_feedback)


def clean_manifest(raw_manifest: dict) -> dict:
    manifest = copy.deepcopy(raw_manifest)
    tools: list[dict] = manifest.get("tools", [])
    original_count = len(tools)
    dropped: list[str] = []
    merged: list[str] = []

    log.info("Cleaning manifest: %d tools", original_count)

    # ── Stage 1: Name normalisation ───────────────────────────────────────────
    for tool in tools:
        old_name = tool["name"]
        tool["name"] = _normalise_name(old_name, tool.get("http_path", ""))
        if tool["name"] != old_name:
            log.info("  Renamed: %s → %s", old_name, tool["name"])

    # ── Stage 2: Drop dead tools ──────────────────────────────────────────────
    live_tools = []
    for tool in tools:
        if _is_dead(tool):
            log.info("  Dropped dead tool: %s", tool["name"])
            dropped.append(tool["name"])
        else:
            live_tools.append(tool)
    tools = live_tools

    # ── Stage 3: LLM description rewrite ─────────────────────────────────────
    if tools:
        log.info("  Rewriting %d descriptions via Gemini...", len(tools))
        rewrites = _rewrite_descriptions(tools)
        rewrite_count = 0
        for tool in tools:
            if tool["name"] in rewrites:
                tool["description"] = rewrites[tool["name"]]
                rewrite_count += 1
        log.info("  Rewrote %d descriptions", rewrite_count)

    # ── Stage 4: Duplicate detection ─────────────────────────────────────────
    if len(tools) >= 2:
        log.info("  Checking for duplicates...")
        dupes = _find_duplicates(tools)
        if dupes:
            drop_names = {d["drop"] for d in dupes}
            for d in dupes:
                log.info(
                    "  Merged: keep=%s drop=%s (%s)", d["keep"], d["drop"], d["reason"]
                )
                merged.append(d["drop"])
            tools = [t for t in tools if t["name"] not in drop_names]

    # ── Assemble cleaned manifest ─────────────────────────────────────────────
    manifest["tools"] = tools
    manifest["tool_count"] = len(tools)
    manifest["title"] = (manifest.get("title") or "").replace(
        " (cleaned)", ""
    ) + " (cleaned)"
    manifest["_cleaning_meta"] = {
        "original_count": original_count,
        "final_count": len(tools),
        "dropped": dropped,
        "merged": merged,
    }

    log.info(
        "Cleaning complete: %d → %d tools (%d dropped, %d merged)",
        original_count,
        len(tools),
        len(dropped),
        len(merged),
    )
    return manifest
