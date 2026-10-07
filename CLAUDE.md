# Real Estate Lead Agent

A client project: a WhatsApp AI agent for **Trez Enterprises** (Karachi; Zameen agent_id `200295`), whose leads
come from Zameen.com listings. It replies 24/7, answers listing questions from data, qualifies and suggests,
and hands leads to the human agent with full context. Their inventory is measured in **Sq. Yd** (not marla).

**Read `ARCHITECTURE.md` first** for the agreed design; its §0 says what is built and what changed since.

## Decisions already made (don't re-argue these without a reason)
- Python **FastAPI** + **arq** worker on an always-on ~$5/mo host. **Not** on Vercel serverless.
- **Supabase** (Postgres + Storage), **Redis** (locks, debounce, queue; Upstash in production).
- **LangGraph** turn: extractor (LLM) → planner (code) → tools → responder (LLM) → validator (code).
  Graph state lives for one turn; the buyer's memory is in Postgres, never a checkpointer.
- LLMs via **OpenRouter** (OpenAI-compatible API), not a provider SDK. Models and options come from
  `.env` (`EXTRACTOR_MODEL`, `RESPONDER_MODEL`, `*_REASONING`, `*_TEMPERATURE`); the user picks them.
  Currently `deepseek/deepseek-v4-flash-vision-exp` for both, reasoning off. Never hard-code a model.
- Code decides tools, questions, scoring, staleness and handoff; the LLM only extracts and words the reply.
- Listing facts come only from tools/DB, never from chat history. Stale (unverified) means unknown, not available.
- **Langfuse** tracing (built): one trace per turn, one session per buyer, phone numbers masked;
  off when its keys are empty. Next.js dashboard on Vercel and **Sentry** for errors: not built yet.
- Zameen scraping: only the client's own listings, slowly; never get around blocks.

## Conventions
- The user prefers tutor-style explanations (why → what → how) and Python.
- Buyers write in Urdu / Roman Urdu / English; keep the glossary in ARCHITECTURE.md §12 in mind.
- Test locally first (local Supabase + Redis in Docker); hosted Supabase later.

## Working rules learned here
- No hand-written text patterns or hard-coded guesses about Zameen's pages: read its structured data
  (`window.state`), derive values from it (e.g. photo size from the page), fail loudly when it changes,
  and keep its whole object so nothing the client entered is dropped.
- Business mappings (Zameen category -> our 6 search types) are allowed, but unknown values must
  degrade gracefully ("other", original kept) and be reported, never break the run.
- Test the agent live with the real model (`scripts/chat.py`) after changing prompts or rules, across
  many kinds of buyer (link, "pehla wala", rent, unknown place, ambiguous place, legal, token offer,
  not interested, dealer, returning buyer), then read the traces back: the live runs found faults
  that scripted tests could not (see ARCHITECTURE.md §0). One run is not proof: the model varies.
- Every claim the model makes about facts or actions must be checkable by the validator: the model
  declares its claims (`says_available`, `claims_no_listings`, `promises_agent_contact`,
  `promises_media`, `says_no_photos`, `unanswered`), and the validator also checks what it can see
  directly (an agent mentioned without a handoff, a question nobody planned, JSON in the message).
- What code can decide, code decides, not the extractor: a link to one of our listings is found in
  the buyer's text in code; a place is checked against the buyer's own words (`said`) and today's
  place list (`places_named`). Never fix the agent by writing today's listing or place names into
  code or prompts: the stock changes. Tests that need a kind of data find it in the live data.
- Measure the extractor over repeated runs with a realistic (long) chat before trusting it: a
  short-context check said 8/8 where the live chat got 1/10.
- Everything the responder is shown is a fact the validator accepts (search results, "which one?"
  candidates, a MUST item's old price); anything else is not.
- Tests never send traces to Langfuse (`tests/conftest.py` blanks the keys). `scripts/chat.py` test
  buyers use 92995555xxxx; test fixtures use other 9299 blocks and clean up only their own.

## Layout
- `scraper/`: Zameen scraper + snapshot validator (Node, Playwright). See `scraper/README.md`.
- `sync/`: snapshot -> Supabase. `parse` (Zameen object -> typed; whole object kept as `zameen_data`),
  `diff` (pure: decides events, 2-miss rule), `ingest` (rows, events, photos, stale-photo pruning).
- `agent/`: the agent. `locations` (Zameen tree, place choices, typo backup), `listings`
  (get/search/resolve; search climbs the tree one level at a time), `media` (gallery photos -> Meta,
  WebP to JPEG; video links), `handoff` (alert with full context, take/release/reassign),
  `context` (rebuild the buyer from Postgres), `extract`, `planner` (pure rules), `runner` (tools),
  `respond`, `validate`, `memory` (write back), `graph` (LangGraph wiring), `llm` (OpenRouter), `money`,
  `trace` (Langfuse: stable step names, masking, scores; every step and tool shows its input and output).
- `app/`: WhatsApp side. `webhook` (verify signature, store, queue, 200), `meta`, `store`, `inbox`
  (Redis debounce + per-lead lock), `turn` (one turn: run the agent, re-check, send, commit, alert),
  `worker` (arq), `sender` (Meta send / dry run), `reply` (agent adapter), `serve` (Windows dev server).
- `scripts/chat.py`: talk to the agent locally as a buyer; shows what it understood, decided, checked, cost.
- `supabase/`: local stack config + migrations. Photos live in the private `listing-photos` bucket.
- `observability/`: local Langfuse (Docker) for viewing traces; secrets in git-ignored `observability/.env`.
- `tests/`: pytest (145). Pure tests always run; DB/Redis tests need the local stack, real-snapshot
  tests need `data/`; they skip cleanly when absent. Model calls in tests are scripted.
- `data/`: git-ignored. `raw/<run>/` immutable snapshots, `media-store/` photos, `archive/` old snapshots.

## Local development
```
npx supabase start                    # Postgres :54322, API :54321, Studio :54323 (Docker)
docker compose up -d                  # Redis :6379
cp .env.example .env                  # keys: SUPABASE_SERVICE_ROLE_KEY (`npx supabase status`), OPENROUTER_API_KEY
uv sync && uv run pytest
uv run python -m sync.ingest data/raw/<run>      # load a snapshot
uv run python -m scripts.chat                    # talk to the agent as a buyer
uv run python -m app.serve                       # webhook on :8000 (selector loop on Windows)
uv run arq app.worker.WorkerSettings             # turn worker
docker compose -f observability/docker-compose.yml up -d   # optional: local Langfuse on :3000
```
Tracing: set `LANGFUSE_HOST`/`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` (Cloud or local). `scripts/chat.py`
prints each turn's trace link. When adding a step or tool, give it a trace step with a stable verb-first name.
Windows: psycopg async cannot use the Proactor event loop; `app/__init__.py` and `app.serve` handle it.

## Status (2026-10-06)
Built and tested: scraper (structured data only), data layer, WhatsApp plumbing, and Phase 1 of the agent:
answers from data, qualifying (one question at a time, memory across turns), suggestions, location
hierarchy, photos/video links, human handoff with full context (bot keeps serving until the agent takes
over; agent-to-agent reassign). Tested live: 3-5 s and < $0.001 per reply.

Phase 1 scope OUT for now: visit booking, compare listings, saved searches, FAQ (anything not in the
data goes to the agent, never guessed). The location alias table is out of scope.

Not done yet: real WhatsApp number (Meta token), Trez's agents in the `agents` table (handoff alerts need
them), agent commands (`#take`, `#release`), returning-buyer episode summaries, voice notes, follow-ups,
Sheet sync, Sentry, dashboard, hosted deploy.
