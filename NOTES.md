# Iteration notes

Running log of non-obvious decisions and failures found while building each phase, kept
as raw material for the eventual `RESULTS.md` honest-failure-modes section.

## 2026-08-12 — Coder baseline v1: over-exploration, never committed to an edit

First real test of the Phase 2 Coder agent (Gemini 3.5 Flash-Lite) against `marshmallow-2900`
(the `Constant` field `required=True` bug). Result: the agent used all 20 tool calls
exploring -- `search_files`/`read_file` -- and never called `write_file`, despite clearly
having located the root cause by roughly call 16 (`src/marshmallow/fields.py:2077-2078`,
`Constant.__init__`). Two searches were exact repeats of earlier ones (calls 1/20, 2/8),
suggesting indecision rather than genuine remaining uncertainty.

Fix: tightened `SYSTEM_INSTRUCTION` in `src/agents/coder.py` to explicitly push toward
committing to an edit once the root cause is understood, and to avoid repeating an
identical search/read. Deliberately did *not* push hard toward "edit fast" -- a confident
wrong edit that skips understanding the bug is worse than indecision (it risks a real
regression), so the instruction asks for "explore enough, then act," not "act
immediately." Re-testing on the same single ticket before spending quota on a full run,
per the debug-on-one-ticket workflow -- this is a controlled before/after on a case where
the failure mode is already known.

## 2026-08-12 — Coder baseline: the full debug arc on marshmallow-2900

Four iterations on the same ticket, each isolating a different failure, before running
the full baseline. Kept as the clearest illustration of infrastructure-vs-capability
judgment in this project:

1. **v1/v2 (prompt tightening alone didn't work):** the agent explored well but never
   called its write tool within the 20-call budget, even after tightening the system
   prompt to push toward committing to an edit. It made exact-duplicate tool calls
   (identical searches/reads repeated verbatim), suggesting genuine looping, not
   legitimate re-verification.
2. **v3 (mechanical fix: block duplicate calls):** added exact-duplicate-call detection
   that refuses to re-execute a repeated (tool, args) pair and tells the model to use
   what it already learned. This worked -- it broke the loop and forced a decision. But
   it surfaced a worse problem: `write_file` was a full-file overwrite, and the model
   regenerated an entire 72KB file from memory to make a 2-line change, introducing
   unrelated corruption elsewhere (a dropped word in a docstring, spurious `\"`
   escaping) -- harmless this time only because it landed in comments, not by design.
   Also extremely token-expensive (810K tokens, ~5 minutes for one ticket).
3. **v4 (design fix: surgical edit_file, still failed):** replaced `write_file` with
   `edit_file(path, old_text, new_text)` -- exact-match-and-replace, fails loudly on an
   ambiguous or missing match, touches nothing else. This should have been strictly
   safer, but the model then reliably hand-escaped quote characters (writing `\"` where
   the file has plain `"`, as if it were emitting a Python string literal instead of raw
   text) and picked huge multi-line old_text blocks, so every edit attempt failed to
   match. One call even passed byte-identical old_text and new_text.
4. **v5 (combined fix, tooling finally correct):** normalized the model's phantom `\"`
   escaping before matching (verified marshmallow's actual source has zero legitimate
   backslash-quote sequences, so this is safe here), rejected old_text==new_text as a
   no-op, and added a one-line tool-description note against hand-escaping. Result: a
   clean, syntactically correct, non-corrupting surgical edit -- and the harness
   correctly scored it as unresolved. Traced the real failure directly (applied the
   candidate patch, ran the target test, read the traceback): the model fixed the
   constructor's `ValueError` (the symptom it found via search) but never discovered
   that marshmallow's actual required-field check runs through a `_validate_missing()`
   method the gold patch overrides -- a mechanism the model never searched for. That's
   a genuine reasoning/discovery gap, not a tooling bug.

The takeaway kept for `RESULTS.md`: three of four rounds were legitimate infrastructure
bugs (a real repetition loop, a dangerous full-file-overwrite design, a model-specific
escaping quirk) worth fixing because they'd affect every later phase, not just this
baseline. The fourth is a genuine capability ceiling of a small, cheap model -- it correctly
identifies a *plausible* bug location but doesn't always find the actual mechanism a fix
needs to touch. That distinction (fix the harness vs. accept the finding) is the actual
skill Phase 2 is meant to exercise, not just "did ticket 2900 resolve."

## 2026-08-12 — Phase 2 baseline result: 6/25 resolved (24%)

Full run across all 25 tickets with Gemini 3.5 Flash-Lite, the tooling fixes from the
marshmallow-2900 debug arc in place. Two more infrastructure issues surfaced mid-run,
both with the same underlying failure mode as each other: a batch script that catches
exceptions per-ticket (so one bad ticket doesn't cost the rest of the day's quota) can
accidentally *poison* the resumability log if the exception was environmental rather than
a real attempt -- a mid-run network drop (`APIConnectionError`, 21 tickets) and sustained
rate-limit pressure near the end of the run (`RuntimeError: exceeded retry attempts`, 2
tickets) both got recorded as permanent "done, failed" results with 0 tool calls and 0
tokens, which would have made a resumed run silently skip them forever. Fixed by: adding
retry-with-backoff for connection errors (not just rate limits) in `coder.py`, and having
`run_baseline.py` treat a still-failing connection/rate-limit error as run-stopping (like
`DailyQuotaExhausted`) rather than a recordable per-ticket result. Manually stripped and
re-ran the 23 affected tickets from the already-poisoned log this one time.

Final, clean number: **6/25 resolved (24%)**, just under SCOPE.md's >=25% target. Read
this as the intended humble control, not a shortfall to fix -- every later phase (Tester
loop, Planner, Reviewer+Judge) gets measured against this exact number, and the point of
Phase 2 is to be beatable, not good.

## 2026-08-17 — Phase 3 Tester loop: two harness bugs, prediction contradicted, root-caused

Full writeup in `RESULTS.md`'s Phase 3 section. Short version for this log: validating
the Tester+Coder retry loop on the 10 known tickets (per the pre-registered discipline)
surfaced two real bugs in the harness itself before either touched a scored number --
the Tester's mid-loop check was blind to regressions elsewhere in the suite, and once
fixed, the fix's own "empty suite result" case was being silently read as "all clear"
instead of "the suite collapsed entirely." Both caught by validating on known tickets
before a full run, exactly the discipline this practice exists for.

After both fixes, the pre-registered prediction (Tester loop recovers ~4 category-B
tickets) did not hold -- 0/4 recovered. Root cause, not just the number: reproduce-gate
variance probing (5 independent runs per ticket, no Coder involved) showed 2 of the 4
tickets are ones the Tester structurally can't write a reproducing test for at all
(0/5 and 1/5 successes), not flaky -- genuinely hard for this model. The one ticket that
did exercise the full retry loop (`marshmallow-2900`) showed the feedback loop working
correctly (the Coder used "you broke this other test" feedback and genuinely stopped
breaking it) but never discovering the specific mechanism (`_validate_missing`) the real
fix needs, across 3 attempts. Capability ceiling, not lack of feedback, not a broken
loop. Full 25-ticket run deliberately deferred until this was understood -- a
contradicted prediction isn't something a full run resolves, it's something a full run
would spend a day's quota merely confirming.

## 2026-08-17 (same session, later) — gold-patch verification gate + per-role temperature

Two more pieces added before hitting the daily quota wall.

**Gold-patch verification gate for the Tester's self-written tests.** The reproduce-gate
variance data above answers "how often does the Tester write a valid test" but not "how
do we know a test that *looks* valid actually is" -- a test could fail against buggy code
for the wrong reason (a bug in the test itself, or coincidentally exercising unrelated
behavior) and still pass the old gate, which only checked outcome=="failed". Fixed with
a second check: apply the real `gold_patch.diff` (the actual source fix, distinct from
the hidden `test_patch.diff` verifying test) to a disposable scratch worktree along with
the Tester's current test, and confirm it now passes. This is leak-free by the same logic
already used in Phase 1's curation self-check (`scripts/curate_tickets.py`: every gold
patch is verified to resolve its own ticket before being trusted) -- the gold patch is
used purely as a harness-internal pass/fail oracle, and only that binary signal (not the
patch's content) ever reaches a prompt. Verified both mechanically (a genuine test passes
the gold-check, a deliberately bogus `assert 1==2` test correctly fails it even though it
also fails against the buggy code) and live end-to-end on marshmallow-2900 before wiring
it in for real. `run_tester` is now round-based (mirrors `run_coder_round`'s continuation
design) so a test that fails the gold-check gets fed back ("your test doesn't hold up
against a correct fix, try a different one" -- never what the fix actually is) and the
Tester gets up to 2 attempts, same MAX_REPRODUCE_ATTEMPTS pattern as everywhere else in
this project that touches an LLM's first attempt not being trustworthy on its own.

**Per-role temperature.** Confirmed via a live A/B prompt comparison that Gemini's
default temperature produces genuine, non-trivial variety (3 different creative
sentences on repeated identical calls) before hitting the daily quota wall mid-comparison
against temperature=0.0 -- so the *quantitative* effect of lowering it is still
unconfirmed, but the qualitative case for doing so on the Tester is solid regardless:
its task (write a test, confirm it fails) benefits from repeatability, while the Coder's
task (find a fix, possibly across several retry attempts) plausibly benefits from
exploring different approaches, so intentionally left at API default. Wired into
`agent_runtime.py`'s `run_agent_loop` as an opt-in `temperature` parameter applied to
every call in a session (not just the first, so it holds across a continued multi-round
conversation) -- `tester.py` passes 0.0, `coder.py` passes nothing.

Next real (quota-costing) steps, in order: finish the temperature A/B comparison,
decide the k-of-N-runs reporting methodology given the confirmed reproduce-gate
variance, re-validate the 10 known tickets with all of today's fixes in place, then the
full 25-ticket run.

## 2026-08-18 — temperature confirmed on the real task; third harness bug found (duplicate-block false positive)

Finished the temperature comparison from yesterday: a generic "write a creative
sentence" prompt showed high variance even at `temperature=0.0` (not a useful proxy),
but re-running the actual reproduce-gate variance probe on `1357`/`1424`/`2936` with
`temperature=0.0` wired in showed dramatic, real improvement on 2 of 3 tickets (`1357`
2/5->5/5, `1424` 0/5->4/5) -- confirming both the temperature fix and yesterday's
round-boundary hypothesis at once.

`2936` got *worse* (0/5) under the same fix, which led to a third real harness bug:
`run_written_test` takes zero parameters, so every call is indistinguishable from the
duplicate-blocker's point of view -- it was silently rejecting every re-check after the
first one *per round*, even though the test file had genuinely changed via `write_test`
in between. Confirmed by replaying from scratch before touching any code. Fixed with a
`no_duplicate_check_tools` exemption parameter on `agent_runtime.py`'s `run_agent_loop`,
scoped to `run_written_test` only. With the fix, `2936` returned to 1/5 -- its original
baseline, not an improvement. Unlike the other two, this ticket appears to be genuinely
hard for this model within a 20-call budget, not a mechanical artifact -- worth
revisiting with Phase 4's Planner rather than more Tester-side fixes.

Full comparison table and reasoning now in `RESULTS.md`'s reproduce-gate variance
section. Three real harness bugs found and fixed during Phase 3 validation total
(regression-blindness, empty-suite-read-as-clean, this duplicate-block false positive)
-- all caught by validating on known cases before trusting a broad run, none by the
broad run itself.
