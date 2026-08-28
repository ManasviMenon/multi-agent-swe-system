"""Shared machinery for the Coder and Tester agents: the read-only filesystem tools both
need, the Gemini interactions-API retry/error handling, and the generic multi-turn
tool-calling loop driver. Extracted out of coder.py once the Tester needed the exact same
pieces -- not a speculative abstraction, a real second caller.
"""

import json
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

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


LARGE_FILE_LINE_THRESHOLD = 200


READ_ONLY_TOOL_DEFS = [
    {
        "type": "function",
        "name": "read_file",
        "description": (
            "Read a file's contents. For large files (over "
            f"{LARGE_FILE_LINE_THRESHOLD} lines), reading without start_line/end_line "
            "returns a preview (line count + a snippet) instead of the whole file -- "
            "use search_files first to find the relevant line numbers, then pass "
            "start_line/end_line here to read just that section. Every full-file read "
            "you make permanently inflates the token cost of every later turn in this "
            "session, so read only the section you actually need."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to repo root"},
                "start_line": {"type": "integer", "description": "1-indexed first line to read (optional)"},
                "end_line": {"type": "integer", "description": "1-indexed last line to read, inclusive (optional)"},
            },
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
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start_line, end_line = args.get("start_line"), args.get("end_line")

        # Content is always returned raw (no injected line-number prefixes) even when
        # sliced -- edit_file needs to match this text verbatim against the real file,
        # and prefixing lines with "N: " would break that the same way the earlier
        # escaped-quote bug did (see coder.py's edit_file history).
        if start_line is not None or end_line is not None:
            start = max(1, start_line or 1)
            end = min(len(lines), end_line or len(lines))
            return {"content": "\n".join(lines[start - 1:end]), "total_lines": len(lines), "shown_lines": f"{start}-{end}"}

        if len(lines) > LARGE_FILE_LINE_THRESHOLD:
            return {
                "content": "\n".join(lines[:30]),
                "total_lines": len(lines),
                "note": (
                    f"This file has {len(lines)} lines -- only showing lines 1-30 (a "
                    "preview) because reading a large file whole permanently inflates "
                    "every later turn's token cost for the rest of this session. Use "
                    "search_files to find the lines you actually need, then call "
                    "read_file again with start_line/end_line for just that section."
                ),
            }

        return {"content": "\n".join(lines)}

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


def _retry_after_seconds(e: Exception, default: float) -> float:
    """Parses the server's own "Please retry in X.Ys" hint out of the error text when
    present, rather than blindly backing off on our own schedule -- more accurate for
    clearing a short per-minute (TPM/RPM) burst than a generic exponential wait."""
    text = str(getattr(e, "body", None) or getattr(e, "details", None) or str(e))
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", text, re.IGNORECASE)
    if match:
        return min(65.0, float(match.group(1)) + 2.0)  # small buffer past the server's own hint
    return default


def create_with_retry(client, **kwargs):
    last_error = None
    for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
            return client.interactions.create(**kwargs)
        except compat_errors.BadRequestError as e:
            # "malformed_tool_call" is the model itself emitting invalid JSON for a
            # function call -- Gemini's own error text says "Please retry the request",
            # i.e. this is a one-off generation glitch, not a bug in our request. Seen
            # twice now (marshmallow-1357, marshmallow-2868) recorded as a permanent
            # per-ticket "error" result because this exception wasn't retried at all --
            # same poisoned-resumability failure mode as DailyQuotaExhausted/NetworkError
            # before those were fixed. Any OTHER BadRequestError (a real malformed
            # request from our own code) should still fail immediately, not retry-and-hide.
            if "malformed_tool_call" not in str(e) or attempt >= MAX_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(min(30, 5 * (2**attempt)))
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
            # Do NOT immediately raise DailyQuotaExhausted on the first sighting of
            # something that looks like a daily-quota error -- the account's own live
            # dashboard showed RPD with headroom (239/500) while TPM was over its cap
            # (363K/250K) at the exact time this was misfiring, meaning the "requests"
            # wording in Google's error text doesn't reliably mean "genuinely exhausted
            # for the day" -- a short-lived per-minute burst can produce the same
            # message. Always let the real retry loop run first; only decide it's a
            # true daily exhaustion if it's STILL failing after actual backoff attempts
            # (a real per-day exhaustion won't clear in under two minutes no matter what
            # it's classified as, so this costs nothing in the genuine case).
            last_error = e
            if attempt >= MAX_RETRY_ATTEMPTS - 1:
                break
            time.sleep(_retry_after_seconds(e, default=min(60, 5 * (2**attempt))))
    if last_error is not None and _is_per_day_quota_error(last_error):
        raise DailyQuotaExhausted(str(last_error)) from last_error
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
    # Without an explicit timeout, a stalled connection to the API blocks the whole
    # pipeline indefinitely -- observed directly: a Phase 3 rerun sat at ~0 CPU seconds
    # for 40+ minutes on one call with no retry ever triggering, since create_with_retry
    # only reacts to exceptions the underlying request never raised.
    client = genai.Client(http_options=types.HttpOptions(timeout=180_000))
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

        if not results:
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

        if transcript["hit_cap"]:
            break

    if transcript["hit_cap"] and interaction.status == "requires_action":
        # The model still wanted to call more tools when its budget ran out. Previously
        # the loop just broke here, discarding whatever results WERE already sent back
        # above and leaving final_message empty -- catastrophic for the Planner, whose
        # entire output IS this field: confirmed via transcripts that 10/10 Phase 4
        # validation tickets hit this exact path and every one produced an empty plan,
        # meaning the Planner never once actually influenced a Coder prompt. One explicit
        # final turn, with no tools offered, forces a real answer from what it already
        # has instead of silently returning nothing.
        nudge_kwargs = {
            "model": model,
            "input": (
                "You have used up your tool-call budget and cannot call any more tools. "
                "Give your final answer now, based on everything you've learned so far."
            ),
            "previous_interaction_id": interaction.id,
        }
        if generation_config:
            nudge_kwargs["generation_config"] = generation_config
        interaction = create_with_retry(client, **nudge_kwargs)
        transcript["total_tokens"] += interaction.usage.total_tokens or 0

    transcript["final_message"] = interaction.output_text or ""
    transcript["last_interaction_id"] = interaction.id
    return transcript
