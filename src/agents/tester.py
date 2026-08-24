"""The Tester agent: writes a single test reproducing a described bug, confirms it
fails against the unmodified code (the TDD "prove the bug is reproducible" step), then
is re-run mechanically (no further LLM calls) after each Coder attempt to check whether
the fix satisfies it.

Writes to a fixed filename (tests/test_agent_generated.py) rather than a model-chosen
name -- each ticket gets a fresh worktree, so there's no collision risk, and a fixed
tests/-prefixed path is automatically excluded from the Coder's final candidate patch by
the same source/test diff-splitting logic used in scripts/curate_tickets.py.

The Tester's own verdict on whether a fix works is NEVER the grader -- eval/run_eval.py's
untouched, hidden test_patch.diff is. This module exists to give the Coder actionable
feedback, and to measure how often the Tester's judgment agrees with ground truth (see
RESULTS.md's Phase 3 pre-registered design, the two calibration buckets).
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent_runtime import (
    READ_ONLY_TOOL_DEFS,
    execute_read_only_tool,
    run_agent_loop,
)
from eval.run_eval import VENV_PYTHON

MODEL = "gemini-3.5-flash-lite"
MAX_TOOL_CALLS = 10
TEST_TIMEOUT_SECONDS = 15
TEST_FILE_PATH = "tests/test_agent_generated.py"

SYSTEM_INSTRUCTION = f"""\
You are the Tester agent in a multi-agent software engineering system. You are given a \
real GitHub issue describing a bug in the `marshmallow` Python library. Your job is NOT \
to fix the bug -- it is to write ONE focused test that reproduces it, proving the bug is \
real and observable, before a separate Coder agent attempts a fix.

Rules:
- Explore the repository (especially existing tests under tests/) to learn the codebase's \
test conventions -- imports, fixtures, how Schema/fields are typically exercised.
- Write exactly one test function, via write_test, that exercises the specific behavior \
described in the issue. Follow the existing tests' style and imports.
- Then call run_written_test to run it against the current (unfixed) code.
- A valid test MUST fail against the unfixed code -- that's what proves it reproduces \
the bug. If run_written_test reports it passed, your test isn't exercising the bug; \
revise it. If it reports an error (the test itself is broken -- e.g. a bad import or \
syntax error), fix the test code, not the library.
- Once run_written_test reports your test failed (a genuine assertion/behavior mismatch, \
not a broken test), you are done -- stop calling tools and briefly summarize what the \
test checks and how it currently fails.
- You cannot see or use the hidden verifying test the grading harness uses. Write your \
own, from the issue description alone.

Your test always lives at exactly {TEST_FILE_PATH} -- write_test overwrites that file \
each time you call it, so include the complete file contents (imports and all) every call.
"""

TOOL_DEFS = READ_ONLY_TOOL_DEFS + [
    {
        "type": "function",
        "name": "write_test",
        "description": f"Writes the complete contents of your test file to {TEST_FILE_PATH}, overwriting any previous version.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Complete file contents, including imports"},
            },
            "required": ["content"],
        },
    },
    {
        "type": "function",
        "name": "run_written_test",
        "description": (
            f"Runs the test currently written to {TEST_FILE_PATH} against the current code. "
            "Returns outcome: 'failed' (a genuine assertion/behavior mismatch -- this is what "
            "you want, it proves the bug is reproduced), 'passed' (the test doesn't currently "
            "fail, so it isn't exercising the bug), 'error' (the test itself is broken, e.g. a "
            "bad import or syntax error -- fix the test), or 'timeout'."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
]


def run_written_test_impl(worktree: Path) -> dict:
    """Runs the fixed test file and classifies the outcome. Not just a tool wrapper --
    the Phase 3 orchestrator calls this directly (no LLM involved) to re-check the
    Tester's test after each Coder attempt, since the test itself doesn't need to be
    rewritten between attempts."""
    test_path = worktree / TEST_FILE_PATH
    if not test_path.is_file():
        return {"outcome": "error", "message": f"{TEST_FILE_PATH} does not exist -- call write_test first"}

    report_file = worktree / ".tester-report.json"
    try:
        subprocess.run(
            [str(VENV_PYTHON), "-m", "pytest", TEST_FILE_PATH,
             "--json-report", f"--json-report-file={report_file.name}",
             "-q", "--tb=short"],
            cwd=worktree,
            capture_output=True,
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"outcome": "timeout", "message": f"test run exceeded {TEST_TIMEOUT_SECONDS}s -- likely a hang or infinite loop"}

    if not report_file.exists():
        return {"outcome": "error", "message": "pytest produced no report -- the test run failed to start"}

    report = json.loads(report_file.read_text(encoding="utf-8"))
    report_file.unlink(missing_ok=True)

    tests = report.get("tests", [])
    if not tests:
        # A genuine collection-time failure (bad import, syntax error) produces an empty
        # tests list -- the failure only shows up in collectors, never as a per-test outcome.
        failed_collectors = [c for c in report.get("collectors", []) if c.get("outcome") == "failed"]
        message = failed_collectors[0].get("longrepr", "unknown collection error") if failed_collectors else "no tests were collected"
        return {"outcome": "error", "message": str(message)[:2000]}

    if len(tests) > 1:
        # System prompt asks for exactly one test function; handle multiple defensively.
        failing = [t for t in tests if t["outcome"] == "failed"]
        if failing:
            t = failing[0]
            longrepr = t.get("call", {}).get("longrepr", "")
            return {"outcome": "failed", "message": str(longrepr)[:2000], "note": f"{len(tests)} test functions found, reporting the first failing one"}
        return {"outcome": "passed" if all(t["outcome"] == "passed" for t in tests) else "error",
                "note": f"{len(tests)} test functions found, none failed"}

    t = tests[0]
    if t["outcome"] == "failed":
        longrepr = t.get("call", {}).get("longrepr", "")
        return {"outcome": "failed", "message": str(longrepr)[:2000]}
    if t["outcome"] == "passed":
        return {"outcome": "passed", "message": "test passed against the current code -- it does not reproduce the bug"}
    return {"outcome": "error", "message": f"unexpected pytest outcome: {t['outcome']}"}


SUITE_TIMEOUT_SECONDS = 60


def run_full_suite_with_details(worktree: Path) -> tuple[dict[str, dict], bool, str | None]:
    """Runs every test under tests/ (including the Tester's own generated test) and
    returns (results, suite_ok, reason). results is {nodeid: {"outcome": ..., "message":
    ...}} with actual failure text, not just pass/fail. Separate from
    eval.run_eval.run_full_suite, which only needs outcome for scoring and is part of the
    already-validated, frozen grading harness -- this exists specifically for the
    orchestrator's mid-loop regression check, which needs actionable detail to feed back
    to the Coder, not just a verdict.

    suite_ok is False if the whole suite failed to even collect (e.g. an edit broke
    tests/conftest.py at import time) or the run timed out. Callers MUST treat that as
    the worst possible signal, not as "no regressions found" -- an empty results dict
    from a genuinely clean run is a completely different situation from one caused by a
    total collection wipeout, and conflating them (as an earlier version of this
    function did) let a Coder round that broke the entire suite pass through undetected.

    reason is None when suite_ok is True, otherwise a short string identifying *why*
    ("timeout", "no_report", or the actual collector error) -- added after a validation
    run showed suite_ok=False on 8/8 attempted tickets (100%, not intermittent) and a
    clean isolated replay couldn't reproduce it, meaning the two possible causes
    (subprocess timeout vs. a genuine collection failure) had been indistinguishable
    from the logged data alone. Needed to catch the next occurrence with real evidence
    instead of guessing again.

    The worktree this runs in never has test_patch.diff applied (that only happens in
    eval/run_eval.py's own separate, isolated worktree during final grading), so this is
    leak-free by construction -- it structurally cannot include the hidden verifying test.
    """
    report_file = worktree / ".suite-report.json"
    # A missing report file (not a timeout, not a real collection failure -- those
    # both produce their own distinct signal) turned out to be environmental flakiness
    # in this session's testing (the exact same failure appeared on a completely clean
    # baseline check, before any Coder edit existed to blame) rather than a genuine
    # "the suite is broken" signal -- retried once before concluding suite_ok=False, so
    # a real collection failure (which always produces *some* report) is unaffected.
    proc = None
    for attempt in range(2):
        try:
            proc = subprocess.run(
                [str(VENV_PYTHON), "-m", "pytest", "tests/",
                 "--json-report", f"--json-report-file={report_file.name}",
                 "-q", "--tb=short"],
                cwd=worktree,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=SUITE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return {}, False, f"timeout after {SUITE_TIMEOUT_SECONDS}s"

        if report_file.exists():
            break
        if attempt == 0:
            time.sleep(2)
    else:
        # Surface the subprocess's actual output instead of just guessing "it crashed" --
        # a first retry that still fails deserves real evidence, not another assumption.
        detail = f"exit_code={proc.returncode}\nstdout:\n{proc.stdout[-1500:]}\nstderr:\n{proc.stderr[-1500:]}"
        return {}, False, f"pytest produced no json report after 2 attempts:\n{detail}"

    report = json.loads(report_file.read_text(encoding="utf-8"))
    report_file.unlink(missing_ok=True)

    tests = report.get("tests", [])
    if not tests:
        # Same distinction run_written_test_impl already makes: a total collection
        # failure produces an empty tests list with the real failure only visible in
        # collectors, never as a per-test outcome.
        failed_collectors = [c for c in report.get("collectors", []) if c.get("outcome") == "failed"]
        if failed_collectors:
            reason = str(failed_collectors[0].get("longrepr", "unknown collection error"))[:1000]
            return {}, False, reason
        return {}, True, None

    results = {}
    for t in tests:
        longrepr = t.get("call", {}).get("longrepr", "") if t["outcome"] != "passed" else ""
        results[t["nodeid"]] = {"outcome": t["outcome"], "message": str(longrepr)[:1000]}
    return results, True, None


def execute_tool(name: str, args: dict, worktree: Path) -> dict:
    try:
        shared_result = execute_read_only_tool(name, args, worktree)
        if shared_result is not None:
            return shared_result

        if name == "write_test":
            path = worktree / TEST_FILE_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"], encoding="utf-8", newline="\n")
            return {"ok": True}

        if name == "run_written_test":
            return run_written_test_impl(worktree)

        return {"error": f"unknown tool: {name}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def verify_against_gold(worktree: Path, base_commit: str, gold_patch_text: str, test_content: str) -> dict:
    """Harness-internal quality gate: applies the REAL gold source fix (never shown to
    any agent, never included in any prompt) plus the Tester's current test to a
    disposable scratch worktree, and checks whether the test passes there.

    This answers "does this test genuinely discriminate broken-vs-fixed code", using the
    gold patch purely as an internal pass/fail oracle -- the same way it's already used
    in scripts/curate_tickets.py's self-check (Phase 1: every gold patch is verified to
    resolve its own ticket before being trusted as ground truth). Nothing about the gold
    patch's *content* ever reaches a model; only a binary "did your test pass" signal
    does, via the retry prompt below.
    """
    from eval.run_eval import apply_patch, create_worktree, install_editable, remove_worktree

    gold_worktree = create_worktree(base_commit)
    try:
        apply_patch(gold_worktree, gold_patch_text)
        (gold_worktree / TEST_FILE_PATH).write_text(test_content, encoding="utf-8", newline="\n")
        install_editable(gold_worktree)
        return run_written_test_impl(gold_worktree)
    finally:
        remove_worktree(gold_worktree)
        # install_editable above repointed the venv's single shared editable install at
        # gold_worktree, which is now gone -- every check against the caller's own
        # worktree for the rest of this ticket (later Tester rounds, baseline suite
        # check, the whole Coder loop) would otherwise silently ModuleNotFoundError.
        install_editable(worktree)


TEMPERATURE = 0.0  # repeatability matters more than variety for "write a test, confirm it fails"


def run_tester_round(prompt_text: str, worktree: Path, previous_interaction_id: str | None = None) -> dict:
    return run_agent_loop(
        model=MODEL,
        initial_input=prompt_text,
        tool_defs=TOOL_DEFS,
        execute_tool_fn=execute_tool,
        worktree=worktree,
        max_tool_calls=MAX_TOOL_CALLS,
        system_instruction=SYSTEM_INSTRUCTION,
        previous_interaction_id=previous_interaction_id,
        temperature=TEMPERATURE,
        # run_written_test takes no arguments, so every call has an identical signature --
        # the default duplicate-blocker would (and did) silently reject every re-check
        # after the first, capping real verification to once per round no matter how many
        # times the model revised its test via write_test in between.
        no_duplicate_check_tools=frozenset({"run_written_test"}),
    )


MAX_REPRODUCE_ATTEMPTS = 2

NOT_YET_FAILING_MESSAGE = (
    "You haven't yet produced a test that fails against the current code. "
    "Try again."
)


def run_tester(issue_text: str, worktree: Path, base_commit: str, gold_patch_text: str) -> dict:
    """Runs the Tester agent loop, now gated by a real-fix quality check: a test that
    fails against the buggy code AND passes against the real fix is a genuine
    reproduction (valid_reproduction=True). One that fails for some other reason (a bug
    in the test itself, or it happens to exercise unrelated behavior) fails the real-fix
    check too, and the Tester gets a bounded number of further attempts, told only that
    its test didn't hold up -- never what the actual fix is.

    Returns a combined transcript dict across all attempts, plus 'valid_reproduction',
    'gold_check_result' (the last real-fix check outcome), and 'reproduce_attempts'.
    """
    combined_steps = []
    total_tool_calls = 0
    total_tokens = 0
    hit_cap = False
    prompt = f"GitHub issue:\n\n{issue_text}"
    previous_interaction_id = None
    valid_reproduction = False
    gold_check_result = None
    attempt = 0

    for attempt in range(1, MAX_REPRODUCE_ATTEMPTS + 1):
        round_transcript = run_tester_round(prompt, worktree, previous_interaction_id)
        combined_steps.extend(round_transcript["steps"])
        total_tool_calls += round_transcript["tool_call_count"]
        total_tokens += round_transcript["total_tokens"]
        hit_cap = hit_cap or round_transcript["hit_cap"]
        previous_interaction_id = round_transcript["last_interaction_id"]

        run_results = [
            s["result"] for s in round_transcript["steps"]
            if s["tool"] == "run_written_test" and not s.get("blocked_duplicate")
        ]
        last_result = run_results[-1] if run_results else None

        if last_result and last_result["outcome"] == "failed":
            test_content = (worktree / TEST_FILE_PATH).read_text(encoding="utf-8")
            gold_check_result = verify_against_gold(worktree, base_commit, gold_patch_text, test_content)
            if gold_check_result["outcome"] == "passed":
                valid_reproduction = True
                break
            prompt = (
                "Your test failed against the current (buggy) code, which is good -- "
                "but it does not pass once the underlying issue is genuinely fixed. That "
                "means it isn't actually testing the behavior described in the issue "
                "(it may be failing for an unrelated reason). Reconsider what specifically "
                "should change and write a different test that captures it precisely.\n\n"
                "Try again."
            )
        else:
            prompt = NOT_YET_FAILING_MESSAGE

    return {
        "steps": combined_steps,
        "tool_call_count": total_tool_calls,
        "total_tokens": total_tokens,
        "hit_cap": hit_cap,
        "valid_reproduction": valid_reproduction,
        "gold_check_result": gold_check_result,
        "reproduce_attempts": attempt,
    }
