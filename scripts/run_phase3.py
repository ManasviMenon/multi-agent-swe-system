"""Runs Phase 3 (Tester-gated Coder retry loop) across tickets, scoring each with the
real eval harness. Resumable the same way as run_baseline.py -- each ticket's summary is
appended to results/phase3_run.jsonl the moment it finishes, and a rerun skips any
ticket_id already present.

Phase 2's results/baseline_run.jsonl is never touched by this script -- it stays frozen
as the control for comparison, per RESULTS.md's pre-registered design.

Usage:
  python scripts/run_phase3.py                                   # full run (resumable)
  python scripts/run_phase3.py --tickets marshmallow-2900          # debug a single ticket
  python scripts/run_phase3.py --tickets marshmallow-1808,marshmallow-2900,...
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "agents"))

from agent_runtime import DailyQuotaExhausted, NetworkError, RateLimitExhausted  # noqa: E402
from eval.run_eval import TICKETS_DIR  # noqa: E402
from orchestrator import run_phase3_ticket  # noqa: E402

RESULTS_DIR = ROOT / "results"
SUMMARY_LOG = RESULTS_DIR / "phase3_run.jsonl"
TRANSCRIPTS_DIR = RESULTS_DIR / "transcripts_phase3"


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
            outcome = run_phase3_ticket(ticket_id)
            result = outcome["result"]
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
            # Same principle as NetworkError -- a transient rate-limit exhaustion is not
            # a real attempt at this ticket. Recording it as a per-ticket failure would
            # poison resumability the same way an earlier version of run_baseline.py did
            # (see NOTES.md) -- stop instead of pretending the ticket was tried.
            print(f"Rate limit retries exhausted: {e}")
            print(f"Stopping here -- rerun this script to resume from {ticket_id} once rate pressure eases.")
            sys.exit(1)
        except Exception as e:
            result = {
                "ticket_id": ticket_id,
                "status": "error",
                "resolved": False,
                "error": f"{type(e).__name__}: {e}",
                "coder_attempts": 0,
                "coder_tool_calls": 0,
                "coder_tokens": 0,
                "tester_tool_calls": 0,
                "tester_tokens": 0,
                "internal_verdict": None,
                "regressed_mid_loop": [],
                "calibration": None,
            }
            outcome = {"result": result, "tester_transcript": None, "coder_rounds": []}

        result["elapsed_seconds"] = round(time.time() - start, 1)
        with open(SUMMARY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")

        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        transcript_path = TRANSCRIPTS_DIR / f"{ticket_id}.json"
        transcript_path.write_text(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "tester": outcome["tester_transcript"],
                    "coder_rounds": outcome["coder_rounds"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        status = "RESOLVED" if result["resolved"] else result["status"].upper()
        extra = f" ({result['error']})" if result.get("error") else ""
        cal = f" [{result['calibration']}]" if result.get("calibration") else ""
        print(
            f"[{status}] {ticket_id} -- tester={result['tester_tool_calls']} calls, "
            f"coder={result['coder_attempts']} attempts/{result['coder_tool_calls']} calls{extra}{cal}"
        )

    all_results = []
    if SUMMARY_LOG.exists():
        with open(SUMMARY_LOG, encoding="utf-8") as f:
            all_results = [json.loads(line) for line in f if line.strip()]
    resolved_count = sum(1 for r in all_results if r["resolved"])
    no_repro = sum(1 for r in all_results if r["status"] == "no_reproducing_test")
    print(f"\nOverall: {resolved_count}/{len(all_results)} resolved (cumulative across all runs)")
    print(f"no_reproducing_test: {no_repro}")


if __name__ == "__main__":
    main()
