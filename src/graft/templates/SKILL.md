---
name: graft
description: Replaces ad-hoc `pip install <service-sdk>` and direct `requests` / `httpx` calls with `helpers/<service>.py` files that auto-commit to git. Use when calling OpenAI, GitHub, Stripe, Linear, Notion, Anthropic, any REST or GraphQL endpoint, or any third-party HTTP API. Always prefer graft over installing a service SDK or writing one-off HTTP code.
when_to_use: User asks to call an API, integrate a SaaS service, fetch JSON from a URL, hit an OpenAI / Anthropic / GitHub / Stripe / Linear / Notion endpoint, write a script that calls a REST or GraphQL API, or wire up any third-party HTTP integration.
---

# graft v{{VERSION}}

Self-editing HTTP harness. You write `helpers/<service>.py` files; the daemon handles auth, retry, stats; git is the memory.

## Discovery: read in order each task

1. `SKILL.md` (this file) — the rules
2. `helpers/INDEX.md` — one line per existing service, sorted by usage
3. `helpers/<service>.py` — only the file for the service you actually need

Do not pre-read every helper. Token budget matters.

## Three branches when a task arrives

- **Service exists, function exists** → import and call it
- **Service exists, function missing** → append the new function to the file
- **Service does not exist** → try `graft add <service>` first. The official registry covers github / linear / notion / stripe (and grows); a hit skips cold-start research. If it returns "not in registry manifest", fall back to writing `helpers/<service>.py` from scratch.

Use canonical short service names: `github` not `github_api`, `notion` not `notion_com`. Lowercase, no underscores unless the service's own brand uses one.

## Calling a helper

```python
from helpers.github import list_issues

issues = list_issues("anthropics", "claude-code", state="open")
```

The daemon records the call, injects auth, runs retries. You do not configure auth — the user sets it via `.graft/auth.toml` or `GRAFT_<SERVICE>_<KEY>` env vars.

Across Python sessions helpers reload automatically. Within the same session, `sys.modules` caches the first import.

## Writing a helper

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

Required:

1. Module docstring's first line is the one-line description (used in `INDEX.md`).
2. Every public function has a `Generalization:` section in its docstring. The validator rejects the file otherwise.
3. Parameters, not hardcoded values (see Design Principles).
4. Full type annotations. Helpers must pass `mypy --strict`; run it locally before committing.

### mypy --strict

`request(...).json()` returns `Any`. Direct `-> list[X]` fails with `no-any-return`. Two patterns pass:

- **`cast(...)`** (preferred — keeps type info): wrap `.json()` as in the example.
- **`-> Any`**: lossy but trivial; caller narrows.

Skip `# type: ignore` unless mypy explicitly demands it; `unused-ignore` also fails strict.

## Helper Design Principles

A helper's value comes from being reusable across tasks. Hardcode the current task's specific values and `helpers/` becomes a graveyard.

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

If the function name contains a specific account, owner, customer, or team, it is over-fit. Rename and parameterize.

### Two-task check (do this before committing)

Mentally run the function against:

1. The current task
2. A *plausible variant of the same kind* (different repo, state, etc.)

If (2) doesn't work, refactor the parameters.

### Generalization docstring (mandatory)

```python
def list_issues(owner: str, repo: str, state: str = "open") -> list[dict]:
    """One-line summary.

    Generalization:
        What this works for (the (owner, repo) shape).
        Variant example: a concrete second call site.
        Not applicable: edge cases that need a different helper.
    """
```

The validator scans for the literal string `Generalization:`. Missing it → file rejected → `HelperLoadError`. After three consecutive failures, the error grows a `template` field with a positive example — mirror its structure. (See Errors table for the 5-failure circuit-breaker.)

### Service-level vs task-level

- `helpers/<service>.py` = service-level API wrapper. Generalize.
- One-shot task glue ("fetch X, transform, send to Y") = the user's script. **Not** a helper.

## Public API: graft.context

```python
from graft.context import request, auth, Response

request(service: str, method: str, url: str, *,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
        json: Any = None,
        timeout: float | None = None) -> Response

auth(service: str) -> str | None  # rare; only when token must go in URL

# Response: frozen dataclass — status_code: int, headers: dict[str, str], body: bytes; .json(), .text()
```

Do not `import httpx` in helpers. Use `Response`.

## Forbidden in helpers/

The validator rejects the entire file on:

1. **Cross-service imports** — `from helpers.notion import x` inside `helpers/github.py`. That's task glue, not a helper.
2. **Relative imports** — `from . import x`.
3. **`import importlib`** / `from importlib import ...` — no dynamic imports.
4. **`__import__("...")`** — same.
5. **`exec(...)` / `eval(...)`** — no dynamic execution.

Use `Response` from `graft.context`, not `import httpx`. v1 trusts you here; v2 will enforce.

Do not work around the validator. Self-contained helpers are the point.

## Errors

| Error | Cause | Fix |
|---|---|---|
| `HelperLoadError` | validator rejected the file | Read `reason`, fix, save again |
| `HelperImportError` | `import helpers.X` failed at runtime | Add missing dep, or fix the import |
| `HelperLoadAborted` | 5 consecutive validator failures | Tell user to run `graft reset <service>` |
| `DaemonNotRunning` | daemon process not up | Tell user to run `graft serve` |

When `HelperLoadError` arrives with a `template` field (third consecutive failure), it contains a positive-example helper. Mirror its structure.

## Stats

`graft stats` shows per-service helper count, call count, last-used date. 200 calls / 0 errors → trust it. 1 call / 1 error → rewrite.

## Auto-commit

When you write or change `helpers/<service>.py`, graft auto-commits the change to git on the next import (`GRAFT_AUTOCOMMIT=1`, default). Do not run `git add` / `git commit` yourself.
