-- Run once in Supabase SQL Editor before enabling the new workflow.
create table if not exists public.gw_update_runs (
  season text not null,
  gw integer not null check (gw between 1 and 38),
  completed_at timestamptz not null default now(),
  primary key (season, gw)
);

create table if not exists public.gw_email_deliveries (
  season text not null,
  gw integer not null check (gw between 1 and 38),
  team_id bigint not null,
  status text not null check (status in ('sending', 'sent')),
  updated_at timestamptz not null default now(),
  primary key (season, gw, team_id)
);

alter table public.gw_update_runs enable row level security;
alter table public.gw_email_deliveries enable row level security;
revoke all on public.gw_update_runs from anon, authenticated;
revoke all on public.gw_email_deliveries from anon, authenticated;
grant all on public.gw_update_runs to service_role;
grant all on public.gw_email_deliveries to service_role;
