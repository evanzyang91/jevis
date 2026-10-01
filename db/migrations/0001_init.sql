-- One migration, one direction. Rollbacks happen by writing the next migration,
-- not by editing this one, so a shipped run's schema stays reproducible.

create extension if not exists pgcrypto;

create table runs (
  id            uuid primary key default gen_random_uuid(),
  goal          text not null,
  refined_goal  text,
  start_url     text not null,
  status        text not null default 'running',
  text_model    text,
  vision_model  text,
  usd_total     numeric(12,6) not null default 0,
  started_at    timestamptz not null default now(),
  ended_at      timestamptz,
  constraint runs_status_valid check (status in ('running','paused','done','blocked','error'))
);

create index runs_started_at on runs (started_at desc);
create index runs_status on runs (status);

-- Every event a layer emits. The dev UI replays a run from this table alone,
-- so nothing about the run may live only in memory.
create table events (
  run_id     uuid not null references runs(id) on delete cascade,
  seq        int  not null,
  kind       text not null,
  payload    jsonb not null,
  ts         timestamptz not null default now(),
  primary key (run_id, seq)
);

create index events_kind on events (run_id, kind);
create index events_ts on events (run_id, ts);

-- Cross-run playbook. Keyed by host, path, and the shape of the previous move.
-- Never keyed by page fingerprint: live pages vary between visits and would never match.
create table playbooks (
  id          bigserial primary key,
  host        text not null,
  path        text not null,
  previous    text not null,
  next        text not null,
  seen        int  not null default 1,
  succeeded   int  not null default 0,
  failed      int  not null default 0,
  last_used   timestamptz not null default now(),
  unique (host, path, previous, next)
);

create index playbooks_lookup on playbooks (host, path, previous);
