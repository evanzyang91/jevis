#!/usr/bin/env bash
# Start Chrome with the debugging port the agent attaches to.
# The agent's AGENT_CDP_URL env var must point at this port
# (default http://localhost:9222). Run this before `uv run agent`.

set -euo pipefail

PORT="${AGENT_CHROME_PORT:-9222}"
PROFILE="${AGENT_CHROME_PROFILE:-$HOME/.config/google-chrome-agent}"
BIN="${AGENT_CHROME_BIN:-google-chrome-stable}"

# Refuse to launch if another Chrome already owns the profile — the port
# will silently drop otherwise.
if curl -sf "http://localhost:${PORT}/json/version" >/dev/null 2>&1; then
  echo "Chrome debugging port ${PORT} is already responding. Reusing it."
  exit 0
fi

if pgrep -f "${PROFILE}" >/dev/null 2>&1; then
  echo "A Chrome process already owns ${PROFILE}. Close it first, then rerun."
  exit 1
fi

exec "${BIN}" \
  --remote-debugging-port="${PORT}" \
  --user-data-dir="${PROFILE}" \
  --no-first-run \
  --no-default-browser-check
