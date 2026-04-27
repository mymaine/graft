# graft

> A self-healing HTTP API harness for AI agents. Agent self-edits its own helpers, git is the memory.

**Status**: pre-alpha. Spec phase. No working code yet.

## Why

Your AI agent (Claude Code / Codex / similar) learned to call the Linear API last week. This week it has to learn again — there's no muscle memory. Every new HTTP service is a cold start.

graft fixes that. One thin daemon + a `helpers/` directory the agent edits as it works. Helpers persist in your git. Next time, the agent already knows.

## Inspiration

Heavily inspired by [browser-use/browser-harness](https://github.com/browser-use/browser-harness) and the [Bitter Lesson of Agent Harnesses](https://browser-use.com/posts/bitter-lesson-agent-harnesses) blog post. graft is the same paradigm applied to arbitrary HTTP APIs instead of Chrome.

## Hard Constraints

This project is an experiment in extreme minimalism. The constraints are part of the product:

- **Core runtime (daemon + CLI) ≤ 800 lines of Python**
- **Each harness module ≤ 400 lines**
- **Total repo ≤ 1500 lines** — if exceeded, the project is archived as failed

LOC is a feature.

## Quickstart

_Not yet implemented. Phase 1 target: end-to-end demo with one HTTP API._

## Development

```bash
uv sync                              # install deps
uv run pre-commit install            # install git hook
uv run pytest                        # run tests
uv run ruff check . && uv run ruff format .   # lint + format
uv run mypy .                        # type check
```

## License

MIT — see [LICENSE](LICENSE).
