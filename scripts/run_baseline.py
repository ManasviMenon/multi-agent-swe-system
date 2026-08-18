"""Runs the Phase 2 single-Coder baseline across tickets and scores each with the eval harness.

Resumable by design: each ticket's summary is appended to results/baseline_run.jsonl the
moment it finishes, and a rerun skips any ticket_id already present in that file. This
matters because the free-tier daily quota (see SCOPE.md) can't guarantee a full 25-ticket
run completes in one sitting -- a 429 on ticket 13 should mean "pick up at 13 tomorrow",
not "start over and re-spend the quota already used."

Usage:
  python scripts/run_baseline.py                              # full run (resumable)
  python scripts/run_baseline.py --tickets marshmallow-2900     # debug a single ticket
  python scripts/run_baseline.py --tickets marshmallow-2900,marshmallow-1312
"""

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "agents"))

from eval.run_eval import (  # noqa: E402
    TICKETS_DIR,
    EvalError,
    create_worktree,
    evaluate,
    remove_worktree,
)
from coder import DailyQuotaExhausted, NetworkError, RateLimitExhausted, run_coder  # noqa: E402

RESULTS_DIR = ROOT / "results"
SUMMARY_LOG = RESULTS_DIR / "baseline_run.jsonl"
TRANSCRIPTS_DIR = RESULTS_DIR / "transcripts"


def load_manifest():
    with open(TICKETS_DIR / "manifest.json", encoding="utf-8") as f:
        return json.load(f)


def load_done_ticket_ids():
    if not SUMMARY_LOG.exists():
        return set()
    done = set()
    with open(SUMMARY_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                done.add(json.loads(line)["ticket_id"])
    return done


def get_candidate_diff(worktree: Path) -> str:
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
    return result.stdout


def run_one_ticket(ticket_id: str) -> dict:
    ticket_dir = TICKETS_DIR / ticket_id
    base_commit = (ticket_dir / "base_commit.txt").read_text().strip()
    issue_text = (ticket_dir / "issue.md").read_text(encoding="utf-8")

    worktree = create_worktree(base_commit)
    try:
        transcript = run_coder(issue_text, worktree)
        candidate_diff = get_candidate_diff(worktree)
    finally:
        remove_worktree(worktree)

    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = TRANSCRIPTS_DIR / f"{ticket_id}.json"
    transcript_path.write_text(
        json.dumps({"ticket_id": ticket_id, "candidate_diff": candidate_diff, **transcript}, indent=2),
        encoding="utf-8",
    )

    if not candidate_diff.strip():
        return {
            "ticket_id": ticket_id,
            "resolved": False,
            "error": "Coder produced no changes",
            "tool_call_count": transcript["tool_call_count"],
            "hit_cap": transcript["hit_cap"],
            "total_tokens": transcript["total_tokens"],
        }

    patch_file = RESULTS_DIR / f".candidate-{uuid.uuid4().hex[:8]}.diff"
    patch_file.write_text(candidate_diff, encoding="utf-8", newline="\n")
    try:
        eval_result = evaluate(ticket_dir, patch_file, use_gold=False)
    except EvalError as e:
        eval_result = {"ticket": ticket_id, "resolved": False, "error": str(e)}
    finally:
        patch_file.unlink(missing_ok=True)

    return {
        "ticket_id": ticket_id,
        "resolved": eval_result.get("resolved", False),
        "regressions": eval_result.get("regressions", []),
        "fail_to_pass": eval_result.get("fail_to_pass", []),
        "error": eval_result.get("error"),
        "tool_call_count": transcript["tool_call_count"],
        "hit_cap": transcript["hit_cap"],
        "total_tokens": transcript["total_tokens"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickets", help="comma-separated ticket_ids to run (debug subset); omit for full run")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    all_ids = [t["ticket_id"] for t in manifest]

    if args.tickets:
        requested = args.tickets.split(",")
        unknown = set(requested) - set(all_ids)
        if unknown:
            parser.error(f"unknown ticket_id(s): {unknown}")
        target_ids = requested
    else:
        target_ids = all_ids

    done = load_done_ticket_ids()
    pending = [t for t in target_ids if t not in done]
    print(f"{len(done & set(target_ids))} already done, {len(pending)} pending")

    for ticket_id in pending:
        print(f"--- {ticket_id} ---")
        start = time.time()
        try:
            result = run_one_ticket(ticket_id)
        except DailyQuotaExhausted as e:
            print(f"Daily quota exhausted: {e}")
            print("Stopping here -- rerun this script tomorrow to resume from this ticket.")
            sys.exit(1)
        except NetworkError as e:
            # Not a per-ticket failure -- don't record it, or resumability would skip
            # this (and every remaining) ticket forever having never really attempted it.
            print(f"Network error after retries: {e}")
            print(f"Stopping here -- rerun this script to resume from {ticket_id} once connectivity is back.")
            sys.exit(1)
        except RateLimitExhausted as e:
            print(f"Rate limit retries exhausted: {e}")
            print(f"Stopping here -- rerun this script to resume from {ticket_id} once rate pressure eases.")
            sys.exit(1)
        except Exception as e:
            # A bug or transient failure in one ticket's processing shouldn't cost the
            # rest of the day's tickets -- record it and move on, same principle as the
            # tool-execution catch-all in coder.py.
            result = {
                "ticket_id": ticket_id,
                "resolved": False,
                "error": f"{type(e).__name__}: {e}",
                "tool_call_count": 0,
                "hit_cap": False,
                "total_tokens": 0,
            }

        result["elapsed_seconds"] = round(time.time() - start, 1)
        with open(SUMMARY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")

        status = "RESOLVED" if result["resolved"] else "FAILED"
        extra = f" ({result['error']})" if result.get("error") else ""
        print(f"[{status}] {ticket_id} -- {result['tool_call_count']} tool calls{extra}")

    all_results = []
    if SUMMARY_LOG.exists():
        with open(SUMMARY_LOG, encoding="utf-8") as f:
            all_results = [json.loads(line) for line in f if line.strip()]
    resolved_count = sum(1 for r in all_results if r["resolved"])
    print(f"\nOverall: {resolved_count}/{len(all_results)} resolved (cumulative across all runs)")


if __name__ == "__main__":
    main()
