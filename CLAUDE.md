# Real Estate Lead Agent

A client project: a WhatsApp AI agent for **Trez Enterprises** (Karachi; Zameen agent_id `200295`), whose leads
come from Zameen.com listings. It replies 24/7, qualifies leads, answers listing questions, books site visits and
hands hot leads to the human agent. Their inventory is measured in **Sq. Yd** (not marla).

**Read `ARCHITECTURE.md` first.** It holds the full agreed design: architecture, data model, qualification,
episodes and the deterministic re-entry pipeline, listing freshness, Zameen sync, discovery mode, stack and tracing.

## Decisions already made (don't re-argue these without a reason)
- Python **FastAPI** + **arq** worker on an always-on ~$5/mo host. **Not** on Vercel serverless.
- **Supabase** (Postgres + pgvector + Storage + Auth), **Upstash Redis** (locks, debounce, cache).
- **LangGraph** turn pipeline: extractor (Haiku 4.5) → planner (plain code) → tools → responder (Sonnet 5.5) → validator (code).
- Code decides re-entry, staleness, scoring and handoff; the LLM only extracts and writes the reply.
- Listing facts come only from tools/DB, never from chat history. Stale (unverified) means unknown, not available.
- Next.js dashboard on Vercel. **Langfuse** for tracing, **Sentry** for errors.
- Zameen scraping: only the client's own listings, slowly; never get around blocks; scraping is one source among several.

## Conventions
- The user prefers tutor-style explanations (why → what → how) and Python.
- Buyers write in Urdu / Roman Urdu / English; keep the glossary in ARCHITECTURE.md §12 in mind.

## Layout
- `scraper/`: Zameen scraper + snapshot validator (Node, Playwright). See `scraper/README.md`.
- `sync/`: snapshot -> Supabase (Python). `parse` (raw -> typed), `diff` (pure: decides events),
  `ingest` (writes rows, events, photos). Run: `uv run python -m sync.ingest data/raw/<date>`.
- `app/`: WhatsApp side. `webhook` (FastAPI: verify signature, store, queue, 200), `meta` (payload
  parsing), `store` (leads/messages/turns), `inbox` (Redis debounce seq + per-lead lock),
  `turn` (one debounced turn), `worker` (arq), `sender` (Meta send / dry run),
  `reply` (PLACEHOLDER until the LangGraph turn pipeline, §17 step 3).
- `supabase/`: local stack config + migrations. Photos live in the private `listing-photos` bucket.
- `tests/`: pytest. Pure tests always run; `test_whatsapp_flow.py` needs local Supabase + Redis,
  `test_real_snapshots.py` needs `data/`. Both skip cleanly when absent.
- `data/`: git-ignored. `raw/<date>/` immutable snapshots, `media-store/` shared photos.

## Local development (test locally first, hosted Supabase later)
```
npx supabase start                    # Postgres :54322, API :54321, Studio :54323 (Docker)
docker compose up -d                  # Redis :6379
cp .env.example .env                  # SUPABASE_SERVICE_ROLE_KEY from `npx supabase status`
uv sync && uv run pytest
uv run python -m app.serve            # webhook on :8000 (forces the selector loop on Windows)
uv run arq app.worker.WorkerSettings  # turn worker
```
Windows: psycopg async cannot use the Proactor event loop; `app/__init__.py` and `app.serve`
handle it. On the Linux server plain `uvicorn app.webhook:app` is fine.

## Status
Design complete (2026-10-06). Built: Zameen scraper; data layer (listings, locations, photos,
`listing_events`, 2-miss rule); WhatsApp plumbing (signed webhook, dedupe, debounce, per-lead lock,
takeover, delivery statuses, dry-run sending). The reply is a placeholder; no real number connected.
Not built: listings step 2 extras (Sheet sync, `#sold` commands), the turn pipeline, Langfuse/Sentry.
The location alias table is out of scope for now.
Next step: §17 step 3, the LangGraph turn pipeline (extractor → planner → tools → responder → validator).
