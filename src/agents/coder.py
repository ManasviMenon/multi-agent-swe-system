"""The Phase 2 baseline Coder agent: one model, no planner, no tester, no reviewer.

Given a ticket's issue text and a worktree checked out at the ticket's base commit,
the Coder explores the repo with read-only tools and edits files directly with a
write tool. It never sees gold_patch.diff or test_patch.diff, and has no test-running
tool -- this is deliberately the weak, single-shot baseline that later phases (Tester
loop, Planner, Reviewer+Judge) have to beat.
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

MODEL = "gemini-3.5-flash-lite"
MAX_TOOL_CALLS = 20
MAX_RETRY_ATTEMPTS = 5

SYSTEM_INSTRUCTION = """\
You are the Coder agent in a multi-agent software engineering system. You are given \
a real GitHub issue describing a bug in the `marshmallow` Python library. Your job is \
to fix the underlying bug with a minimal, targeted source code change.

Rules:
- Explore the repository using the read-only tools before making any change.
- Only edit files under src/marshmallow/ -- never touch anything under tests/.
- Make the smallest change that fixes the described bug. Do not refactor unrelated code.
- You cannot run tests. Reason carefully about correctness instead of relying on execution.
- When you are confident the fix is complete, stop calling tools and reply with a short \
summary of what you changed and why.

You have a limited number of tool calls, so explore efficiently:
- Never repeat a search or file read you've already done -- you already have that result.
- As soon as you have located the root cause, call edit_file to apply the fix. Do not \
keep exploring after you already understand what needs to change -- but also do not \
write a fix before you've actually located and understood the relevant code. A confident \
edit based on a misunderstanding is worse than taking a few extra calls to get it right.
"""

TOOL_DEFS = [
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
    {
        "type": "function",
        "name": "edit_file",
        "description": (
            "Replace one exact occurrence of old_text with new_text in an existing file. "
            "old_text must match the file's current contents exactly (including whitespace) "
            "and must occur exactly once -- this fails loudly rather than guessing if the "
            "match isn't unique, so include enough surrounding context in old_text to pin down "
            "the right location. Only edits the text you specify; nothing else in the file changes. "
            "Provide old_text as raw file content -- do NOT backslash-escape quote characters, "
            "the file's actual text has plain \" characters, not \\\". Prefer the smallest old_text "
            "that uniquely identifies the change (often a single line), not a whole surrounding block."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to repo root"},
                "old_text": {"type": "string", "description": "Exact existing text to replace, with enough context to be unique"},
                "new_text": {"type": "string", "description": "Text to replace it with"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
]


class DailyQuotaExhausted(Exception):
    pass


def _resolve_in_worktree(worktree: Path, rel_path: str) -> Path:
    """Resolves a model-supplied path and enforces it stays inside the worktree.

    The Coder's tools are its only way to touch the filesystem, and SCOPE.md's
    role-restriction guardrail says that has to be enforced in code, not just
    asked nicely of the model in the system prompt.
    """
    resolved = (worktree / rel_path).resolve()
    if worktree.resolve() not in resolved.parents and resolved != worktree.resolve():
        raise ValueError(f"path '{rel_path}' escapes the worktree")
    return resolved


def execute_tool(name: str, args: dict, worktree: Path) -> dict:
    try:
        if name == "read_file":
            path = _resolve_in_worktree(worktree, args["path"])
            if not path.is_file():
                return {"error": f"no such file: {args['path']}"}
            return {"content": path.read_text(encoding="utf-8", errors="replace")}

        if name == "list_directory":
            path = _resolve_in_worktree(worktree, args.get("path", "."))
            if not path.is_dir():
                return {"error": f"no such directory: {args.get('path', '.')}"}
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
            return {"entries": entries}

        if name == "search_files":
            import re

            base = _resolve_in_worktree(worktree, args.get("path", "."))
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

        if name == "edit_file":
            if not args["path"].startswith("src/marshmallow/"):
                return {"error": "edit_file is only permitted under src/marshmallow/"}
            path = _resolve_in_worktree(worktree, args["path"])
            if not path.is_file():
                return {"error": f"no such file: {args['path']}"}

            # The model reliably hand-escapes quote characters as if writing a Python
            # string literal (producing \" where the file just has "), which breaks exact
            # matching even when it has correctly located the right text. This codebase
            # has zero legitimate backslash-quote sequences in its source, so unescaping
            # is safe here and works with the model's quirk instead of fighting it.
            old_text = args["old_text"].replace('\\"', '"')
            new_text = args["new_text"].replace('\\"', '"')

            if old_text == new_text:
                return {"error": "old_text and new_text are identical -- this edit would be a no-op, nothing to apply"}

            content = path.read_text(encoding="utf-8", errors="replace")
            count = content.count(old_text)
            if count == 0:
                return {"error": "old_text not found in file -- it must match the file's current contents exactly"}
            if count > 1:
                return {"error": f"old_text matches {count} locations -- include more surrounding context to make it unique"}
            path.write_text(content.replace(old_text, new_text, 1), encoding="utf-8", newline="\n")
            return {"ok": True}

        return {"error": f"unknown tool: {name}"}
    except Exception as e:
        # Any tool-execution failure -- including ones we didn't anticipate (e.g. an
        # invalid regex from the model) -- must become feedback to the model, not a
        # crash of the whole batch run. This boundary exists specifically so an unusual
        # model output costs one tool call, not the rest of the day's quota.
        return {"error": f"{type(e).__name__}: {e}"}


def _status_code(e: Exception) -> int | None:
    if isinstance(e, errors.ClientError):
        return e.code
    response = getattr(e, "response", None)
    return getattr(response, "status_code", None)


def _is_per_day_quota_error(e: Exception) -> bool:
    text = str(getattr(e, "body", None) or getattr(e, "details", None) or str(e)).lower()
    return "perday" in text.replace(" ", "") or "generate_requests_per_day" in text.replace(" ", "_")


RATE_LIMIT_EXCEPTIONS = (errors.ClientError, compat_errors.RateLimitError, compat_errors.APIStatusError)


class NetworkError(Exception):
    pass


def _create_with_retry(client, **kwargs):
    for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
            return client.interactions.create(**kwargs)
        except compat_errors.APIConnectionError as e:
            # A DNS/network blip is usually transient -- worth a few retries before
            # giving up, unlike a real bug in the model's tool call. If it's still
            # failing after retries, this propagates as NetworkError so the caller can
            # stop the whole batch run rather than recording every remaining ticket as
            # a bogus "failure" (see the run that produced 21 spurious results this way).
            if attempt >= MAX_RETRY_ATTEMPTS - 1:
                raise NetworkError(str(e)) from e
            time.sleep(min(30, 5 * (2**attempt)))
        except RATE_LIMIT_EXCEPTIONS as e:
            if _status_code(e) != 429:
                raise
            if _is_per_day_quota_error(e):
                raise DailyQuotaExhausted(str(e)) from e
            time.sleep(min(60, 5 * (2**attempt)))
    raise RuntimeError("exceeded retry attempts on transient rate limiting")


DUPLICATE_CALL_MESSAGE = (
    "You already called this exact tool with these exact arguments earlier in this "
    "session and got the same result -- repeating it wastes your limited tool-call "
    "budget. Use what you already learned from that earlier call, or call edit_file "
    "now if you understand the fix."
)


def run_coder(issue_text: str, worktree: Path) -> dict:
    """Runs the Coder agent loop. Returns a transcript dict for logging/debugging."""
    client = genai.Client()
    transcript = {"steps": [], "tool_call_count": 0, "hit_cap": False, "total_tokens": 0}
    seen_calls = set()

    interaction = _create_with_retry(
        client,
        model=MODEL,
        input=f"GitHub issue:\n\n{issue_text}",
        tools=TOOL_DEFS,
        system_instruction=SYSTEM_INSTRUCTION,
    )
    transcript["total_tokens"] += interaction.usage.total_tokens or 0

    while interaction.status == "requires_action":
        fc_steps = [s for s in interaction.steps if s.type == "function_call"]
        if not fc_steps:
            break

        results = []
        for fc in fc_steps:
            if transcript["tool_call_count"] >= MAX_TOOL_CALLS:
                transcript["hit_cap"] = True
                break
            transcript["tool_call_count"] += 1

            call_signature = (fc.name, json.dumps(fc.arguments, sort_keys=True))
            is_duplicate = call_signature in seen_calls
            if is_duplicate:
                result = {"error": DUPLICATE_CALL_MESSAGE}
            else:
                seen_calls.add(call_signature)
                result = execute_tool(fc.name, fc.arguments, worktree)

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

        interaction = _create_with_retry(
            client,
            model=MODEL,
            input=results,
            tools=TOOL_DEFS,
            previous_interaction_id=interaction.id,
        )
        transcript["total_tokens"] += interaction.usage.total_tokens or 0

    transcript["final_message"] = interaction.output_text or ""
    return transcript
