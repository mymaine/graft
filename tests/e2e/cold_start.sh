#!/usr/bin/env bash
# AC-1a: end-to-end cold-start test.
#
# Drives Claude Code (non-interactive) to write a GitHub helper from scratch
# inside a fresh project, then verifies the five acceptance gates:
#   (a) helpers/github.py exists
#   (b) .graft/stats.jsonl contains at least one service=github ok=true row
#   (c) git log helpers/ contains at least one daemon auto-commit
#   (d) mypy --strict on the new helper passes
#   (e) overall exit code is 0
#
# Requires: claude CLI (with ANTHROPIC_API_KEY in CI, or OAuth session via
# 'claude login' for local users), uv, git.

set -euo pipefail

command -v claude >/dev/null || { echo "claude CLI not installed"; exit 1; }
command -v uv >/dev/null     || { echo "uv not installed"; exit 1; }

# Auth: claude CLI uses ANTHROPIC_API_KEY (CI) or its own OAuth session (local).
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "Note: ANTHROPIC_API_KEY not set — relying on claude CLI OAuth session."
  echo "If claude fails below with an auth error, run 'claude login' first."
fi

GRAFT_REPO="${GRAFT_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
TEST_DIR="$(mktemp -d -t graft-coldstart-XXXXXX)"

cleanup() {
  if [[ -n "${DAEMON_PID:-}" ]]; then
    kill "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID"  2>/dev/null || true
  fi
  if [[ "${EXIT_CODE:-0}" -eq 0 ]]; then
    rm -rf "$TEST_DIR"
  else
    echo "TEST_DIR preserved for inspection: $TEST_DIR" >&2
  fi
}
trap cleanup EXIT

cd "$TEST_DIR"

# Empty git repo — cold-start prerequisite per AC-1a
git init -q
git config user.email "test@cold-start"
git config user.name  "test"

# Ignore graft runtime so the dirty-tree gate doesn't trip auto-commit
cat > .gitignore <<EOF
.graft/
daemon.log
EOF

# Install graft from source (editable)
uv venv -q
uv pip install -q -e "$GRAFT_REPO"
uv pip install -q mypy

# Initialize graft scaffold
uv run graft init

# Commit scaffold so the dirty-tree gate stays clean during auto-commit
git add .
git commit -q -m "scaffold"

# Start daemon in background and wait for the port file
uv run graft serve >daemon.log 2>&1 &
DAEMON_PID=$!
for _ in {1..20}; do
  [[ -f .graft/daemon.port ]] && break
  sleep 0.5
done
[[ -f .graft/daemon.port ]] || { echo "daemon failed to start"; cat daemon.log; exit 1; }

# Constrained prompt — service name fixed to "github" + type-annotation requirement
# matches AC-1a's prompt-shape lock to reduce LLM variance.
read -r -d '' PROMPT <<'PROMPT_EOF' || true
You are working inside a freshly initialized graft project. Read SKILL.md and
helpers/INDEX.md first to learn the conventions; do not skip this step.

Then complete the task:
  Write a helper at helpers/github.py with a function named list_issues that
  fetches the latest open issues from a GitHub repository.

Constraints:
  - Service name must be exactly "github" (the file must be helpers/github.py).
  - Do NOT use `graft add` or pull from any registry. Write the helper from
    scratch — this test exercises the cold-start self-write path.
  - Every public function must have full type annotations on parameters and
    return type. CI runs mypy --strict.
  - Use graft.context.request to call the GitHub API. Do not import httpx.
  - Include a Generalization: section in the docstring per SKILL.md.

After writing the helper, call:
  list_issues(owner="anthropics", repo="claude-code", limit=5)

and print the resulting list to stdout.
PROMPT_EOF

claude -p "$PROMPT" --max-turns 30

# Acceptance gates
HELPER_FILE="helpers/github.py"
EXIT_CODE=0

if [[ -f "$HELPER_FILE" ]]; then
  echo "PASS (a): $HELPER_FILE exists"
else
  echo "FAIL (a): $HELPER_FILE not created"; EXIT_CODE=1
fi

if [[ -f .graft/stats.jsonl ]] \
   && grep -q '"service":"github"' .graft/stats.jsonl \
   && grep -q '"ok":true'          .graft/stats.jsonl; then
  echo "PASS (b): stats.jsonl has service=github ok=true"
else
  echo "FAIL (b): no successful github call recorded in stats.jsonl"
  [[ -f .graft/stats.jsonl ]] && head -5 .graft/stats.jsonl
  EXIT_CODE=1
fi

GATE_C_LOG="$(git log --oneline -- helpers/ 2>&1)"
GATE_C_GIT_EXIT=$?
echo "$GATE_C_LOG" | grep -qi 'graft'
GATE_C_GREP_EXIT=$?
if [[ $GATE_C_GREP_EXIT -eq 0 ]]; then
  echo "PASS (c): git log helpers/ contains daemon auto-commit"
else
  echo "FAIL (c): no auto-commit found in git log helpers/"
  echo "         git exit=$GATE_C_GIT_EXIT, grep exit=$GATE_C_GREP_EXIT"
  echo "         git log --oneline -- helpers/ output:"
  echo "$GATE_C_LOG" | sed 's/^/           /'
  EXIT_CODE=1
fi

if uv run mypy --strict "$HELPER_FILE"; then
  echo "PASS (d): mypy --strict $HELPER_FILE"
else
  echo "FAIL (d): mypy failed on $HELPER_FILE"
  EXIT_CODE=1
fi

if [[ $EXIT_CODE -eq 0 ]]; then
  echo "PASS (e): cold-start acceptance criteria all green"
else
  echo "FAIL: cold-start gates above failed"
  echo
  echo "=== evidence dump (paste this back to lead for diagnosis) ==="
  echo "--- git log --all --oneline ---"
  git log --all --oneline 2>&1 || true
  echo "--- git log --diff-filter=A --all --stat -- helpers/github.py ---"
  git log --diff-filter=A --all --stat -- helpers/github.py 2>&1 || true
  echo "--- git show --stat HEAD ---"
  git show --stat HEAD 2>&1 || true
  echo "--- git ls-tree -r --name-only HEAD ---"
  git ls-tree -r --name-only HEAD 2>&1 || true
  echo "--- git log --all --stat ---"
  git log --all --stat 2>&1 || true
  echo "--- git status --short ---"
  git status --short 2>&1 || true
  echo "--- git diff HEAD ---"
  git diff HEAD 2>&1 || true
  echo "--- daemon.log (tail 100) ---"
  tail -100 daemon.log 2>&1 || true
  echo "--- .graft/stats.jsonl ---"
  cat .graft/stats.jsonl 2>&1 || true
  echo "--- helpers/github.py ---"
  cat helpers/github.py 2>&1 || true
  echo "--- ls -la .graft/ ---"
  ls -la .graft/ 2>&1 || true
  echo "=== end evidence dump ==="
fi

exit $EXIT_CODE
