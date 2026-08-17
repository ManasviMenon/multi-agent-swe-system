# RESULTS.md

Living document, updated as each phase completes. Per the project plan, this is meant to
end with: resolution rate, regression rate, escalation precision, and cost per ticket for
the full pipeline vs. the Phase 2 baseline; which agents added value on which ticket
types; and an honest failure-mode section. Right now it only has Phase 2 — the baseline
every later phase is measured against.

## Phase 2 — single Coder agent baseline (control)

**Config:** Gemini 3.5 Flash-Lite, `client.interactions.create` multi-turn function
calling, tools = `read_file` / `list_directory` / `search_files` / `edit_file` (surgical
exact-match-and-replace, not full-file overwrite), 20-tool-call hard cap, no test-running
tool, no planner. Full ticket text is the only input — the Coder never sees the gold
patch or the test patch.

**Headline metrics** (25 held-out tickets, see `SCOPE.md`):

| Metric | Value |
|---|---|
| Resolution rate | **6/25 (24%)** |
| Regression rate | **0/25 (0%)** |
| Mean tokens/ticket | 550,433 |
| Median tokens/ticket | 498,992 |
| Total tokens, full run | 13,760,833 |
| Mean tool calls/ticket | 17.6 |
| Tickets that hit the 20-call cap | **20/25 (80%)** |

The 80% cap-hit rate is a headline finding on its own: the tool-call budget is binding
for most runs, not just the failing ones — even several *resolved* tickets used all 20
calls. Any later phase that changes the effective budget (a Planner that front-loads
investigation, a Tester that adds more calls per iteration) has to be read against this,
since more calls is not free.

**0% regression rate** is the number that matters most per `SCOPE.md`'s own framing, and
it held even under real-agent conditions (not just the gold-patch harness self-check) —
every accepted or attempted edit that changed something either fixed the target test or
changed nothing observable; nothing broke a previously-passing test.

### Failure analysis

19 of 25 tickets failed. Categorized by reading each transcript structurally — the
summary fields first (empty diff? hit the cap? did `edit_file` ever succeed?), then the
tool-call sequence to understand why — rather than treating "6/25" as one undifferentiated
number. The categories are a prediction of which Phase 3+ addition should help each
failure type, checked against reality once that phase exists.

| Category | Count | Tickets |
|---|---|---|
| A — Resolved | 6 | 1808, 2249, 2270, 2821, 2868, 2870 |
| B — Edited, wrong/incomplete fix | 4 | 1357, 1424, 2900, 2936 |
| C — Attempted edit, match failed, gave up on editing | 3 | 1506, 2118, 2149 |
| D — Never attempted an edit, hit the cap exploring | 9 | 1312, 1369, 1378, 1384, 1404, 1768, 2227, 2924, 946 |
| E — Gave up early, well under the cap, no edit | 3 | 1350, 1721, 2985 |

Sorted mechanically: every failure sorts into B/C/D/E by two signals from the transcript
— is `candidate_diff` empty, and did `edit_file` ever return `{"ok": true}`.

#### Category B — edited, but the fix was wrong or incomplete (4)

The most informative category: the agent found *a* plausible-looking place to change,
made a clean edit (no corruption, no regressions), and it still didn't make the target
test pass. This is the profile a **Tester loop (Phase 3)** should fix directly — feed the
failing test back and let it retry.

**marshmallow-2900** (`Constant` field `required=True` raises `load_default` warning) —
verified via actual traceback (not inference): the fix stops the constructor's
`ValueError` but marshmallow's real required-field check runs through a
`_validate_missing()` method that the gold patch overrides and the agent never
discovered. Fixed the symptom it found, not the mechanism.

**marshmallow-1357** (DateTime as inner field of List/Tuple) — the edit adds an
`isinstance(schema, SchemaABC)` guard around a `getattr(schema.opts, ...)` call. That's a
defensive type-check, not a fix for "DateTime fields cannot be used as inner field" — it
guards against a crash rather than making the described use case work. Plausible pattern
match (something that looks like a null/type-safety fix) applied to the wrong problem.

**marshmallow-1424** (`List(Pluck())` raises while `Pluck(many=True)` works) — the edit
adds explicit `isinstance(self.inner, Pluck)` branches in `List._serialize` and
`List.deserialize` to route Pluck fields differently. This is a genuinely targeted,
plausible-looking special case — closest of the four to a real fix — but still didn't
satisfy the test, most likely because the actual bug is in exactly which method Pluck's
special serialize/deserialize logic needs to be called through, and the agent guessed the
call signature rather than verifying it.

**marshmallow-2936** (Email validator IDN support) — restructured the validation flow to
attempt IDNA-encoding before the regex check rather than only as a fallback after a first
regex failure. Conceptually a reasonable simplification of the original logic, and it
took 5 failed `edit_file` attempts to land (the trickiest match of the four) — but still
doesn't satisfy the full IDN test suite, likely missing an edge case in what counts as a
validly-encodable vs. rejected domain.

#### Category C — attempted an edit, couldn't get it to match, gave up on editing (3)

Distinct from category D: these *did* try `edit_file`, so they were closer to a fix than
category D, but the match failed (`old_text` didn't align with the file) and instead of
retrying with corrected `old_text`, the agent reverted to searching — often re-asking
questions it already had answers to.

- **marshmallow-1506** (dots in field names during JSON deserialization): one failed edit
  attempt, then four blocked-duplicate searches in a row — found the edit point, missed
  the match, then thrashed instead of retrying.
- **marshmallow-2118** (URL relative-only validation): four separate failed edit
  attempts across the session — persistent, but never landed a matching `old_text`.
- **marshmallow-2149** (Nested partial not working as expected): one failed edit attempt,
  then abandoned editing entirely for the rest of the session.

This category is a mechanical near-miss, not a reasoning failure — a Tester loop
wouldn't help here (there's no successful edit to test), but a **Planner** that
pre-identifies the exact target region, or simply letting the Coder retry a failed
`edit_file` call before moving on, plausibly would.

#### Category D — never attempted an edit at all (9)

The largest category. Split further by what kind of ticket these are:

**Diffuse, multi-method bugs (no single obvious edit point):**
- **marshmallow-946** (verified via direct read, see `NOTES.md`): the fix for propagating
  `only`/`exclude` to nested containers is spread across `_bind_to_schema`,
  `__apply_nested_option`, `_nested_normalized_option`, and container handling in
  `List`/`Nested` — no single crisp line to change, so the agent kept searching without
  converging.
- **marshmallow-1384** (dotted `only`/`exclude` for nested schema *instances*) — same
  family of bug as 946, same diffuse-machinery profile.
- **marshmallow-1404** (unpickleable object in nested schema context) — search/read-heavy
  with duplicates near the end, consistent with the same "couldn't find one anchor point"
  pattern.

**Refactors/renames spanning many call sites, not a single bug location:**
- **marshmallow-1369** (rename `pass_many` to `pass_collection`) — a rename across
  decorators and schema internals, not a localized fix.
- **marshmallow-2924** (make `Number`/`Mapping` abstract base classes) — a typing/ABC
  restructuring likely requiring coordinated changes across multiple class definitions.
- **marshmallow-2227** (deprecate `__version__` and related attributes) — needs every
  usage site found and given a deprecation warning; genuinely broad, though notably
  *zero* blocked-duplicates here, meaning this was broad-but-non-repetitive search, not
  thrashing.

**New-feature tickets (nothing "broken" to locate, needs to design new code):**
- **marshmallow-1312** (add `Schema.from_dict`) — adding a new classmethod, not fixing an
  existing one; heavy duplicate searching suggests it struggled to find an anchor because
  there wasn't a bug site to find.
- **marshmallow-1768** (add `validate.And`) — adding a new `Validator` class; used
  `list_directory` repeatedly (looking for where validators live and how they're
  structured) rather than fixing something.
- **marshmallow-1378** (empty string `data_key` disallowed) — shallower exploration (only
  3 file reads) relative to its 20 calls, suggesting it circled the topic without pinning
  down the actual validation logic location.

This category is where a **Planner (Phase 4)** should show the clearest lift: decomposing
"how does X propagate through the codebase" into a structured investigation before
attempting to code, rather than searching flat. New-feature tickets specifically may
also just need a different prompt framing ("design new code," not "locate and fix a bug").

#### Category E — gave up early, no edit, well under the cap (3)

**marshmallow-1350**, **marshmallow-1721**, **marshmallow-2985** all stopped in 2-5 tool
calls, far short of the 20-call cap. Checking the actual issue text explains why cleanly:
all three are phrased as open questions or RFCs, not bug reports —

- 1350: *"RFC: Change the way we store metadata?"*
- 1721: *"Is it intentional that setting parameter unknown=... behaves the same as..."*
- 2985: *"Question: Should `fields.Enum()` accept `None`..."*

These passed curation (real issue, real merged PR, real net-new test) but the *issue
text itself* doesn't read as an actionable bug description — a human engineer reading
only the issue would face the same ambiguity about what concrete change is being asked
for. This isn't a Coder weakness; arguably stopping quickly rather than guessing at a
discussion-style ticket is reasonable behavior. Noted here as a dataset-composition
caveat, not a failure to fix — a stronger model likely wouldn't "solve" these
differently, since the ambiguity is in the input, not the reasoning.

### Prediction for Phase 3+

| Category | Best predicted fix | Tickets |
|---|---|---|
| B (edited, wrong mechanism) | **Tester loop** — feed the failing test back | 4 |
| C (edit attempted, match failed) | Planner (pinpoint target) or simple edit-retry | 3 |
| D — diffuse/multi-method | **Planner** — decompose before coding | 3 |
| D — refactors/new-features | Planner + possibly different prompt framing | 6 |
| E (ambiguous/discussion tickets) | Neither — dataset characteristic, not agent failure | 3 |

If Phase 3's Tester loop is built next, the honest expectation is it should recover most
of category B (4 tickets) and little else — categories C, D, and E have no successful
edit for a Tester to react to. That's the falsifiable prediction to check once Phase 3
exists: resolution rate should move from 6/25 toward roughly 10/25 primarily by fixing
category-B tickets, not by touching D or E. If Phase 3 instead resolves a very different
set of tickets than predicted, that's itself a finding worth writing up.

## Phase 3 — Tester loop: validation results (prediction did not hold, and why)

Built per the pre-registered design above (self-written test, TDD reproduce-gate, 3
retry attempts, final grading always via the untouched hidden `test_patch.diff`). Per
the single-variable discipline, validated on the same 10 known tickets (6 resolved + 4
category-B) before any full run, exactly as pre-registered.

**Two real harness bugs were found and fixed during validation, before either touched a
scored number** — worth stating plainly rather than glossing over, because catching them
here is what makes the numbers below trustworthy:

1. **The Tester's mid-loop re-check was blind to regressions.** The first validation
   pass (v1) showed all 6 previously-resolved tickets held (0 regressed — the specific
   "too-strict trap" risk named in the pre-registered design did not occur), but 0/4
   category-B tickets recovered. Tracing `marshmallow-2900` (no extra API cost — the
   Coder's edits were reconstructed from the logged transcript and reapplied to a fresh
   worktree) showed the Coder's fix *did* find `_validate_missing`, the exact mechanism
   Phase 2 missed — but it also silently broke `test_constant_none_allows_none_value`,
   and nothing in the loop caught it because the Tester's re-check only ran its own
   single test, never the broader suite. Fixed by adding a full-suite regression check
   after every Coder attempt, using a baseline snapshot taken before the Coder touches
   anything. Confirmed leak-free by construction: the orchestrator's shared worktree
   never has `test_patch.diff` applied (that only happens in `eval/run_eval.py`'s own
   separate, isolated worktree during final grading), so running the full existing suite
   there structurally cannot include the hidden verifying test.
2. **The regression check itself had an "empty result = clean" bug.** Re-validating
   with fix #1 in place, ticket 1 (`marshmallow-1808`, previously resolved cleanly)
   broke — 3 Coder attempts, final grading failed with a full collection error. The
   Coder's last edit had broken `tests/conftest.py` itself (a genuine `AttributeError`
   at import time), but the new suite-check function only read `report["tests"]`, which
   is empty on a total collection failure — so it silently returned `{}`, and the
   orchestrator's `if after_suite else []` treated that emptiness as "no regressions,"
   the exact opposite of what it meant. The run was stopped after 2 of 10 tickets (not
   let finish) once this was understood, to avoid quietly under-reporting regressions
   across the rest of the batch. Fixed by making the function return an explicit
   `(results, suite_ok)` pair so "ran clean" and "collapsed entirely" can never be
   confused again — the general lesson being that an empty check result needs to say
   *why* it's empty, since "all clear" and "catastrophic failure" can look identical if
   you only check for emptiness.

**Re-validated from scratch after both fixes (single variable: only the re-check scope
changed between this run and the original baseline).**

| | Result |
|---|---|
| Resolved | 5/10 (down from 6/10 in v1 -- see below) |
| Regressions among the 6 previously-resolved | 0 |
| Category-B recovered | 0/4 |

The drop from 6 to 5 is `marshmallow-1808` flipping to `no_reproducing_test` — not a
regression caused by the new code (the Tester's own test-writing logic was untouched by
either fix), but LLM sampling variance in the Tester's reproduce-gate step. This turned
out to be a real, measurable property of the system, not a one-off:

### Reproduce-gate variance (quantified, not asserted)

Rather than infer variance from single runs, the Tester's reproduce-gate step (write a
test, confirm it fails -- no Coder involved, ~10 API calls per attempt) was re-run 5
times independently on the tickets that failed it, to separate "stochastic" from
"genuinely hard":

| Ticket | Valid reproductions | Reading |
|---|---|---|
| `marshmallow-1357` | 2/5 | Genuine variance -- works less than half the time |
| `marshmallow-1424` | 0/5 | Genuinely hard -- never once reproduced |
| `marshmallow-2936` | 1/5 | Mostly hard, rare success |

All three used the *full* 10-call budget on every single attempt, success or failure --
the Tester never confidently concludes on these, it just runs out of room. This means
"0/4 category-B recovered" was conflating three different situations: two tickets where
the Tester structurally can't set up a reproducing test at all, one that's genuinely
noisy, and only `marshmallow-2900` that actually exercised the Coder retry loop this
phase exists to test.

### The one ticket that did exercise the loop: capability ceiling, not haste

`marshmallow-2900`'s 3 Coder rounds were reconstructed and replayed against the mid-loop
checks (free -- transcripts already existed) to see exactly what feedback the Coder
received and whether it used it:

- **After round 1**: target test still fails, no regression (a plausible dead end).
- **After round 2**: target test still fails, **and the exact same regression from the
  bug-discovery run reappears** (`test_constant_none_allows_none_value`) -- independent
  confirmation this is a real, recurring failure mode of this fix approach, not a fluke.
- **After round 3**: target test *still* fails, but **the regression is gone** -- the
  Coder used the feedback and genuinely stopped breaking the other test.

That last point is the key evidence: the feedback loop mechanically works -- the Coder
received "you broke this other test" and correctly avoided it next attempt. It just
never discovered `_validate_missing`, the actual mechanism the fix needs, across all 3
tries with explicit, actionable feedback each time. This is capability ceiling, not lack
of feedback -- more retries would plausibly not have helped, because each retry was the
same model missing the same piece of systematic knowledge about marshmallow's validation
architecture, not rushing past information it had.

### Revised conclusion

The pre-registered prediction (Tester loop recovers ~4 category-B tickets) did not hold,
and tracing why matters more than the number: it did not fail because the loop is
broken -- two real self-inflicted harness bugs were caught and fixed *before* either
touched a scored number, and the regression-checking mechanism is now demonstrably
working (it caught a real bug, and separately, the Coder demonstrably used its feedback
correctly on `marshmallow-2900`). It failed to recover category B because of two
separate, more specific limits: the Tester frequently cannot construct a reproducing
test for these particular bugs at all (1424, 2936), and even when it can, this model
often lacks the specific systematic understanding needed to find the right fix within 3
attempts (2900). "Feedback loops help a hasty-but-capable model; they don't help a
model that has hit a genuine capability ceiling" is a sharper, more useful finding than
the original prediction would have been if it had simply come true.

**Full 25-ticket run deliberately not yet run.** Per the same discipline that gated the
original 10-ticket validation: a live, unexplained contradiction (0/4 predicted vs.
actual) isn't something a full run resolves, it's something a full run would spend a
day's quota merely confirming. Investigate first, run broadly only once the numbers can
be interpreted -- which is now the case.

## Phase 3 — Tester loop: pre-registered experimental design

Written before any Phase 3 code exists, so it can be checked against what actually
happens rather than rationalized after the fact. Four question groups, each with the
concrete decision or commitment made now.

### 1. Am I solving the right problem?

- **Hypothesis, with a number, before building:** the Tester loop should recover
  category B specifically (4 tickets: 1357, 1424, 2900, 2936) by feeding failing-test
  output back to the Coder for retries. Predicted outcome: 6/25 -> ~10/25. Not "a Tester
  will help" — this specific count, on these specific tickets.
- **Is it worth it?** A Tester roughly doubles API calls per ticket (test-run feedback
  means more model turns). Spending ~2x cost to recover 4 tickets is not assumed to be
  worth it going in — it's a question this phase's cost data has to answer, not a
  foregone conclusion. If the cost-per-resolved-ticket gets worse, that's a legitimate
  "not worth it yet" finding, not a failure of the phase.
- **One variable at a time.** Same model (Gemini 3.5 Flash-Lite), same 25 tickets, same
  eval harness. Only the Tester loop is new. The Phase 2 baseline (6/25, commit
  `950d877`) is frozen for this comparison — if the Coder's prompt or the model changes
  at the same time as the Tester is added, any delta becomes unattributable.

### 2. What is the Tester's contract?

- **What it sees is a methodology-integrity question, not plumbing.** The Tester must
  run the existing test suite (already visible to the Coder) but must never expose
  `test_patch.diff` — the hidden FAIL_TO_PASS verifier — to either agent. Leaking it
  would let the Coder read the exact assertion it needs to satisfy and write to the
  test instead of fixing the bug, which would corrupt every number in this document.
  Decided now, before the loop exists, specifically because it's easy to leak by
  accident (e.g. a test-runner tool that returns full output including test source).
- **What it tells the Coder must be actionable, not binary.** "Test failed" would not
  have pointed 2900's agent toward `_validate_missing`; a traceback with the actual
  exception and line would have a chance to. Decision: pass back the real
  failure/traceback text (truncated to a reasonable length), not a pass/fail bit.
- **The loop needs a bound**, same principle as the 20-tool-call cap: a max number of
  test-fix cycles, and an explicit decision for what happens at the cap — give up,
  return the best attempt so far, or escalate. This is the same retry-cap guardrail
  from `SCOPE.md`, now applied one level up.

### 3. How will success be measured, and what would prove this wrong?

- **Success criterion, stated in advance:** ~10/25, and specifically by flipping the 4
  named category-B tickets — not just any 4. A run that hits 10/25 by fixing a
  different 4 tickets is a signal the mechanism-model in this document is wrong, which
  is more informative than the headline number matching.
- **Falsification, named in advance:** if Phase 3 resolves a substantially different set
  of tickets than predicted, or if category C/D/E tickets flip while category B doesn't,
  that contradicts the theory of what a Tester loop actually fixes and gets written up
  as such, not quietly reconciled after the fact.
- **Cost is a first-class metric, not an afterthought.** The baseline already logs
  tokens and tool calls per ticket, so this phase has to report the delta on both —
  calls-per-ticket and cost-per-resolved-ticket, before vs. after — alongside the
  resolution delta. "Resolution went 6->10 but cost per resolved ticket tripled" is a
  real, honest, reportable outcome.

### 4. What could go wrong or contaminate the result?

- **New failure modes a test-running loop introduces:** a hanging test run (needs a
  timeout), slow suites inflating cost, or misleading/truncated failure output steering
  the Coder toward the wrong fix. Naming these now so they're recognized as expected
  categories if they show up mid-run, not mistaken for something else.
- **The Tester could make things worse, not just fail to help.** A noisy or misleading
  failure message might distract a Coder that would otherwise have produced a fine
  fix on the first attempt — turning a would-be pass into a fail. So the measurement
  explicitly includes: **did any of the 6 currently-resolved tickets regress once the
  Tester was added?** Net improvement has to account for losses among the tickets that
  already worked, not just gains among the ones that didn't. This is checked the same
  way regression rate is checked elsewhere in this document — by comparing against the
  frozen Phase 2 baseline, ticket by ticket, not just by the aggregate count.
