"""The Planner agent (Phase 4): explores the repo read-only and produces a structured
plan -- which files/methods are likely involved and why -- handed to the Coder as
additional context alongside the issue text and the Tester's failing test. The Planner
never edits code itself; only the Coder does, per SCOPE.md's role-restricted-tools
guardrail.

Runs once per ticket, before the Tester/Coder loop starts -- not part of the retry loop,
per the pre-registered design in RESULTS.md (a wrong plan silently reused across 3 Coder
attempts would just compound the error rather than get corrected).
"""

from pathlib import Path

from agent_runtime import READ_ONLY_TOOL_DEFS, execute_read_only_tool, run_agent_loop

MODEL = "gemini-3.5-flash-lite"
MAX_TOOL_CALLS = 15

SYSTEM_INSTRUCTION = """\
You are the Planner agent in a multi-agent software engineering system. You are given \
a real GitHub issue describing a bug in the `marshmallow` Python library. Your job is \
NOT to fix the bug and NOT to write any code -- it is to investigate the codebase and \
produce a plan that a separate Coder agent will use to make the actual fix.

Rules:
- Explore the repository using the read-only tools to understand where the behavior \
described in the issue actually lives -- which file(s), which class(es), which \
method(s) are involved, and how they interact.
- You have no tool for editing files. Do not attempt to describe a diff or exact code \
change -- describe WHERE the fix likely needs to happen and WHY, in enough detail that \
someone who hasn't read the code yet can go straight to the right place.
- If the fix plausibly needs changes in more than one place (e.g. a behavior that's \
implemented in one method but also needs to propagate through related methods), say so \
explicitly and name each location.
- If this looks like it needs new code (e.g. a new field/validator/method) rather than \
fixing something existing, say that explicitly and describe what the new code's \
responsibility should be, not just where old code lives.
- When you're confident in your investigation, stop calling tools and give your plan as \
your final message: a short, concrete list of the specific files/classes/methods \
involved and what's wrong or missing at each one. This final message IS the plan -- \
write it as something directly useful to hand to another engineer, not a narration of \
what you did.
"""

TOOL_DEFS = READ_ONLY_TOOL_DEFS


def execute_tool(name: str, args: dict, worktree: Path) -> dict:
    try:
        result = execute_read_only_tool(name, args, worktree)
        if result is not None:
            return result
        return {"error": f"unknown tool: {name}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def run_planner(issue_text: str, worktree: Path) -> dict:
    """Runs the Planner agent loop. Returns a transcript dict; transcript["final_message"]
    is the plan text to hand to the Coder."""
    return run_agent_loop(
        model=MODEL,
        initial_input=f"GitHub issue:\n\n{issue_text}",
        tool_defs=TOOL_DEFS,
        execute_tool_fn=execute_tool,
        worktree=worktree,
        max_tool_calls=MAX_TOOL_CALLS,
        system_instruction=SYSTEM_INSTRUCTION,
    )
