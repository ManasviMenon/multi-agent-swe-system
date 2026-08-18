"""Shared machinery for the Coder and Tester agents: the read-only filesystem tools both
need, the Gemini interactions-API retry/error handling, and the generic multi-turn
tool-calling loop driver. Extracted out of coder.py once the Tester needed the exact same
pieces -- not a speculative abstraction, a real second caller.
"""

import json
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors

# The interactions API (used for multi-turn function calling) raises through a
# different, more internal error hierarchy than the rest of the SDK -- errors.ClientError
# doesn't catch what client.interactions.create() actually throws on a 429.
from google.genai._gaos.lib import compat_errors

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent.parent / ".env")

MAX_RETRY_ATTEMPTS = 5


class DailyQuotaExhausted(Exception):
    pass


class NetworkError(Exception):
    pass


class RateLimitExhausted(Exception):
    pass


def resolve_in_worktree(worktree: Path, rel_path: str) -> Path:
    """Resolves a model-supplied path and enforces it stays inside the worktree.

    An agent's tools are its only way to touch the filesystem, and SCOPE.md's
    role-restriction guardrail says that has to be enforced in code, not just
    asked nicely of the model in the system prompt.
    """
    resolved = (worktree / rel_path).resolve()
    if worktree.resolve() not in resolved.parents and resolved != worktree.resolve():
        raise ValueError(f"path '{rel_path}' escapes the worktree")
    return resolved


READ_ONLY_TOOL_DEFS = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read the full contents of a file in the repository.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to repo root"}},
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "list_directory",
        "description": "List files and subdirectories at a path in the repository.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to repo root"}},
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "search_files",
        "description": "Search for a regex pattern across .py files under a path. Returns matching lines with file:line.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Path relative to repo root to search under"},
            },
            "required": ["pattern"],
        },
    },
]


def execute_read_only_tool(name: str, args: dict, worktree: Path) -> dict | None:
    """Handles the three shared read-only tools. Returns None if name isn't one of
    them, so a caller's own execute_tool can fall through to its role-specific tools."""
    if name == "read_file":
        path = resolve_in_worktree(worktree, args["path"])
        if not path.is_file():
            return {"error": f"no such file: {args['path']}"}
        return {"content": path.read_text(encoding="utf-8", errors="replace")}

    if name == "list_directory":
        path = resolve_in_worktree(worktree, args.get("path", "."))
        if not path.is_dir():
            return {"error": f"no such directory: {args.get('path', '.')}"}
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return {"entries": entries}

    if name == "search_files":
        import re

        base = resolve_in_worktree(worktree, args.get("path", "."))
        try:
            regex = re.compile(args["pattern"])
        except re.error as e:
            return {"error": f"invalid regex pattern: {e}"}
        matches = []
        for py_file in base.rglob("*.py"):
            try:
                text = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    rel = py_file.relative_to(worktree)
                    matches.append(f"{rel}:{i}: {line.strip()}")
                    if len(matches) >= 100:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches}

    return None


def _status_code(e: Exception) -> int | None:
    if isinstance(e, errors.ClientError):
        return e.code
    response = getattr(e, "response", None)
    return getattr(response, "status_code", None)


def _is_per_day_quota_error(e: Exception) -> bool:
    text = str(getattr(e, "body", None) or getattr(e, "details", None) or str(e)).lower()
    normalized = text.replace(" ", "")
    # Google has used at least two different metric name patterns for the free-tier
    # daily request cap across different error responses ("...requests_per_day..." and
    # "free_tier_requests, limit: 500") -- match both rather than just the first one we
    # happened to see, since a daily-quota error that isn't recognized as such gets
    # retried as if it were transient, wasting the retry budget on a wall that won't
    # move until the next day regardless of how long the backoff is.
    return (
        "perday" in normalized
        or "generate_requests_per_day" in text.replace(" ", "_")
        or "free_tier_requests" in normalized
    )


RATE_LIMIT_EXCEPTIONS = (errors.ClientError, compat_errors.RateLimitError, compat_errors.APIStatusError)


def create_with_retry(client, **kwargs):
    for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
            return client.interactions.create(**kwargs)
        except compat_errors.APIConnectionError as e:
            # A DNS/network blip is usually transient -- worth a few retries before
            # giving up, unlike a real bug in the model's tool call. If it's still
            # failing after retries, this propagates as NetworkError so the caller can
            # stop the whole batch run rather than recording every remaining ticket as
            # a bogus "failure" (see the Phase 2 run that produced 21 spurious results
            # this way before this fix existed).
            if attempt >= MAX_RETRY_ATTEMPTS - 1:
                raise NetworkError(str(e)) from e
            time.sleep(min(30, 5 * (2**attempt)))
        except RATE_LIMIT_EXCEPTIONS as e:
            if _status_code(e) != 429:
                raise
            if _is_per_day_quota_error(e):
                raise DailyQuotaExhausted(str(e)) from e
            time.sleep(min(60, 5 * (2**attempt)))
    raise RateLimitExhausted("exceeded retry attempts on transient rate limiting")


DUPLICATE_CALL_MESSAGE = (
    "You already called this exact tool with these exact arguments earlier in this "
    "session and got the same result -- repeating it wastes your limited tool-call "
    "budget. Use what you already learned from that earlier call instead."
)


def run_agent_loop(
    model: str,
    initial_input,
    tool_defs: list,
    execute_tool_fn,
    worktree: Path,
    max_tool_calls: int,
    system_instruction: str | None = None,
    previous_interaction_id: str | None = None,
    temperature: float | None = None,
    no_duplicate_check_tools: frozenset = frozenset(),
) -> dict:
    """Runs one bounded multi-turn tool-calling session.

    If previous_interaction_id is given, this continues an existing conversation
    (system_instruction is not re-sent -- it's already established on that chain) rather
    than starting a fresh one. Returns a transcript dict including last_interaction_id so
    a caller can chain further rounds (used for the Coder's multi-attempt retry loop).

    temperature is left at the API default (None) unless a caller opts in -- the Tester
    passes 0.0 for repeatability on its write-a-test/confirm-it-fails task, the Coder
    intentionally does not, since some randomness is part of exploring different fix
    approaches across retry attempts. Applied to every call in the session (not just the
    first), so it stays consistent across a continued multi-round conversation too.

    no_duplicate_check_tools exempts specific tool names from duplicate-call blocking.
    That blocking assumes "same tool + same arguments = same result," which is right for
    tools like search_files (identical args really do mean a wasted repeat) but wrong for
    a tool whose real input is external state not captured in its arguments -- e.g. the
    Tester's run_written_test takes no parameters at all, so every call after the first
    was being silently blocked as a "duplicate" even when the test file had genuinely
    changed via write_test in between, capping verification to once per round regardless
    of how many times the model revised its test.
    """
    client = genai.Client()
    transcript = {"steps": [], "tool_call_count": 0, "hit_cap": False, "total_tokens": 0}
    seen_calls = set()
    generation_config = {"temperature": temperature} if temperature is not None else None

    create_kwargs = {"model": model, "input": initial_input, "tools": tool_defs}
    if previous_interaction_id:
        create_kwargs["previous_interaction_id"] = previous_interaction_id
    elif system_instruction:
        create_kwargs["system_instruction"] = system_instruction
    if generation_config:
        create_kwargs["generation_config"] = generation_config

    interaction = create_with_retry(client, **create_kwargs)
    transcript["total_tokens"] += interaction.usage.total_tokens or 0

    while interaction.status == "requires_action":
        fc_steps = [s for s in interaction.steps if s.type == "function_call"]
        if not fc_steps:
            break

        results = []
        for fc in fc_steps:
            if transcript["tool_call_count"] >= max_tool_calls:
                transcript["hit_cap"] = True
                break
            transcript["tool_call_count"] += 1

            call_signature = (fc.name, json.dumps(fc.arguments, sort_keys=True))
            is_duplicate = fc.name not in no_duplicate_check_tools and call_signature in seen_calls
            if is_duplicate:
                result = {"error": DUPLICATE_CALL_MESSAGE}
            else:
                seen_calls.add(call_signature)
                result = execute_tool_fn(fc.name, fc.arguments, worktree)

            transcript["steps"].append(
                {"tool": fc.name, "args": fc.arguments, "result": result, "blocked_duplicate": is_duplicate}
            )
            results.append(
                {
                    "type": "function_result",
                    "name": fc.name,
                    "call_id": fc.id,
                    "result": [{"type": "text", "text": json.dumps(result)}],
                }
            )

        if transcript["hit_cap"] or not results:
            break

        continue_kwargs = {
            "model": model,
            "input": results,
            "tools": tool_defs,
            "previous_interaction_id": interaction.id,
        }
        if generation_config:
            continue_kwargs["generation_config"] = generation_config
        interaction = create_with_retry(client, **continue_kwargs)
        transcript["total_tokens"] += interaction.usage.total_tokens or 0

    transcript["final_message"] = interaction.output_text or ""
    transcript["last_interaction_id"] = interaction.id
    return transcript
