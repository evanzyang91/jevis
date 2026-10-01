#!/usr/bin/env bash
# Start Chrome with the debugging port the agent attaches to.
# The agent's AGENT_CDP_URL env var must point at this port
# (default http://localhost:9222). Run this before `uv run agent`.

set -euo pipefail

PORT="${AGENT_CHROME_PORT:-9222}"
# Where Chrome keeps its user data. The agent reads the SAME env to find the
# DevToolsActivePort file Chrome writes here, so both ends agree on the path.
# AGENT_CHROME_PROFILE is the profile DISPLAY NAME inside this folder — not a
# path — and belongs in the agent's config, not here.
USER_DATA_DIR="${AGENT_CHROME_USER_DATA_DIR:-$HOME/.config/google-chrome-agent}"
BIN="${AGENT_CHROME_BIN:-google-chrome-stable}"

# Refuse to launch if another Chrome already owns the profile — the port
# will silently drop otherwise.
if curl -sf "http://localhost:${PORT}/json/version" >/dev/null 2>&1; then
  echo "Chrome debugging port ${PORT} is already responding. Reusing it."
  exit 0
fi

if pgrep -f "${USER_DATA_DIR}" >/dev/null 2>&1; then
  echo "A Chrome process already owns ${USER_DATA_DIR}. Close it first, then rerun."
  exit 1
fi

exec "${BIN}" \
  --remote-debugging-port="${PORT}" \
  --user-data-dir="${USER_DATA_DIR}" \
  --no-first-run \
  --no-default-browser-check
