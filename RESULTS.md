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
| `marshmallow-1424` | 0/5 | Never once reproduced (but see below -- not necessarily "hard") |
| `marshmallow-2936` | 1/5 | Mostly hard, rare success (but see below) |

All three used the *full* 10-call budget on every single attempt, success or failure --
the Tester never confidently concludes on these, it just runs out of room. This means
"0/4 category-B recovered" was conflating three different situations: two tickets where
the Tester structurally can't set up a reproducing test at all, one that's genuinely
noisy, and only `marshmallow-2900` that actually exercised the Coder retry loop this
phase exists to test.

**Refinement after reading the full transcripts (free -- both tickets' logged runs
already existed, no new API calls):** `1424` and `2936` don't look like pure task
difficulty on closer read -- they look like a round-boundary problem. In `1424`'s single
recorded run, the Tester wrote a test using `attr`/`attrs` (a package not installed in
this environment), got an ImportError, correctly diagnosed it, and rewrote the test with
plain Python classes instead -- a real, sensible self-correction. But that rewrite was
its *last* tool call before hitting the 10-call cap; it never got to re-run and verify
the corrected version. `2936` shows the same shape: its first test (broad, using real
Greek/Arabic/punycode IDN examples) passed against the unfixed code -- not a
reproduction -- so it went back to the source, reasoned through the actual
`DOMAIN_REGEX`/`encode("idna")` mechanism in detail (visible as analysis notes written
directly into the test file), and was mid-rewrite when the same 10-call cap hit. In both
cases the model was visibly converging on a better test right as the single round ran
out of room, under the *old* single-round Tester (built before today's round-based
retry + gold-check gate). This is a more fixable failure mode than "structurally hard,"
and specifically the kind of problem an additional round should address -- worth
re-testing under the new 2-round Tester before concluding these tickets resist
reproduction on the merits, not just on the old budget.

**Follow-up (2026-08-18) -- the round-boundary hypothesis confirmed, plus a third real
harness bug found while testing it.** Re-ran the same 5x variance probe with two fixes
in place: `temperature=0.0` on the Tester (wired in after the prior session, for
repeatability), and the round-based retry + gold-check gate. Result:

| Ticket | Before (default temp, 1 round) | After (temp=0, 2 rounds) |
|---|---|---|
| `marshmallow-1357` | 2/5 | **5/5** |
| `marshmallow-1424` | 0/5 | **4/5** |
| `marshmallow-2936` | 1/5 | 0/5 (see below) |

`1357` and `1424` improved dramatically, confirming the round-boundary hypothesis --
giving the Tester a second, fresh-budget attempt let it finish what it had already
correctly started figuring out. `2936` got *worse* (0/5), which is what led to finding
a third real harness bug: `run_written_test` takes zero parameters, so every call has
an identical `(tool_name, args)` signature -- the duplicate-call blocker (correct for
tools like `search_files`, where identical arguments really do mean a wasted repeat)
was silently rejecting every re-check after the first *in each round*, even though the
underlying test file had genuinely changed via `write_test` in between. The Tester
could rewrite its test as many times as it wanted within a round but only ever got to
verify the first attempt. Confirmed via a from-scratch worktree replay before touching
any code (0 evidence needed guessing -- the tool schema itself has `"properties": {}`).
Fixed by exempting specific tool names from duplicate-checking in
`agent_runtime.py`'s `run_agent_loop` (a `no_duplicate_check_tools` parameter), used
for `run_written_test` only -- this doesn't affect the Coder, whose tools all have
genuinely distinguishing arguments.

With that fix, `2936` returned to **1/5** -- back to its original baseline, not an
improvement. Every one of its 5 runs used the full 20-call budget (both rounds) with
`hit_cap=True` regardless. Unlike `1357`/`1424`, this ticket's difficulty appears to be
genuine, not a mechanical artifact this round of fixes happened to address -- Email IDN
validation's actual mechanism (`DOMAIN_REGEX` vs. `encode("idna")` interaction) seems to
consistently take this model more reasoning than a 20-call budget affords, even with
determinism and a real duplicate-check bug both fixed. Worth revisiting with a larger
budget or a Planner's decomposition (Phase 4) rather than further Tester-side fixes.

**k-of-N methodology, decided on this evidence:** `temperature=0.0` + the round-based
retry made variance rare rather than common (2 of 3 previously-noisy tickets are now
at or near 5/5). Given that, the full Phase 3 run uses **N=1 per ticket as the primary,
headline methodology** -- every ticket's single result reported honestly, resolved or
not, no rescuing. Multiple reruns (N=3-5) remain available as an optional *side*
investigation for specific tickets that come back unresolved and look like they might
be genuinely stochastic rather than hard -- but any such rerun is reported as the honest
fraction (e.g. "1/5 across reruns"), never folded into the headline resolution count as
if a lucky pass were a real one. A hard ticket gets characterized, not rescued.

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

## Phase 3 — final result: full 25-ticket run

**6/25 resolved — identical to Phase 2's 6/25.** After six real harness bugs found and
fixed during validation (see above), a full clean pass across all 25 tickets, one run,
current code:

| Status | Count | Tickets |
|---|---|---|
| Resolved | 6 | `1808, 2249, 2821, 2868, 2870, 1378` |
| `no_reproducing_test` | 5 | `2985, 2936, 2118, 1768, 1350` |
| Model-level error | 1 | `1357` (malformed tool-call JSON — an API-level hiccup, not a capability signal) |
| Attempted, not resolved | 13 | everything else |

Same headline count as Phase 2, but **not the same set of tickets** — which matters
more than the number matching would have:

- `marshmallow-2270` flipped from resolved (Phase 2, and most Phase 3 validation runs)
  to unresolved this run — consistent with the reproduce-gate variance already
  documented for this ticket, an honest N=1 result rather than a regression to chase.
- `marshmallow-1378` flipped from unresolved to resolved — but it's a **category-D
  "new feature" ticket** (`empty string data_key disallowed`), not one of the four
  category-B tickets the Tester loop was specifically built to fix.
- **The original pre-registered prediction failed on its own terms**: 0 of the 4 named
  category-B tickets (`1357`, `1424`, `2900`, `2936`) resolved. `1357` hit a model-level
  error, `1424`/`2900` were attempted across all 3 Coder rounds without resolving,
  `2936` couldn't produce a reproducing test at all in this run.

Per the falsification clause written into the original pre-registered design ("if
Phase 3 resolves a substantially different set of tickets than predicted, that's
itself a finding worth writing up") — this is exactly that case, and it should be read
as such rather than quietly filed as "no change." The honest interpretation, combining
this result with the deep-dive evidence gathered during validation: the Tester+Coder
retry loop mechanically works (verified: it correctly used regression feedback and
stopped breaking things on `marshmallow-2900`'s replay; it correctly avoided harming
any of the 6 previously-resolved tickets in aggregate), but the specific hypothesis
that *this* mechanism would recover *these* 4 category-B tickets did not hold. The
recovery that did happen (`1378`) came from a category the design didn't predict for,
which is a real, if serendipitous, data point for Phase 4's premise that different
failure categories need different interventions — not proof the Tester loop overall
was worth its ~3-4x cost over Phase 2 on this ticket set.

**Cost, reported as promised in the pre-registered design (not just resolution):**
every attempted ticket used all 3 Coder rounds and the Tester's full available budget
regardless of outcome — cost per ticket rose substantially over Phase 2's baseline
(individual tickets ran 400K-3M+ tokens combined across Tester+Coder, vs. Phase 2's
single-shot ~200K-1M token range) for a 0-net resolution change. Taken at face value,
Phase 3 did not earn its added cost on this ticket set. Taken with the mechanism
evidence from validation, the more precise conclusion is that the loop works correctly
but this model hits a capability ceiling on category B specifically, which more retries
alone don't fix — motivating Phase 4's different kind of intervention (decomposition
before coding) rather than more retries of the same kind.

## Phase 3 — correction: an 8th bug found during Phase 4 work invalidates the 6/25 headline

**The section above was written against a corrupted run and is left unmodified above for
the record, but its headline number and one specific causal claim are wrong.** Found
during Phase 4 smoke-test debugging, days later: `verify_against_gold()` (the
gold-patch quality gate for the Tester's own test, added in Phase 3) borrows a
disposable scratch worktree to check the test against the real fix, then tears that
worktree down — but never re-pointed the shared venv's editable marshmallow install
back at the caller's own worktree afterward. Every suite check for the rest of that
ticket (later Tester rounds, the baseline snapshot, every Coder retry round) then hit
`ModuleNotFoundError` and was misreported as `internal_verdict="suite_broken"`, telling
the Coder "you broke the entire test suite" regardless of what it actually did.

Checked directly against the data: **19 of the 25 committed tickets show this exact
signature.** The frozen grader (`eval/run_eval.py`'s `evaluate()`) always re-installs
into its own independent fresh worktree, so the **6 resolved tickets were still
genuinely resolved** — but the Coder's retry loop was fed corrupted feedback on 76%
of tickets, meaning the other 19 never got an honest shot.

A second, unrelated bug (8th) surfaced during the corrected rerun: `genai.Client()` had
no request timeout, so a stalled connection could hang the whole pipeline indefinitely
instead of triggering the existing retry/backoff logic — observed directly as a process
sitting at ~0.05s total CPU time for 40+ minutes on one call. Fixed with a 180s
per-request timeout (`http_options=types.HttpOptions(timeout=180_000)`), which plugs
into the existing `create_with_retry` path since a timeout raises `httpx.TimeoutException`
→ `APIConnectionError`, already handled there.

**Corrected result, full clean 25-ticket rerun with both bugs fixed: 9/25 resolved.**

| Status | Count | Tickets |
|---|---|---|
| Resolved | 9 | `2821, 2868, 2870, 2249, 1369, 2270, 1808, 1506, 1378` |
| `no_reproducing_test` | 4 | `2985, 2936, 2118, 1350` |
| `suite_broken` (genuine — Coder's own edit broke `NameError: EXCLUDE`) | 1 | `1721` |
| Attempted, not resolved | 11 | everything else |

All 6 originally-resolved tickets held; 3 new recoveries (`1369`, `2270`, `1506`) that
the corrupted feedback loop had been suppressing. One correction to the original
write-up above: **`marshmallow-2270`'s "flip," attributed there to reproduce-gate
variance, was wrong** — checking the buggy run's data, `2270` was itself one of the 19
`suite_broken` tickets. It didn't flip due to stochastic non-reproduction; it flipped
because the harness was broken. The variance explanation should be retracted.

**What survives the correction, unchanged:** the falsification clause's core finding.
0 of the 4 named category-B tickets (`1357`, `1424`, `2900`, `2936`) resolved even in
the corrected run — this was not an artifact of the bug, and now stands on solid
footing rather than partially-confounded ground. The headline count (9/25) also lands
much closer to the pre-registered ~10/25 prediction than 6/25 did, even though it's
still the *wrong* 9 relative to the named hypothesis.

**Known impact on Phase 4, flagged rather than silently fixed:** Phase 4's
pre-registered design (below) lists `marshmallow-1369` under "refactors/renames across
many call sites" as a ticket the Planner might help recover. That table is left
untouched below, per this project's own rule that a pre-registered design isn't edited
after the fact — but `1369` is now already resolved by Phase 3 alone, so it is no
longer a valid target for Phase 4 to claim credit for recovering.

## Phase 4 — Planner: pre-registered experimental design

Written before any Phase 4 code exists, on paper only, per the same discipline as
Phase 3's design above. This is the plan to build against, not a decision made mid-run.

### 1. Am I solving the right problem?

Phase 2's failure analysis (category D, 9 tickets: never attempted an edit, hit the cap
exploring) splits into three qualitatively different subgroups, and the Planner should
not be expected to help them equally:

| Subgroup | Tickets | Why a Planner should/shouldn't help |
|---|---|---|
| Diffuse, multi-method bugs | `946`, `1384`, `1404` | **Best fit.** The fix is spread across several methods (`_bind_to_schema`, `__apply_nested_option`, etc.) with no single obvious edit point — exactly "decompose the investigation before coding," the Planner's core job. |
| Refactors/renames across many call sites | `1369`, `2924`, `2227` | **Partial fit.** A Planner enumerating every call site needing change is directly useful, but the Coder still has to correctly edit all of them — and Phase 3's `marshmallow-2900` finding (capability ceiling on a *single*-location fix) means a multi-location fix is plausibly harder, not easier, for this model. Expect the Planner to help *locate* but not guarantee *execute*. |
| New-feature tickets (nothing broken to find) | `1312`, `1768`, `1378` | **Weakest fit.** These need designing new code, not finding an existing bug's location — a Planner built around "where does this behavior live" doesn't obviously transfer to "what should a new classmethod/Validator look like." Plausibly needs a different prompt framing more than decomposition. |

**Falsifiable prediction, named tickets, not just a count:** the Planner primarily
recovers from the diffuse-bug subgroup — predicting **2 of 3** (`946`, `1384`, `1404`)
resolve, **1 of 3** refactor tickets resolves (predicting `2227`, since deprecation
warnings are a more mechanical, lower-risk change than the `Number`/`Mapping` ABC
restructuring in `2924` or the `pass_many`→`pass_collection` rename in `1369`, which
touches decorator internals more invasively), and **0 of 3** new-feature tickets
resolve without a separate prompt change. Total: **3/9 category-D tickets**, concentrated
in one subgroup — a materially different distribution than "roughly a third of D
resolves evenly," which would instead suggest the Planner helps generically rather than
for the specific decomposition reason hypothesized here.

**Cost, stated honestly before building:** Phase 4 stacks a third agent's calls on top
of Tester + Coder. If the Planner runs read-only exploration (~15 calls) *before* the
Tester/Coder loop even starts, a single category-D ticket could cost Planner(~15) +
Tester(~10-20) + Coder(up to 3×20) = well over 100 calls, several times Phase 2's
baseline cost for one ticket. Given the free-tier RPD ceiling, this may mean category-D
tickets alone approach a full day's quota for a handful of tickets — worth deciding
up front whether Phase 4 initially runs against a *subset* of category D (the 6
diffuse+refactor tickets predicted to matter) rather than the full 9, to keep the
validation-before-full-run discipline affordable.

### 2. What is the Planner's contract?

- **Sees:** the issue text and the same read-only exploration tools already shared
  between Coder and Tester (`agent_runtime.py`'s `READ_ONLY_TOOL_DEFS` — direct reuse,
  not a new tool surface). Never sees `gold_patch.diff` or `test_patch.diff`.
- **Produces:** a structured plan (which files/methods are likely involved and why,
  not a diff) that becomes additional context handed to the Coder alongside the issue
  text and the Tester's failing test -- the Planner never edits code itself, only the
  Coder does, matching the guardrail that each role's tool access is enforced in code.
- **Bound:** its own tool-call cap (proposing 15 -- more than the Tester's 10 since
  broad investigation is its whole job, less than the Coder's 20 since it never edits).
  Runs once per ticket, before the Tester/Coder loop starts -- not part of the retry
  loop itself, since a plan that's wrong on attempt 1 being silently reused on attempts
  2 and 3 would just compound the error; if this turns out to matter, letting the
  Planner revise its plan using Coder failure feedback is a natural later addition, not
  part of the initial design.

### 3. How will success be measured, and what would prove this wrong?

- **Success criterion, stated in advance:** the 3 named tickets above (`946`, `1384`,
  `2227`) resolve; the rest of category D and the already-resolved/Phase-3-recovered
  tickets are unaffected.
- **Falsification:** if a substantially different set of category-D tickets resolves
  (e.g. new-feature tickets recover but diffuse-bug ones don't), that contradicts the
  "decomposition helps diffuse-location bugs specifically" theory and needs its own
  writeup, not quiet reconciliation.
- **Cost tracked explicitly:** Planner calls/tokens per ticket, on top of the existing
  Tester+Coder cost breakdown -- report cost-per-resolved-ticket for the full pipeline,
  not just the resolution delta.

### 4. What could go wrong or contaminate the result?

- **A wrong plan could steer the Coder worse than no plan at all** -- the same
  "too-strict trap" shape as Phase 3's Tester risk, but here it's "too-confident-wrong"
  rather than "too-strict": a plausible-but-incorrect plan handed to the Coder as
  apparent ground truth could be more misleading than the Coder exploring fresh, since
  the Coder may trust it over its own investigation. Explicitly check: does adding a
  Planner regress any ticket that Phase 2 or Phase 3 already resolved without one?
- **The Coder might explore less because it trusts the plan**, potentially missing a
  plan error it would otherwise have caught through its own reading -- worth watching
  for in the transcripts (does the Coder's own tool-call count drop sharply once a
  Planner is added, and does that correlate with worse outcomes on any ticket).

## Phase 4 — two more harness bugs found (7th and 8th), one Planner-specific

**7th (already covered above, restated for continuity):** `verify_against_gold()`'s
stale editable-install pointer, found during Phase 4 smoke-testing but retroactively
invalidating 19/25 of the original Phase 3 tickets. See the correction section above.

**8th, Planner-specific: `run_agent_loop()` silently discarded the model's final
answer whenever it hit its tool-call cap.** The loop broke out the instant
`tool_call_count` reached `max_tool_calls`, without sending back results already
collected in that final batch and without giving the model a chance to conclude --
`final_message` fell back to the previous interaction's `output_text`, which is always
empty for a pure function-call turn. Harmless for the Tester/Coder, which only read
`transcript["steps"]` (already populated correctly) and never touch `final_message`.
**Catastrophic for the Planner**, whose entire output IS `final_message`.

Found by actually reading the transcripts rather than trusting the summary numbers:
the first Phase 4 validation-10 run showed 3 of 9 previously-resolved tickets
regressing, which looked like "the Planner is actively harmful." Checking the
Planner's own transcripts showed why: **all 10/10 validation tickets hit exactly
15/15 tool calls with `hit_cap=True` and an empty plan.** The Planner had never once
produced a plan -- Phase 4 as validated up to that point was silently just re-running
Phase 3's pipeline with 15 wasted calls tacked on front, and the "3 regressions" were
ordinary Coder/Tester non-determinism, not the Planner's effect (it had no effect).

Fixed by sending back whatever results were already collected before the cap was hit
(previously discarded even when non-empty), then -- if the model still wants more
tools after seeing them -- giving one explicit final turn with no tools offered to
force a real answer. Verified in isolation before spending validation quota on it: a
single fresh Planner run on `marshmallow-946` produced a genuine 1547-character plan
with concrete root-cause analysis, still within its original 15-call budget. The
entire validation-10 run was archived (`phase4_run.v1-planner-cap-blackout.jsonl`) and
redone from scratch with the fix in place.

## Phase 4 — final result: full 25-ticket run

**8/25 resolved — one fewer than Phase 3's corrected 9/25.** Full clean pass, all 25
tickets, one run, current code (both the 7th and 8th bugs fixed):

| Status | Count | Tickets |
|---|---|---|
| Resolved | 8 | `1369, 1378, 1506, 1808, 2249, 2270, 2868, 2870` |
| `no_reproducing_test` | 8 | `1350, 1404, 1424, 2118, 2149, 2924, 2936, 2985` |
| Attempted, not resolved | 9 | everything else |

Direct comparison against Phase 3's corrected 9/25:

| | Count | Tickets |
|---|---|---|
| Lost (P3 resolved, P4 did not) | 1 | `2821` |
| Gained (P4 resolved, P3 did not) | 0 | — |
| Held (resolved in both) | 8 | `1369, 1378, 1506, 1808, 2249, 2270, 2868, 2870` |

**The risk named in advance in section 4 of the design ("does adding a Planner
regress any ticket Phase 3 already resolved without one?") is the headline finding.**
`marshmallow-2821` regressed. Checked directly, per the same design's own instruction
to watch for it: the Planner's plan for `2821` was factually correct (it correctly
named `RegexMemoizer._regex_generator` in `marshmallow.validate.URL` and the right
general fix -- Unicode support in the hostname regex for IDN URLs), and the Coder's
resulting attempt caused **zero PASS_TO_PASS regressions** -- it simply didn't fully
solve a genuinely hard Unicode/IDN regex problem in 3 rounds. That reads as ordinary
Coder-level difficulty on a hard ticket (the Coder is not run at temperature 0; a
Phase-3-only replay on a different day could plausibly fail here too) rather than the
plan actively misleading the Coder. One regression out of 9 watched tickets, with no
evidence of the specific "too-confident-wrong" failure mode the design worried about,
is a mild result rather than an alarming one -- but it is a real cost, not zero.

**The named, falsifiable prediction failed outright.** The design predicted 3/9
category-D tickets would resolve (`946`, `1384`, `2227` specifically), concentrated in
the diffuse-bug subgroup. Actual outcome for all 9 named tickets:

| Ticket | Subgroup | Predicted | Actual |
|---|---|---|---|
| `946` | diffuse | resolve | attempted, not resolved |
| `1384` | diffuse | resolve | attempted, not resolved (no source changes) |
| `1404` | diffuse | resolve | no_reproducing_test |
| `1369` | refactor | not predicted | resolved -- **but already resolved by Phase 3 alone**, moot for Phase 4 credit |
| `2924` | refactor | not predicted | no_reproducing_test |
| `2227` | refactor | **resolve** | attempted, not resolved |
| `1312` | new-feature | not predicted | attempted, not resolved |
| `1768` | new-feature | not predicted | attempted, not resolved |
| `1378` | new-feature | not predicted | resolved -- **but already resolved by Phase 3 alone**, moot for Phase 4 credit |

Of the 7 tickets where Phase 4 could actually earn credit (excluding `1369`/`1378`,
both already resolved before Phase 4 ran), **zero resolved.** Not "a different 3
resolved than predicted" (which the falsification clause explicitly said would still
be an interesting, salvageable finding) -- genuinely zero. The decomposition-helps
theory, as implemented and prompted here, does not hold on this ticket set.

**Cost, reported as promised:** the Planner adds ~14.4 calls and ~92K tokens per
ticket on average (2.31M tokens across all 25, 27.6% of the full pipeline's 8.37M
total tokens) on top of Tester+Coder. For zero net-new recoveries and one regression,
Phase 4 did not earn its added cost on this ticket set.

**Honest conclusion:** the Planner mechanism itself works correctly (verified: it
produces substantive, factually-grounded plans that correctly locate the relevant
code, not generic filler -- confirmed by inspecting `2821`'s and `946`'s plans
directly). The failure is not mechanical, it's that a correct plan didn't translate
into a successful fix for tickets this Coder+model combination couldn't already solve
unaided. This is a real negative result, not a broken experiment -- the two harness
bugs that could have manufactured a false negative (stale install pointer, silently
empty plans) were both found and fixed before this number was reported, and the
regression was individually inspected rather than assumed. Per the same discipline as
Phase 3: this stands as reported rather than being quietly re-run in search of a
better number.

## Phase 4 — deep dive: does a correct plan get the Coder closer, even when it doesn't resolve?

The resolved/not-resolved count above hides a sharper question: on tickets that
failed in *both* phases, did having a plan measurably change what the Coder produced,
or was it wasted effort regardless? Answerable at zero extra cost -- both phases'
full transcripts were already sitting on disk. Filtered to the 8 tickets where the
Coder actually ran in both phases and neither resolved (excludes tickets where the
Tester itself never reproduced the bug in one phase or the other, which isn't a
Coder/Planner question):

| Ticket | P3 verdict/calls | P4 verdict/calls | Read verdict |
|---|---|---|---|
| `1721` | `suite_broken`, 31 calls | `passed_clean`, 21 calls | Plan helped |
| `2900` | `failed`, 38 calls | `passed_clean`, 13 calls | Plan helped |
| `946` | `failed`, 96 regressions, 47 calls | `failed`, 0 regressions, 40 calls | Plan "helped" only via inaction |
| `1768` | `passed_clean`, 15 calls | `suite_broken`, 1072 regressions, 38 calls | Plan looked harmful, wasn't really |
| `1312` | `passed_clean`, 19 calls | `passed_clean`, 18 calls | No change |
| `1357` | `passed_clean`, 19 calls | `passed_clean`, 13 calls | Same verdict, -32% calls |
| `1384` | `failed`, 60 calls | `failed`, 26 calls | Same verdict, -57% calls |
| `2227` | `failed`, 57 calls | `failed`, 25 calls | Same verdict, -56% calls |

**Three case studies, read in full, not just the summary stats:**

- **`946` (plan correct, Coder never edited anything):** the plan correctly named
  `_normalize_nested_options`/`__apply_nested_option` in `schema.py` and the
  `List`-vs-`Nested` propagation gap -- materially the same diagnosis a fresh Planner
  run independently reproduced in a separate spot-check. But across all 3 Coder
  rounds, the transcript shows the Coder re-deriving that *exact same* diagnosis from
  scratch each time (re-reading the same methods, re-concluding the same root cause)
  and never once calling `edit_file` -- two of the three rounds ended only because the
  tool-call cap forced a final message (see the 8th bug above), not because the model
  chose to stop and act. Zero regressions here isn't the plan paying off; it's the
  Coder never attempting anything, which happens to be safer than Phase 3's Coder
  (which did edit, and broke 96 tests) but isn't a case of the plan producing a better
  edit -- there was no edit.
- **`2900` (plan = a fix a human had already proposed in the issue thread; Coder
  executed *and* verified independently):** the Coder implemented the plan's exact
  suggested `Constant.__init__` change in round 1 after 5 exploration calls, then in
  round 2 caught and fixed a real regression its own fix introduced
  (`test_constant_none_allows_none_value`, an `allow_none` edge case the plan's
  snippet didn't mention) entirely through its own investigation -- directly
  contradicting the design's pre-registered worry that the Coder would "explore less
  because it trusts the plan." It still didn't resolve, but because the Tester's own
  self-written test didn't cover the full scope of the real hidden test -- a Tester
  ceiling, not a Planner or Coder failure.
- **`1768` (looks like the one clear harm case; isn't, on close reading):** the
  Coder's first edit used `old_text="class Length(Validator):"` -- just the bare
  declaration line -- as its insertion anchor for the new `And` validator class,
  which silently deleted `Length`'s own body when replaced, corrupting the module at
  class-definition time (`TypeError: Can't instantiate abstract class Length`) and
  cascading into every test in the suite failing to even collect (the "1072
  regressions" is really "the whole suite refused to load," not 1072 independent
  findings). The Coder spent rounds 2-3 trying to repair its own corruption, mostly
  failing (`"old_text not found"` twice), and round 3 ends with a **false claim of
  success** ("I have corrected... without disrupting existing classes") while the
  suite was still broken. Checked directly against Phase 3's attempt at the same
  ticket: Phase 4's Coder did **just as much** upfront exploration before its first
  edit (18 calls vs. Phase 3's 15) -- this was not a case of trusting the plan and
  skipping verification. The actual difference is a single risky anchor-text choice
  in one `edit_file` call, a general surgical-edit risk that exists with or without a
  Planner. Phase 3 happened to pick a longer, safer anchor on this specific ticket.
  Attributing this one to "the Planner made it worse" would be overclaiming what the
  transcripts actually show.

**The pattern that survives scrutiny: efficiency, not correctness.** On 3 of the 4
tickets that landed on the identical verdict in both phases, Phase 4 reached that
same conclusion using substantially fewer Coder tool calls -- `1384` 60→26 (-57%),
`2227` 57→25 (-56%), `1357` 19→13 (-32%) -- while `1312` barely moved (19→18, -5%).
The plan doesn't appear to change *what* the Coder concludes on tickets it can't
solve, but it does appear to make reaching that conclusion cheaper, at least on
harder, higher-call-count tickets where there's more redundant exploration to cut.
That's a real, distinct, separately-useful finding from the headline resolution
count, and -- combined with `946`'s pattern of the Coder re-deriving the plan's own
conclusions instead of trusting them -- suggests the actual lever worth pulling next
isn't a better Planner, it's a Coder prompt that explicitly tells it to treat a
supplied plan as a starting point to verify quickly and act on, not re-investigate
from zero.
