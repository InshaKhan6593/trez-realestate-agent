-- Listings data layer (ARCHITECTURE.md §6, §10, §11).
--
-- The bot never trusts chat history for facts: price, size and availability
-- come only from these tables, and every change is recorded as an event.
-- Rows are never deleted. A lead may still reference a listing that has gone,
-- and a relisted property keeps its history.

-- ---------------------------------------------------------------------------
-- Snapshots loaded from data/raw/<date>/
-- ---------------------------------------------------------------------------
create table ingest_runs (
  id            bigint generated always as identity primary key,
  snapshot      text        not null unique,     -- 'data/raw/2026-10-06'
  scraped_at    timestamptz not null,            -- latest scrapedAt in the snapshot
  -- From run-summary.json: did the scrape see every search result? Absences
  -- are only counted from complete runs.
  complete      boolean     not null,
  listing_count int         not null,
  summary       jsonb       not null default '{}',  -- counts per event type
  started_at    timestamptz not null default now(),
  finished_at   timestamptz
);

-- ---------------------------------------------------------------------------
-- Location tree, from Zameen's breadcrumb location ids
-- ---------------------------------------------------------------------------
create table locations (
  id         int  primary key,                   -- Zameen location id (21109 = Askari 6)
  name       text not null,                      -- 'Askari 6' (type suffix stripped)
  parent_id  int  references locations(id),
  path       int[] not null,                     -- {2,525,6654,21109}, root first
  depth      int  not null
);
create index locations_parent_idx on locations (parent_id);
-- "everything under Malir Cantonment":  where path @> array[6654]
create index locations_path_idx on locations using gin (path);

-- ---------------------------------------------------------------------------
-- Listings
-- ---------------------------------------------------------------------------
create table listings (
  id            bigint generated always as identity primary key,
  -- Null for listings that only exist in the client's sheet (§10).
  zameen_id     bigint unique,
  url           text,
  title         text   not null,
  description   text,

  purpose       text   not null check (purpose in ('sale', 'rent')),
  -- Rent and sale share this column; price_period keeps them apart. The
  -- cheapest "house" is a PKR 1.35 lakh/month rental against a 7 crore sale.
  price_pkr     bigint not null check (price_pkr > 0),
  price_period  text   check ((purpose = 'sale' and price_period is null)
                         or (purpose = 'rent' and price_period = 'monthly')),
  price_text    text,                            -- source string, kept for audit

  property_type text   not null
                check (property_type in ('house', 'flat', 'plot', 'commercial', 'portion', 'other')),
  zameen_type   text,                            -- 'Residential Plot' as Zameen says it
  size_sqyd     numeric check (size_sqyd > 0),   -- Karachi inventory is in Sq. Yd
  size_text     text,                            -- '375 Sq. Yd'
  bedrooms      int,                             -- null is data: plots have none
  bathrooms     int,
  location_id   int    references locations(id),

  -- §10 state machine. Only an agent moves a listing to sold/rented/on_hold;
  -- the Zameen sync only ever sets available or needs_verification.
  status        text   not null default 'available'
                check (status in ('available', 'on_hold', 'sold', 'rented',
                                  'withdrawn', 'needs_verification')),

  -- Hash of the descriptive fields (not price, not photos): a change emits
  -- details_changed. Titles do get corrected upstream ("West Open" -> "East Open").
  content_hash  text   not null,

  first_seen_at          timestamptz not null,
  last_seen_on_portal_at timestamptz,
  -- Stale (> 7 days unverified) means unknown, not available (§10).
  last_verified_at       timestamptz,
  -- Consecutive complete runs this listing was absent from. 2 -> needs_verification.
  -- Each snapshot is ingested once and in date order, so a miss is never counted twice.
  missing_runs  int    not null default 0,

  updated_at    timestamptz not null default now()
);
create index listings_search_idx   on listings (purpose, property_type, price_pkr)
  where status = 'available';
create index listings_location_idx on listings (location_id);
create index listings_status_idx   on listings (status);

-- ---------------------------------------------------------------------------
-- Photos: rows here, files in the private 'listing-photos' Storage bucket
-- ---------------------------------------------------------------------------
create table listing_media (
  listing_id   bigint not null references listings(id) on delete cascade,
  asset_id     text   not null,                  -- Zameen's image id, stable across runs
  seq          int    not null,                  -- gallery order
  storage_path text   not null,                  -- '303460756.jpeg' in listing-photos
  source_url   text,
  -- Some scraped "photos" are 120x90 gallery thumbnails: too small to send.
  width        int,
  height       int,
  primary key (listing_id, asset_id)
);
create index listing_media_seq_idx on listing_media (listing_id, seq);

-- ---------------------------------------------------------------------------
-- Every change, from every source (§11: fresh context via change data capture)
-- ---------------------------------------------------------------------------
create table listing_events (
  id            bigint generated always as identity primary key,
  listing_id    bigint not null references listings(id),
  type          text   not null
                check (type in ('new', 'price_changed', 'details_changed',
                                'missing_from_portal', 'relisted', 'status_changed')),
  old           jsonb,
  new           jsonb,
  source        text   not null
                check (source in ('zameen_scrape', 'sheet', 'agent_cmd', 'agent_verify', 'echo')),
  ingest_run_id bigint references ingest_runs(id),
  at            timestamptz not null default now()
);
create index listing_events_listing_idx on listing_events (listing_id, at desc);
create index listing_events_run_idx     on listing_events (ingest_run_id);

-- ---------------------------------------------------------------------------
-- The bot and dashboard reach these through the backend's service role only.
-- RLS on with no policies = closed to the public anon key.
-- ---------------------------------------------------------------------------
alter table ingest_runs    enable row level security;
alter table locations      enable row level security;
alter table listings       enable row level security;
alter table listing_media  enable row level security;
alter table listing_events enable row level security;
