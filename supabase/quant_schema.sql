-- ============================================================
--  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
--  >>> FILE PATH:   supabase/quant_schema.sql
--  z163 - the quant book's live tables. Run ONCE in the **Bot** Supabase project (the one with bot_trades /
--  bot_equity / bot_state) → SQL Editor → paste → Run. Safe to re-run. The engine writes with the service key;
--  the app reads with the anon key (select only).
-- ============================================================

create table if not exists public.quant_trades (
  id          text primary key,
  day         date not null,
  strategy    text not null,
  mode        text not null,            -- live | shadow
  symbol      text not null,
  net_bps     double precision,
  pnl         double precision,
  closed_at   timestamptz,
  trade       jsonb not null,
  updated_at  timestamptz not null default now()
);
create index if not exists quant_trades_day_idx on public.quant_trades (day desc, strategy);

create table if not exists public.quant_state (
  id          text primary key,         -- 'live': open positions, day P&L, counts, feed health, gate
  doc         jsonb not null,
  updated_at  timestamptz not null default now()
);

alter table public.quant_trades enable row level security;
alter table public.quant_state  enable row level security;
drop policy if exists "read quant_trades" on public.quant_trades;
drop policy if exists "read quant_state"  on public.quant_state;
create policy "read quant_trades" on public.quant_trades for select to anon, authenticated using (true);
create policy "read quant_state"  on public.quant_state  for select to anon, authenticated using (true);
grant select on public.quant_trades, public.quant_state to anon, authenticated;
grant all    on public.quant_trades, public.quant_state to service_role;
