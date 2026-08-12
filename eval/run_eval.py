"""Applies a candidate patch to a clean checkout of a ticket's base commit and scores it.

Methodology (matches SWE-bench): a ticket is "resolved" only if the fix makes its
FAIL_TO_PASS tests pass *and* every test that was already passing beforehand
(PASS_TO_PASS) still passes. Regression rate — not just "did the new test pass" —
is the number SCOPE.md says actually matters.

Usage:
  python eval/run_eval.py --ticket data/tickets/marshmallow-2900 --patch candidate.diff
  python eval/run_eval.py --ticket data/tickets/marshmallow-2900 --gold
  python eval/run_eval.py --all --gold          # harness self-check across every ticket
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_CLONE = ROOT / ".cache" / "marshmallow-repo"
WORKTREE_ROOT = ROOT / ".cache" / "worktrees"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TICKETS_DIR = ROOT / "data" / "tickets"

ADDED_TEST_DEF_RE = re.compile(r"^\+[ \t]*def (test_\w+)[ \t]*\(", re.MULTILINE)
REMOVED_TEST_DEF_RE = re.compile(r"^-[ \t]*def (test_\w+)[ \t]*\(", re.MULTILINE)


class EvalError(Exception):
    pass


def _run(cmd, cwd=None, check=True):
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if check and result.returncode != 0:
        raise EvalError(
            f"command failed: {' '.join(map(str, cmd))}\n--- stdout ---\n{result.stdout}"
            f"\n--- stderr ---\n{result.stderr}"
        )
    return result


def create_worktree(base_commit: str) -> Path:
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    path = WORKTREE_ROOT / uuid.uuid4().hex[:12]
    _run(["git", "worktree", "add", "--detach", str(path), base_commit], cwd=REPO_CLONE)
    return path


def remove_worktree(path: Path):
    _run(["git", "worktree", "remove", "--force", str(path)], cwd=REPO_CLONE, check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def apply_patch(worktree: Path, diff_text: str):
    patch_file = worktree / f".eval-patch-{uuid.uuid4().hex[:8]}.diff"
    patch_file.write_text(diff_text, encoding="utf-8", newline="\n")
    try:
        _run(["git", "apply", "--whitespace=nowarn", patch_file.name], cwd=worktree)
    finally:
        patch_file.unlink(missing_ok=True)


def install_editable(worktree: Path):
    _run([str(VENV_PYTHON), "-m", "pip", "install", "-e", str(worktree), "-q"])


def run_full_suite(worktree: Path) -> dict[str, str]:
    report_file = worktree / ".eval-report.json"
    _run(
        [
            str(VENV_PYTHON), "-m", "pytest", "tests/",
            "--json-report", f"--json-report-file={report_file.name}",
            "-q", "--tb=no",
        ],
        cwd=worktree,
        check=False,  # test failures are expected data, not a harness error
    )
    if not report_file.exists():
        raise EvalError("pytest did not produce a json report — the suite likely errored at collection")
    report = json.loads(report_file.read_text(encoding="utf-8"))
    report_file.unlink(missing_ok=True)
    return {t["nodeid"]: t["outcome"] for t in report["tests"]}


def extract_fail_to_pass_targets(test_patch_text: str) -> set[str]:
    """Returns function names that are genuinely new (added, not merely modified).

    A name that appears on both a '+def name(' and a '-def name(' line is a
    signature/body edit to an existing test, not a new one — it can't be used
    as a FAIL_TO_PASS target since we have no baseline for "does this test
    already exist in some form". We match on bare function name (not full
    nodeid with class path) since the diff doesn't tell us class nesting
    reliably — real nodeids are matched against this by base-name suffix in
    resolve_target_nodeids().
    """
    added = set(ADDED_TEST_DEF_RE.findall(test_patch_text))
    removed = set(REMOVED_TEST_DEF_RE.findall(test_patch_text))
    return added - removed


def resolve_target_nodeids(pass_map: dict[str, str], target_names: set[str]) -> set[str]:
    """Matches nodeids by base function name, stripping any parametrize suffix.

    A parametrized test's nodeid looks like 'test_foo[some-param-value]' —
    comparing that verbatim against the bare function name from the diff
    would never match, so we strip everything from '[' onward first.
    """
    return {
        nodeid
        for nodeid in pass_map
        if nodeid.split("::")[-1].split("[")[0] in target_names
    }


def evaluate(ticket_dir: Path, candidate_patch_path: Path | None, use_gold: bool) -> dict:
    ticket_dir = Path(ticket_dir)
    base_commit = (ticket_dir / "base_commit.txt").read_text().strip()
    test_patch_text = (ticket_dir / "test_patch.diff").read_text(encoding="utf-8")
    candidate_text = (
        (ticket_dir / "gold_patch.diff").read_text(encoding="utf-8")
        if use_gold
        else Path(candidate_patch_path).read_text(encoding="utf-8")
    )

    target_names = extract_fail_to_pass_targets(test_patch_text)
    if not target_names:
        raise EvalError(f"{ticket_dir.name}: no added test functions found in test_patch.diff")

    worktree = create_worktree(base_commit)
    try:
        apply_patch(worktree, test_patch_text)
        install_editable(worktree)

        baseline = run_full_suite(worktree)
        candidate_ids = resolve_target_nodeids(baseline, target_names)
        if not candidate_ids:
            raise EvalError(f"{ticket_dir.name}: target test(s) {target_names} not found in baseline run")

        # Not every added test reproduces the bug: e.g. a "reject invalid input" case
        # can already pass at baseline if the missing feature only affected acceptance,
        # not rejection. Only the tests that actually fail pre-fix are FAIL_TO_PASS
        # targets; the rest fold into the regression baseline like any other test.
        fail_to_pass_ids = {n for n in candidate_ids if baseline[n] != "passed"}
        if not fail_to_pass_ids:
            raise EvalError(
                f"{ticket_dir.name}: none of {candidate_ids} fail before the fix — "
                "malformed ticket (not a reproducible bug)"
            )
        baseline_pass_set = {n for n, outcome in baseline.items() if outcome == "passed"}

        apply_patch(worktree, candidate_text)
        install_editable(worktree)
        after = run_full_suite(worktree)
        after_pass_set = {n for n, outcome in after.items() if outcome == "passed"}

        targets_passing = fail_to_pass_ids & after_pass_set

        # A handful of marshmallow tests parametrize on the current timestamp, so their
        # exact nodeid can differ between the baseline and after runs (seconds apart)
        # even though it's the same logical test. Only count a vanished nodeid as a real
        # regression if no test sharing its base name (pre-"[") still passes afterward.
        after_base_names_passing = {n.split("[")[0] for n in after_pass_set}
        regressions = {
            n
            for n in (baseline_pass_set - after_pass_set) - fail_to_pass_ids
            if n.split("[")[0] not in after_base_names_passing
        }
        resolved = targets_passing == fail_to_pass_ids and not regressions

        return {
            "ticket": ticket_dir.name,
            "resolved": resolved,
            "fail_to_pass": sorted(fail_to_pass_ids),
            "targets_passing": sorted(targets_passing),
            "regressions": sorted(regressions),
        }
    finally:
        remove_worktree(worktree)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket", help="path to a single ticket dir, e.g. data/tickets/marshmallow-2900")
    parser.add_argument("--patch", help="path to a candidate patch file (source fix only)")
    parser.add_argument("--gold", action="store_true", help="evaluate the ticket's own gold_patch.diff")
    parser.add_argument("--all", action="store_true", help="run across every ticket in data/tickets/")
    args = parser.parse_args()

    if not args.gold and not args.patch:
        parser.error("provide --patch <file> or --gold")

    ticket_dirs = (
        sorted(p for p in TICKETS_DIR.iterdir() if p.is_dir())
        if args.all
        else [Path(args.ticket)]
    )

    results = []
    for tdir in ticket_dirs:
        try:
            result = evaluate(tdir, args.patch, args.gold)
        except EvalError as e:
            result = {"ticket": tdir.name, "resolved": False, "error": str(e)}
        results.append(result)
        status = "RESOLVED" if result.get("resolved") else "FAILED"
        extra = f" — {result['error']}" if "error" in result else (
            f" — regressions: {result['regressions']}" if result.get("regressions") else ""
        )
        print(f"[{status}] {result['ticket']}{extra}")

    resolved_count = sum(1 for r in results if r.get("resolved"))
    print(f"\n{resolved_count}/{len(results)} resolved")
    if any("error" in r for r in results):
        errored = [r["ticket"] for r in results if "error" in r]
        print(f"{len(errored)} harness/ticket errors: {errored}")

    sys.exit(0 if resolved_count == len(results) else 1)


if __name__ == "__main__":
    main()
