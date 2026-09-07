# Does multi-agent orchestration actually make an LLM better at fixing bugs?

I tested whether wrapping a coding LLM in Planner, Tester and Judge agents improves real
bug fixing. The benchmark is 25 genuine
[marshmallow](https://github.com/marshmallow-code/marshmallow) issues, each paired with
its real merged fix and a hidden verifying test the agents never get to see, scored the
way SWE-bench scores things.

The short answer: the architecture was never the bottleneck. The model's inability to
follow structured contracts was.

Adding a Tester loop helped, taking resolution from 24% to 36%. Adding a Planner made
things worse (32%), even though the plans it produced were factually correct. Adding a
Judge lined up with the best score of 40%, but that gain came from somewhere else
entirely, and the Judge failed to produce a parseable decision in 4 of its 5 real
invocations.

The same failure kept turning up at every layer. The model could reason its way to the
correct fix and then fail to apply it: malformed tool arguments, fixes described in prose
but never executed, and confident claims of success when every underlying tool call had
actually failed.

[Live results dashboard](https://manasvimenon.github.io/multi-agent-swe-system/)

## Reproduce it yourself, no API key needed

The eval harness is the part that has to be trustworthy, so it proves itself on a clean
machine with no credentials and no local state:

```bash
git clone https://github.com/ManasviMenon/multi-agent-swe-system.git
cd multi-agent-swe-system
docker build -t marshmallow-agents .

# One ticket, about 19 seconds:
docker run --rm marshmallow-agents \
  python eval/run_eval.py --ticket data/tickets/marshmallow-2900 --gold

# All 25, about 15 minutes:
docker run --rm marshmallow-agents
```

This applies each ticket's real merged fix to a fresh git worktree at that ticket's base
commit, runs marshmallow's full test suite before and after, and checks that all 25
resolve with zero regressions. If the scoring logic were wrong, this is where it would
show up. It passes 25/25 both locally and in the container.

The agent phases are deliberately left out of the image. They cost API quota and they're
non-deterministic, so putting them in a container run would make it neither free nor
reproducible.

## Results

25 tickets, one clean run per phase, `gemini-3.5-flash-lite` on the free tier throughout.
A ticket only counts as resolved if the hidden test passes and nothing that was already
passing broke.

| Phase | Pipeline | Resolved | |
|---|---|---|---|
| 2. Baseline | Coder alone, one blind attempt | **6/25** (24%) | the control |
| 3. Tester loop | Tester writes a failing test first, Coder retries 3x with regression feedback | **9/25** (36%) | the one clear win |
| 4. Planner | Planner investigates and hands the Coder a plan | **8/25** (32%) | worse than Phase 3 |
| 5. Judge | Judge reviews exhausted retries, gives one guided retry or escalates | **10/25** (40%) | but not thanks to the Judge |

The caveats matter more than the numbers:

**Phase 4 failed its own pre-registered prediction.** The design named three tickets (946,
1384, 2227) that decomposition was supposed to recover. None of them did. Reading the
transcripts afterwards showed the plans were correct and the Coder simply re-derived the
same diagnosis itself instead of acting on them. The Planner bought efficiency rather than
correctness: on tickets it couldn't solve either way, it cut Coder tool calls by 57%, 56%
and 32%.

**Phase 5's +2 didn't come from the Judge.** All three newly-resolved tickets passed inside
the normal retry loop before the Judge was ever invoked. Its actual mechanism, the one
guided retry, never converted a single failure into a success.

**These are N=1 runs.** The Tester's reproduction step is stochastic, so several ticket
flips between phases are noise rather than signal. Where a number looks like a finding but
isn't, [RESULTS.md](RESULTS.md) says so.

## Why the model and not the architecture

The failure that recurred at every layer was structured-output compliance.

*Malformed tool arguments.* The `edit_file` tool matches exact text. The model hand-escaped
characters as though it were writing a Python string literal, emitting `\"` where the file
has `"` and a literal `\n` where the file has a real newline. Every one of those edits
failed with "text not found". This had been quietly costing edits since Phase 2 and only
surfaced in Phase 5, by which point it had probably depressed every earlier number.

*Described but never applied.* On several tickets the Coder wrote a correct diagnosis in
prose across all three attempts without ever calling the edit tool.

*Confident false success.* In one run every edit call failed and the Coder still finished
with "I have successfully fixed the issue."

*Ignored decision contracts.* The Judge had to answer `RETRY` or `ESCALATE` on the first
line. In 4 of 5 real invocations it wrote a root-cause essay instead, once misspelling the
keyword as `ESCATE`. The analysis was usually right. The contract everything downstream
depended on was not honoured.

A bigger model would probably clear most of this, which is the point. On a
resource-constrained model, orchestration sophistication isn't what's holding you back.

## Method

The discipline mattered more than the architecture, and it's the part I'd want reviewed:

**Eval harness first.** Built and validated before any agent existed. All 25 gold patches
were verified to resolve their own tickets, so a failure means the agent failed rather
than the scoring.

**Pre-registration.** Each phase's hypothesis, contract, success criteria, falsification
clause and risks went into [RESULTS.md](RESULTS.md) before the code existed. Two
predictions were falsified and are written up as falsified rather than quietly reframed.

**No leakage by construction.** Agents never see `gold_patch.diff` or `test_patch.diff`.
The Tester's self-written test is stripped out of the candidate diff before grading, and
final grading always happens in a separate, independently installed worktree.

**Superseded runs are kept.** Every `results/*.v*.jsonl` is a run that a later bug
invalidated, archived next to the corrected one. Phase 3's headline was corrected from
6/25 to 9/25 that way, in public, in the commit history.

Nine real harness bugs came out of this process, several of which had been corrupting
results for weeks. One of them fed the Coder "you broke the entire test suite" on 19 of 25
tickets regardless of what it had actually done. Each is written up in
[NOTES.md](NOTES.md) and [RESULTS.md](RESULTS.md) along with the evidence that caught it.

## Repository layout

```
eval/run_eval.py        Grading harness, FAIL_TO_PASS / PASS_TO_PASS scoring
src/agents/             planner, coder, tester, judge, shared agent runtime
src/orchestrator.py     Wires the pipeline, per-phase entry points
scripts/                Per-phase resumable runners plus ticket curation
data/tickets/           25 curated tickets (issue, base commit, gold patch, hidden test)
results/                Run logs and full agent transcripts, superseded runs included
tests/                  Unit tests for the scoring logic and an import smoke check
dashboard/              Generates docs/index.html from the run logs
RESULTS.md              Pre-registered designs, results, failure analysis
SCOPE.md                Success criteria and safety constraints, written up front
NOTES.md                Running diagnostic log
```

## Running the agents

Needs a `GEMINI_API_KEY` in `.env` (see `.env.example`). Each phase is resumable and tells
transient rate limits apart from real failures, because the free tier's 500 requests a day
means a full 25-ticket run spans several days.

```bash
python scripts/run_baseline.py    # Phase 2
python scripts/run_phase3.py      # Phase 3
python scripts/run_phase4.py      # Phase 4
python scripts/run_phase5.py      # Phase 5
```
