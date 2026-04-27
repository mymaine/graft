# graft v{{VERSION}}

> Self-healing HTTP API harness. You write the helpers. Git is the memory.

## What graft is

graft is a thin Python harness that lets you (the agent) accumulate HTTP API
capabilities as plain `helpers/<service>.py` files. Each helper is a normal
Python function, not a tool schema. The next time anyone uses this graft repo,
the helper is already there — you don't relearn the API from docs.

The flow is:

```
[you, the agent]
      ↓ python import
helpers/<service>.py        ← you author and edit these
      ↓ function call
graft daemon (localhost)    ← maintains http client, auth, retry, stats
      ↓ http
external service
```

graft is **not** a tool framework, prompt template, or planner. It does not
manage your control flow. It gives you raw Python and a thin HTTP relay.

## Discovery: read these in order each task

1. `SKILL.md` (this file) — the rules
2. `helpers/INDEX.md` — one line per existing service, sorted by usage
3. `helpers/<service>.py` — only the file for the service you actually need

Do not pre-read every helper. Token budget matters.

## Three branches when a task arrives

After reading `INDEX.md`:

- **Service exists, function exists** → import and call it
- **Service exists, function missing** → append the new function to the file
- **Service does not exist** → try `graft add` first (see below), then create `helpers/<service>.py` if not in registry

Use the canonical short service name (`github`, not `github_api`; `notion`,
not `notion_com`). Lowercase, no underscores unless the service's own brand
uses one.

### Before writing a new helper from scratch

Try `graft add <service>` first. The official registry currently covers
github / linear / notion / stripe (and grows). If `graft add` succeeds, you
skip the cold-start research entirely. If it returns "not in registry
manifest", that's expected for internal / niche / unreleased services —
proceed to write your own.

When you write or change `helpers/<service>.py`, graft auto-commits the change
to git when the helper is next imported (if `GRAFT_AUTOCOMMIT=1`, the default).
You don't run `git add` / `git commit` yourself.

Across Python sessions, helpers reload automatically — Claude Code typically
forks a fresh process per task, so you'll see your latest helper. Within the
same Python session, `sys.modules` caches the first import; you'd need to
restart the session (or remove the entry) to see edits.

## Calling a helper

```python
from helpers.github import list_issues

issues = list_issues("anthropics", "claude-code", state="open")
```

That's it. The daemon records the call, injects auth, and runs retries.

You do not configure auth. The user sets it via `.graft/auth.toml` or
`GRAFT_<SERVICE>_<KEY>` env vars before running the daemon. Your job is to
write helpers that call `request(...)` — auth injection happens inside the
daemon.

## Writing a helper

A complete first cut:

```python
"""GitHub REST + GraphQL helpers."""

from typing import Any, cast

from graft.context import request


def list_issues(owner: str, repo: str, state: str = "open", limit: int = 30) -> list[dict[str, Any]]:
    """List GitHub issues for a repository.

    Generalization:
        Works for any (owner, repo). Filter by state, limit count.
        Variant example: list_issues("python", "cpython", state="closed")
        Not applicable: GitHub Enterprise on custom domains.
    """
    return cast(
        list[dict[str, Any]],
        request(
            "github",
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": limit},
        ).json(),
    )
```

Four things this file does right:

1. The first line of the module docstring is a one-line description (used in
   `INDEX.md`).
2. Every public function has a `Generalization:` section in its docstring.
   The validator rejects the file otherwise.
3. The function takes `(owner, repo)` as arguments, not hardcoded values.
   See "Helper Design Principles" below.
4. Every public function has full type annotations on parameters and return
   type. The CI runs `mypy --strict` over `helpers/`, and the cold-start
   acceptance criterion runs it locally. Untyped helpers fail the gate.

### mypy --strict tips

`request(...).json()` returns `Any`. Declaring `-> list[X]` directly fails
with `no-any-return` because `Any` cannot implicitly narrow. Two patterns
pass strict:

- **`cast(...)`** (preferred — keeps type info for IDE and readers): wrap
  the `.json()` call as in the example above.
- **`-> Any`**: lossy but trivial; useful for one-off calls where the caller
  will narrow.

Skip `# type: ignore` unless mypy explicitly demands it; `unused-ignore`
also fails strict.

## Helper Design Principles (read this carefully)

A helper's value comes from being reusable across tasks. If you hardcode the
specific repo, user, or query the current task asks for, the helper turns into
a one-shot script and `helpers/` becomes a graveyard.

### What to generalize, what to fix

| Property | Treatment | Example |
|---|---|---|
| Service identity | Hardcode | base URL, auth scheme, response parser |
| Anything that varies between calls | Function parameter | repo name, user id, date range |
| Stable user-environment identity | env var + default param | company account id, team slug |
| Task assembly logic | **Do not write into helpers** | "fetch issues then post to slack" is script logic, not a helper |

### Naming

- ✅ `list_issues(owner: str, repo: str, state: str = "open")`
- ❌ `list_anthropics_issues()` — owner is hardcoded
- ❌ `get_my_repo()` — what is "my"?
- ❌ `fetch_team_data()` — which team? what data?

If the function name contains a specific account, owner, customer, or team,
it is over-fit. Rename and parameterize.

### Two-task check (do this before committing)

After writing the function, mentally run it against:

1. The current task (what the user just asked)
2. A *plausible variant of the same kind* (different repo, different state, etc.)

If (2) doesn't work, the function is over-fit. Refactor the parameters.

### Generalization docstring (mandatory format)

```python
def list_issues(owner: str, repo: str, state: str = "open") -> list[dict]:
    """One-line summary.

    Generalization:
        What this works for (the (owner, repo) shape).
        Variant example: a concrete second call site.
        Not applicable: edge cases that need a different helper.
    """
```

The validator scans for the literal string `Generalization:` in the docstring
of every public function. Missing it → the file is rejected and the daemon
returns a `HelperLoadError`. Three failures in a row → you get a full
positive-example template injected into the error. Five in a row →
`HelperLoadAborted`, and the user has to run `graft reset <service>` before
you can retry.

### Service-level vs task-level

- `helpers/<service>.py` = a service-level API wrapper. Generalize.
- One-shot task assembly ("fetch X, transform, send to Y") = the user's
  script, not a helper. Do **not** put task-specific glue in `helpers/`.

## Public API: graft.context

This is everything you import from graft:

```python
from graft.context import request, auth, Response
```

### request

```python
request(
    service: str,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
    json: Any = None,
    timeout: float | None = None,
) -> Response
```

Routes through the daemon. Auth is injected automatically. Returns a
`Response`.

### auth

```python
auth(service: str) -> str | None
```

Returns the configured token for `service`, or `None`. Use this only when you
need to put the token directly into a URL (rare — most services accept it as a
header, which `request` injects automatically).

### Response

A frozen dataclass:

- `status_code: int`
- `headers: dict[str, str]`
- `body: bytes`
- `.json() -> Any`
- `.text() -> str`

Do not `import httpx` in helpers. Use `Response` instead.

## Forbidden in helpers/

The validator rejects the entire file if any of these appear:

1. **Cross-service import**: `from helpers.notion import x` inside `helpers/github.py`. If you need to coordinate two services, that's task-level glue — write it in the user's script, not in `helpers/`.
2. **`import importlib`** or `from importlib import ...` — no dynamic imports.
3. **`__import__("...")`** — same reason.
4. **`exec(...)` / `eval(...)`** — no dynamic code execution.
5. **`import httpx`** — use `Response` from `graft.context` instead. v2 will
   reject. v1 trusts you not to import httpx — the wrap mechanism counts on
   you using `Response` instead.

The validator is lint-grade, not a sandbox. Do not try to work around it. The
rules exist so helpers stay self-contained and the auto-wrap stats stay
accurate.

## Errors and what to do

| Error | Source | Cause | Fix |
|---|---|---|---|
| `HelperLoadError` | daemon | validator rejected the file (missing `Generalization:`, forbidden import, syntax error) | Read the `reason` field, fix the file, save again |
| `HelperImportError` | daemon | `import helpers.X` failed at runtime (missing dep, real ImportError) | Add the missing dep to the user's project, or fix the import |
| `HelperLoadAborted` | daemon | 5 consecutive validator failures for this service | Tell the user to run `graft reset <service>`, then try again |
| `DaemonNotRunning` | client | daemon process is not up | Tell the user to run `graft serve` |

When a `HelperLoadError` arrives with a `template` field (third consecutive
failure), it contains a full positive-example helper. Mirror its structure.

## Stats: trust signals

Run `graft stats` to see how each helper has performed:

```
github     12 helpers   342 calls   last: 2026-04-26
linear      8 helpers   189 calls   last: 2026-04-26
notion      5 helpers    45 calls   last: 2026-04-20
```

If a helper has 200 calls and 0 errors, you can trust it. If it has 1 call
and 1 error, rewrite it. The stats are append-only JSONL at
`.graft/stats.jsonl`; the daemon records every wrapped function call.

## What graft does NOT do

- No tool schema (this is graft's whole point — you write Python, not JSON Schema)
- No agent framework, no prompt templates, no planner-executor
- No sandbox or security layer (v1 trusts the local environment)
- No GUI, no dashboard
- No cross-language clients (Python only)
- No vector store / semantic memory (git is the memory)
- No OAuth flow automation (the user provides the token)
- No streaming responses, multipart upload, or cross-call cookies in v1

If your task needs one of these, tell the user. Do not build it inside
`helpers/`.

## Common patterns

Examples below use GitHub because the API is well-documented and most agents
have seen it in training. The patterns apply to any HTTP API — Linear, Notion,
Stripe, your company's internal API. The shape is what matters, not the host.

### Authenticated GET

```python
def get_user(username: str) -> dict:
    """Fetch a GitHub user profile.

    Generalization:
        Works for any GitHub username.
        Variant: get_user("torvalds")
        Not applicable: GitHub Enterprise.
    """
    return request("github", "GET", f"https://api.github.com/users/{username}").json()
```

### POST with JSON body

```python
def create_issue(owner: str, repo: str, title: str, body: str = "") -> dict:
    """Create a GitHub issue.

    Generalization:
        Works for any (owner, repo) where the auth token has write access.
        Variant: create_issue("foo", "bar", "Track flaky test")
        Not applicable: locked repositories.
    """
    return request(
        "github",
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        json={"title": title, "body": body},
    ).json()
```

### Handling non-2xx

`request` returns the `Response` regardless of status. Inspect it:

```python
def get_repo(owner: str, repo: str) -> dict | None:
    """Fetch a repo, returning None on 404.

    Generalization:
        Any (owner, repo). 404 is treated as "not found" rather than an error.
        Variant: get_repo("foo", "missing-repo")
        Not applicable: rate-limit responses (caller should retry).
    """
    r = request("github", "GET", f"https://api.github.com/repos/{owner}/{repo}")
    if r.status_code == 404:
        return None
    return r.json()
```

### Pagination

```python
def list_all_issues(owner: str, repo: str, state: str = "open") -> list[dict]:
    """List all issues across paginated results.

    Generalization:
        Walks the `Link: rel="next"` header until exhausted.
        Variant: list_all_issues("python", "cpython", state="closed")
        Not applicable: cursor-based APIs (those need a different helper).
    """
    out: list[dict] = []
    url = f"https://api.github.com/repos/{owner}/{repo}/issues"
    params: dict = {"state": state, "per_page": 100}
    while url:
        r = request("github", "GET", url, params=params)
        out.extend(r.json())
        link = r.headers.get("link", "")
        nxt = next((p.split(";")[0].strip("<> ") for p in link.split(",") if 'rel="next"' in p), None)
        url = nxt
        params = {}  # next-link already includes query params
    return out
```

## When you're not sure

If a service requires something graft doesn't support yet (streaming, OAuth
flow, multipart upload), say so out loud and let the user decide. Don't
silently skip the rule or try to work around the validator. The rules are
the guardrails that make graft worth using next time.
