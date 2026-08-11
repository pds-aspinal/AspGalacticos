-- FPL League Tracker — Supabase schema
-- Run this once in the Supabase SQL Editor (Project > SQL Editor > New query)

-- ── Tables ──────────────────────────────────────────────

create table if not exists teams (
  team_id      bigint primary key,       -- FPL entry ID
  manager_name text not null,
  team_name    text not null,
  updated_at   timestamptz not null default now()
);

create table if not exists gameweek_snapshots (
  id           bigserial primary key,
  gw           int not null,
  team_id      bigint not null references teams(team_id) on delete cascade,
  gw_points    int not null,             -- points scored that gameweek
  total_points int not null,             -- cumulative season points after this gameweek
  captured_at  timestamptz not null default now(),
  unique (gw, team_id)
);

create index if not exists idx_snapshots_gw on gameweek_snapshots(gw);

-- Placeholder tables for once prize rules are decided.
-- Leave empty/unused until you want to log actual winners —
-- the views below already cover the two most common rules.
create table if not exists prize_rules (
  rule_name   text primary key,
  description text,
  active      boolean not null default true
);

create table if not exists prize_winners (
  id         bigserial primary key,
  gw         int not null,
  rule_name  text not null references prize_rules(rule_name),
  team_id    bigint not null references teams(team_id),
  awarded_at timestamptz not null default now(),
  unique (gw, rule_name)
);

-- ── Views (derived, no data duplication) ───────────────

-- League-specific rank per gameweek, computed from cumulative points.
-- (FPL's own "rank" field on each team is GLOBAL rank across all of FPL,
--  not your private league — this view fixes that.)
create or replace view league_rankings as
select
  gw,
  team_id,
  gw_points,
  total_points,
  rank() over (partition by gw order by total_points desc) as league_rank
from gameweek_snapshots;

-- Highest scorer each gameweek
create or replace view weekly_winner as
select gw, team_id, gw_points
from (
  select gw, team_id, gw_points,
         rank() over (partition by gw order by gw_points desc) as r
  from gameweek_snapshots
) t
where r = 1;

-- Lowest scorer each gameweek (handy if you fine/prize the "loser")
create or replace view weekly_loser as
select gw, team_id, gw_points
from (
  select gw, team_id, gw_points,
         rank() over (partition by gw order by gw_points asc) as r
  from gameweek_snapshots
) t
where r = 1;

-- Week-on-week movement (positive = moved up the table, negative = down)
create or replace view rank_movement as
select
  curr.gw,
  curr.team_id,
  prev.league_rank - curr.league_rank as rank_change
from league_rankings curr
left join league_rankings prev
  on prev.team_id = curr.team_id and prev.gw = curr.gw - 1;

-- ── Row Level Security ──────────────────────────────────
-- The collector script writes using the SERVICE ROLE key, which bypasses
-- RLS entirely. These policies only govern what the public dashboard
-- (using the anon key) is allowed to read. Anon gets read-only, always.

alter table teams enable row level security;
alter table gameweek_snapshots enable row level security;
alter table prize_rules enable row level security;
alter table prize_winners enable row level security;

create policy "public read teams" on teams
  for select using (true);

create policy "public read snapshots" on gameweek_snapshots
  for select using (true);

create policy "public read prize_rules" on prize_rules
  for select using (true);

create policy "public read prize_winners" on prize_winners
  for select using (true);

-- No insert/update/delete policies are created for the anon role,
-- so those remain blocked by default — only the service role key can write.
