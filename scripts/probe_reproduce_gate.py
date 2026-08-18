"""Diagnostic: runs ONLY the Tester's reproduce-gate step (write a test, confirm it
fails) multiple times per ticket, with no Coder involved. Used to distinguish "the
Tester is stochastic on this ticket" from "this ticket is genuinely hard for the Tester
to reproduce" -- a single run can't tell those apart, but several can.

Much cheaper than a full Phase 3 run: no Coder rounds, no mid-loop suite checks, just
the Tester's own ~10-call budget per attempt.

Usage:
  python scripts/probe_reproduce_gate.py --tickets marshmallow-1357,marshmallow-1424 --runs 5
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "agents"))

from agent_runtime import DailyQuotaExhausted, NetworkError  # noqa: E402
from eval.run_eval import TICKETS_DIR, create_worktree, install_editable, remove_worktree  # noqa: E402
from tester import run_tester  # noqa: E402

RESULTS_DIR = ROOT / "results"
OUTPUT_FILE = RESULTS_DIR / "reproduce_gate_variance.json"


def probe_one(ticket_id: str) -> dict:
    ticket_dir = TICKETS_DIR / ticket_id
    base_commit = (ticket_dir / "base_commit.txt").read_text().strip()
    issue_text = (ticket_dir / "issue.md").read_text(encoding="utf-8")
    gold_patch_text = (ticket_dir / "gold_patch.diff").read_text(encoding="utf-8")

    worktree = create_worktree(base_commit)
    try:
        install_editable(worktree)
        transcript = run_tester(issue_text, worktree, base_commit, gold_patch_text)
    finally:
        remove_worktree(worktree)

    return {
        "valid_reproduction": transcript["valid_reproduction"],
        "tool_call_count": transcript["tool_call_count"],
        "hit_cap": transcript["hit_cap"],
        "reproduce_attempts": transcript["reproduce_attempts"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickets", required=True, help="comma-separated ticket_ids")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    all_results = {}
    if OUTPUT_FILE.exists():
        all_results = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))

    for ticket_id in args.tickets.split(","):
        runs = all_results.setdefault(ticket_id, [])
        needed = args.runs - len(runs)
        for i in range(needed):
            print(f"--- {ticket_id} run {len(runs) + 1}/{args.runs} ---")
            try:
                result = probe_one(ticket_id)
            except (DailyQuotaExhausted, NetworkError) as e:
                print(f"Stopping: {e}")
                OUTPUT_FILE.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
                sys.exit(1)
            runs.append(result)
            print(f"  valid_reproduction={result['valid_reproduction']} calls={result['tool_call_count']} hit_cap={result['hit_cap']}")
            OUTPUT_FILE.write_text(json.dumps(all_results, indent=2), encoding="utf-8")

    print("\n=== summary ===")
    for ticket_id, runs in all_results.items():
        successes = sum(1 for r in runs if r["valid_reproduction"])
        print(f"{ticket_id}: {successes}/{len(runs)} runs produced a valid reproducing test")


if __name__ == "__main__":
    main()
