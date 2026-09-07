# Does multi-agent orchestration actually make an LLM better at fixing bugs?

An honest evaluation of whether adding Planner/Tester/Judge agents around a coding LLM
improves real bug-fixing — measured SWE-bench-style on 25 genuine
[marshmallow](https://github.com/marshmallow-code/marshmallow) issues, each with its real
merged fix and a hidden verifying test the agents never see.

**The finding: the bottleneck was not the architecture. It was the model's inability to
reliably honor structured contracts.** Adding a Tester loop helped (24% → 36%). Adding a
Planner made it *worse* (32%) despite producing plans that were factually correct. Adding
a Judge coincided with the best score (40%) — but that gain came from somewhere else
entirely, and the Judge failed to emit a parseable decision in 4 of its 5 real
invocations. Across every phase, the recurring failure was the same shape: a model that
could reason correctly about a fix and then fail to *apply* it — malformed tool arguments,
described-but-never-executed edits, and confident claims of success while every underlying
tool call had failed.

📊 **[Live results dashboard →](https://manasvimenon.github.io/multi-agent-swe-system/)**

---

## Reproduce it yourself — no API key needed

The evaluation harness is the part that has to be trustworthy, so it's built to prove
itself on a clean machine with no credentials and no local state:

```bash
git clone https://github.com/ManasviMenon/multi-agent-swe-system.git
cd multi-agent-swe-system
docker build -t marshmallow-agents .

# One ticket (~19 seconds):
docker run --rm marshmallow-agents \
  python eval/run_eval.py --ticket data/tickets/marshmallow-2900 --gold

# All 25 (~15 minutes):
docker run --rm marshmallow-agents
```

That applies each ticket's **real merged fix** to a fresh git worktree at that ticket's
base commit, runs marshmallow's full test suite before and after, and confirms all 25
resolve with zero regressions. If the harness's scoring were wrong, this is where it would
show. It has been verified passing 25/25 both locally and inside the container.

The agent phases are deliberately *not* in the image — they cost API quota and are
non-deterministic, so they'd make a container run neither free nor reproducible.

---

## Results

25 tickets, one clean run per phase, `gemini-3.5-flash-lite` (free tier) throughout.
A ticket counts as resolved only if the hidden test passes **and** nothing that was
previously passing broke.

| Phase | Pipeline | Resolved | |
|---|---|---|---|
| 2 — Baseline | Coder alone, one blind attempt | **6/25** (24%) | the control |
| 3 — Tester loop | Tester writes a failing test first; Coder retries 3× with regression feedback | **9/25** (36%) | the one clear win |
| 4 — Planner | Planner investigates and hands the Coder a plan | **8/25** (32%) | *worse than Phase 3* |
| 5 — Judge | Judge reviews exhausted retries: one guided retry, else escalate | **10/25** (40%) | but not because of the Judge |

**The honest caveats, which matter more than the numbers:**

- **Phase 4 failed its own pre-registered prediction outright.** The design named three
  tickets (946, 1384, 2227) that decomposition should recover. Zero of them resolved. A
  transcript deep-dive found the plans were *correct* — the Coder simply re-derived the
  same diagnosis itself rather than acting on them. The Planner bought efficiency
  (−57%, −56%, −32% Coder tool calls on tickets it couldn't solve), not correctness.
- **Phase 5's +2 was not the Judge.** All three newly-resolved tickets passed inside the
  normal retry loop before the Judge was ever invoked. Its actual mechanism — one guided
  retry — never once converted a failure into a success.
- **These are N=1 runs.** The Tester's reproduction step is stochastic; several ticket
  flips between phases are noise, not signal. Where a number looks like a finding but
  isn't, [RESULTS.md](RESULTS.md) says so.

---

## Why the model, not the architecture

The failure mode that recurred at every layer was structured-output compliance:

- **Malformed tool arguments.** The `edit_file` tool matches exact text. The model
  hand-escaped characters as if writing a Python string literal — emitting `\"` where the
  file has `"`, and a literal `\n` where the file has a real newline. Every such edit
  failed with "text not found." This had been silently costing edits since Phase 2 and was
  only caught in Phase 5, by which point it had plausibly depressed *every* prior number.
- **Described, never applied.** On multiple tickets the Coder wrote a correct diagnosis in
  prose across all three attempts and never once called the edit tool.
- **Confident false success.** In one run, every edit call failed and the Coder's final
  message still declared "I have successfully fixed the issue."
- **Ignored decision contracts.** The Judge was required to answer `RETRY` or `ESCALATE`
  on the first line. In 4 of 5 real invocations it wrote a root-cause essay instead — once
  misspelling the keyword as `ESCATE`. The analysis was substantively right; the contract
  it depended on was not honored.

A larger model would likely clear most of this. That is precisely the point: on a
resource-constrained model, orchestration sophistication is not the binding constraint.

---

## Method

Discipline mattered more than architecture here, and it's the part worth reviewing:

- **Eval harness first.** Built and validated before any agent existed — all 25 gold
  patches verified to resolve their own tickets, so a failure means the agent failed, not
  the scoring.
- **Pre-registration.** Every phase's hypothesis, contract, success criteria, falsification
  clause and risks were written into [RESULTS.md](RESULTS.md) *before* the code existed.
  Two predictions were falsified and are reported as such rather than quietly reframed.
- **No leakage by construction.** Agents never see `gold_patch.diff` or `test_patch.diff`.
  The Tester's self-written test is stripped from the candidate diff before grading, and
  final grading always happens in a separate, independently-installed worktree.
- **Superseded runs are kept, not deleted.** Every `results/*.v*.jsonl` is a run
  invalidated by a bug found later, archived alongside the corrected one. Phase 3's
  headline was corrected from 6/25 to 9/25 this way — in public, in the commit history.

**Nine real harness bugs were found and fixed through this process**, several of which had
been silently corrupting results for weeks — including one that fed the Coder "you broke
the entire test suite" on 19 of 25 tickets regardless of what it actually did. Each is
documented in [NOTES.md](NOTES.md) and [RESULTS.md](RESULTS.md) with the evidence that
caught it.

---

## Repository layout

```
eval/run_eval.py        Grading harness — FAIL_TO_PASS / PASS_TO_PASS scoring
src/agents/             planner · coder · tester · judge · shared agent runtime
src/orchestrator.py     Wires the pipeline; per-phase entry points
scripts/                Per-phase resumable runners + ticket curation
data/tickets/           25 curated tickets (issue, base commit, gold patch, hidden test)
results/                Run logs and full agent transcripts, including superseded runs
tests/                  Unit tests for the scoring logic + import smoke check
dashboard/              Generates docs/index.html from the run logs
RESULTS.md              Pre-registered designs, results, failure analysis
SCOPE.md                Success criteria and safety constraints, written up front
NOTES.md                Running diagnostic log
```

## Running the agents

Requires a `GEMINI_API_KEY` in `.env` (see `.env.example`). Each phase is resumable and
distinguishes transient rate limits from real failures, because the free tier's 500
requests/day means a full 25-ticket run spans days.

```bash
python scripts/run_baseline.py    # Phase 2
python scripts/run_phase3.py      # Phase 3
python scripts/run_phase4.py      # Phase 4
python scripts/run_phase5.py      # Phase 5
```
