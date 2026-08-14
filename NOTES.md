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
