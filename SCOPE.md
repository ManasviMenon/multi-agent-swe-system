# SCOPE.md

Written before any agent code exists, per the project's own discipline: you can't tune what you haven't defined success for.

## Target codebase

**Source:** a self-curated, SWE-bench-lite-**style** dataset — real closed GitHub issues on a well-tested Python
repo, each paired with its actual merged fix PR and that PR's own test diff (which becomes our verifying test).
Note: this is *not* drawn from the published SWE-bench-lite HuggingFace dataset (verified 2026-08-09 — that
dataset's 11 repos are Django, Flask, sympy, scikit-learn, matplotlib, numpy/pandas-adjacent projects, etc.;
marshmallow is not among them). We're following the same methodology on a repo of our own choosing, not reusing
their data.

**Chosen repo:** `marshmallow-code/marshmallow` — a serialization/deserialization library, small surface area,
clean test suite, real closed-issue history on GitHub. Tradeoff accepted knowingly: its small surface area
means tickets may skew toward simpler single-file fixes, which could put our resolution rate on the optimistic
end of the 20–40% baseline range rather than the pessimistic end. `RESULTS.md` should call this out explicitly
rather than let a high number stand unexplained.

The 20–40 curated tickets go in `data/tickets/`, one directory per ticket:
```
data/tickets/<repo>-<issue_id>/
  issue.md          # original ticket text
  gold_patch.diff    # the real merged PR, for reference only (never shown to agents)
  test_patch.diff     # the test-only diff from the gold PR — this is what verifies success
  base_commit.txt      # commit SHA to check out before applying any patch
```

## Success metrics (numeric, measured by `eval/run_eval.py`)

| Metric | Target | Why this number |
|---|---|---|
| Resolution rate | ≥ 25% of held-out tickets produce a patch that passes `test_patch` | SWE-bench-lite leaderboards for single-agent baselines sit in the 20-40% range; claiming higher without evidence is not credible |
| Regression rate | 0 pre-existing passing tests broken by an accepted PR | This is the number that actually matters in production — a "fix" that breaks 3 other things is worse than no fix |
| Escalation precision | ≥ 70% of Judge escalations are tickets that a single-agent baseline (Phase 2) also failed | Proxy for "the Judge escalates genuinely hard cases," not cases it gave up on early |
| Cost per resolved ticket | tracked, no fixed target yet | Baseline unknown until Phase 2 runs; revisit after first full pipeline run |
| Agent-calls per ticket | tracked, no fixed target yet | Same — establish baseline before setting a ceiling |

Resolution/regression are measured against the **held-out set only** — never against tickets used while iterating on prompts, or the number is meaningless.

## Guardrails

1. **Isolation:** every ticket run operates on its own `git worktree` on a dedicated branch (`agent/<ticket-id>`). Agents never check out or touch the repo's default branch.
2. **Reversibility:** all agent actions are git-based (branch commits). No agent has filesystem access outside its worktree. No destructive git ops (`reset --hard`, `push --force`, branch deletion) are in any agent's tool set.
3. **No autonomous merge:** the Coder/Tester/Reviewer/Judge pipeline can only *open* a PR. Merging is a human action, always.
4. **Retry cap:** max 3 Coder attempts per ticket before mandatory escalation to a human review queue. No silent infinite loops.
5. **Role-restricted tools:** each agent's tool set is enforced in code (not just system-prompt instruction) — e.g. the Reviewer literally has no file-write tool available to it.
6. **Sandboxed execution:** any agent-generated code runs inside a container, never directly on the host. (Tracked separately — see environment note below.)

## Environment note (as of 2026-08-09)

Docker Desktop is installed but not yet functional — WSL2 needs to be enabled from an elevated shell (`wsl --install --no-distribution`, admin required) followed by a restart, which requires the user to do manually. Until that lands, Phase 1's eval harness uses a plain subprocess + venv per run for isolation (not a real security boundary — acceptable for now since ticket patches come from our own agents in a controlled dev loop, not untrusted input). This is a known gap, not a silent scope change: guardrail 6 stays a hard requirement before this system runs anything beyond curated dev/test tickets, and Docker sandboxing lands as originally planned once the environment is unblocked.
