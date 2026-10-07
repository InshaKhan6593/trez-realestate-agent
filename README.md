# Trez real estate WhatsApp agent

A WhatsApp agent for **Trez Enterprises**, a Karachi real estate agency whose buyers come from their
listings on Zameen.com. It replies in seconds, day and night, in Roman Urdu, Urdu or English: it answers
questions from the listings' real data, asks one useful question at a time, suggests listings, sends
photos, and hands the buyer to a human agent with the full context when a person is needed.

## How it works

```
Zameen (Trez's listings) ──scraper──► snapshot ──sync──► Supabase (listings, places, photos, change events)
                                                               ▲
buyer on WhatsApp ──► webhook ──► Redis (debounce, lock) ──► worker ──► agent turn ──► reply / photos
                                                                              │
                                                                              └──► alert to the human agent
```

One agent turn (LangGraph): **extractor** (model reads the messages) → **planner** (code decides) →
**tools** (database) → **responder** (model writes one reply) → **validator** (code checks every price,
claim and promise; retry once, then a safe template). The model never decides and never states a fact
the database did not give it. The buyer's memory lives in Postgres, so a reply three days later works
exactly like one three seconds later.

Location requests follow Zameen's own place tree: the exact place first; if it has nothing, the
closest matches from one level up, then the next, always saying where they are and how far.

## Tracing
Every buyer turn is one [Langfuse](https://langfuse.com) trace, and each buyer is one session, so a
conversation reads turn by turn. A trace shows what the buyer sent (voice notes play inline once
transcription is built), what the agent already knew, what the extractor understood (exact prompt,
answer, tokens, cost), what the code decided, every tool with its arguments and result, each reply
draft and why the validator passed or rejected it, and what was sent to the buyer and the agent.
Scores (`reply-passed`, `used-template`, `handoff`, `fit`, `intent`, `outcome`) make problems filterable.
Phone numbers are masked. Tracing is off when the Langfuse keys in `.env` are empty, and always off in tests.

To read one conversation: open **Sessions** in Langfuse and pick the buyer (`lead-<id>`). In the Tracing
table, filter *Is Root Observation = True* for one row per turn, and click a row for its step tree.

## Stack
Python (FastAPI, arq, LangGraph, psycopg) · Supabase (Postgres, Storage) · Redis · models via
OpenRouter (chosen in `.env`) · Node + Playwright for the scraper · Meta WhatsApp Cloud API.

## Run it locally

Needs Docker, Node, [uv](https://docs.astral.sh/uv/).

```bash
npm install && npx supabase start          # local Postgres + Storage (Docker)
docker compose up -d                       # local Redis
cp .env.example .env                       # fill in the keys (see comments in the file)
uv sync

cd scraper && npm ci && node scrape-zameen.mjs --output-dir ../data/raw/$(date +%Y-%m-%d) \
  --media-store ../data/media-store --download-media && cd ..
uv run python -m sync.ingest data/raw/$(date +%Y-%m-%d)

uv run python -m scripts.chat              # talk to the agent as a buyer (nothing is sent on WhatsApp)
uv run pytest                              # 145 tests
```

Traces: Langfuse Cloud (keys and host in `.env`; this project is in the **US** region,
`https://us.cloud.langfuse.com`) or locally with
`docker compose -f observability/docker-compose.yml up -d` (UI on http://localhost:3000).

Serving WhatsApp: `uv run python -m app.serve` (webhook) and `uv run arq app.worker.WorkerSettings`
(worker). Without a WhatsApp token, replies are recorded as not sent.

## Repository
| Folder | What |
|---|---|
| `scraper/` | Zameen scraper and snapshot validator ([README](scraper/README.md)) |
| `sync/` | Snapshot → database: parse, diff into change events, photos |
| `agent/` | The agent: tools, planner, prompts, validator, memory, LangGraph graph |
| `app/` | WhatsApp webhook, debounce and lock, worker, sending |
| `supabase/` | Local stack config and database migrations |
| `scripts/` | `chat.py`: try the agent locally |
| `tests/` | pytest; database tests skip when the local stack is not running |

Design and decisions: [ARCHITECTURE.md](ARCHITECTURE.md) (start with §0 for what is built).
Scraped data (`data/`) is never committed: it is third-party content and the client's inventory.
