# Real Estate Lead Agent: Architecture & Design

> A WhatsApp AI agent for a Pakistani real estate agent whose leads come from **Zameen.com** listings.
> It replies to every buyer within a minute, 24/7, qualifies them, answers listing questions, books
> site visits and hands hot leads to the human agent, and it keeps working when buyers reply days later.
>
> Client: **Trez Enterprises**, Karachi (Zameen agent_id 200295, ~75 listings, sizes in Sq. Yd).
> Examples below that use Lahore areas or marla are illustrative.
>
> Status: **scraper, data layer, WhatsApp plumbing and Phase 1 of the agent built and tested** (see §0) ·
> Last updated: 2026-10-07

---

## Table of contents

0. [Implementation status and changes to the design](#0-implementation-status-and-changes-to-the-design)
1. [The problem](#1-the-problem)
2. [What we automate](#2-what-we-automate)
3. [How Zameen leads arrive](#3-how-zameen-leads-arrive)
4. [Why agents fail in production (Redis research)](#4-why-agents-fail-in-production-redis-research)
5. [System architecture](#5-system-architecture)
6. [Data model](#6-data-model)
7. [Lead qualification](#7-lead-qualification)
8. [Async WhatsApp: late replies, many questions, episodes](#8-async-whatsapp-late-replies-many-questions-episodes)
9. [Deterministic re-entry pipeline](#9-deterministic-re-entry-pipeline)
10. [Listing freshness: how we know a listing is sold or removed](#10-listing-freshness-how-we-know-a-listing-is-sold-or-removed)
11. [Zameen sync job (scrape → diff → events → context)](#11-zameen-sync-job-scrape--diff--events--context)
12. [Finding the right listing when the buyer is unsure](#12-finding-the-right-listing-when-the-buyer-is-unsure)
13. [Follow-ups & WhatsApp pricing](#13-follow-ups--whatsapp-pricing)
14. [Tech stack & hosting](#14-tech-stack--hosting)
15. [Observability, tracing & debugging](#15-observability-tracing--debugging)
16. [Guardrails & privacy](#16-guardrails--privacy)
17. [Build order](#17-build-order)
18. [Open questions / to verify](#18-open-questions--to-verify)
19. [Sources](#19-sources)

---

## 0. Implementation status and changes to the design

The sections after this one are the original design. This section says what is built and **where the
build deliberately differs**, with the reason. When they disagree, this section and the code win; the
migrations in `supabase/migrations/` are the source of truth for the schema.

### Built and tested (2026-10-07)
| Part | Where | Notes |
|---|---|---|
| Zameen scraper + validator | `scraper/` | Structured data only; complete-run proof; field-change warnings |
| Data layer | `sync/`, migrations | Listings with Zameen's full object, coordinates, amenities, installment plans; `listing_events`; 2-miss rule; photos in Storage; stale-photo pruning |
| WhatsApp plumbing | `app/` | Signed webhook, dedupe, debounce, per-lead lock, delivery statuses, dry-run sending |
| Agent, Phase 1 | `agent/` | Extractor → planner → tools → responder → validator in LangGraph; memory in Postgres; handoff with full context |
| Tracing | `agent/trace.py`, Langfuse Cloud (US) | One trace per turn, one session per buyer, every step and tool with input and output, masked phones, scores (§15) |
| Live testing tool | `scripts/chat.py` | Talk to the agent as a buyer with the real model; prints each turn's trace link |
| Live verification (2026-10-07) | `scripts/chat.py` + traces | Ten kinds of buyer run live repeatedly and their traces read back; every fault found is fixed (rows below) and covered by a test |

**Phase 1 scope (agreed with the client side):** answer listing questions from data; qualify one question
at a time with memory across turns; suggest listings; location hierarchy; photos and video links;
human handoff with full context. **Out for now:** visit booking, comparing listings, saved searches, FAQ
(anything not in the data is handed to the agent, never guessed).

### Changes to the design, and why
| Design said | Built | Why |
|---|---|---|
| Haiku (extractor) + Sonnet (responder) | **Any model via OpenRouter**, set in `.env` per role (currently `deepseek/deepseek-v4-flash-vision-exp`, reasoning off) | The user chooses and tests models. Live: reasoning on took 12-21 s per extraction and once misread a message; off: 2-3 s, same answers. Replies take 3-5 s and cost < $0.001 |
| Read listings from the page | **Only Zameen's structured page data** (`window.state`), whole object kept | Page text rounded prices and sizes, cut descriptions, padded results with other agencies' cards and once attached a non-listing photo. Structured data cannot be misread, and a format change stops the run loudly |
| `area_aliases` table + normalisation (§6, §12) | **No alias table.** The extractor picks a place from the real list of Zameen place names by id; a conservative fuzzy match is only a typo backup | No hand-written aliases to maintain; the model understands "Askari V", "malir cantt" |
| No match: "nothing found" | **Closest options** when nothing fits everything at any level: same buy/rent and type, ranked by how few and how small the differences (budget, size, bedrooms) and by nearness to the asked place; each result says what it misses (`differs_from_request`) and the reply must say first that nothing fits exactly (checked) | Live: "10 marla ghar Bahria Town, 5 crore" found nothing, and "jo hai woh dikha dein" got the area question again. Buy/rent not said and nothing in the asked place: if only one way has anything nearer or closest, those are shown (live: plots asked after Askari 6 got "our agent will contact you") |
| Relax area to "nearby phase" (§12 Case 3) | **Climb Zameen's tree one level at a time**: exact place (with everything under it), else the first level up that has matches, closest first, naming the level and each distance | The client's rule. Places containing all stock (Pakistan, Sindh) are not offered to the model, a named place not in the tree is never swapped for a broader one, and a new place drops the old one |
| `takeover` on/off | **Handoff states** `none` / `requested` / `taken` | Marking a lead hot alerts the agent but the bot keeps serving (facts only, no questions, no negotiation) until the agent actually takes over; it re-checks right before sending. Agent-to-agent reassign keeps the chain |
| Validator checks prices and MUSTs (§9) | Also checks claims the responder **declares**: listings called available, "we have none", "our agent will contact you", photos coming / no photos, unanswered questions; and what it can see directly: the agent mentioned without a handoff, a question when none was planned, the model's JSON pasted into the message. Facts it accepts = everything the responder was shown (incl. "which one?" candidates, a MUST's old price, the buyer's own amounts) | Live runs showed the model inventing "we have no flats here", "the agent will send photos", "photos not available" (3 were sent), "visit karna chahenge?" and a raw JSON reply. Unanswered questions go to the agent. Known limit: a claim that is neither declared nor directly visible (e.g. an undeclared "we have nothing there") can still pass |
| Buy/rent from the extractor | **Purpose counts only when the buyer states it**; until then a stock preview (counts for sale and rent, no prices) | The model guessed "buy" from "flat chahiye". The rule's first wording ("wanting a property does not say which") then made the model drop the **other** fields too when buy/rent was not said: "mujhe ek plot chahiye" kept the type 0/8, "ghar chahiye 3 crore tak" 0/8. Reworded ("leave only purpose null"): 8/8 both. Reasoning on also fixes it but takes 12-65 s. After a long chat, words that gave the size were not reused for the type ("200 gaz ka ghar": house 2/10); the prompt now says the same words can give several fields: 10/10. "I need a house" got no type 0/8 (the model read a house as "a home", not a kind); the prompt now says a home asked for is a house: 8/8, and nothing is invented for "Askari 6 mein kuch hai?" (0/8). A bare value without its evidence (`"location_id": 6655`), which failed the whole extraction, is now accepted and dropped as not said. A message with no Urdu letters is never labelled Urdu script (code, from the letters) |
| Memory then alert | **Memory is committed before the handoff alert is built** | Otherwise the alert missed the very listing being negotiated |
| Rent is monthly | **Rent frequency as Zameen states it**, else unknown | Zameen leaves it empty; never assumed |
| WhatsApp photos | **Converted to JPEG** when needed, and **shrunk to WhatsApp's 5 MB** limit; 4 sent at a time, and the reply says that number (`photos_coming_said`, checked) | Zameen serves WebP; WhatsApp images accept only JPEG/PNG. Live: "Photos 11 hain, bhej raha hoon" while 4 went out |
| Handoff alert on WhatsApp (§7) | Sent as an **approved Meta template** when `WHATSAPP_ALERT_TEMPLATE` is set (body `{{1}}` = the alert on one line), else as text | The alert is business-initiated: outside the agent's 24-hour window Meta delivers only templates, and a plain text fails later (error 131047), silently for the buyer |
| "wo DHA wala" resolved from the buyer's history (§12 Case 2) | The extractor also gets **our last list, numbered in the order the reply named them** (`turns.plan.listings_mentioned`), so "pehla / doosra / sasta wala" maps to a listing. Still unclear → the one question is "which one?" with the candidates; nothing is answered about a guess | Live trace: "pehle wale" was unresolved (all three shown at the same instant), the correct answer was rejected and an empty message went out |
| Link → ID (§12 Case 1) | **Found in code** in the buyer's text (numbers that are our Zameen ids, checked in the DB) | The model once returned no id for a message with a link |
| Area relaxing (§12) | **Code checks the model's place against the buyer's own words** (every extracted value carries `said`, its evidence) and today's place list (`places_named`): words that fit **several places** ("Emaar" = Panorama / The Views, "Askari" = 2/4/5/6) are **asked**, never searched across the city; words naming exactly one place overrule a different pick; words naming none ("Askari V", "malir cantt") keep the model's reading. The answer fills `location_id` (stored as that open question) and shows what is there. No place name is written in code or prompts | Live: Askari 5 flats were shown for "Emaar"; "askari" was mapped to DHA Defence; with an Emaar example removed from the prompt the model guessed one tower 7 times in 8 |
| One extraction per message | **Two readings at once** (`EXTRACTOR_READINGS`, default 2), combined: the reading with most values backed by the buyer's words is the base; the other fills only fields it left empty, also backed by their words; intents and signals from the base | Replaying a live turn's exact input 10 times: one reading dropped "plot" and most details 2 times in 10. Parallel, so no extra wait; about the extractor's cost again (small) |
| Reaction to what we showed | When a turn has no listing, search or preview, **our last reply's listings are read again** from the database (`listings_in_our_last_reply`), not recorded as inquiries | Live: "bohat mehnge hain" named no listing; the reply repeated prices from the chat (not facts), was rejected twice and the template went out |
| One loose value type for every `wants` field | **Each field has its own typed value and description** in the Pydantic schema (enums for purpose, type, timeline, payment, decider, use; whole PKR; sq yd; a list of kinds), so the schema says what each field holds; a field filled wrongly is dropped alone (logged), the rest of the reading kept | The fields had no description and accepted "any value"; the meanings were only in the prompt text. "Askari 6 house price?" kept the type 0/8, then 6/8 with the typed schema |
| Models | **`openai/gpt-6-luna`** for the extractor and the responder | Same prompt and schema, 2026-10-07: price questions kept the type 36/36 (DeepSeek v4 flash 30/36; "DHA flat rates?" 1/6); hard readings after a long chat 45/50 vs 40/50 (DeepSeek read the Urdu "پہلے والے" as the second listing 5/5); no false types; ~4 s, $0.00016 per reading. Qwen 3.8 flash 44/50 but slower (p90 10 s); Gemini 3.8 flash failed with reasoning off. Responder: the DeepSeek models ran away under the reply's JSON schema (one call 7,954 tokens in 36 s, others past 90 s; 33 of 42 live replies fell back to the template); gpt-6-luna answered the same real prompts in 2-3 s, ~200 tokens |
| Price talk | The responder declares `judges_price` (accepts, refuses or judges an offer, or says whether the price can change); the validator rejects it. A turn about one of our last list's listings also gets the others (`read_last_shown`) | Live: "wo 8.5 crore pe nahi de sakte" to a token offer; "pehle aur doosre mein farq?" got only one listing and the other's price was rejected |
| Model calls and load | **Every model call has a total deadline** (`LLM_TIMEOUT_SECONDS`, default 25; httpx's timeout counts each read only); **a responder that fails or times out gets the template reply**, not silence; the worker runs as many turns at once as it has database connections (`WORKER_MAX_TURNS`, default 10), and a turn is stopped at 120 s | Live, 17 buyers at once: one call stayed open for 300 s while sending little, and turns waited 30 s for one of 5 connections (arq runs 10 jobs) and failed: those buyers got no reply |
| Extractor field order | `signals` comes **before** `intents` and is required | After a 12-message chat, "8 crore final karein to aaj token de dun" was caught 1/10 times with signals last, 10/10 with signals first |
| Template fallback (§9) | **Never empty**: with no facts it says the agent will reply and hands the message to the agent; it records the listings it named | Live: an empty WhatsApp message was sent |
| Strong buying signal +15 (§7) | **Token / bayana offer = hot**, remembered as `ready_to_pay`; handoff reasons ordered (ready to pay, negotiation, visit, legal, ...) not alphabetical; price talk loads the listing being discussed | Live: "aaj token de dun" stayed WARM and the true price was rejected |
| Agent alert (§7) | Wants and open questions **in words** ("buy · house · Askari 6 · up to PKR 8.5 Cr") | Raw slot names and numbers in the first alerts |
| Hosting (§14): Upstash Redis, ~$5 host | **Railway** (webhook + worker from one Docker image, start commands set per service since Railway dropped `railway.json` config files; **Railway's Redis**), hosted Supabase via the **transaction pooler** (port 6543, prepared statements off; the session pooler's 15 clients ran out during a redeploy; the nightly sync uses the session pooler), **GitHub Actions** for the daily scrape + sync, Sentry optional (phones masked). Steps in DEPLOY.md | arq polls Redis ~twice a second (~5M commands/month); Upstash free allows 500K. Supabase's direct connection is IPv6-only, the pooler is IPv4 |
| Langfuse trace (§15) with ingest/stt/reentry spans | **Built** with steps `load-context`, `extract-request`, `plan-turn`, `run-tools` (each tool), `write-reply`, `check-reply` (guardrail), `use-template`, `send-reply`, `send-media`, `save-memory`, `open-handoff`, `alert-agent`; scores `reply-passed`, `used-template`, `handoff`, `fit`, `intent`, `priority`, `outcome`; `turns.langfuse_trace_id` links the record to its trace | Names are verb-first and stable (Langfuse best practice: filters and evaluators refer to them). Re-entry findings appear inside `plan-turn` (`must`); `stt` comes with voice notes. Phone numbers are masked before export. Local Langfuse runs in Docker (`observability/`) |

### Not built yet
Real WhatsApp number (Meta token) · Trez's agents in `agents` (handoff alerts need them) · the approved alert template in Meta · agent
commands (`#take`, `#release`, `#sold`) · episode summaries for returning buyers (change reports on
return already work) · voice notes · follow-ups · Sheet sync · Sentry · dashboard. (Deployed 2026-10-07 in dry run: DEPLOY.md.)

---

## 1. The problem

The client has many listings on Zameen.com. Buyers message or call about them, but the agent can't
reply at night or while busy on site visits, so **leads are lost simply because nobody replied in time.**

Why reply speed matters so much:
- **Speed:** leads contacted within 5 minutes are about **100× more likely to convert** than leads contacted
  after 30 minutes, and **21× more likely to be qualified** (MIT study).
- **Portal leads go cold fastest:** a portal lead reached after 2h converts at about **40%** of its 5-minute
  rate (versus about 70% for the agent's own website leads).
- **The first agent to reply usually wins:** about 78% of deals go to the first responder.
- **Zameen buyers shop around:** one buyer often messages 5–10 agents about similar plots. Whoever replies
  first with real details gets the site visit.

**Goal:** reply in under 60 seconds, 24/7, with **correct** listing details → qualify → book a visit → hand off.

---

## 2. What we automate

| # | Problem | What the agent does |
|---|---|---|
| 1 | Nobody replies at night or during site visits | Replies instantly, 24/7 |
| 2 | Same questions all day (price, size, possession, installments, approval, transfer fee) | Answers from listing data and an FAQ knowledge base |
| 3 | Agent's time goes to time-wasters | Qualifies and scores leads (Hot / Warm / Cold / Junk) |
| 4 | Listing sold → lead lost | Suggests 2–3 similar listings |
| 5 | Visit back-and-forth, no-shows | Books visits, sends a location pin, reminders the day before and 2h before |
| 6 | No follow-up (deals need 5+ touches) | Runs follow-up sequences and new-listing alerts |
| 7 | Agent doesn't know the buyer's history | One-message lead summary at handoff |
| 8 | Urdu / Roman Urdu voice notes | Speech-to-text, then replies in the buyer's language |
| 9 | Stale listing data | Single source of truth + Zameen sync + verification loop |
| 10 | No idea what works | Weekly report: leads → visits → deals per listing |
| 11 | Overseas Pakistani buyers in other time zones | Separate flow (video tours; documents and power of attorney questions go to the agent) |

**MVP = items 1–3. Items 4–6 are where the extra revenue comes from.**

---

## 3. How Zameen leads arrive

1. The buyer clicks **WhatsApp** or **Call** on a Zameen listing. The WhatsApp message usually arrives
   prefilled with the listing link or reference (verify this on the client's listings).
2. The client's WhatsApp number is connected to the **Meta WhatsApp Cloud API** in **coexistence mode**,
   so the agent can keep using the WhatsApp Business app on the same number.
3. Messages reach our webhook.

Zameen has a built-in lead manager (in Zameen Pro) but we found **no public API**. Listing data
comes from a **Google Sheet** (source of truth) + **Zameen sync** + **agent commands**
(see §10–11).

---

## 4. Why agents fail in production (Redis research)

Redis, *"The 4 failure modes of agent context"* (May 2026): **"Agents don't have an intelligence
problem. They have a context problem."**

| Failure mode | Finding | How it would hit us | Our fix |
|---|---|---|---|
| **Fragmentation**: partial or stale data → confident wrong answers | GPT-4-Turbo: 19% correct searching one shared vector store vs 85% with the right page | Says "available" for a listing sold yesterday | Exact lookup by ID, fresh sync, `last_verified_at`, "stale = unknown", event-driven updates (§10–11) |
| **Opacity**: data exists but the agent can't find it | Access controls and data layout block discovery | "wo DHA wala 10 marla corner" isn't matched; marla/kanal, "Ph 6" vs "Phase VI" | Fixed listing fields, alias table, structured tool output (§12) |
| **Speed degradation**: delays add up across steps | Tool-using agents make ~9.2× more LLM calls; tools take 35–61% of request time | 40s reply = lost lead | Redis cache, FAQ cache, one main LLM call per reply, parallel tools, 8s budget + holding reply |
| **Non-accumulation**: no persistent memory | 240-message session: 54.3% recall without structured memory vs 99.6% with | Re-asks budget on day 9 | Two-layer memory: lead profile + episode summaries; weekly review of failures (§8, §15) |

Also from the same research and related 2026 reports:
- About **5% of production LLM calls fail**, mostly from rate limits and timeouts → retries, a fallback
  model and a queue.
- These failures are **wrong answers, not crashes**: a confident, nicely formatted, wrong reply with nothing
  in the logs → validator + nightly evals.

---

## 5. System architecture

**Key principle: the conversation is never held in memory.** Each turn is processed fresh: rebuild
context from the DB, decide, reply, save. A reply 3 seconds or 3 days later goes through the same path.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 1. INGRESS  (FastAPI)                                                │
│   Meta webhook → verify signature → dedupe by wa_message_id          │
│   → enqueue → return 200 immediately (Meta retries slow webhooks)    │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 2. PER-LEAD INBOX  (Redis)                                           │
│   • lock per phone number → one worker per lead at a time            │
│   • debounce 6–8s → "hi" + "available?" + link = ONE turn            │
│   • turn_seq → if a new msg arrives mid-generation, discard the      │
│     draft and regenerate with everything                             │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 3. PRE-PROCESS                                                       │
│   voice → STT (Urdu)  ·  image → vision (Zameen screenshot)          │
│   link → listing ID   ·  location pin → lat/lng                      │
│   quoted reply (context.id) → the exact message they're replying to  │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 4. CONTEXT BUILDER                                                   │
│   lead profile (slots + confidence + timestamps) · open questions    │
│   listings discussed + CURRENT status · pending lead_notices         │
│   episode summaries + last ~10 raw messages                          │
│   gap ≥ 6h → RE-ENTRY PIPELINE (§9)                                  │
│   takeover flag on → stop, just log                                  │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 5. TURN PIPELINE  (LangGraph)                                        │
│  a) EXTRACTOR  (LLM, structured) → intents[], slot updates,          │
│     listing refs, answers to open questions, buying signals          │
│  b) PLANNER    (plain Python) → what to answer, which tools, next    │
│     question, handoff?, score update, merge re-entry directives      │
│  c) TOOLS      (run in parallel) resolve_listing · search_listings · │
│     get_listing · faq_search · book_visit · schedule_followup ·      │
│     handoff                                                          │
│  d) RESPONDER  (LLM) → ONE WhatsApp reply                            │
│  e) VALIDATOR  (code) → MUST directives covered, every price/size/   │
│     status matches tool output → else regenerate → else template     │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 6. SEND + PERSIST                                                    │
│   send text/media/buttons → save messages, turn record, slot changes,│
│   score, open questions, status_shown snapshots → reschedule or      │
│   cancel follow-ups                                                  │
└──────────────────────────────────────────────────────────────────────┘

Side services
 • SCHEDULER (arq): follow-ups, visit reminders, 24h-window nudges
   (cancelled when the lead replies)
 • LISTING SYNC: Google Sheet on-edit webhook + Zameen sync job (§11)
   → listing_events
 • AGENT SIDE: WhatsApp alerts, #sold-style commands, dashboard,
   takeover. Coexistence echoes (agent replies from the Business app)
   should reach the webhook → the bot pauses
 • EVALS: nightly replay of ~50 real test chats against live data
```

### Who decides what

| Part | Owner |
|---|---|
| Is this a returning buyer? stale details? listing changes? new matches? | **Code** |
| Stage playbook, merging the buyer's message into the plan, scoring, handoff rules | **Code** |
| Understanding the buyer (Roman Urdu, voice, vague references) | LLM (extractor) |
| Writing a natural, short reply | LLM (responder) |
| Checking MUST items and facts in the reply | **Code** (validator) + template fallback |

---

## 6. Data model

> Original design. The built schema is in `supabase/migrations/` (§0): no `area_aliases`; added `agents`,
> `handoffs`, `ingest_runs`, `locations` (Zameen tree), `listing_media`, and Zameen's full object per listing.

```
leads           id, phone, name, language, stage, fit_score, intent_score, priority,
                last_inbound_at, last_outbound_at, takeover(bool), created_at

lead_slots      lead_id, slot, value(jsonb), confidence, source(stated|inferred),
                status(unknown|asked|answered|stale), asked_count, updated_at

open_questions  lead_id, question_key, asked_at, wa_message_id, status(open|answered|dropped)

lead_listings   lead_id, listing_id, relation(inquired|shown|liked|rejected|visited),
                reason, status_shown, price_shown, shown_at

lead_notices    lead_id, listing_id, event_type, priority, status(pending|delivered|expired), created_at

episodes        lead_id, n, started_at, ended_at, summary

messages        lead_id, direction, wa_message_id, type, text, media_url, context_id,
                delivery_status(sent|delivered|read|failed), at

turns           id, lead_id, episode, inbound_msg_ids[], transcript, extracted(jsonb),
                directives(jsonb), plan(jsonb), tools(jsonb), reply, validator_result,
                latency_ms, cost_usd, langfuse_trace_id, created_at

listings        id, zameen_id, title, society, phase, block, type, size_marla, beds,
                price_pkr, status, features[], media[], content_hash,
                last_verified_at, last_seen_on_portal_at, missing_runs

listing_events  id, listing_id, type(new|price_changed|details_changed|status_changed|
                missing_from_portal), old(jsonb), new(jsonb),
                source(sheet|agent_cmd|zameen_scrape|agent_verify|echo), at

area_aliases    (not built: places are chosen from the Zameen tree by id, §0)
saved_searches  lead_id, filters(jsonb), active, created_at
followups       lead_id, type, run_at, status
visits          lead_id, listing_id, slot_at, status(tentative|confirmed|done|no_show|cancelled)
```

---

## 7. Lead qualification

### Slots
| Slot | Values | Notes |
|---|---|---|
| `intent` | buy / rent / invest | Picks the flow |
| `property_type` | house / plot / flat / commercial / plot file | |
| `areas[]` | DHA Ph 6, Bahria Town … | Can be more than one |
| `size` | 5–10 marla | Normalize kanal → marla (1 kanal = 20 marla) |
| `budget` | min/max PKR | Normalize "4 cr", "40 lakh", "4.2" |
| `timeline` | <1mo / 1–3mo / 3–6mo / browsing | |
| `payment_mode` | cash / installments / bank loan / selling another property first | |
| `decision_maker` | self / with family / for someone else | |
| `must_haves[]` | corner, park facing, near main road | |
| `deal_breakers[]` | "not near nala", "no basement" | Learned from rejected listings |
| rent-only | family/bachelor, move-in date | |
| `overseas` | yes/no, country | Separate flow |

Each slot stores **value + confidence + source (stated/inferred) + timestamp**.

### Two scores (computed in code; the LLM only detects signals)
- **Fit (0–100): can we serve them?** Budget, area and size match the inventory we actually have.
- **Intent (0–100): how ready are they?** Timeline, payment mode, buying signals (token/bayana, transfer,
  visit "today", asks for documents), reply speed and how fully they answer. **Decays weekly** while they're silent.

```
                 INTENT high           INTENT low
FIT high    →   🔥 HOT: alert now      🌱 NURTURE: new-listing alerts
FIT low     →   🔁 REDIRECT: show       ❄️ COLD / polite close
                alternatives / partner
```

Suggested points: budget match 20 · timeline (<1mo 25 / 1–3mo 15 / browsing 0) · cash 15 / installments 8 ·
purpose 10 · decision maker 10 · flexibility 5 · strong buying signal +15 · red flag
(dealer fishing for inventory, only asks "last price", never answers) −20.

### How to ask
- **Give information first, then ask one question.** Max 1 question per message, and always answer the buyer's questions first.
- Order: what they want → **budget** ("tell me your budget and I'll shortlist the 3 best") → timeline and payment (around
  visit booking) → decision maker (while booking).
- **Never re-ask an answered slot.** After 2 unanswered asks, stop: infer it or leave it to the agent.
- Use **reply buttons** for questions with fixed answers (budget bands, timeline).

### Handoff triggers
Hot priority · negotiation · legal or ownership questions · asks for a human · frustration · 2× "I don't know" · visit booked.

### Handoff message to the agent
> 🔥 HOT lead, Ahmed (0300-xxxxxxx). Listing: 10 Marla, DHA Ph 6, PKR 4.2 Cr. Buying to live in, cash, within
> 3 weeks, coming with his wife. Asked about transfer fee and whether there's room on price. Visit booked Sat 11am. [Open chat]

---

## 8. Async WhatsApp: late replies, many questions, episodes

### Episodes
An **episode is one sitting** of the conversation. **A gap of more than 6h starts a new episode.**

```
Mon 10:02–10:20pm  Episode 1: asked about 10M DHA Ph6, budget 4cr
   …3 days…
Thu 9:15–9:40am    Episode 2: "koi aur option?", saw 3, rejected 1 (too far)
   …10 days…
Sun 6:00pm         Episode 3: books a visit
```

When an episode closes, a background job:
1. writes a short **summary**
2. updates the **lead profile** slots
3. keeps the **status_shown / price_shown** snapshots for every listing discussed

Why: (a) memory stays small and sharp (the LLM gets the profile + summaries + the last ~10 messages,
not 300); (b) it marks a **returning buyer**, which triggers the re-entry pipeline; (c) analytics (episodes before a visit).

### Late answers: the open-questions list
The bot asks "Budget kitna hai?" on Monday. The buyer replies "3 tak" on Thursday. The extractor receives the
open questions with each turn, so "3 tak" → `budget.max = 3 cr`. A **quoted reply (`context.id`)** is the most
reliable signal. "haan" → the most recent open yes/no question. If it's ambiguous, confirm: *"3 crore tak budget, sahi samjha?"*

### Many questions or bursts of messages
1. The debounce merges the burst into one turn.
2. The extractor returns a **list** of intents:
   ```json
   {"intents":[
     {"type":"negotiation","listing_ref":"current"},
     {"type":"faq","topic":"utilities.gas","listing_ref":"current"},
     {"type":"show_listing","listing_ref":"5 marla mentioned earlier"},
     {"type":"faq","topic":"payment_plan","listing_ref":"current"}],
    "slot_updates":{"payment_mode":{"value":"installments","confidence":0.6,"source":"inferred"}},
    "answers_to_open_questions":[]}
   ```
3. The planner handles each item (facts from data, negotiation → agent).
4. The responder writes **one** reply, grouped by listing, answering in order. More than 4 questions → answer
   the main ones and offer the rest via the agent.
5. **New message mid-generation:** if `turn_seq` has moved on → discard the draft and regenerate.

---

## 9. Deterministic re-entry pipeline

**Code decides what to recap, what's stale and what changed. The LLM only writes the reply.**

```
message arrives → [CODE] gap = now - last_inbound_at
   gap < 6h  → normal turn
   gap ≥ 6h  → RE-ENTRY (pure code):
       1. close + summarize the previous episode
       2. load state (stage, slots, lead_listings, open questions, visits, lead_notices)
       3. run checks → FINDINGS
       4. apply the stage playbook → DIRECTIVES (MUST / SHOULD / NEVER)
       5. build the BRIEFING
→ [LLM] extractor → [CODE] planner merges the buyer's intents with the directives
→ [LLM] responder → [CODE] validator
```

### Checks
**a) Slot shelf life**
```python
SLOT_TTL_DAYS = {
    "intent": 90, "areas": 30, "size": 30, "budget": 14,
    "timeline": 7, "payment_mode": 30, "decision_maker": 60,
    "visit_availability": 1,
}
```
Plus timeline arithmetic in code: "within 1 month" stated 25 days ago → urgent, about 5 days left.

**b) Listing changes:** compare `status_shown` / `price_shown` with the current data → `gone | on_hold | price_drop | price_up | unverified`.

**c) New matches** since the last episode, excluding rejected listings.

**d) Unfinished business:** open questions, past or upcoming visits, agent takeover or app replies since then, pending `lead_notices`.

### Stage playbook
```python
PLAYBOOK = {
  "browsing":        [("SHOULD","brief_welcome_back"), ("SHOULD","offer_new_matches_if_any"),
                      ("SHOULD","ask_one_missing_core_slot")],
  "qualifying":      [("SHOULD","recap_known_requirements"), ("MUST","confirm_stale_slots"),
                      ("SHOULD","continue_open_question")],
  "shortlisted":     [("MUST","report_listing_changes"), ("SHOULD","offer_new_matches_if_any"),
                      ("SHOULD","push_visit_booking")],
  "visit_scheduled": [("MUST","report_listing_changes"),
                      ("MUST","confirm_or_reschedule_visit_if_past_or_soon")],
  "visited":         [("MUST","ask_visit_feedback"), ("SHOULD","next_step_offer_or_alternatives")],
  "negotiating":     [("MUST","handoff_to_agent_immediately")],
  "cold":            [("MUST","reconfirm_requirements"), ("SHOULD","offer_new_matches_if_any")],
}
```
Global rules: a liked listing is gone / on hold / changed price → MUST report it first + offer alternatives. An unverified
listing → NEVER say "available"; trigger agent verification. Takeover on → no bot reply.

### Gap-based behaviour
| Gap | Behaviour |
|---|---|
| < 6h | Continue |
| 6h–7d | New episode, short recap, report listing changes |
| 7–30d | + confirm stale budget and timeline (one line) |
| 30d+ | Returning lead: "Kya abhi bhi same requirement hai?" |

### Example briefing (goes into the responder's context)
```
<returning_lead>
gap: 10 days (episode 3)   stage: shortlisted   priority: WARM
language: Roman Urdu      name: Ahmed

PROFILE (✓ fresh, ⚠ stale – confirm)
  ✓ intent: buy to live          ✓ area: DHA Ph 6 / Ph 5
  ✓ size: 10 marla, corner pref  ⚠ budget: ≤4 cr (21 days ago)
  ⚠ timeline: "within 1 month" (21 days ago → ~9 days left)
  ✗ payment_mode: unknown (asked once)

PREVIOUS EPISODES
  E1: Inquired #48213 (10M Ph6, 4.2cr). Asked about gas & installments.
  E2: Saw 3 options. Liked #48213. Rejected #48300 – "near nala".

LISTING CHANGES:  #48213 available → SOLD (shown 26 Sep)
NEW MATCHES:      #48510 10M Ph6 Blk K 4.1cr park facing (verified today)
                  #48522 10M Ph5 3.85cr corner (verified yesterday)

DIRECTIVES
  [MUST]   Tell buyer #48213 is sold. Don't offer it.
  [MUST]   Offer #48510 and #48522.
  [SHOULD] Push visit (tomorrow 11am / 4pm).
  [SHOULD] Confirm budget – max ONE question.
  [NEVER]  Mention #48300. Never state availability for unverified listings.
</returning_lead>
```
Responder rule: *MUST items first → answer the buyer's message → at most one SHOULD question → under ~80 words.*

### When the buyer's message changes the plan (the buyer's message wins)
| Buyer's message | Planner |
|---|---|
| "wo DHA wala abhi hai?" | Matches the plan → report sold + alternatives |
| "ab rent pe chahiye" | Intent change → drop buy directives, reset slots, switch to the rent flow |
| "budget ab 5 cr hai" | Slot update → re-run search before replying |
| "price kam karo" | Negotiation → handoff, overrides the playbook |
| "bas dekh raha tha" | Low intent → short reply, offer alerts, ask nothing |

### Validator
```python
def validate(reply, plan, facts):
    for d in plan.must:
        if d.kind == "report_gone" and not mentions_listing_gone(reply, d.listing_id):
            return Fail("didn't tell buyer listing is sold")
        if d.kind == "handoff" and not plan.handoff_executed:
            return Fail("handoff not triggered")
    for lid in plan.never_mention:
        if mentions(reply, lid): return Fail(f"mentioned rejected {lid}")
    for price in extract_prices(reply):
        if price not in facts.allowed_prices: return Fail("price not from tools")
    return Ok()
```
Fail → regenerate once → if it fails again, send a **template** for the critical part. Re-entry is unit-testable without an LLM.

---

## 10. Listing freshness: how we know a listing is sold or removed

There are two problems: **A) how the DB learns the listing's status**, and **B) how the bot notices that what it told this buyer is now wrong.**

### A. Sources of truth (several, because the agent will forget to update)
1. **Google Sheet** `status` dropdown → Apps Script on-edit webhook → Postgres within seconds.
2. **Agent WhatsApp commands** to the bot:
   ```
   #sold 48213      #hold 48213      #price 48213 3.9cr      #available 48213
   ```
   → *"✅ 48213 marked SOLD. 4 leads were interested. Send them alternatives?"*
3. **Verification loop:** weekly message per listing with buttons `✅ Available` `⏸ On hold` `❌ Sold` `💰 Price changed`.
   Before booking a visit, if `last_verified_at` is more than 24h old → book as tentative and ask the agent now.
4. **Zameen sync** (§11): missing from the portal → `needs_verification` (a warning, not proof of sale).
5. **Coexistence echoes:** the agent types "ye sell ho gaya" in the Business app → the bot asks the agent to confirm marking it sold.

Rules: **never delete listings** (deleted row → `withdrawn`). **Stale (>7d unverified) means unknown, not available.**

```
available ──► on_hold (token) ──► sold / rented
    │              └──► available (deal fell through)
    ├──► withdrawn (owner pulled it)
    └──► needs_verification (portal gone / stale) ──► agent confirms
```

### B. Per-buyer change detection
Each time the bot shows a listing, it saves a snapshot to `lead_listings.status_shown / price_shown`. On every turn:
```python
def listing_changes(lead_id):
    rows = db.fetch("""
        SELECT ll.listing_id, ll.status_shown, ll.price_shown,
               l.status, l.price_pkr, l.last_verified_at
        FROM lead_listings ll JOIN listings l ON l.id = ll.listing_id
        WHERE ll.lead_id = %s AND ll.relation IN ('inquired','shown','liked')
    """, lead_id)
    changes = []
    for r in rows:
        if r.status in ("sold", "rented", "withdrawn"):        changes.append(("gone", r))
        elif r.status == "on_hold" and r.status_shown == "available": changes.append(("on_hold", r))
        elif r.price_pkr != r.price_shown:
            changes.append(("price_drop" if r.price_pkr < r.price_shown else "price_up", r))
        elif is_stale(r.last_verified_at):                      changes.append(("unverified", r))
    return changes
```
**The bot never trusts the chat history for facts.**

---

## 11. Zameen sync job (scrape → diff → events → context)

This is Redis's **"fresh context via change data capture"**: record each change as an **event**, and let
everything that depends on the data react to it.

```
Zameen agency page ──(every 2–6h, slow rate)──► SCRAPE ─► NORMALIZE ─► DIFF vs listings
                                                                           │
                                                          listing_events (the log)
                                                                           │
        ┌───────────────────────┬───────────────────────┬──────────────────┴─────────┐
        ▼                       ▼                       ▼                            ▼
 update listings          clear Redis cache       affected leads               alert agent
 (+last_verified_at)      listing:{id}            (lead_listings) →            (needs_verification)
                                                  lead_notices
```

```python
def diff(scraped: dict[str, Listing], db: dict[str, Listing], run_id):
    events = []
    for zid, s in scraped.items():
        d = db.get(zid)
        if d is None:
            events.append(Event("new", zid, None, s))
        else:
            if s.price_pkr != d.price_pkr:
                events.append(Event("price_changed", zid, d.price_pkr, s.price_pkr))
            if s.content_hash != d.content_hash:
                events.append(Event("details_changed", zid, d, s))
            mark_seen(zid, run_id)                       # resets missing_runs
    for zid, d in db.items():
        if zid not in scraped and d.status in ("available", "on_hold"):
            d.missing_runs += 1
            if d.missing_runs >= 2:
                events.append(Event("missing_from_portal", zid, d.status, "needs_verification"))
    return events
```

Rules:
- **Missing doesn't mean sold** (it could be expired, quota, unpublished) → `needs_verification` + ask the agent.
- **2 missing runs in a row** before acting. **Stop the whole run** if far fewer listings come back than usual
  (e.g. under 70% of the normal count) and alert us.
- Price changes from the portal are applied directly. New listings → added and checked against `saved_searches`.

How changes reach buyers:
- Each buyer's context updates **automatically** on their next turn, because the context builder reads the current status and compares it with `status_shown`.
- Events also: (1) **clear the cache**; (2) write **`lead_notices`** → MUST directives at re-entry, marked
  delivered once the reply covers them; (3) **proactive rules**: price drop on a Warm/Hot liked listing → follow-up
  template; sold with a visit booked → alert the agent now + template the buyer alternatives; sold for a cold lead → just a pending notice.

Scraping care:
- Only the client's **own** agency page and listing URLs, a few times a day, slowly, with the client's written OK.
- Check `robots.txt` and Zameen's terms first.
- Prefer JSON embedded in the page over CSS selectors.
- **If Zameen challenges or blocks the scraper, don't try to get around it.** Fall back to the Zameen Pro export, the Sheet and agent commands.
- Scraping is one source among several, never the only one.

---

## 12. Finding the right listing when the buyer is unsure

### Case 1: link or screenshot → exact match
Link → ID → direct lookup. Screenshot → vision reads title, price and location → search → confirm with the buyer.

### Case 2: vague reference ("wo DHA wala", "the cheaper one", "jo kal bheja tha")
```
resolve_listing(reference, lead_id):
  1. Look in THIS lead's history (lead_listings) first
  2. Extract attributes; places by id from the real place list (no alias table, §0)
  3. SQL filter → candidates
  4. 1 → confirm with a photo card · 2–3 → WhatsApp list message · 0 → Case 3
```
Never guess silently.

### Case 3: the buyer doesn't know what they want → guided recommendation
1. **Three anchor questions using buttons:** live / invest / rent · budget band · area (or "suggest karein").
2. **Hybrid search:** hard SQL filters (city, `status=available`, verified within 48h, budget within +10%, type) + soft
   ranking (area, how close the size and price are, must-haves, a small boost for text similarity) − penalties (deal breakers, rejected).
3. **Show 3 options, not 20, and make them different on purpose:** best match · cheaper · bigger/better location. Cards with photo,
   price, size, one line on why it fits, and buttons `Visit book karein` · `Aur details` · `Pasand nahi`.
4. **Learn from reactions:** "bohat door hai" → deal breaker; "thora bara chahiye" → raise the minimum size;
   "acha hai lekin mehenga" → similar features, lower price.
5. **Relax filters one step at a time and tell the buyer what changed:** area first, by climbing Zameen's location tree one
   level at a time (built, §0: the first level with matches, closest first, with distances) → size → budget +10% → type.
6. **Nothing found → saved search** → template alert when a match is listed.

### Local terms glossary
marla/kanal · lakh/crore ("4.5" in a house conversation = 4.5 crore) · file (plot file) · possession / non-possession ·
corner · park facing · main boulevard · category plot · token/bayana (advance payment) · transfer fee · NOC / LDA approved ·
double unit · upper portion.

---

## 13. Follow-ups & WhatsApp pricing

- A buyer's message opens a **24-hour window**; free-form replies inside it are free.
- Inside the window: nudge at 1h if they go quiet; a useful message at about 20h, before it closes.
- After it closes: **templates** on day 3, day 7 and day 14, then a monthly "new listings in your budget."
  **Cancel all pending follow-ups the moment the lead replies.** Every follow-up must carry something useful
  (price drop, new match, visit slot).
- Pakistan rates (July 2026): **marketing ≈ $0.0473**, **utility ≈ $0.0100** per message. One source says some
  free categories become chargeable from **1 Oct 2026**. **Verify on Meta's pricing page.**
- Meta restricts general-purpose AI chatbots on the WhatsApp Business API. A bot that only does support for your own
  listings is allowed. **Keep it on-topic.**

### Night mode
11pm–9am: the bot handles the full conversation and books next-day visits. Hot leads (score ≥ 80) alert the agent immediately;
everyone else goes into a 9am morning digest.

---

## 14. Tech stack & hosting

| Layer | Choice | Why |
|---|---|---|
| Database | **Supabase (Postgres)** | Relational data + pgvector (FAQ) + Storage (voice/media) + Auth (dashboard) + Realtime + pg_cron |
| Cache, locks, debounce | **Upstash Redis** | Per-lead lock, debounce, turn_seq, listing cache. Free tier is enough |
| Bot backend | **Python FastAPI + arq worker** | Webhook, queue, LangGraph pipeline, scheduler, scraper |
| Agent framework | **LangGraph** | Fits the stage machine; state per turn only |
| LLMs | **Via OpenRouter**, model per role in `.env` (now `deepseek/deepseek-v4-flash-vision-exp`) | The user picks and tests models; no provider lock-in (§0) |
| STT | ElevenLabs Scribe / Whisper | Benchmark on Urdu voice notes |
| WhatsApp | Meta Cloud API (coexistence) or a reseller like 360dialog | |
| Dashboard | **Next.js on Vercel** | Leads, conversation timeline, takeover, inventory, traces |
| Tracing | **Langfuse** | §15 |
| Errors | **Sentry** | |

### Hosting decisions
- **Supabase: yes.** Free tier limits: projects pause after about a week of inactivity, roughly 500 MB DB / 1 GB storage,
  no automatic backups. Move to Pro (~$25/mo) once the client pays.
- **Vercel: dashboard only.** Don't run the bot backend there: debounce waits, locks, background workers,
  cron jobs and the scraper don't suit serverless functions. **The Hobby plan is non-commercial**, so use Pro ($20/mo) once live.
- **Bot backend: an always-on ~$5/mo host** (Railway / Fly.io / Hetzner VPS). Avoid free hosts that sleep (e.g. Render free):
  a 30s+ cold start on the first message defeats the purpose and can make Meta retry the webhook.

| Piece | Where | Cost now |
|---|---|---|
| Dashboard | Vercel Hobby (demo) | Free |
| DB / storage / auth | Supabase free | Free |
| Redis | Upstash free | Free |
| Backend + worker + scraper | Railway / Fly / Hetzner | ~$5/mo |
| Tracing | Langfuse Cloud free | Free |
| WhatsApp | Meta Cloud API | Your replies in the 24h window are free |

---

## 15. Observability, tracing & debugging

The industry standard has three layers.

### Layer 1: LLM and agent tracing (Langfuse)
Options: Langfuse (open source, self-hostable, free cloud tier), LangSmith, Arize Phoenix, Helicone. All use OpenTelemetry-style
traces. **Pick Langfuse.** One **trace per turn**, **session = lead**:

```
Session: lead_77 (Ahmed)        tags: episode=3, stage=shortlisted, reentry=true
└─ Trace: turn_1842  (msgs: 2, voice: 1)                total 4.8s  $0.011
   ├─ span: ingest            wa_ids=[...], debounce=6.2s, merged=2
   ├─ span: stt               audio=storage://voice/77/1842.ogg lang=ur
   │                          transcript="rehne ke liye, cash hai..." conf=0.91 1.3s
   ├─ span: reentry           findings=[#48213 SOLD] stale=[budget,timeline]
   │                          directives=[MUST report_gone, SHOULD confirm_budget]
   ├─ generation: extractor   (model) input/output JSON, tokens, 0.6s
   ├─ span: planner           plan={...} handoff=false
   ├─ span: tool.search_listings  args={10M, DHA Ph5/6, ≤4cr} → [48510, 48522]
   ├─ generation: responder   (model) prompt + reply, 1.9s
   ├─ span: validator         PASS (musts 2/2, prices verified)
   └─ span: send              wa_msg_id=..., sent→delivered→read
```

### Layer 2: business audit (the `turns` table in Supabase)
The dashboard shows a per-lead timeline with a **"🔍 why"** expander on each bot message (a plain summary of its plan and directives
+ a link to the trace). Store **WhatsApp delivery statuses**: a failed send is a lost lead nobody sees.

### Layer 3: errors and alerts
Sentry · structured JSON logs with `lead_id`, `turn_id`, `trace_id` · alerts for: p95 reply > 15s, validator failures,
a scrape that returns too few listings, template send failures, webhook quiet for hours.

### Weekly improvement habit
Filter traces for validator failures, handoffs, "I don't know" answers and low-confidence STT → add them to a **Langfuse dataset**
→ fix → re-run the dataset before deploying. **Nightly evals** on about 50 real test chats (Roman Urdu, voice notes, late replies,
sold listings, vague references): zero wrong prices or statuses allowed.

### Metrics
Time to first reply (< 1 min) · % qualified · visits booked per 100 leads · handoff rate · wrong-fact rate (0) · cost per lead.

---

## 16. Guardrails & privacy

- Price, availability and size come **only from tool output**. Never invented.
- Negotiation, discounts, legal or ownership questions → human. Never promise a discount.
- Max 2 questions per message (1 during qualification). Reply in the buyer's language.
- Unverified listing → never "available."
- Voice notes in a **private** Supabase bucket, signed URLs in traces. Mask phone numbers in Langfuse.
  Retention policy (e.g. delete audio after 90 days). Note in the first message that the chat may be recorded to improve service.
- Stay on-topic (Meta's chatbot policy).

---

## 17. Build order

1. **Plumbing:** FastAPI webhook, signature check, dedupe, queue, per-lead lock, debounce, persistence, Supabase schema. ✅
2. **Listings:** `listings` + location tree ✅, exact resolver ✅; Sheet sync and `#sold` agent commands ⬜.
3. **Turn pipeline:** extractor (Pydantic schemas) → planner → tools → responder → validator; slots and open questions. ✅
   Built as Phase 1 of the agent without cutting scope (see §0).
4. **Re-entry:** listing change detection on return ✅; episode summaries, slot shelf life, stage playbook ⬜.
5. **Zameen sync module:** scraper, diff, `listing_events` ✅; cache clearing, `lead_notices` ⬜.
6. **Discovery mode:** cards, buttons, learning from reactions, relaxing filters, saved searches.
7. **Scheduler:** follow-ups, visit reminders, verification loop; handoff dashboard (Next.js).
8. **Observability:** Langfuse traces, `turns` table, Sentry, nightly evals.

---

## 18. Open questions / to verify

- [ ] Does Zameen's WhatsApp button prefill the listing link or ID? Check on the client's listings.
- [ ] Is WhatsApp **coexistence** available for the client's number in Pakistan, and do app echoes reach the webhook?
- [ ] Current Meta pricing for Pakistan after 1 Oct 2026 (are service and utility replies still free in the window?).
- [x] Zameen `robots.txt` allows the agency search pages (checked on every run). Still open: terms of use, and whether a
  Zameen Pro export is available as a second source.
- [ ] Where does the client keep inventory today (Sheet / Zameen Pro / notebook)?
- [ ] Client's working hours, visit slots, handoff channel (WhatsApp group vs dashboard).
- [ ] STT benchmark: ElevenLabs Scribe vs Whisper on real Urdu voice notes.
- [x] Supabase free plan: 500 MB database, 1 GB storage, 5 GB egress; pauses after a week idle (checked 2026-10-06).
- [x] WhatsApp image messages accept only JPEG/PNG (5 MB); WebP only as stickers: photos are converted.
- [ ] Trez's agents (names, WhatsApp numbers) for handoff alerts; target response time; night-time rule.

---

## 19. Sources

- Redis – The 4 failure modes of agent context: https://redis.io/blog/the-4-failure-modes-of-agent-context
- daily.dev summary: https://daily.dev/posts/the-4-failure-modes-of-agent-context-in-production-zynt7rdjl
- IT Brief – Redis launches Iris (May 2026): https://itbrief.co.uk/story/redis-launches-iris-platform-to-fix-ai-agent-context
- AI Accelerator Institute – Why agents keep breaking: https://www.aiacceleratorinstitute.com/ai-agents-keep-breaking-in-production-heres-why-nobodys-fixed-it-yet/
- LayerLens – Ten agent failures of 2026: https://layerlens.ai/blog/ai-agent-production-failures-2026
- LeadAngel – MIT lead response study: https://www.leadangel.com/?p=520779
- Luxury Presence – Speed to lead: https://www.luxurypresence.com/?p=53210
- Callin.io – Speed to lead: https://callin.io/speed-to-lead-real-estate/
- ChatMaxima – WhatsApp API pricing Pakistan: https://chatmaxima.com/whatsapp-api-pricing/pakistan/
- YCloud – April 2026 pricing change: https://www.ycloud.com/blog/whatsapp-api-message-pricing-update-effective-april-1-2026
- Zameen Help – Manage leads: https://help.zameen.com/hc/en-us/articles/5643846609821-How-to-manage-edit-and-add-new-leads
- WATI – WhatsApp for real estate: https://www.wati.io/industries/real-estate/
- Redis – The state of context engineering (2026 report): survey; navigable, fast, fresh, compounding context
- Meta – WhatsApp Cloud API supported media: https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media
- Supabase pricing: https://supabase.com/pricing
