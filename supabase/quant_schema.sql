-- ============================================================
--  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
--  >>> FILE PATH:   supabase/quant_schema.sql
--  z165 - the quant book's live tables. They live in the PORT+ANALYTICS Supabase project (created there
--  10-28; the engine's SUPABASE_URL / SUPABASE_SERVICE_ROLE and the app's quant card point at it).
--  SQL Editor > New query > paste ALL of this > Run.
--  No DROP or DELETE anywhere (the editor's "destructive operation" prompt never appears), ASCII only,
--  and safe to run again: every step checks first.
--  The engine writes with the service key; the app reads with the anon key (select only).
-- ============================================================

create table if not exists public.quant_trades (
  id          text primary key,
  day         date not null,
  strategy    text not null,
  mode        text not null,
  symbol      text not null,
  net_bps     double precision,
  pnl         double precision,
  closed_at   timestamptz,
  trade       jsonb not null,
  updated_at  timestamptz not null default now()
);

create index if not exists quant_trades_day_idx on public.quant_trades (day desc, strategy);

create table if not exists public.quant_state (
  id          text primary key,
  doc         jsonb not null,
  updated_at  timestamptz not null default now()
);

alter table public.quant_trades enable row level security;
alter table public.quant_state enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'quant_trades' and policyname = 'read quant_trades') then
    create policy "read quant_trades" on public.quant_trades for select to anon, authenticated using (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'quant_state' and policyname = 'read quant_state') then
    create policy "read quant_state" on public.quant_state for select to anon, authenticated using (true);
  end if;
end
$$;

grant select on table public.quant_trades to anon, authenticated;
grant select on table public.quant_state to anon, authenticated;
grant all on table public.quant_trades to service_role;
grant all on table public.quant_state to service_role;

-- Check (should list both tables, each with its read policy):
select tablename, policyname from pg_policies where schemaname = 'public' and tablename like 'quant%' order by 1;
