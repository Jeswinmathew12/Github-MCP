"""GitHub MCP Server.

Exposes a small set of GitHub read-only tools over the Model Context Protocol
(stdio transport) so an LLM client such as Claude Desktop can inspect any
public (or token-accessible) GitHub repository.

Auth: reads GITHUB_TOKEN from the environment. A token is optional for public
repos but strongly recommended (unauthenticated requests are limited to 60/hr).
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timedelta, timezone

import requests
from mcp.server.mcpserver import MCPServer

GITHUB_API = "https://api.github.com"

mcp = MCPServer(
    name="github",
    instructions=(
        "Read-only GitHub tools. All tools take a `repo` argument in "
        "'owner/name' form, e.g. 'anthropics/anthropic-sdk-python'."
    ),
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-mcp-server",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# Transient-failure policy. Retrying a GET is safe (it is idempotent), so we
# retry the failures that a second attempt can actually fix: network blips,
# 5xx responses, and GitHub's *secondary* (abuse-detection) rate limit, which
# clears in seconds. The primary hourly limit is deliberately excluded — its
# reset can be an hour away, far longer than a tool call should block a model.
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0  # seconds, doubled each attempt
RETRY_MAX_DELAY = 20.0  # cap, applied to backoff and to Retry-After alike
RETRYABLE_STATUS = (500, 502, 503, 504)


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter: roughly 1s, 2s, 4s for attempts 0, 1, 2.

    The jitter keeps concurrent tool calls from retrying in lockstep and
    hammering the API at the same instant.
    """
    delay = RETRY_BASE_DELAY * (2 ** attempt)
    return min(delay + random.uniform(0, delay * 0.1), RETRY_MAX_DELAY)


def _retry_after(resp) -> float | None:
    """Seconds to wait per the response's Retry-After header, if usable."""
    value = (resp.headers.get("Retry-After") or "").strip()
    return min(float(value), RETRY_MAX_DELAY) if value.isdigit() else None


def _is_primary_rate_limit(resp) -> bool:
    """True when the hourly quota is exhausted — waiting it out is not viable."""
    return (
        resp.status_code in (403, 429)
        and resp.headers.get("X-RateLimit-Remaining") == "0"
        and _retry_after(resp) is None
    )


def _is_secondary_rate_limit(resp) -> bool:
    """True for a short abuse-detection limit, which is worth retrying.

    GitHub signals these either with a Retry-After header or with a message
    body naming the secondary limit, while hourly quota usually remains.
    """
    if resp.status_code not in (403, 429):
        return False
    if _retry_after(resp) is not None:
        return True
    try:
        payload = resp.json()
    except ValueError:
        return False
    message = payload.get("message", "") if isinstance(payload, dict) else ""
    return "secondary rate limit" in message.lower() or "abuse" in message.lower()


def _status_error(resp, path: str) -> GitHubError:
    """Map a non-200 response onto a GitHubError the model can act on."""
    if resp.status_code == 404:
        return GitHubError(
            f"GitHub returned 404 for {path}. The repository may not exist, "
            "may be private (set GITHUB_TOKEN), or the name may be misspelled. "
            "Repo names must be in 'owner/name' form."
        )
    if resp.status_code == 401:
        return GitHubError(
            "GitHub rejected the credentials (401). The GITHUB_TOKEN is invalid or expired."
        )
    if _is_primary_rate_limit(resp):
        reset = resp.headers.get("X-RateLimit-Reset", "")
        when = ""
        if reset.isdigit():
            when = f" Limit resets at {datetime.fromtimestamp(int(reset), tz=timezone.utc):%H:%M UTC}."
        return GitHubError(
            "GitHub API rate limit exceeded." + when
            + " Set a GITHUB_TOKEN environment variable to raise the limit from 60 to 5000 requests/hour."
        )
    if _is_secondary_rate_limit(resp):
        return GitHubError(
            "GitHub applied a secondary rate limit (abuse detection) and the "
            "request kept failing. Slow down and try again shortly."
        )
    return GitHubError(f"GitHub API error {resp.status_code} for {path}: {resp.text[:200]}")


def _gh_get(path: str, params: dict | None = None) -> dict | list:
    """GET a GitHub API path, retrying transient failures with backoff.

    Network errors, 5xx responses, and secondary rate limits are retried up to
    MAX_ATTEMPTS times; a Retry-After header, when GitHub sends one, overrides
    the computed backoff. Failures a retry cannot fix — 404, 401, a bad repo
    name, the primary hourly rate limit — are raised immediately.

    Raises GitHubError with an LLM-readable message.
    """
    last_error: GitHubError | None = None

    for attempt in range(MAX_ATTEMPTS):
        final_attempt = attempt == MAX_ATTEMPTS - 1

        try:
            resp = requests.get(
                f"{GITHUB_API}{path}", headers=_headers(), params=params, timeout=15
            )
        except requests.RequestException as exc:
            last_error = GitHubError(f"Network error talking to GitHub: {exc}")
            if final_attempt:
                break
            time.sleep(_backoff_delay(attempt))
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code in RETRYABLE_STATUS or _is_secondary_rate_limit(resp):
            last_error = _status_error(resp, path)
            if final_attempt:
                break
            time.sleep(_retry_after(resp) or _backoff_delay(attempt))
            continue

        raise _status_error(resp, path)

    # Every attempt failed on something transient.
    raise GitHubError(f"{last_error} (gave up after {MAX_ATTEMPTS} attempts)")


class GitHubError(Exception):
    """A GitHub API failure with a message suitable for showing to the model."""


def _validate_repo(repo: str) -> str:
    repo = repo.strip().strip("/")
    if repo.count("/") != 1 or not all(part for part in repo.split("/")):
        raise GitHubError(
            f"Invalid repo name '{repo}'. Expected 'owner/name', e.g. 'python/cpython'."
        )
    return repo


def _issue_summary(item: dict) -> dict:
    return {
        "number": item["number"],
        "title": item["title"],
        "author": (item.get("user") or {}).get("login", "unknown"),
        "labels": [lbl["name"] for lbl in item.get("labels", [])],
        "created_at": item.get("created_at"),
        "url": item.get("html_url"),
    }


# ---------------------------------------------------------------------------
# Tools (plain functions; registered with MCP at the bottom of the file so the
# test script can import and call them directly)
# ---------------------------------------------------------------------------

def list_open_issues(repo: str, limit: int = 10) -> dict:
    """List the most recently updated open issues in a GitHub repository.

    Returns for each issue: its number, title, author username, labels,
    creation date, and a browser URL. Pull requests are excluded — use
    list_open_prs for those. Use this when the user asks what issues, bugs,
    or feature requests are currently open in a repo.

    Args:
        repo: Repository in 'owner/name' form, e.g. 'python/cpython'.
        limit: Maximum number of issues to return (default 10, max 100).
    """
    try:
        repo = _validate_repo(repo)
        limit = max(1, min(int(limit), 100))
        items = _gh_get(
            f"/repos/{repo}/issues",
            {"state": "open", "per_page": 100, "sort": "updated"},
        )
    except GitHubError as exc:
        return {"error": str(exc)}
    # The issues endpoint also returns PRs; filter them out.
    issues = [i for i in items if "pull_request" not in i][:limit]
    return {"repo": repo, "open_issues": [_issue_summary(i) for i in issues],
            "count": len(issues)}


def list_open_prs(repo: str, limit: int = 10) -> dict:
    """List the most recently updated open pull requests in a GitHub repository.

    Returns for each PR: its number, title, author username, labels, creation
    date, source branch, and a browser URL. Use this when the user asks what
    changes or PRs are currently in flight or awaiting review in a repo.

    Args:
        repo: Repository in 'owner/name' form, e.g. 'python/cpython'.
        limit: Maximum number of PRs to return (default 10, max 100).
    """
    try:
        repo = _validate_repo(repo)
        limit = max(1, min(int(limit), 100))
        prs = _gh_get(
            f"/repos/{repo}/pulls",
            {"state": "open", "per_page": limit, "sort": "updated", "direction": "desc"},
        )
    except GitHubError as exc:
        return {"error": str(exc)}
    results = []
    for pr in prs[:limit]:
        summary = _issue_summary(pr)
        summary["branch"] = (pr.get("head") or {}).get("ref")
        summary["draft"] = pr.get("draft", False)
        results.append(summary)
    return {"repo": repo, "open_prs": results, "count": len(results)}


def get_issue(repo: str, issue_number: int) -> dict:
    """Get the full detail of one GitHub issue, including its body and comments.

    Returns the issue's title, state, author, labels, full body text, and up
    to 30 comments (each with author, date, and body). Use this to answer
    questions about a specific issue's content or discussion — e.g. after
    list_open_issues surfaced an interesting issue number, or when the user
    references an issue by number.

    Args:
        repo: Repository in 'owner/name' form, e.g. 'python/cpython'.
        issue_number: The issue number, e.g. 42.
    """
    try:
        repo = _validate_repo(repo)
        issue = _gh_get(f"/repos/{repo}/issues/{int(issue_number)}")
        comments = _gh_get(
            f"/repos/{repo}/issues/{int(issue_number)}/comments", {"per_page": 30}
        )
    except GitHubError as exc:
        return {"error": str(exc)}
    return {
        "repo": repo,
        "number": issue["number"],
        "title": issue["title"],
        "state": issue["state"],
        "author": (issue.get("user") or {}).get("login", "unknown"),
        "labels": [lbl["name"] for lbl in issue.get("labels", [])],
        "created_at": issue.get("created_at"),
        "url": issue.get("html_url"),
        "is_pull_request": "pull_request" in issue,
        "body": issue.get("body") or "(no description)",
        "comments": [
            {
                "author": (c.get("user") or {}).get("login", "unknown"),
                "created_at": c.get("created_at"),
                "body": c.get("body") or "",
            }
            for c in comments
        ],
    }


def search_repo_activity(repo: str, since_days: int = 7) -> dict:
    """Summarize recent activity in a GitHub repository over the last N days.

    Returns three lists covering the time window: recent commits (sha, author,
    message, date), issues opened or closed, and pull requests opened or
    closed. Use this when the user asks what's been happening in a repo
    lately, whether a project is active, or for a summary of recent changes.

    Args:
        repo: Repository in 'owner/name' form, e.g. 'python/cpython'.
        since_days: Size of the lookback window in days (default 7, max 90).
    """
    try:
        repo = _validate_repo(repo)
        since_days = max(1, min(int(since_days), 90))
    except GitHubError as exc:
        return {"error": str(exc)}
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        commits = _gh_get(
            f"/repos/{repo}/commits", {"since": since_iso, "per_page": 30}
        )
        # 'issues' endpoint with state=all&since=... returns both issues and PRs
        # updated in the window; we split and keep only opened/closed ones.
        updated = _gh_get(
            f"/repos/{repo}/issues",
            {"state": "all", "since": since_iso, "per_page": 100, "sort": "updated"},
        )
    except GitHubError as exc:
        return {"error": str(exc)}

    def _in_window(ts: str | None) -> bool:
        return bool(ts) and ts >= since_iso

    issues, prs = [], []
    for item in updated:
        opened = _in_window(item.get("created_at"))
        closed = item.get("state") == "closed" and _in_window(item.get("closed_at"))
        if not (opened or closed):
            continue  # updated in window, but neither opened nor closed in it
        summary = _issue_summary(item)
        summary["state"] = item.get("state")
        summary["activity"] = "opened" if opened else "closed"
        (prs if "pull_request" in item else issues).append(summary)

    return {
        "repo": repo,
        "window_days": since_days,
        "since": since_iso,
        "recent_commits": [
            {
                "sha": c["sha"][:10],
                "author": ((c.get("commit") or {}).get("author") or {}).get("name", "unknown"),
                "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
                "message": ((c.get("commit") or {}).get("message") or "").split("\n")[0],
                "url": c.get("html_url"),
            }
            for c in commits
        ],
        "issues_opened_or_closed": issues,
        "prs_opened_or_closed": prs,
        "commit_count": len(commits),
    }


# Register the plain functions as MCP tools. Registering this way (instead of
# stacking decorators) keeps the module attributes as ordinary callables so
# test_tools.py can invoke them directly.
mcp.tool()(list_open_issues)
mcp.tool()(list_open_prs)
mcp.tool()(get_issue)
mcp.tool()(search_repo_activity)


if __name__ == "__main__":
    mcp.run()  # stdio transport (default) — what Claude Desktop expects
