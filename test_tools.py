"""Smoke test: call each tool function directly against a real public repo.

This bypasses the MCP transport entirely — it just exercises the same Python
functions the MCP server exposes as tools, so you can verify the GitHub
integration works before wiring the server into Claude Desktop.

Usage:
    python test_tools.py [owner/repo]

Defaults to 'modelcontextprotocol/python-sdk' (public, active).
"""

import json
import sys

from server import get_issue, list_open_issues, list_open_prs, search_repo_activity

REPO = sys.argv[1] if len(sys.argv) > 1 else "modelcontextprotocol/python-sdk"


def show(title: str, result: dict) -> bool:
    ok = "error" not in result
    print(f"\n{'='*70}\n{'PASS' if ok else 'FAIL'}  {title}\n{'='*70}")
    print(json.dumps(result, indent=2)[:2500])
    return ok


def main() -> int:
    failures = 0

    r1 = list_open_issues(REPO, limit=5)
    failures += not show(f"list_open_issues({REPO!r}, limit=5)", r1)

    r2 = list_open_prs(REPO, limit=5)
    failures += not show(f"list_open_prs({REPO!r}, limit=5)", r2)

    # Pick a real issue number from the first call if available, else fall back
    issues = r1.get("open_issues") or []
    number = issues[0]["number"] if issues else 1
    r3 = get_issue(REPO, number)
    failures += not show(f"get_issue({REPO!r}, {number})", r3)

    r4 = search_repo_activity(REPO, since_days=7)
    failures += not show(f"search_repo_activity({REPO!r}, since_days=7)", r4)

    # Error handling checks — these SHOULD return an error message, not raise
    r5 = list_open_issues("this-owner-does-not-exist-xyz/nope")
    ok5 = "error" in r5
    print(f"\n{'PASS' if ok5 else 'FAIL'}  404 handling -> {r5.get('error', r5)!s:.120}")
    failures += not ok5

    r6 = list_open_issues("not-a-valid-repo-name")
    ok6 = "error" in r6
    print(f"{'PASS' if ok6 else 'FAIL'}  bad-name handling -> {r6.get('error', r6)!s:.120}")
    failures += not ok6

    print(f"\n{'-'*70}\n{6 - failures}/6 checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
