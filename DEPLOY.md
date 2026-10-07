# Deploying the bot

```
WhatsApp (Meta) ──► Railway: webhook ──► Railway: Redis ──► Railway: worker ──► agent turn
                                                                 │
                     Supabase (hosted): Postgres + photo bucket ◄┘
GitHub Actions (daily 02:30 Karachi): scrape Zameen ──► sync into Supabase
Langfuse Cloud (traces) · Sentry (errors, optional)
```

| Piece | Where | Cost |
|---|---|---|
| Webhook + worker | Railway, one Docker image, two services | Railway Hobby ($5 includes usage) |
| Redis (debounce, locks, job queue) | Railway Redis, same project | inside the $5 |
| Database + photos | Supabase (hosted) | free; Pro $25/mo once the client pays |
| Daily scrape + sync | GitHub Actions (`.github/workflows/sync-listings.yml`) | free |
| Traces / errors | Langfuse Cloud (US) / Sentry | free |

Why Railway's Redis and not Upstash: the arq worker asks Redis for work about twice a second,
roughly 5 million commands a month; Upstash's free tier is 500,000.

**Region: put Railway and Supabase in the same region (Singapore is closest to Pakistan):**
one agent turn makes many small database calls, so the distance between them matters more than
the distance to the buyer.

Secrets never go in the repo (it is public): Railway variables, GitHub Actions secrets, `.env` locally.

---

## 1. Supabase (hosted)

1. supabase.com → New project. Region **Southeast Asia (Singapore)**. Save the database password.
2. Create the schema and the private `listing-photos` bucket from the migrations:
   ```bash
   npx supabase login
   npx supabase link --project-ref <project-ref>
   npx supabase db push
   ```
3. Note three values (Project Settings / the **Connect** button):
   - `DATABASE_URL`: a **pooler** connection string (IPv4; the direct connection is IPv6-only and
     Railway and GitHub cannot reach it):
     - for **Railway** (webhook, worker): the **transaction pooler**, port **6543**. The session
       pooler allows only 15 clients, and a redeploy (old and new copies side by side) used them up
       on 2026-10-07. The app turns prepared statements off, which this pooler needs.
       `postgresql://postgres.<ref>:<password>@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres`
     - for the **GitHub Actions** sync (one long job): the **session pooler**, port **5432**.
   - `SUPABASE_URL`: `https://<project-ref>.supabase.co`
   - `SUPABASE_SERVICE_ROLE_KEY`: API keys → `service_role` (secret: server use only)

## 2. First data load: GitHub Actions

1. GitHub repo → Settings → Secrets and variables → Actions → add the secrets `DATABASE_URL`,
   `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (from step 1), and under **Variables** the variable
   `LISTING_SYNC_ENABLED` = `true` (until then the job is skipped, so it cannot fail nightly).
2. Actions → **Daily listing sync** → **Run workflow**. The first run downloads every photo once
   (about 10-15 min); later runs reuse them from the cache.
3. It then runs every night. A failed run (e.g. Zameen blocked the scrape, which is never worked
   around) emails the repo owner. Listings not seen for 7 days count as unverified.

## 3. Railway

Done for Trez (2026-10-07): project **trez-agent**, everything in Singapore
(`asia-southeast1-eqsg3a`); webhook at `https://webhook-production-20e7.up.railway.app`.
Railway builds both app services from the root `Dockerfile`. Railway no longer accepts
`railway.json` config files, so each service's settings live on the service itself:

| Service | Start command | Other settings |
|---|---|---|
| **webhook** | `sh -c 'uvicorn app.webhook:app --host 0.0.0.0 --port ${PORT:-8000}'` | `PORT=8080`; healthcheck `/health` (60 s); restart on failure; public domain (port 8080) |
| **worker** | `arq app.worker.WorkerSettings` | restart always; no domain |
| **Redis** | Railway's Redis template (`redis:8.2`, volume `/data`) | private network only |

To recreate it: New project → **+ New → Database → Redis**; two empty services `webhook` and
`worker` (Settings → Source → this repo, branch `main`), each with the start command above and
region Singapore; on the webhook, Networking → **Generate domain**.

Variables are stored once as project **Shared Variables**; each app service holds references
(`DATABASE_URL = ${{shared.DATABASE_URL}}`, ...) plus `REDIS_URL = ${{Redis.REDIS_URL}}`:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | from step 1 |
   | `REDIS_URL` | `${{Redis.REDIS_URL}}` (Railway fills it in) |
   | `OPENROUTER_API_KEY`, `EXTRACTOR_MODEL`, `RESPONDER_MODEL`, `EXTRACTOR_REASONING`, `RESPONDER_REASONING` | as in your `.env` |
   | `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | as in `.env`; `LANGFUSE_TRACING_ENVIRONMENT=production` |
   | `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` | random for now (lets `scripts/smoke.py` sign test messages); the app secret is replaced by Meta's in step 4 |
   | `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | **leave empty until Meta is ready**: empty = dry run, nothing is sent |
   | `WHATSAPP_ALERT_TEMPLATE` | name of the approved alert template (see `.env.example`) |
   | `SENTRY_DSN` | optional, from sentry.io (free); errors are reported with phone numbers masked |
   | `DEBOUNCE_SECONDS` | `7` |

Deploys happen on every push to `main`. Check: `https://<domain>/health` → `{"ok": true}`; the
worker's logs show `Starting worker for 1 functions: process_turn`. (Railway labels these lines
"error" only because Python logs to stderr.)

End-to-end check without WhatsApp: a signed, Meta-shaped message to the live webhook, then the
turn read back from the database (dry run: recorded as `not_sent`; the trace is in Langfuse,
environment `production`):
```bash
SMOKE_APP_SECRET=<WHATSAPP_APP_SECRET> SMOKE_DATABASE_URL=<DATABASE_URL> \
  uv run python -m scripts.smoke https://<domain> "Askari 6 mein ghar khareedna hai, 9 crore tak"
```
Measured 2026-10-07: 5-7 s inside the bot (almost all model time; each database step ~0.05 s),
plus the 7 s wait for more messages.

## 4. WhatsApp (when Trez's number is ready)

1. Meta app → WhatsApp → Configuration → Webhook: callback URL `https://<domain>/webhook`,
   verify token = `WHATSAPP_VERIFY_TOKEN`; subscribe to `messages`.
2. Put `WHATSAPP_ACCESS_TOKEN` (a permanent System User token) and `WHATSAPP_PHONE_NUMBER_ID`
   in Railway: the bot leaves dry run and starts replying.
3. Add Trez's agents to the `agents` table (name, WhatsApp number) so handoff alerts reach someone.

## Updating

Code: push to `main` → Railway redeploys both services. Schema: add a migration, then
`npx supabase db push` **before** pushing code that needs it.
