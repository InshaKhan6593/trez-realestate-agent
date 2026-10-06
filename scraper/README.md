# Zameen scraper

Collects **Trez Enterprises'** own listings (Karachi, Zameen agent_id `200295`)
into a dated, immutable snapshot. First source for the Zameen sync module in
[ARCHITECTURE.md §11](../ARCHITECTURE.md#11-zameen-sync-job-scrape--diff--events--context).

```bash
cd scraper
npm ci                                   # playwright 1.62.1 + chromium

# 1. Scrape into a new folder (one per run; add THHMM for a second run that day)
node scrape-zameen.mjs --output-dir ../data/raw/2026-10-07 \
  --media-store ../data/media-store --download-media

# 2. Validate: errors block, warnings don't
node validate-zameen-data.mjs --input-dir ../data/raw/2026-10-07 --require-downloaded-media

# 3. Load (from the repo root)
uv run python -m sync.ingest data/raw/2026-10-07
```

## One source: Zameen's own structured data

Zameen's pages carry their data as JSON (`window.state`). The scraper reads
**only that**, never the visible text, so a wording or layout change on the page
cannot silently corrupt a price or drop a listing:

| What | From |
|---|---|
| Which listings are results, how many | search data: `hits`, `nbHits` |
| Paging | `nbPages` + the page's `<link rel="next">` |
| Every listing field | `property.data`, saved whole as `record.zameen` |
| Photos | `property.data.photos`, at the largest size the page itself uses |
| "Is this Trez's?" | `property.data.agency.externalID == 200295` |

If that data is missing or changes shape, the run **stops with an error**
instead of guessing. The visible page text is saved only as evidence.

**Nothing the agent enters is dropped.** `record.zameen` is Zameen's complete
listing object: price, area, rooms, description, amenities, installment plan,
photos, videos, floor plans, contact, statuses, dates, coordinates, and any
field Zameen adds later. The validator compares the fields with the previous
snapshot and **warns when Zameen adds or removes one**.

## Output

```
data/                                   git-ignored: third-party content + client inventory
  media-store/<assetId>-<size>.<ext>    shared photo pool, each photo downloaded once
  raw/<YYYY-MM-DD>[THHMM]/
    listings.json                       every listing (record.zameen = Zameen's object)
    listings/<id>.json                  one file per listing (same records)
    search-hits.json                    Zameen's search data for every result
    rejected-listings.json              results that were not Trez's
    run-summary.json                    did this run see the WHOLE search?
  archive/                              older snapshots without structured data (not loaded)
```

`run-summary.json` is what makes a snapshot trustworthy for diffing:

```json
{ "complete": true,
  "sources": { "sales":   { "expected": 72, "saved": 72, "complete": true },
               "rentals": { "expected": 2,  "saved": 2,  "complete": true } } }
```

`expected` is Zameen's own `nbHits`. A run that is capped, interrupted or short
is `complete: false`, and the sync never uses it to decide a listing has gone.

## Rules it follows

- **Only the client's listings.** Start URLs carry `agent_id=200295`; every
  listing's own data must say agency `200295`.
- **Slow and polite.** 2.5–4.5 s between requests; robots.txt checked first.
- **Never get around a block.** A captcha, "unusual traffic" page or HTTP
  401/403/429 stops the run. Only transient failures (network, timeout, 5xx)
  are retried, twice.
- **Raw is immutable.** Never edit `data/raw/<run>/`; scrape again instead.
- **Fresh every time.** `--resume` only recovers an interrupted run.

## What we learned about Zameen (2026-10-06)

| Finding | Consequence |
|---|---|
| The agency search can omit a live listing (54550059: live, Trez, PKR 10 Cr, not in search) | The sync needs two misses from complete runs before asking the agent; never "sold" |
| The result list is padded with other agencies' "Discover more" cards | Results come from Zameen's search data, not from the page's cards |
| Page text rounds prices ("16.64 Crore" vs 166,446,426) and sizes; descriptions were cut where the text said "Amenities" | No page text is parsed at all |
| An image-guessing fallback picked up a non-listing picture for a listing with no photos | A photo counts only if it is in Zameen's gallery for that listing |
| About a third of listings changed in a month | Scrape at least daily |
