"""The Judge agent (Phase 5): reviews a ticket after the Coder's normal retry budget
(MAX_CODER_ATTEMPTS) is exhausted without a clean pass. Per SCOPE.md's own pre-existing
definition, the Judge's job is an escalation-precision gate, not a resolution booster --
"max 3 Coder attempts per ticket before mandatory escalation to a human review queue."

Runs once per ticket, only for tickets that reach the Coder (no_reproducing_test tickets
never reach it -- there's no code to review). Sees the full round-by-round history (which
tool calls happened each round, especially edit_file), not just the final state, since the
same "still failing" outcome can hide qualitatively different failure shapes (diagnosed but
never executed; corrupted the file and never re-verified; genuinely converging but short on
rounds) that were only distinguishable in RESULTS.md's Phase 5 pre-registration by reading
full transcripts.

Decides one of two outcomes, expressed via a strict RETRY/ESCALATE first line so the
caller can parse it reliably without touching the shared run_agent_loop:
- RETRY: one more guided Coder round, with a specific instruction tailored to the actual
  failure shape it observed (not a generic "try harder").
- ESCALATE: hand off to a human review queue with a written reason.
"""

from pathlib import Path

from agent_runtime import READ_ONLY_TOOL_DEFS, execute_read_only_tool, run_agent_loop

MODEL = "gemini-3.5-flash-lite"
MAX_TOOL_CALLS = 10

SYSTEM_INSTRUCTION = """\
You are the Judge agent in a multi-agent software engineering system. A Coder agent has \
just exhausted its retry budget on a bug-fix ticket for the `marshmallow` Python library \
without producing a clean pass. Your job is to decide whether ONE more guided attempt is \
worth spending, or whether this ticket should be escalated to a human reviewer instead.

You are given the issue, the Tester's reproducing test, and a full round-by-round summary \
of what the Coder tried each attempt -- including whether it actually called edit_file, \
and its own final reasoning each round. You have the same read-only tools the Coder had \
(read_file, search_files, list_directory) so you can inspect the worktree's CURRENT actual \
state yourself rather than trusting the Coder's own account of what it did -- use them if \
the round history looks suspicious (e.g. the Coder claims success but the suite is still \
broken, or you want to confirm whether a file is actually corrupted before telling the \
Coder to re-read it).

Common failure shapes worth distinguishing (from real prior cases -- not an exhaustive list):
- The Coder's own final message correctly describes the fix, but it never called edit_file \
at all (or made one failed attempt and gave up without retrying) -- this is a pure \
execution gap, not a reasoning gap. RETRY with an instruction to actually apply the fix it \
already described.
- The Coder made real, converging progress (each round fixing one more piece of the same \
underlying issue) but ran out of rounds before covering everything -- RETRY with a summary \
of what's still missing.
- The Coder's edit corrupted something (broke the whole suite's ability to even collect), \
and then spent further rounds blindly re-editing without ever re-reading the actual current \
file state -- RETRY with an instruction to read the file first before editing again, and \
name what needs fixing based on what you can see.
- The code already looks correct by every signal available (clean internal test pass, no \
regressions, sound reasoning) and the failure is that the Coder's own test doesn't fully \
capture the issue's real scope -- there is nothing for a guided Coder retry to fix here, \
since the code isn't the problem. ESCALATE.
- The round history shows genuine confusion, contradictory diagnoses across rounds, or no \
sign that another attempt would do anything different -- ESCALATE.

Your final message MUST begin with a single line containing exactly the word RETRY or the \
word ESCALATE, nothing else on that line. On the following line(s):
- If RETRY: write the exact instruction to hand to the Coder for its next attempt. Be \
specific to what you actually observed -- name the file/mechanism, say what to do \
differently, don't just say "try again" or "be more careful."
- If ESCALATE: write a short reason a human reviewer would find useful (what was tried, \
why you don't expect another automated attempt to help).
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


def summarize_coder_rounds(coder_rounds: list[dict]) -> str:
    """Concise per-round summary for the Judge's prompt: tool-call counts, whether
    edit_file was ever called and whether those calls succeeded, and each round's full
    final message. Deliberately omits full tool-call args (old_text/new_text can be
    large) -- the Judge can read_file itself if it needs to see actual current content."""
    parts = []
    for i, round_ in enumerate(coder_rounds, 1):
        edit_calls = [s for s in round_["steps"] if s["tool"] == "edit_file"]
        edit_summary = (
            f"{len(edit_calls)} edit_file call(s): "
            + ", ".join("ok" if e["result"].get("ok") else f"failed ({e['result'].get('error', '?')[:80]})" for e in edit_calls)
            if edit_calls
            else "0 edit_file calls -- never attempted an edit this round"
        )
        parts.append(
            f"--- Round {i} ({round_['tool_call_count']} total tool calls) ---\n"
            f"{edit_summary}\n"
            f"Round's final message: {round_.get('final_message', '')[:800]}"
        )
    return "\n\n".join(parts)


def build_judge_prompt(
    issue_text: str,
    test_source: str,
    coder_rounds: list[dict],
    internal_verdict: str,
    regressed_mid_loop: list,
    suite_broken_reason: str | None,
) -> str:
    verdict_detail = f"Final internal verdict after all Coder attempts: {internal_verdict}"
    if suite_broken_reason:
        verdict_detail += f"\nThe suite failed to even collect: {suite_broken_reason}"
    elif regressed_mid_loop:
        verdict_detail += f"\n{len(regressed_mid_loop)} previously-passing test(s) now broken: {regressed_mid_loop[:5]}"

    return (
        f"GitHub issue:\n\n{issue_text}\n\n"
        f"The Tester's reproducing test:\n\n```python\n{test_source}\n```\n\n"
        f"The Coder used its full retry budget without a clean pass. Round-by-round history:\n\n"
        f"{summarize_coder_rounds(coder_rounds)}\n\n"
        f"{verdict_detail}\n\n"
        "Decide RETRY (with a specific instruction) or ESCALATE (with a reason), per your instructions."
    )


def parse_judge_verdict(final_message: str) -> tuple[str, str]:
    """Returns (action, message). Defaults to 'escalate' if the output doesn't clearly
    start with RETRY or ESCALATE -- the safe choice per SCOPE.md's "no silent infinite
    loops" principle, and the mismatch is recorded in the message so it's visible in
    results rather than silently misclassified."""
    text = final_message.strip()
    first_line, _, rest = text.partition("\n")
    first_line_upper = first_line.strip().upper()
    if first_line_upper.startswith("RETRY"):
        return "retry", rest.strip()
    if first_line_upper.startswith("ESCALATE"):
        return "escalate", rest.strip()
    return "escalate", f"[judge output did not start with RETRY/ESCALATE -- defaulting to escalate] {text}"


def run_judge(
    issue_text: str,
    test_source: str,
    coder_rounds: list[dict],
    internal_verdict: str,
    regressed_mid_loop: list,
    suite_broken_reason: str | None,
    worktree: Path,
) -> dict:
    """Runs the Judge agent loop. Returns a transcript dict (matching the other agents'
    shape) plus 'action' ('retry'|'escalate') and 'message' (instruction or reason),
    parsed from final_message."""
    prompt = build_judge_prompt(
        issue_text, test_source, coder_rounds, internal_verdict, regressed_mid_loop, suite_broken_reason
    )
    transcript = run_agent_loop(
        model=MODEL,
        initial_input=prompt,
        tool_defs=TOOL_DEFS,
        execute_tool_fn=execute_tool,
        worktree=worktree,
        max_tool_calls=MAX_TOOL_CALLS,
        system_instruction=SYSTEM_INSTRUCTION,
    )
    action, message = parse_judge_verdict(transcript["final_message"])
    transcript["action"] = action
    transcript["message"] = message
    return transcript
