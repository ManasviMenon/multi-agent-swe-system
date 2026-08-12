"""Builds data/tickets/ from real marshmallow-code/marshmallow history.

For each merged PR that closes a GitHub issue and touches both a src/marshmallow/*.py
file and a tests/*.py file, writes:
  data/tickets/marshmallow-<issue_number>/
    issue.md          the original issue title + body (what an agent sees)
    gold_patch.diff     the real fix, source files only (reference, never shown to agents)
    test_patch.diff      the real fix's test-only diff (the verifier eval/run_eval.py applies)
    base_commit.txt       commit to check out before applying any patch

Requires: gh CLI authenticated with repo read access.
Usage: python scripts/curate_tickets.py
"""

import json
import os
import re
import subprocess
import time

REPO = "marshmallow-code/marshmallow"
OUT_DIR = "data/tickets"
MAX_FILES_PER_PR = 6
PR_HISTORY_PAGES = 20  # 50 PRs/page

PR_QUERY = """
query($cursor: String) {
  repository(owner: "marshmallow-code", name: "marshmallow") {
    pullRequests(first: 50, states: MERGED, orderBy: {field: CREATED_AT, direction: DESC}, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        mergeCommit { oid }
        closingIssuesReferences(first: 3) { nodes { number title } }
        files(first: 30) { nodes { path } }
      }
    }
  }
}
"""


def gh(args, retries=4):
    for attempt in range(retries):
        out = subprocess.run(
            ["gh"] + args, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if out.returncode == 0:
            return out.stdout
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"gh {args} failed: {out.stderr}")


def fetch_merged_prs():
    all_prs = []
    cursor = None
    for _ in range(PR_HISTORY_PAGES):
        args = ["api", "graphql", "-f", f"query={PR_QUERY}"]
        args += ["-f", f"cursor={cursor}"] if cursor else ["-F", "cursor="]
        data = json.loads(gh(args))
        conn = data["data"]["repository"]["pullRequests"]
        all_prs.extend(conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return all_prs


def filter_candidates(prs):
    candidates = []
    for pr in prs:
        issues = pr["closingIssuesReferences"]["nodes"]
        if not issues:
            continue
        paths = [n["path"] for n in pr["files"]["nodes"]]
        has_src = any(p.startswith("src/marshmallow/") and p.endswith(".py") for p in paths)
        has_test = any(p.startswith("tests/") and p.endswith(".py") for p in paths)
        scoped = len([p for p in paths if p not in ("CHANGELOG.rst", "AUTHORS.rst")]) <= MAX_FILES_PER_PR
        if has_src and has_test and scoped and pr["mergeCommit"]:
            candidates.append(
                {
                    "number": pr["number"],
                    "merge_commit": pr["mergeCommit"]["oid"],
                    "issue_number": issues[0]["number"],
                }
            )
    return candidates


def split_diff(diff_text):
    """Split a unified diff into (source_chunks, test_chunks), dropping changelog noise."""
    chunks = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    src, test = [], []
    for c in chunks:
        if not c.strip():
            continue
        first_line = c.splitlines()[0]
        if "tests/" in first_line or "conftest.py" in first_line:
            test.append(c)
        elif "CHANGELOG.rst" in first_line or "AUTHORS.rst" in first_line:
            continue
        else:
            src.append(c)
    return "".join(src), "".join(test)


ADDED_TEST_DEF_RE = re.compile(r"^\+[ \t]*def (test_\w+)[ \t]*\(", re.MULTILINE)
REMOVED_TEST_DEF_RE = re.compile(r"^-[ \t]*def (test_\w+)[ \t]*\(", re.MULTILINE)


def has_net_new_test(test_diff_text):
    """True only if the test patch adds a genuinely new test function.

    A name appearing on both a '+def name(' and a '-def name(' line is a
    signature/body edit to an EXISTING test (e.g. a type-annotation tweak or a
    changed assertion), not a new one -- eval/run_eval.py has no baseline for
    "does this test already exist in some form", so it can't be used as a
    FAIL_TO_PASS target. Tickets that only modify existing tests (new
    parametrize cases, changed expected values) are excluded here rather than
    forced through a methodology that can't verify them.
    """
    added = set(ADDED_TEST_DEF_RE.findall(test_diff_text))
    removed = set(REMOVED_TEST_DEF_RE.findall(test_diff_text))
    return bool(added - removed)


def build_ticket(candidate):
    num = candidate["number"]
    issue_num = candidate["issue_number"]
    merge_sha = candidate["merge_commit"]
    ticket_id = f"marshmallow-{issue_num}"
    tdir = os.path.join(OUT_DIR, ticket_id)

    commit = json.loads(gh(["api", f"repos/{REPO}/commits/{merge_sha}"]))
    parents = commit.get("parents", [])
    if not parents:
        return None, "no parent commit"
    base_commit = parents[0]["sha"]

    issue = json.loads(gh(["api", f"repos/{REPO}/issues/{issue_num}"]))
    diff_text = gh(["pr", "diff", str(num), "--repo", REPO])
    src_diff, test_diff = split_diff(diff_text)
    if not src_diff.strip() or not test_diff.strip():
        return None, "empty split"
    if not has_net_new_test(test_diff):
        return None, "no net-new test function (modifies existing tests only)"

    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "issue.md"), "w", encoding="utf-8") as f:
        f.write(f"# {issue['title']}\n\n{issue.get('body') or '(no body)'}\n")
    with open(os.path.join(tdir, "gold_patch.diff"), "w", encoding="utf-8", newline="\n") as f:
        f.write(src_diff)
    with open(os.path.join(tdir, "test_patch.diff"), "w", encoding="utf-8", newline="\n") as f:
        f.write(test_diff)
    with open(os.path.join(tdir, "base_commit.txt"), "w", encoding="utf-8") as f:
        f.write(base_commit + "\n")

    return {
        "ticket_id": ticket_id,
        "pr_number": num,
        "issue_number": issue_num,
        "title": issue["title"],
        "base_commit": base_commit,
    }, None


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    prs = fetch_merged_prs()
    candidates = filter_candidates(prs)
    print(f"{len(candidates)} candidates out of {len(prs)} merged PRs")

    manifest = []
    for c in candidates:
        entry, err = build_ticket(c)
        if entry:
            manifest.append(entry)
            print(f"OK   {entry['ticket_id']} (PR #{entry['pr_number']})")
        else:
            print(f"SKIP PR#{c['number']}: {err}")

    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nBuilt {len(manifest)} tickets -> {OUT_DIR}/manifest.json")


if __name__ == "__main__":
    main()
