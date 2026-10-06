-- The agent's memory of each buyer, the human agents, and handoffs with full
-- context (ARCHITECTURE.md §6-§9, Phase 1 scope in CLAUDE.md).
--
-- Nothing here is held in an LLM's chat history: every turn rebuilds the
-- buyer's picture from these rows.

create extension if not exists pg_trgm with schema extensions;

-- ---------------------------------------------------------------------------
-- Trez's human agents: who gets the handoff alert, who is talking to a buyer
-- ---------------------------------------------------------------------------
create table agents (
  id         bigint generated always as identity primary key,
  name       text not null,
  phone      text not null unique,             -- WhatsApp number, digits only
  active     boolean not null default true,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Leads: handoff state replaces the old on/off takeover flag
-- ---------------------------------------------------------------------------
alter table leads drop column takeover;
alter table leads
  -- none:      the bot is the only one talking
  -- requested: the agent has been alerted; the bot keeps serving, within limits
  -- taken:     the agent is talking to the buyer; the bot stays silent
  add column handoff_state text not null default 'none'
             check (handoff_state in ('none', 'requested', 'taken')),
  add column agent_id      bigint references agents(id),     -- who has (or was asked to take) it
  add column language      text,                             -- the buyer's language, as last detected
  -- Scores are computed in code (§7); the LLM only reports signals.
  add column fit_score     int check (fit_score between 0 and 100),
  add column intent_score  int check (intent_score between 0 and 100),
  add column priority      text
             check (priority in ('hot', 'warm', 'nurture', 'redirect', 'cold', 'junk'));

-- One row per handoff. A reassignment (agent to agent) is a new row pointing
-- at the one it replaces, so the full chain and every summary are kept.
create table handoffs (
  id            bigint generated always as identity primary key,
  lead_id       bigint not null references leads(id),
  reason        text not null,                -- e.g. negotiation, asked_for_human, hot_lead, not_in_data
  summary       text not null,                -- what the agent reads first
  open_items    jsonb not null default '[]',  -- questions the bot could not answer
  agent_id      bigint references agents(id),
  replaces_id   bigint references handoffs(id),
  requested_at  timestamptz not null default now(),
  taken_at      timestamptz,
  released_at   timestamptz
);
create index handoffs_lead_idx on handoffs (lead_id, requested_at desc);

-- ---------------------------------------------------------------------------
-- What we know about the buyer (§7): one row per fact, with how we know it
-- ---------------------------------------------------------------------------
create table lead_slots (
  lead_id    bigint not null references leads(id),
  slot       text   not null,                  -- purpose, property_type, location, budget_max, ...
  value      jsonb  not null,
  confidence real   not null check (confidence between 0 and 1),
  source     text   not null check (source in ('stated', 'inferred')),
  turn_id    bigint references turns(id),
  updated_at timestamptz not null default now(),
  primary key (lead_id, slot)
);

-- What we asked and are waiting for, so a late "3 tak" lands on the right slot (§8).
create table open_questions (
  id         bigint generated always as identity primary key,
  lead_id    bigint not null references leads(id),
  slot       text   not null,
  turn_id    bigint references turns(id),
  asked_at   timestamptz not null default now(),
  status     text   not null default 'open' check (status in ('open', 'answered', 'dropped'))
);
create index open_questions_lead_idx on open_questions (lead_id) where status = 'open';

-- Every listing a buyer touched, and exactly what we told them about it, so
-- a later change (sold, price) can be reported instead of repeated (§9-§10).
create table lead_listings (
  lead_id       bigint not null references leads(id),
  listing_id    bigint not null references listings(id),
  relation      text   not null check (relation in ('inquired', 'shown', 'liked', 'rejected')),
  reason        text,                          -- why rejected: "bohat door hai"
  status_shown  text,
  price_shown   bigint,
  first_at      timestamptz not null default now(),
  last_at       timestamptz not null default now(),
  primary key (lead_id, listing_id)
);

-- One summary per sitting; a gap of 6 hours or more starts a new episode (§8).
create table episodes (
  lead_id    bigint not null references leads(id),
  n          int    not null,
  started_at timestamptz not null,
  ended_at   timestamptz,
  summary    text,
  primary key (lead_id, n)
);

-- ---------------------------------------------------------------------------
-- Turns: the full audit of every reply (§15, layer 2)
-- ---------------------------------------------------------------------------
alter table turns
  add column extracted  jsonb,     -- what the extractor understood
  add column plan       jsonb,     -- what the planner decided
  add column tool_calls jsonb,     -- every tool call with its arguments and result
  add column validation jsonb,     -- what the validator checked, pass/fail
  add column usage      jsonb;     -- model, tokens and cost per LLM call

-- ---------------------------------------------------------------------------
-- Photos sent on WhatsApp: Meta takes an upload once and returns an id that
-- can be re-sent until it expires, so each photo is uploaded once, not per buyer.
-- ---------------------------------------------------------------------------
alter table listing_media
  add column meta_media_id    text,
  add column meta_uploaded_at timestamptz;

-- ---------------------------------------------------------------------------
-- Location search helpers
-- ---------------------------------------------------------------------------
create index locations_name_trgm on locations using gin (name extensions.gin_trgm_ops);

-- Great-circle distance in km. Plenty for a city's worth of listings.
create or replace function distance_km(lat1 float8, lng1 float8, lat2 float8, lng2 float8)
returns float8 language sql immutable as $$
  select 6371 * 2 * asin(sqrt(
    power(sin(radians(lat2 - lat1) / 2), 2) +
    cos(radians(lat1)) * cos(radians(lat2)) * power(sin(radians(lng2 - lng1) / 2), 2)
  ))
$$;

alter table agents         enable row level security;
alter table handoffs       enable row level security;
alter table lead_slots     enable row level security;
alter table open_questions enable row level security;
alter table lead_listings  enable row level security;
alter table episodes       enable row level security;
