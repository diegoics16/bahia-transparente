-- ============================================================
-- Bahía Transparente — database schema
-- Paste this whole file into Supabase → SQL Editor → New query → Run.
-- Safe to re-run: uses "if not exists" / "create or replace" where possible.
-- ============================================================

-- ------------------------------------------------------------
-- 1. SNIFA — regulated facilities and their dated status events
-- ------------------------------------------------------------
create table if not exists snifa_facilities (
  id text primary key,              -- the "Unidad Fiscalizable" id from SNIFA's URL
  razon_social text,
  comuna text,
  detail_url text,
  lat numeric,
  lon numeric,
  last_scraped_at timestamptz
);

create table if not exists snifa_events (
  id bigserial primary key,
  facility_id text references snifa_facilities(id) on delete cascade,
  section text,                     -- 'Procedimientos Sancionatorios' | 'Fiscalizaciones' | etc.
  rol_or_expediente text,
  fecha date,
  estado text,
  raw jsonb,                        -- full original row — keeps you safe if SNIFA's columns change
  scraped_at timestamptz default now()
);

create index if not exists idx_snifa_events_facility on snifa_events(facility_id);

-- ------------------------------------------------------------
-- 2. POAL — DIRECTEMAR water quality readings
-- ------------------------------------------------------------
create table if not exists poal_readings (
  id bigserial primary key,
  cuerpo_de_agua text,
  matriz text,
  estacion_poal text,
  parametro text,
  fecha date,
  valor numeric,
  unidad text,
  lat numeric,
  lon numeric,
  coord_source text,
  anio int,
  semestre text,
  laboratorio text,
  n_source_rows int,
  synced_at timestamptz default now(),
  unique (cuerpo_de_agua, estacion_poal, parametro, fecha)   -- re-running the monthly sync updates, not duplicates
);

create index if not exists idx_poal_station on poal_readings(estacion_poal);
create index if not exists idx_poal_fecha on poal_readings(fecha);

-- ------------------------------------------------------------
-- 3. Community reports — the public "¿Vio algo en la bahía?" form
-- ------------------------------------------------------------
create table if not exists community_reports (
  id uuid primary key default gen_random_uuid(),
  happened_at timestamptz,
  report_type text,                 -- smell | fishkill | color | oil | smoke | health | other
  notes text,
  reporter_name text,
  lat numeric,
  lon numeric,
  reported_to_directemar boolean default false,
  created_at timestamptz default now()
);

-- ------------------------------------------------------------
-- 4. Sync log — every scraper run writes one row here, success or fail.
--    This is what tells you a scraper silently broke instead of finding
--    out three weeks later that the map hasn't updated.
-- ------------------------------------------------------------
create table if not exists sync_runs (
  id bigserial primary key,
  source text,                      -- 'snifa' | 'poal'
  started_at timestamptz,
  finished_at timestamptz,
  status text,                      -- 'success' | 'failed'
  rows_written int,
  error text
);

-- ============================================================
-- Row Level Security — this is what makes it safe to expose the
-- "anon" key in the public HTML/JS. Without this, anyone with the
-- key could read AND write everything.
-- ============================================================

alter table snifa_facilities enable row level security;
alter table snifa_events enable row level security;
alter table poal_readings enable row level security;
alter table community_reports enable row level security;
alter table sync_runs enable row level security;

-- Public read access on everything (this is a transparency platform —
-- the whole point is that anyone can see the data)
drop policy if exists "public read" on snifa_facilities;
create policy "public read" on snifa_facilities for select using (true);

drop policy if exists "public read" on snifa_events;
create policy "public read" on snifa_events for select using (true);

drop policy if exists "public read" on poal_readings;
create policy "public read" on poal_readings for select using (true);

drop policy if exists "public read" on community_reports;
create policy "public read" on community_reports for select using (true);

-- sync_runs is operational/internal — no public read policy on purpose.
-- Only the service-role key (used by GitHub Actions) can see or write it.

-- Public WRITE (insert only, no update/delete) on community_reports only.
-- This is the one table the public-facing anon key is allowed to write to.
drop policy if exists "public insert reports" on community_reports;
create policy "public insert reports" on community_reports for insert with check (true);

-- Everything else (snifa_facilities, snifa_events, poal_readings, sync_runs)
-- has NO insert/update/delete policy for the anon key — only the
-- service_role key (which GitHub Actions holds as a secret, never the
-- browser) can write to those, because it bypasses RLS entirely.
