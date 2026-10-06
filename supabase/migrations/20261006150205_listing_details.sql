-- Everything Zameen's own page data (window.state.property.data) carries about
-- a listing, beyond what the visible page text gave us. All of it is what the
-- agent entered on Zameen: the bot presents it as "the listing says".

alter table listings
  -- Page-text readings, no longer used: every field comes from Zameen's data.
  drop column price_text,
  drop column size_text,

  -- Zameen's complete listing object as scraped: nothing the agent entered is
  -- lost, including fields not mapped to a column yet.
  add column zameen_data         jsonb,

  -- Exact coordinates: real "nearby" by distance, not only by the location tree.
  add column lat                 double precision,
  add column lng                 double precision,
  add column geo_exact           boolean,          -- Zameen's hasExactGeography

  -- Ticked amenities, e.g. [{"group": "Main Features", "slug": "parking-spaces",
  -- "label": "Parking Spaces", "value": 2}]; a ticked checkbox has value true.
  -- Absent = the listing does not say, never "no".
  add column amenities           jsonb not null default '[]',

  -- Installment plan when the listing has one: advance, monthly amount and
  -- count, balloon payments, possession/development/balloting fees. Null = none.
  add column payment_plan        jsonb,

  add column furnishing_status   text,              -- furnished / unfurnished; null = not specified
  add column completion_status   text,              -- completed / under-construction
  add column occupancy_status    text,
  add column ownership_status    text,
  add column zameen_verification text,              -- Zameen's own check: verified / unverified
  add column contact_name        text,              -- the Trez agent who posted it
  add column video_urls          text[] not null default '{}',

  -- Zameen's timestamps: when it was posted and last edited on the portal.
  add column zameen_created_at     timestamptz,
  add column zameen_updated_at     timestamptz,
  add column zameen_reactivated_at timestamptz;

-- Rent frequency exactly as Zameen states it, or null when it does not say:
-- never assumed.
alter table listings drop constraint listings_check;
alter table listings add constraint listings_price_period_check
  check (purpose = 'rent' or price_period is null);

create index listings_amenities_idx on listings using gin (amenities jsonb_path_ops);
