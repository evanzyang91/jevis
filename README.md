# Agent

A browser agent that chooses actions from a typed action space.

## What is here

- `agent/` — Python. Executor (Playwright), perception (DOM reader), policy (Jev), planner, supervisor, memory, providers, transport, HTTP + WebSocket server.
- `web/` — Next.js 15 App Router. One route at `/`; its Developer toggle opens the decision inspector.
- `db/` — Postgres migrations and sqlc queries.
- `docker-compose.yml` — local Postgres.

## Prerequisites

- Python 3.12, `uv` on `$PATH`.
- Node 20+, `pnpm` on `$PATH`.
- Docker Desktop or Docker Engine.
- `sqlc` (optional; only for regenerating `web/lib/db/*.ts` after query changes).
- API keys: `TYPESAFE_API_KEY` (required), plus one of `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

## First-time setup

```bash
# Python deps
uv sync

# Chromium for Playwright
uv run playwright install chromium

# Node deps
cd web && pnpm install && cd ..

# API keys and server ports
cp .env.example .env
$EDITOR .env
# Tell the browser where the agent server lives (WebSocket target)
cp web/.env.local.example web/.env.local

# Postgres (applies migrations on first boot)
docker compose up -d

# Optional: regenerate the typed TS query layer (only if you edited db/queries/*.sql)
sqlc generate
```

Set `DATABASE_URL` in `.env` to enable Postgres-backed playbooks:
```
DATABASE_URL=postgres://agent:agent@127.0.0.1:5432/agent
```
Without it, the server boots and uses an in-memory playbook that resets when the process stops.

## Run

Two terminals.

**Terminal 1 — the agent server (Python, port 8787):**
```bash
uv run agent
```

**Terminal 2 — the web UI (Next.js, port 3000):**
```bash
cd web
pnpm dev
```

Open http://localhost:3000. Enter a goal and click Start. The agent opens a real Chromium window (visible), streams frames to the UI, and drives the page.

Developer view: turn on **Developer** in the page header. It shows the live browser, the policy's ranked choices, the decision trail, and a trace export.

### Troubleshooting

- **Black screencast, "Status: starting" forever.** The browser cannot reach the agent server on the WebSocket. Check `web/.env.local` — `NEXT_PUBLIC_AGENT_WS_URL` must match where `uv run agent` is listening (default `ws://127.0.0.1:8787`). Restart `pnpm dev` after editing.
- **Instant error event.** The Python server logs the reason. Almost always a missing `TYPESAFE_API_KEY` or `ANTHROPIC_API_KEY` in `.env`.
- **No Chromium window opens.** `uv run playwright install chromium` did not complete. Rerun it, then retry.

## Tests

```bash
uv run pytest              # 84 unit + integration tests
uv run ruff check agent/   # lint
```

## What is *not* wired yet

- **CAPTCHA resume flow.** Endpoint accepts the resume token; the supervisor pause is not implemented pending a real captcha in a real run.
- **Event persistence.** Events flow over WebSocket to the browser. Nothing writes them to `events` in Postgres. Add that after the first real run.

## Layout at a glance

```
agent/
  cli.py                     # `uv run agent` entry point
  server/                    # FastAPI + WebSocket routes
  supervisor/                # run loop + guards + budget + speculative parallelism
  perception/                # DOM reader + vision fallback
  planner/                   # goal refinement + subgoals + verification
  policy/                    # action space + Jev decide + text helper
  executor/                  # Playwright driver + motion + stealth + screencast
  memory/                    # session recall + playbook store
  providers/                 # model registry + text adapters + Jev client
  transport/                 # typed events + async pub/sub bus
  tests/
web/
  app/                       # Next.js App Router pages + API routes
  components/                # Composer, ScreencastFrame, CursorLayer, EventLog, ...
  lib/                       # events.ts, ws.ts, cursor.ts
db/
  migrations/0001_init.sql
  queries/*.sql
```
