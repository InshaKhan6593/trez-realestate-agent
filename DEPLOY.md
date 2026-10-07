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
   - `DATABASE_URL`: **Session pooler** connection string (IPv4; the direct connection is
     IPv6-only and Railway and GitHub cannot reach it). Looks like
     `postgresql://postgres.<ref>:<password>@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres`
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

1. railway.com → New project → **Deploy from GitHub repo** → this repo. Region **Singapore**.
2. That first service is the **webhook**. Settings:
   - Config-as-code file: `/deploy/railway.webhook.json` (Dockerfile build, `/health` check)
   - Networking → **Generate domain**. The webhook URL for Meta is `https://<domain>/webhook`.
3. **+ New → Database → Redis** (same project).
4. **+ New → GitHub repo** → the same repo again: the **worker**. Settings:
   - Config-as-code file: `/deploy/railway.worker.json` (no domain needed)
5. Variables. Easiest as project **Shared Variables**, used by both services:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | from step 1 |
   | `REDIS_URL` | `${{Redis.REDIS_URL}}` (Railway fills it in) |
   | `OPENROUTER_API_KEY`, `EXTRACTOR_MODEL`, `RESPONDER_MODEL`, `EXTRACTOR_REASONING`, `RESPONDER_REASONING` | as in your `.env` |
   | `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | as in `.env`; `LANGFUSE_TRACING_ENVIRONMENT=production` |
   | `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` | from Meta (step 4); a long random verify token you choose |
   | `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | **leave empty until Meta is ready**: empty = dry run, nothing is sent |
   | `WHATSAPP_ALERT_TEMPLATE` | name of the approved alert template (see `.env.example`) |
   | `SENTRY_DSN` | optional, from sentry.io (free); errors are reported with phone numbers masked |
   | `DEBOUNCE_SECONDS` | `7` |

6. Deploys happen on every push to `main`. Check: `https://<domain>/health` → `{"ok": true}`;
   the worker's logs show `Starting worker for 1 functions: process_turn`.

## 4. WhatsApp (when Trez's number is ready)

1. Meta app → WhatsApp → Configuration → Webhook: callback URL `https://<domain>/webhook`,
   verify token = `WHATSAPP_VERIFY_TOKEN`; subscribe to `messages`.
2. Put `WHATSAPP_ACCESS_TOKEN` (a permanent System User token) and `WHATSAPP_PHONE_NUMBER_ID`
   in Railway: the bot leaves dry run and starts replying.
3. Add Trez's agents to the `agents` table (name, WhatsApp number) so handoff alerts reach someone.

## Updating

Code: push to `main` → Railway redeploys both services. Schema: add a migration, then
`npx supabase db push` **before** pushing code that needs it.
