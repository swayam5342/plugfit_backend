import json
import logging
import re
import copy
from plugfit.app.config import settings
from plugfit.ingest.models import stable_tool_id
from google.genai.types import GenerateContentResponse

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
        model=settings.AI_MODEL_NAME,
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
    """
    A tool is only dropped when ALL weak signals coincide: an HTTP-style
    tool at the root/blank path with no description AND no params (typical
    index/health junk). Metadata absence alone never kills a callable tool,
    and MCP tools (no http metadata at all) are never metadata-dropped —
    prefer false-keeps over false-drops.
    """
    no_desc = not tool.get("description", "").strip()
    no_params = not tool.get("parameters")
    path = tool.get("http_path", "")
    method = tool.get("http_method", "")
    if not (method or path):
        return False
    return no_desc and no_params and path in ("/", "")


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
  - get_item and fetch_item_by_id (if both take an id param)

Return a JSON object:
{
  "duplicates": [
    {"keep": "tool_name_to_keep", "drop": "tool_name_to_drop", "reason": "one sentence"}
  ]
}

If there are no duplicates, return: {"duplicates": []}
Return ONLY the JSON object, no markdown."""


def _merge_allowed(keep: dict, drop: dict) -> bool:
    """
    Deterministic behavior-preservation guard for Gemini-proposed merges.

    A merge is only safe if the two tools demonstrably hit the same
    operation: identical http_method + http_path. Tools without any HTTP
    metadata (MCP) must instead declare identical parameter names.
    """
    keep_method = (keep.get("http_method") or "").upper()
    drop_method = (drop.get("http_method") or "").upper()
    keep_path = keep.get("http_path") or ""
    drop_path = drop.get("http_path") or ""

    if keep_method or drop_method or keep_path or drop_path:
        return keep_method == drop_method and keep_path == drop_path

    keep_params = set((keep.get("parameters") or {}).keys())
    drop_params = set((drop.get("parameters") or {}).keys())
    return keep_params == drop_params


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


def clean_manifest(raw_manifest: dict) -> dict:
    manifest = copy.deepcopy(raw_manifest)
    tools: list[dict] = manifest.get("tools", [])
    original_count = len(tools)
    dropped: list[str] = []
    dropped_ids: list[str] = []
    merged: list[str] = []
    merged_ids: list[str] = []

    log.info("Cleaning manifest: %d tools", original_count)

    # ── Stage 0: Ensure stable tool ids (legacy manifests may lack them) ─────
    # Must run BEFORE renaming so the id is derived from the original name.
    for tool in tools:
        if not tool.get("tool_id"):
            tool["tool_id"] = stable_tool_id(
                tool.get("name", ""),
                tool.get("http_method", ""),
                tool.get("http_path", ""),
            )

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
            dropped_ids.append(tool["tool_id"])
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
            by_name = {t["name"]: t for t in tools}
            drop_names: set[str] = set()
            for d in dupes:
                keep_tool = by_name.get(d.get("keep", ""))
                drop_tool = by_name.get(d.get("drop", ""))
                if not keep_tool or not drop_tool or keep_tool is drop_tool:
                    log.warning("  Merge rejected (unknown tool): %s", d)
                    continue
                if not _merge_allowed(keep_tool, drop_tool):
                    log.warning(
                        "  Merge rejected (method/path mismatch): keep=%s drop=%s (%s)",
                        d["keep"],
                        d["drop"],
                        d.get("reason", ""),
                    )
                    continue
                log.info(
                    "  Merged: keep=%s drop=%s (%s)",
                    d["keep"],
                    d["drop"],
                    d.get("reason", ""),
                )
                merged.append(d["drop"])
                merged_ids.append(drop_tool["tool_id"])
                drop_names.add(d["drop"])
            tools = [t for t in tools if t["name"] not in drop_names]
    manifest["tools"] = tools
    manifest["tool_count"] = len(tools)
    manifest["title"] = (manifest.get("title") or "").replace(
        " (cleaned)", ""
    ) + " (cleaned)"
    manifest["_cleaning_meta"] = {
        "original_count": original_count,
        "final_count": len(tools),
        "dropped": dropped,
        "dropped_ids": dropped_ids,
        "merged": merged,
        "merged_ids": merged_ids,
    }

    log.info(
        "Cleaning complete: %d → %d tools (%d dropped, %d merged)",
        original_count,
        len(tools),
        len(dropped),
        len(merged),
    )
    return manifest
