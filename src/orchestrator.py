"""Phase 3 orchestrator: ties the Tester and Coder agents together into the loop
described in RESULTS.md's pre-registered design.

1. Tester writes a test from the issue text alone and confirms it fails against the
   unmodified code (the TDD "prove the bug is reproducible" gate). If it can't produce
   a genuinely failing test, the ticket is logged as "no_reproducing_test" and the Coder
   loop is skipped entirely -- a real, distinct outcome, not a crash.
2. Coder gets the issue + the Tester's test + its failure output, attempts a fix.
3. After each attempt, two things are re-checked mechanically (no further LLM call --
   nothing needs to be rewritten between attempts): the Tester's own test, AND the full
   existing suite compared against a baseline snapshot taken before the Coder touched
   anything. A "pass" only counts if the target test passes AND nothing that was
   previously passing broke -- this second check exists because validating against only
   the Tester's own test proved blind to regressions elsewhere (see RESULTS.md, v1
   validation run: marshmallow-2900's fix satisfied the Tester's test but broke
   test_constant_none_allows_none_value, and nothing in the loop caught it until final
   grading, too late to retry). The full-suite check is leak-free by construction: this
   worktree never has test_patch.diff applied to it -- that only happens in
   eval/run_eval.py's own separate, isolated worktree during final grading -- so it
   structurally cannot include the hidden verifying test.
4. Capped at 3 total Coder attempts. The final diff has the Tester's own test file
   stripped out (reusing the exact src/test diff splitter already validated in
   scripts/curate_tickets.py) and is graded by the real, untouched eval/run_eval.py
   against the hidden test_patch.diff -- completely independent of whatever the Tester
   or the mid-loop regression check believed.
5. Both verdicts are recorded so calibration can be measured: did the loop's own
   judgment (target test passing AND no regressions) agree with the real grader?
"""

import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "agents"))
sys.path.insert(0, str(ROOT / "scripts"))

from curate_tickets import split_diff  # noqa: E402
from eval.run_eval import (  # noqa: E402
    TICKETS_DIR,
    EvalError,
    create_worktree,
    evaluate,
    install_editable,
    remove_worktree,
)
from coder import run_coder_round  # noqa: E402
from planner import run_planner  # noqa: E402
from tester import (  # noqa: E402
    TEST_FILE_PATH,
    run_full_suite_with_details,
    run_tester,
    run_written_test_impl,
)

MAX_CODER_ATTEMPTS = 3
RESULTS_DIR = ROOT / "results"


def get_source_only_diff(worktree: Path) -> str:
    """git diff of the whole worktree, with the Tester's own test file (and any other
    tests/ changes) stripped out -- only src/marshmallow/ changes count as the Coder's
    candidate patch. Reuses Phase 1's diff splitter rather than reinventing it."""
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    src_diff, _test_diff = split_diff(result.stdout)
    return src_diff


def compute_regressions(baseline_pass_set: set, after_results: dict) -> list:
    """Tests that were passing at baseline and no longer pass, tolerating the same
    timestamp-parametrized flakiness eval/run_eval.py already accounts for (a nodeid
    can differ by its bracketed parametrize suffix between two time-separated runs even
    though it's the same logical test)."""
    after_pass_set = {n for n, r in after_results.items() if r["outcome"] == "passed"}
    after_base_names_passing = {n.split("[")[0] for n in after_pass_set}
    return sorted(
        n for n in (baseline_pass_set - after_pass_set)
        if n.split("[")[0] not in after_base_names_passing
    )


def build_initial_coder_prompt(
    issue_text: str, test_source: str, failure_message: str, plan_text: str | None = None
) -> str:
    plan_section = (
        f"\n\nA Planner agent investigated this issue first and produced the following "
        f"plan for where the fix likely needs to happen:\n\n{plan_text}\n\n"
        "Use this as a starting point, but verify it against the actual code yourself -- "
        "the plan may be incomplete or wrong.\n"
        if plan_text
        else ""
    )
    return (
        f"GitHub issue:\n\n{issue_text}\n\n"
        "A Tester agent has written the following test, which reproduces this bug "
        f"(it currently fails against the unfixed code):\n\n```python\n{test_source}\n```\n\n"
        f"The test currently fails with:\n{failure_message}\n"
        f"{plan_section}\n"
        "Fix the underlying bug so this test passes. You cannot see or run this test "
        "yourself -- it will be re-run after your edit and you'll be told the result, "
        "along with whether your fix broke any other previously-passing test."
    )


def build_retry_coder_prompt(check_result: dict, regressions: list, after_results: dict) -> str:
    parts = []
    if check_result["outcome"] == "error":
        parts.append(
            "Your previous edit did not fix the bug -- re-running the test errored, "
            "which means your edit broke something else (e.g. a syntax error or import "
            f"failure), not that the original bug is fixed: {check_result['message']}"
        )
    elif check_result["outcome"] != "passed":
        parts.append(
            "Your previous edit did not fix the bug -- the test still fails:\n"
            f"{check_result.get('message', check_result)}"
        )
    else:
        parts.append("Your previous edit made the target test pass.")

    if regressions:
        broken_detail = "\n".join(
            f"- {nodeid}: {after_results.get(nodeid, {}).get('message', '(no detail)')[:400]}"
            for nodeid in regressions[:5]
        )
        parts.append(
            f"However, it broke {len(regressions)} previously-passing test(s) that were "
            f"fine before your change:\n{broken_detail}\n\n"
            "A fix that breaks other tests is not acceptable -- revise your fix so the "
            "target test passes without breaking these."
        )

    parts.append("Try again.")
    return "\n\n".join(parts)


def run_planner_for_ticket(ticket_id: str) -> dict:
    """Runs the Planner in its own dedicated worktree. The Planner only ever calls
    read_file/list_directory/search_files -- pure filesystem reads, it never imports or
    executes marshmallow -- so it deliberately does NOT call install_editable here.
    Doing so originally (copied out of habit from the Tester/Coder setup) caused a real
    bug: pip sees the same marshmallow version already installed from a different
    worktree path and skips reinstalling rather than repointing the editable link, so by
    the time the Tester/Coder's *separate* worktree ran its suite check, the editable
    install still pointed at the Planner's already-deleted worktree
    (ModuleNotFoundError: No module named 'marshmallow'). The real fix is just not doing
    an install this function never needed in the first place."""
    ticket_dir = TICKETS_DIR / ticket_id
    base_commit = (ticket_dir / "base_commit.txt").read_text().strip()
    issue_text = (ticket_dir / "issue.md").read_text(encoding="utf-8")

    worktree = create_worktree(base_commit)
    try:
        return run_planner(issue_text, worktree)
    finally:
        remove_worktree(worktree)


def _run_ticket(ticket_id: str, plan_text: str | None = None, planner_stats: dict | None = None) -> dict:
    """Runs the Tester-gated Coder retry loop for one ticket, optionally seeded with a
    Planner's plan (Phase 4) -- with plan_text=None this is exactly Phase 3's behavior,
    unchanged. Returns a dict with: result -- the summary to log, tester_transcript --
    for the transcripts dir, coder_rounds -- list of per-attempt Coder transcripts."""
    planner_stats = planner_stats or {"tool_calls": 0, "tokens": 0}
    ticket_dir = TICKETS_DIR / ticket_id
    base_commit = (ticket_dir / "base_commit.txt").read_text().strip()
    issue_text = (ticket_dir / "issue.md").read_text(encoding="utf-8")
    gold_patch_text = (ticket_dir / "gold_patch.diff").read_text(encoding="utf-8")

    worktree = create_worktree(base_commit)
    coder_rounds = []
    try:
        install_editable(worktree)

        tester_transcript = run_tester(issue_text, worktree, base_commit, gold_patch_text)
        if not tester_transcript["valid_reproduction"]:
            return {
                "result": {
                    "ticket_id": ticket_id,
                    "status": "no_reproducing_test",
                    "resolved": False,
                    "coder_attempts": 0,
                    "coder_tool_calls": 0,
                    "coder_tokens": 0,
                    "tester_tool_calls": tester_transcript["tool_call_count"],
                    "tester_tokens": tester_transcript["total_tokens"],
                    "tester_reproduce_attempts": tester_transcript["reproduce_attempts"],
                    "planner_tool_calls": planner_stats["tool_calls"],
                    "planner_tokens": planner_stats["tokens"],
                    "internal_verdict": None,
                    "regressed_mid_loop": [],
                    "suite_broken_reason": None,
                    "calibration": None,
                },
                "tester_transcript": tester_transcript,
                "coder_rounds": [],
            }

        run_results = [
            s["result"] for s in tester_transcript["steps"]
            if s["tool"] == "run_written_test" and not s.get("blocked_duplicate")
        ]
        gate_result = next(r for r in reversed(run_results) if r["outcome"] == "failed")
        test_source = (worktree / TEST_FILE_PATH).read_text(encoding="utf-8")

        # Baseline snapshot taken right after the reproduce gate passes -- before the
        # Coder has touched anything -- so later rounds can be compared against "what
        # was passing before this ticket's fix started." baseline_suite_ok should always
        # be True here (unmodified source + the Tester's own test, which already passed
        # its own isolated collection check) -- if it's somehow not, baseline_pass_set is
        # just empty and regression comparisons degrade to finding nothing to compare
        # against, rather than crashing.
        baseline_suite, _baseline_suite_ok, baseline_suite_reason = run_full_suite_with_details(worktree)
        baseline_pass_set = {n for n, r in baseline_suite.items() if r["outcome"] == "passed"}
        if not _baseline_suite_ok:
            print(f"WARNING: baseline suite check itself failed ({baseline_suite_reason}) -- unexpected, since only the unmodified source + Tester's own already-validated test are present at this point")

        prompt = build_initial_coder_prompt(issue_text, test_source, gate_result["message"], plan_text)
        previous_interaction_id = None
        internal_verdict = None
        regressed_mid_loop = []
        suite_broken_reason = None
        check_result = {}

        for _attempt in range(1, MAX_CODER_ATTEMPTS + 1):
            round_transcript = run_coder_round(prompt, worktree, previous_interaction_id)
            coder_rounds.append(round_transcript)
            previous_interaction_id = round_transcript["last_interaction_id"]

            check_result = run_written_test_impl(worktree)
            after_suite, suite_ok, suite_reason = run_full_suite_with_details(worktree)

            if not suite_ok:
                # Worse than any individual regression: the edit broke the suite's
                # ability to even collect (e.g. an exception at import/class-definition
                # time elsewhere). Never treat this as passing, regardless of what the
                # Tester's own isolated test says. suite_reason is logged verbatim (not
                # just the generic "suite_broken" label) so a repeat occurrence is
                # diagnosable from the log alone, not another live investigation.
                internal_verdict = "suite_broken"
                suite_broken_reason = suite_reason
                regressed_mid_loop = ["<entire test suite failed to collect>"]
                prompt = (
                    "Your previous edit broke something so badly that the ENTIRE test "
                    "suite can no longer even be collected -- not just the target test. "
                    "This usually means an exception or error at import/class-definition "
                    f"time somewhere in the codebase, triggered by your change: {suite_reason}\n\n"
                    "Find and fix that, or reconsider your approach.\n\nTry again."
                )
                continue

            regressed_mid_loop = compute_regressions(baseline_pass_set, after_suite)

            if check_result["outcome"] == "passed" and not regressed_mid_loop:
                internal_verdict = "passed_clean"
                break
            if check_result["outcome"] == "passed":
                internal_verdict = "passed_with_regressions"
            else:
                internal_verdict = check_result["outcome"]

            prompt = build_retry_coder_prompt(check_result, regressed_mid_loop, after_suite)

        candidate_src_diff = get_source_only_diff(worktree)
    finally:
        remove_worktree(worktree)

    coder_tool_calls = sum(r["tool_call_count"] for r in coder_rounds)
    coder_tokens = sum(r["total_tokens"] for r in coder_rounds)

    if candidate_src_diff.strip():
        patch_file = RESULTS_DIR / f".candidate-{uuid.uuid4().hex[:8]}.diff"
        patch_file.write_text(candidate_src_diff, encoding="utf-8", newline="\n")
        try:
            eval_result = evaluate(ticket_dir, patch_file, use_gold=False)
        except EvalError as e:
            eval_result = {"resolved": False, "error": str(e)}
        finally:
            patch_file.unlink(missing_ok=True)
    else:
        eval_result = {"resolved": False, "error": "Coder produced no source changes"}

    real_resolved = eval_result.get("resolved", False)
    loop_said_pass = internal_verdict == "passed_clean"

    calibration = None
    if loop_said_pass and not real_resolved:
        calibration = "tester_fooled"  # loop said clean pass, hidden test disagrees
    elif not loop_said_pass and real_resolved:
        calibration = "tester_too_strict"  # loop never passed clean, but hidden test would've

    return {
        "result": {
            "ticket_id": ticket_id,
            "status": "attempted",
            "resolved": real_resolved,
            "regressions": eval_result.get("regressions", []),
            "fail_to_pass": eval_result.get("fail_to_pass", []),
            "error": eval_result.get("error"),
            "coder_attempts": len(coder_rounds),
            "coder_tool_calls": coder_tool_calls,
            "coder_tokens": coder_tokens,
            "tester_tool_calls": tester_transcript["tool_call_count"],
            "tester_tokens": tester_transcript["total_tokens"],
            "tester_reproduce_attempts": tester_transcript["reproduce_attempts"],
            "planner_tool_calls": planner_stats["tool_calls"],
            "planner_tokens": planner_stats["tokens"],
            "internal_verdict": internal_verdict,
            "regressed_mid_loop": regressed_mid_loop,
            "suite_broken_reason": suite_broken_reason,
            "calibration": calibration,
        },
        "tester_transcript": tester_transcript,
        "coder_rounds": coder_rounds,
    }


def run_phase3_ticket(ticket_id: str) -> dict:
    """Phase 3: no Planner. Identical behavior to before this function was refactored
    to share logic with Phase 4 -- plan_text=None means build_initial_coder_prompt
    produces exactly the same prompt it always did."""
    return _run_ticket(ticket_id)


def run_phase4_ticket(ticket_id: str) -> dict:
    """Phase 4: runs the Planner first, then the same Tester-gated Coder retry loop
    seeded with its plan. Returns the same shape as run_phase3_ticket, plus a top-level
    'planner_transcript' key."""
    planner_transcript = run_planner_for_ticket(ticket_id)
    plan_text = planner_transcript["final_message"]
    outcome = _run_ticket(
        ticket_id,
        plan_text=plan_text,
        planner_stats={
            "tool_calls": planner_transcript["tool_call_count"],
            "tokens": planner_transcript["total_tokens"],
        },
    )
    outcome["planner_transcript"] = planner_transcript
    return outcome
