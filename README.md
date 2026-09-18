# GitHub MCP Server

[![tests](https://github.com/Jeswinmathew12/Github-MCP/actions/workflows/test.yml/badge.svg)](https://github.com/Jeswinmathew12/Github-MCP/actions/workflows/test.yml)

A small, working [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes read-only GitHub tools to an LLM client such as Claude Desktop. Built with the official `mcp` Python SDK (stdio transport) and direct GitHub REST calls via `requests`.

The design goal is a handful of tools that work end-to-end rather than a large stubbed-out surface: an LLM can browse a repo's open issues and PRs, drill into a specific issue's discussion, and summarize recent activity.

## Tools

| Tool | What it returns |
|---|---|
| `list_open_issues(repo, limit=10)` | Open issues with number, title, author, labels, URL |
| `list_open_prs(repo, limit=10)` | Open PRs with the same fields plus source branch and draft status |
| `get_issue(repo, issue_number)` | Full issue detail: body text plus up to 30 comments |
| `search_repo_activity(repo, since_days=7)` | Recent commits, and issues/PRs opened or closed in the window |

All tools take `repo` in `owner/name` form (e.g. `python/cpython`). GitHub API errors (bad repo names, 404s, rate limits, bad tokens) are caught and returned as a readable `error` message the model can act on, never a stack trace.

## Handling failure

Every request goes through one helper, `_gh_get`, which retries the failures a second attempt can actually fix and fails fast on the ones it can't:

| Failure | Behaviour | Why |
|---|---|---|
| Network error (DNS, reset, timeout) | Retry, up to 3 attempts | Usually a blip |
| `500` / `502` / `503` / `504` | Retry, up to 3 attempts | GitHub-side, transient; a GET is idempotent so replaying it is safe |
| Secondary rate limit (abuse detection) | Retry, honouring `Retry-After` | Clears in seconds |
| **Primary** hourly rate limit | Fail immediately | Reset can be an hour away — far longer than a tool call should block the model |
| `404`, `401`, malformed repo name | Fail immediately | Retrying cannot change the answer |

Backoff is exponential with jitter (~1s, ~2s, ~4s), capped at 20s, and a server-supplied `Retry-After` overrides the computed delay. When GitHub sends one, we listen to it rather than guessing. The jitter matters when several tool calls fail at once: without it they retry in lockstep and hit the API in a thundering herd.

Whatever the outcome, the caller sees a readable `error` string it can act on — never a stack trace, and never a silent hang.

## Setup

Requires Python 3.10+.

```powershell
pip install -r requirements.txt
```

Optionally set a GitHub token — not required for public repos, but it raises the API rate limit from 60 to 5,000 requests/hour and enables private repos your token can see:

```powershell
# PowerShell (current session)
$env:GITHUB_TOKEN = "ghp_your_token_here"
```

The token is only ever read from the environment — never hardcoded or written to disk.

## Verify it works (before touching Claude Desktop)

Two suites, for two different questions.

**Unit tests** — the GitHub API is mocked, so they need no network and no token. This is what CI runs on every push:

```powershell
pip install pytest
pytest test_server.py
```

**Live smoke test** — calls each tool function directly against a real public repo and also exercises the error paths, so you can confirm the GitHub integration works before wiring the server into Claude Desktop:

```powershell
python test_live.py                        # defaults to modelcontextprotocol/python-sdk
python test_live.py owner/some-other-repo  # or pick your own
```

You should see `6/6 checks passed`.

## Register in Claude Desktop

1. Open the config file (create it if missing):
   - **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
   - **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

2. Add the server under `mcpServers`, using absolute paths for both the Python interpreter and `server.py`:

```json
{
  "mcpServers": {
    "github": {
      "command": "C:\\Users\\jeswi\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe",
      "args": ["C:\\Users\\jeswi\\OneDrive\\Documents\\Resume Project\\Github-MCP\\server.py"],
      "env": {
        "GITHUB_TOKEN": "ghp_your_token_here"
      }
    }
  }
}
```

   The `env` block is how Claude Desktop passes the token to the server process; omit it entirely for anonymous access to public repos.

   > **Use the real interpreter path, not the Windows Store alias.** `(Get-Command python).Source` often returns `...\WindowsApps\python.exe`, which is a shim that fails when launched as a subprocess. Get the true path with:
   >
   > ```powershell
   > python -c "import sys; print(sys.executable)"
   > ```

3. Fully quit and restart Claude Desktop (system tray → Quit, not just closing the window).

4. Confirm the connection: in a new chat, the tools icon (below the message box) should list the four `github` tools.

## Example prompts

Prompts that naturally trigger each tool:

- *"What are the open issues in `modelcontextprotocol/python-sdk` right now?"* → `list_open_issues`
- *"Are there any open pull requests in `pallets/flask`? Which ones are drafts?"* → `list_open_prs`
- *"Summarize the discussion on issue #3307 in `modelcontextprotocol/python-sdk`."* → `get_issue`
- *"How active has `python/cpython` been in the last two weeks? What got merged or closed?"* → `search_repo_activity`
- *"Look at recent activity in `anthropics/anthropic-sdk-python` and tell me if any of the newly opened issues look like duplicates of each other."* → `search_repo_activity` + `get_issue`

## How it works

```
Claude Desktop ── stdio (JSON-RPC / MCP) ──> server.py ── HTTPS ──> api.github.com
```

- `server.py` registers four plain Python functions as MCP tools; their docstrings become the tool descriptions the LLM reads when deciding what to call.
- The server runs over **stdio**: Claude Desktop launches it as a subprocess and speaks MCP over stdin/stdout. No ports, no web server, no persistence.
- Tool results are returned as structured JSON, so the model gets fields (number, author, labels, URL) rather than prose to parse.

## Scope

Deliberately excluded: write operations (creating issues, commenting), OAuth flows, a web UI, and any database. This is a clean local demo of the MCP tool-serving pattern.
