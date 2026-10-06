# Zameen scraper

Collects **Trez Enterprises'** own listings (Karachi, Zameen agent_id `200295`)
into a dated, immutable snapshot. This is the first source for the Zameen sync
module in [ARCHITECTURE.md §11](../ARCHITECTURE.md#11-zameen-sync-job-scrape--diff--events--context).

```bash
cd scraper
npm ci                                   # playwright 1.62.1 + chromium

# 1. Scrape fresh into a new dated folder (must not exist yet)
node scrape-zameen.mjs --output-dir ../data/raw/$(date +%Y-%m-%d) \
  --media-store ../data/media-store --download-media

# 2. Validate: errors block, warnings don't
node validate-zameen-data.mjs --input-dir ../data/raw/$(date +%Y-%m-%d) \
  --require-downloaded-media
```

## Output

```
data/                          git-ignored: third-party content + client inventory
  media-store/<assetId>.jpeg   shared photo pool, reused by every run
  raw/<date>/
    listings.json              every verified listing
    listings/<id>.json         one file per listing (same records)
    rejected-listings.json     visited but not verified as Trez
    run-summary.json           did this run see the WHOLE search?
```

`run-summary.json` is what makes a snapshot trustworthy for diffing:

```json
{ "complete": true,
  "sources": { "sales":   { "expected": 72, "saved": 72, "complete": true },
               "rentals": { "expected": 2,  "saved": 2,  "complete": true } } }
```

`expected` is Zameen's own "1 to 25 of **72** Properties" counter. A run with
`complete: false` (capped, interrupted, or short) must never be used to decide
that a listing has gone.

## Rules it follows

- **Only the client's listings.** Start URLs must carry `agent_id=200295`, and
  every detail page must name Trez Enterprises or it is rejected.
- **Slow and polite.** 2.5–4.5 s between requests, robots.txt checked first.
- **Never get around a block.** A captcha or "unusual traffic" page stops the run.
- **Raw is immutable.** Never edit `data/raw/<date>/`; scrape again instead.
- **Fresh every time.** Don't use `--resume` for the daily run: it keeps
  listings that have disappeared. It only exists to recover an interrupted run.

## Things Zameen does that will bite you (verified 2026-10-06)

| What happens | How the scraper handles it |
|---|---|
| When the agency's results run out, Zameen fills the same list with "Discover more properties" from other agencies (23 of 25 cards on the rentals page) | Stops at that heading |
| The agency search can leave out a live listing: 54550059 was live, by Trez, PKR 10 Crore, yet missing from search (profile said 73 for sale, search returned 72) | Nothing here; the diff step must treat a single absence as "maybe", not "sold" (2 consecutive misses → ask the agent) |
| The results counter is hidden at some viewport widths | Read with `textContent`, not `innerText` |
| Price text runs into the next field: `"PKR 11 Crore Bath(s) 4"` | Raw string is kept beside the parsed amount; re-derive from the raw string downstream |
| About a third of listings changed between 3 Sep and 6 Oct (8 gone, 25 new, 5 price changes) | Scrape at least daily |
