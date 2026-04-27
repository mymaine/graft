# graft

> A self-healing HTTP API harness for AI agents. The agent self-edits its own helpers; git is the memory.

**Status**: Phase 1 (MVP) — end-to-end self-edit loop working with GitHub API. Public release imminent.

## Why

Your AI agent (Claude Code / Codex / similar) learned to call the Linear API last week. This week it has to learn again — there's no muscle memory. Every new HTTP service is a cold start.

graft fixes that. One thin daemon + a `helpers/` directory the agent edits as it works. Helpers persist in your git. Next time, the agent already knows.

## Inspiration

Heavily inspired by [browser-use/browser-harness](https://github.com/browser-use/browser-harness) and the [Bitter Lesson of Agent Harnesses](https://browser-use.com/posts/bitter-lesson-agent-harnesses) blog post. graft applies the same paradigm to arbitrary HTTP APIs instead of Chrome.

## Hard Constraints

This project is an experiment in extreme minimalism. The constraints are part of the product:

- **Core runtime (daemon + CLI) ≤ 800 lines of Python** (CI-enforced via `scc`)
- **Per-module budgets** — each module has a fixed line budget; PRs that exceed it must shrink elsewhere
- **bug fix must reduce or hold LOC** — adding lines means you missed the root cause

LOC is a feature.

## Install

graft requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
uv pip install graft        # once published; for now: uv pip install -e .
```

## Quickstart

### 1. Initialize a project

In any project directory:

```bash
graft init
```

This creates:

- `helpers/` — where the agent will write Python files (one per service)
- `helpers/__init__.py` — auto-wraps helper functions for stats tracking on import
- `.graft/` — daemon runtime data (port file, stats, auth tokens)
- `SKILL.md` — the rules for the agent (read by Claude Code / Codex on session start)

### 2. Add your auth tokens

`.graft/auth.toml` (gitignored — never commit secrets):

```toml
[github]
token = "ghp_..."

[linear]
token = "lin_api_..."
```

Or via env: `GRAFT_GITHUB_TOKEN=...` (env wins over auth.toml).

### 3. Start the daemon

```bash
graft serve
```

The daemon listens on `127.0.0.1` (a free port chosen by the OS, written to `.graft/daemon.port`). It injects auth, retries 5xx, and records stats for every helper call.

### 4. Use Claude Code (or any agent) on the project

The agent reads `SKILL.md`, sees `helpers/INDEX.md`, and writes Python helpers as needed. Each helper file is a thin Python module like:

```python
"""GitHub REST + GraphQL helpers."""

from graft.context import request


def list_issues(owner: str, repo: str, state: str = "open", limit: int = 30) -> list[dict]:
    """List GitHub issues for a repository.

    Generalization:
        Works for any (owner, repo). Filter by state, limit count.
        Variant example: list_issues("python", "cpython", state="closed")
        Not applicable: GitHub Enterprise on custom domains.
    """
    return request(
        "github",
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        params={"state": state, "per_page": limit},
    ).json()
```

The daemon auto-commits new and edited helpers to git. Next session, the agent finds them already there.

### 5. Inspect what's accumulated

```bash
graft stats          # per-service helper count, total calls, last-used
graft sync           # regenerate helpers/INDEX.md from helpers/ + stats
```

### CLI reference

| Command | Purpose |
|---|---|
| `graft init` | Set up `helpers/` + `.graft/` + `SKILL.md` in cwd |
| `graft serve` | Start the localhost daemon (blocks until Ctrl-C) |
| `graft sync` | Regenerate `helpers/INDEX.md`; warn if `SKILL.md` differs from installed template |
| `graft stats` | Show per-service usage table |
| `graft reset <service>` | Clear the validator failure counter for `<service>` (after a `HelperLoadAborted`) |

### How agents learn the rules

`SKILL.md` (written by `graft init`) is graft's manual to the agent. It covers:

- Three branches when a task arrives (helper exists / function missing / new service)
- Helper Design Principles (generalize the right things, name correctly, two-task validation)
- The mandatory `Generalization:` docstring format (validator rejects the file otherwise)
- The `graft.context` API (`request`, `auth`, `Response`)
- What's forbidden in `helpers/` (cross-service imports, dynamic imports, `exec`, `eval`, `httpx`)
- Common patterns (auth GET, JSON POST, 404 handling, pagination)

You don't write `SKILL.md`. Read it once to know what the agent will see.

## Development

```bash
uv sync                                       # install deps
uv run pre-commit install                     # install git hook
uv run pytest                                 # run all tests (unit + integration)
uv run ruff check . && uv run ruff format .   # lint + format
uv run mypy --strict src/graft/               # type check (core runtime only)
scc src/graft/                                # LOC audit
```

### Running the cold-start E2E

`tests/e2e/cold_start.sh` drives Claude Code through the entire flow (write helper → call → stats → commit) and verifies five acceptance gates. Requires `ANTHROPIC_API_KEY` and the `claude` CLI:

```bash
ANTHROPIC_API_KEY=... bash tests/e2e/cold_start.sh
```

## Quality gates for your helpers

graft's loader does fast, scoped validation at import time (Generalization docstring + a small set of forbidden imports / builtins). It deliberately does *not* run `ruff` or `mypy` against `helpers/` — those would slow the agent's edit loop and overlap with what your own CI should already do.

Add these to your project's CI to catch the things graft's loader doesn't:

```yaml
# .github/workflows/ci.yml (your project, not graft's)
- run: pip install ruff mypy
- run: ruff check helpers/
- run: mypy --strict helpers/
```

Why split the work this way: the loader's job is to give the agent *immediate* feedback on the rules that matter for graft's contract (Generalization, no cross-service imports, no `httpx`). Style, unused imports, and type holes are real issues but they belong in CI where they don't stall a writing session.

## License

MIT — see [LICENSE](LICENSE).
