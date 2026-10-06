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
uv run pytest                              # 109 tests
```

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
