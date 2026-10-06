-- Conversations (ARCHITECTURE.md §5, §6): who wrote, what they wrote, and
-- which bot turn answered it. The conversation is never held in memory:
-- every turn is rebuilt from these rows.
--
-- Only what step 1 (plumbing) uses. Stage, scores, slots, episodes and the
-- turn's extract/plan/tools columns arrive with the steps that fill them.

create table leads (
  id               bigint generated always as identity primary key,
  phone            text not null unique,          -- Meta wa_id, digits only: '923001234567'
  name             text,                          -- WhatsApp profile name, as sent by Meta
  -- The agent is handling this chat by hand: the bot stays silent (§7, §9).
  takeover         boolean not null default false,
  last_inbound_at  timestamptz,
  last_outbound_at timestamptz,
  created_at       timestamptz not null default now()
);

-- One bot reply to everything the buyer sent in one burst (debounced).
create table turns (
  id          bigint generated always as identity primary key,
  lead_id     bigint not null references leads(id),
  status      text   not null default 'running'
              -- not_sent: dry run (no WhatsApp token yet), never claimed as sent.
              check (status in ('running', 'sent', 'not_sent', 'superseded', 'takeover', 'failed')),
  reply       text,
  error       text,
  latency_ms  int,                                 -- first message of the burst -> sent
  created_at  timestamptz not null default now(),
  finished_at timestamptz
);
create index turns_lead_idx on turns (lead_id, created_at desc);

create table messages (
  id              bigint generated always as identity primary key,
  lead_id         bigint not null references leads(id),
  direction       text   not null check (direction in ('in', 'out')),
  -- Meta retries webhooks: this unique key is the dedupe. Null only for an
  -- outbound message that was never accepted by Meta.
  wa_message_id   text   unique,
  type            text   not null,                 -- text, image, audio, location, interactive, ...
  text            text,
  media_id        text,                            -- Meta media id (voice note, photo)
  context_id      text,                            -- wa_message_id this one quotes/replies to
  payload         jsonb,                           -- the raw Meta message object, kept for reprocessing
  -- Outbound only. A failed send is a lost lead nobody sees unless it is stored (§15).
  delivery_status text
                  check (delivery_status in ('pending', 'sent', 'delivered', 'read', 'failed', 'not_sent')),
  error           text,
  turn_id         bigint references turns(id),     -- inbound: the turn that answered it
  at              timestamptz not null,            -- Meta's timestamp for inbound, send time for outbound
  created_at      timestamptz not null default now()
);
create index messages_lead_idx on messages (lead_id, at);
-- What the next turn must answer: inbound messages no turn has claimed yet.
create index messages_unanswered_idx on messages (lead_id, at)
  where direction = 'in' and turn_id is null;

alter table leads    enable row level security;
alter table turns    enable row level security;
alter table messages enable row level security;
