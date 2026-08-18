"""The Coder agent: explores a repo with read-only tools and edits files with a
surgical exact-match-and-replace tool. It never sees gold_patch.diff or test_patch.diff.

Phase 2 usage (run_coder): a single blind, one-shot attempt -- no test feedback, no
planner. This is deliberately the weak baseline that later phases have to beat.

Phase 3 usage (run_coder_round): the same agent, but callable multiple times as a
continued conversation (via previous_interaction_id) so it can react to a Tester's
failure feedback across retry attempts.
"""

from pathlib import Path

from agent_runtime import (  # noqa: F401  (exceptions re-exported for callers)
    READ_ONLY_TOOL_DEFS,
    DailyQuotaExhausted,
    NetworkError,
    RateLimitExhausted,
    execute_read_only_tool,
    run_agent_loop,
)

MODEL = "gemini-3.5-flash-lite"
MAX_TOOL_CALLS = 20

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

TOOL_DEFS = READ_ONLY_TOOL_DEFS + [
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


def execute_tool(name: str, args: dict, worktree: Path) -> dict:
    try:
        shared_result = execute_read_only_tool(name, args, worktree)
        if shared_result is not None:
            return shared_result

        if name == "edit_file":
            if not args["path"].startswith("src/marshmallow/"):
                return {"error": "edit_file is only permitted under src/marshmallow/"}
            path = (worktree / args["path"]).resolve()
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
        # Any tool-execution failure -- including ones we didn't anticipate -- must
        # become feedback to the model, not a crash of the whole batch run.
        return {"error": f"{type(e).__name__}: {e}"}


def run_coder_round(prompt_text: str, worktree: Path, previous_interaction_id: str | None = None) -> dict:
    """Runs one bounded Coder attempt (fresh MAX_TOOL_CALLS budget), optionally
    continuing a prior conversation so the Coder remembers earlier attempts and
    reasoning. Used by the Phase 3 orchestrator for multi-attempt retries."""
    return run_agent_loop(
        model=MODEL,
        initial_input=prompt_text,
        tool_defs=TOOL_DEFS,
        execute_tool_fn=execute_tool,
        worktree=worktree,
        max_tool_calls=MAX_TOOL_CALLS,
        system_instruction=SYSTEM_INSTRUCTION,
        previous_interaction_id=previous_interaction_id,
    )


def run_coder(issue_text: str, worktree: Path) -> dict:
    """Phase 2: a single blind attempt, no test feedback. Returns a transcript dict."""
    return run_coder_round(f"GitHub issue:\n\n{issue_text}", worktree)
