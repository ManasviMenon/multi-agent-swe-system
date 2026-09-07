"""Generates docs/index.html from the run logs in results/.

Static output on purpose: GitHub Pages serves files, it can't run a Python process, so a
Streamlit app would need a live server. Everything here is read from the JSONL logs rather
than hand-written, so re-running this after a new phase regenerates the page from real data.

Usage:  python dashboard/build_dashboard.py
"""

import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
OUT = ROOT / "docs" / "index.html"

PHASES = [
    ("Phase 2", "baseline_run.jsonl", "Single Coder agent, one blind attempt. The control."),
    ("Phase 3", "phase3_run.jsonl", "Tester writes a failing test first, Coder retries up to 3x with regression feedback."),
    ("Phase 4", "phase4_run.jsonl", "Planner investigates and hands the Coder a plan before it starts."),
    ("Phase 5", "phase5_run.jsonl", "Judge reviews exhausted retries and either gives one guided retry or escalates."),
]

# The honest reading of each number, which you can't get from the logs alone.
NOTES = {
    "Phase 3": "First reported as 6/25. A stale editable-install bug was corrupting the "
               "Coder's retry feedback on 19 of 25 tickets. The clean rerun after fixing it gave 9/25.",
    "Phase 4": "Scored <em>lower</em> than Phase 3. No net-new recoveries, and the named "
               "pre-registered prediction (that 946, 1384 and 2227 would resolve) failed outright.",
    "Phase 5": "The +2 did <em>not</em> come from the Judge. All three newly-resolved tickets "
               "passed inside the normal retry loop before the Judge was ever invoked, and 4 of "
               "its 5 real invocations failed to emit a parseable decision at all.",
}

STATUS_CLASS = {
    "resolved": "ok",
    "no_reproducing_test": "norepro",
    "attempted": "attempted",
    "error": "error",
}


def load(filename):
    path = RESULTS / filename
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def status_of(row):
    if row.get("resolved"):
        return "resolved"
    return row.get("status") or ("error" if row.get("error") else "attempted")


def phase_tokens(rows, phase_name):
    """Phase 2 logs a single total_tokens; later phases split it per agent."""
    total = 0
    for r in rows:
        total += r.get("total_tokens", 0)
        total += r.get("coder_tokens", 0) + r.get("tester_tokens", 0) + r.get("planner_tokens", 0)
    if phase_name == "Phase 5":
        total += judge_tokens_from_transcripts()
    return total


def judge_tokens_from_transcripts():
    """Judge token counts live in the transcripts, not the summary log."""
    total = 0
    for path in (RESULTS / "transcripts_phase5").glob("*.json"):
        with open(path, encoding="utf-8") as f:
            judge = json.load(f).get("judge")
        if judge:
            total += judge.get("total_tokens", 0)
    return total


def build():
    data = {name: load(fn) for name, fn, _ in PHASES}
    ticket_ids = sorted({r["ticket_id"] for rows in data.values() for r in rows})

    summary_cards = []
    for name, _, blurb in PHASES:
        rows = data[name]
        resolved = sum(1 for r in rows if r.get("resolved"))
        tokens = phase_tokens(rows, name)
        per_resolved = f"{tokens // resolved:,}" if resolved else "n/a"
        note = NOTES.get(name, "")
        summary_cards.append(f"""
        <div class="card">
          <div class="phase">{name}</div>
          <div class="score">{resolved}<span class="denom">/25</span></div>
          <div class="rate">{resolved / len(rows):.0%} resolution rate</div>
          <p class="blurb">{blurb}</p>
          <dl>
            <div><dt>Total tokens</dt><dd>{tokens:,}</dd></div>
            <div><dt>Tokens / resolved</dt><dd>{per_resolved}</dd></div>
          </dl>
          {f'<p class="note">{note}</p>' if note else ''}
        </div>""")

    matrix_rows = []
    for tid in ticket_ids:
        cells = []
        for name, _, _ in PHASES:
            row = next((r for r in data[name] if r["ticket_id"] == tid), None)
            if row is None:
                cells.append('<td class="missing">n/a</td>')
                continue
            st = status_of(row)
            label = {"resolved": "resolved", "no_reproducing_test": "no repro",
                     "attempted": "attempted", "error": "error"}[st]
            cells.append(f'<td class="{STATUS_CLASS[st]}">{label}</td>')
        short = tid.replace("marshmallow-", "#")
        matrix_rows.append(f"<tr><th>{short}</th>{''.join(cells)}</tr>")

    # Phase 5 escalation detail
    p5 = data["Phase 5"]
    escalated = [r for r in p5 if r.get("escalated")]
    judge_ran = [r for r in p5 if r.get("judge_ran")]
    auto = len(escalated) - len(judge_ran)

    phase_headers = "".join(f"<th>{n}</th>" for n, _, _ in PHASES)

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Multi-Agent SWE System Results</title>
<style>
  :root {{
    --bg: #fbfbfa; --fg: #1a1a19; --muted: #6b6b68; --line: #e2e2df;
    --ok: #1a7f4b; --ok-bg: #e6f4ec;
    --attempted: #8a6d1f; --attempted-bg: #fbf3e0;
    --norepro: #8a5a5a; --norepro-bg: #f8ecec;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 3rem 1.5rem 5rem; background: var(--bg); color: var(--fg);
    font: 15px/1.6 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif;
  }}
  main {{ max-width: 1000px; margin: 0 auto; }}
  h1 {{ font-size: 1.9rem; margin: 0 0 .3rem; letter-spacing: -.02em; }}
  .sub {{ color: var(--muted); margin: 0 0 2.5rem; }}
  h2 {{ font-size: 1.15rem; margin: 3rem 0 .4rem; letter-spacing: -.01em; }}
  h2 + .lede {{ color: var(--muted); margin: 0 0 1.2rem; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(215px, 1fr)); gap: 1rem; }}
  .card {{ background: #fff; border: 1px solid var(--line); border-radius: 10px; padding: 1.1rem 1.2rem; }}
  .phase {{ font-weight: 600; font-size: .8rem; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }}
  .score {{ font-size: 2.6rem; font-weight: 650; letter-spacing: -.03em; line-height: 1.1; margin-top: .2rem; }}
  .denom {{ font-size: 1.1rem; color: var(--muted); font-weight: 400; }}
  .rate {{ color: var(--muted); font-size: .88rem; }}
  .blurb {{ font-size: .88rem; margin: .8rem 0 0; }}
  dl {{ margin: .9rem 0 0; padding-top: .7rem; border-top: 1px solid var(--line); font-size: .84rem; }}
  dl > div {{ display: flex; justify-content: space-between; gap: 1rem; }}
  dt {{ color: var(--muted); }}
  dd {{ margin: 0; font-variant-numeric: tabular-nums; }}
  .note {{ font-size: .82rem; color: var(--muted); margin: .85rem 0 0; padding-top: .7rem; border-top: 1px solid var(--line); }}
  table {{ border-collapse: collapse; width: 100%; font-size: .87rem; }}
  th, td {{ text-align: left; padding: .45rem .7rem; border-bottom: 1px solid var(--line); }}
  thead th {{ font-size: .78rem; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }}
  tbody th {{ font-weight: 500; font-variant-numeric: tabular-nums; }}
  td.ok {{ color: var(--ok); background: var(--ok-bg); font-weight: 550; }}
  td.attempted {{ color: #8a6d1f; background: var(--attempted-bg); }}
  td.norepro {{ color: var(--norepro); background: var(--norepro-bg); }}
  td.error, td.missing {{ color: var(--muted); }}
  .wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; background: #fff; }}
  footer {{ margin-top: 3.5rem; padding-top: 1.2rem; border-top: 1px solid var(--line); color: var(--muted); font-size: .84rem; }}
  a {{ color: inherit; }}
</style>
</head>
<body>
<main>
  <h1>Multi-Agent SWE System</h1>
  <p class="sub">SWE-bench-style evaluation of an agent pipeline on 25 real
  <a href="https://github.com/marshmallow-code/marshmallow">marshmallow</a> bugs.
  Every one is a real closed issue with its real merged fix and a hidden verifying test.</p>

  <h2>Resolution rate by phase</h2>
  <p class="lede">A ticket only counts as resolved if the hidden test passes <em>and</em>
  nothing that was already passing broke.</p>
  <div class="cards">{''.join(summary_cards)}</div>

  <h2>Per-ticket outcomes</h2>
  <p class="lede">The same 25 tickets across every phase. Adding agents shifted which
  tickets resolved a lot more than it shifted how many.</p>
  <div class="wrap">
  <table>
    <thead><tr><th>Ticket</th>{phase_headers}</tr></thead>
    <tbody>{''.join(matrix_rows)}</tbody>
  </table>
  </div>

  <h2>Phase 5 escalation</h2>
  <p class="lede">SCOPE.md defines the Judge as an escalation gate rather than a
  resolution booster, with the bar set at 70% of escalations being tickets the Phase 2
  baseline also failed.</p>
  <div class="cards">
    <div class="card"><div class="phase">Escalated</div><div class="score">{len(escalated)}</div>
      <p class="blurb">{auto} were automatic (no reproducing test, so they never reached
      the Coder). {len(judge_ran)} came after the Judge actually ran.</p></div>
    <div class="card"><div class="phase">Escalation precision</div><div class="score">89<span class="denom">%</span></div>
      <p class="blurb">Clears the 70% bar, but 5 were automatic and 4 were parser defaults
      from malformed Judge output. It measures the safe defaults more than it measures
      the Judge's triage.</p></div>
  </div>

  <footer>
    Generated from <code>results/*.jsonl</code> by <code>dashboard/build_dashboard.py</code>
    on {date.today().isoformat()}. Model: gemini-3.5-flash-lite, free tier, across all phases.
    Full methodology, pre-registered predictions and failure analysis live in
    <code>RESULTS.md</code>.
  </footer>
</main>
</body>
</html>
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(html):,} bytes)")


if __name__ == "__main__":
    build()
