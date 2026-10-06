#!/usr/bin/env node
// Collect Trez Enterprises' own Zameen listings into a dated, immutable snapshot.
//
// Everything is read from Zameen's own structured page data (window.state),
// never from the visible text: which listings are results (search hits), how
// many there are (nbHits), paging (nbPages + <link rel="next">), and every
// listing field (property.data). If Zameen changes that data, the run fails
// loudly instead of quietly mis-reading a page. The visible page text is kept
// only as evidence of what the page showed.

import {
  mkdir,
  readFile,
  readdir,
  rename,
  stat,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const AGENCY = "Trez Enterprises";
const AGENT_ID = "200295";
const SOURCE_URLS = {
  sales: `https://www.zameen.com/Homes/Pakistan-1521-1.html?agent_id=${AGENT_ID}&types=all&property_status=available`,
  rentals: `https://www.zameen.com/Rentals/Pakistan-1521-1.html?agent_id=${AGENT_ID}&types=all&property_status=available`,
};

function usage(message) {
  if (message) console.error(`Error: ${message}\n`);
  console.log(`Usage: node scrape-zameen.mjs [options]

  --output-dir <path>           required: a new, empty folder, e.g. ../data/raw/2026-10-07
  --download-media
  --media-store <path>          shared photo pool reused by every run (default: <output-dir>/media)
  --media-concurrency <number>  default: 4
  --max-pages <number>          0 follows every results page (default: 0)
  --max-listings <number>       0 follows every listing (default: 0)
  --min-delay-ms <number>       default: 2500
  --max-delay-ms <number>       default: 4500
  --sources <sales,rentals>     default: sales,rentals
  --resume                      merge into an existing output folder (recovery only)
  --skip-robots-check
  --headed
`);
  process.exit(message ? 1 : 0);
}

function parseArgs(argv) {
  const options = {
    headed: false,
    maxPages: 0,
    maxListings: 0,
    minDelayMs: 2500,
    maxDelayMs: 4500,
    downloadMedia: false,
    mediaConcurrency: 4,
    mediaStore: null,
    resume: false,
    outputDir: null,
    skipRobotsCheck: false,
    sources: ["sales", "rentals"],
  };
  const valued = [
    "--max-pages", "--max-listings", "--min-delay-ms", "--max-delay-ms",
    "--media-concurrency", "--media-store", "--output-dir", "--sources",
  ];
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--help" || arg === "-h") usage();
    else if (arg === "--headed") options.headed = true;
    else if (arg === "--download-media") options.downloadMedia = true;
    else if (arg === "--resume") options.resume = true;
    else if (arg === "--skip-robots-check") options.skipRobotsCheck = true;
    else if (valued.includes(arg)) {
      const value = argv[++i];
      if (!value || value.startsWith("--")) usage(`${arg} requires a value.`);
      if (arg === "--max-pages") options.maxPages = Number(value);
      if (arg === "--max-listings") options.maxListings = Number(value);
      if (arg === "--min-delay-ms") options.minDelayMs = Number(value);
      if (arg === "--max-delay-ms") options.maxDelayMs = Number(value);
      if (arg === "--media-concurrency") options.mediaConcurrency = Number(value);
      if (arg === "--media-store") options.mediaStore = path.resolve(value);
      if (arg === "--output-dir") options.outputDir = path.resolve(value);
      if (arg === "--sources")
        options.sources = value.split(",").map((part) => part.trim()).filter(Boolean);
    } else usage(`Unknown option: ${arg}`);
  }
  if (!options.outputDir) usage("--output-dir is required.");
  for (const [name, n] of [["--max-pages", options.maxPages], ["--max-listings", options.maxListings]])
    if (!Number.isInteger(n) || n < 0) usage(`${name} must be a whole number of 0 or greater.`);
  if (![options.minDelayMs, options.maxDelayMs].every((n) => Number.isFinite(n) && n >= 0))
    usage("Delays must be non-negative numbers.");
  if (options.maxDelayMs < options.minDelayMs)
    usage("--max-delay-ms must be at least --min-delay-ms.");
  if (!Number.isInteger(options.mediaConcurrency) || options.mediaConcurrency < 1 || options.mediaConcurrency > 8)
    usage("--media-concurrency must be a whole number from 1 to 8.");
  if (!options.sources.length || options.sources.some((s) => !(s in SOURCE_URLS)))
    usage("--sources may only contain sales and rentals.");
  return options;
}

// --------------------------------------------------------------------------
// Small helpers
// --------------------------------------------------------------------------

/** Zameen's page data changed shape: never guess, stop and say so. */
class FormatChanged extends Error {}
/** Zameen refused us (block, rate limit, challenge): never retried, never bypassed. */
class Blocked extends Error {}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function nextDelay(options) {
  return Math.round(options.minDelayMs + Math.random() * (options.maxDelayMs - options.minDelayMs));
}

async function atomicJson(filePath, value) {
  const temp = `${filePath}.${process.pid}.tmp`;
  await writeFile(temp, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  await rename(temp, filePath);
}

async function loadExistingRecords(outputDir) {
  try {
    const aggregate = JSON.parse(await readFile(path.join(outputDir, "listings.json"), "utf8"));
    return new Map(aggregate.map((listing) => [listing.id, listing]));
  } catch {
    return new Map();
  }
}

async function assertFreshOutputDirectory(outputDir) {
  if ((await readdir(outputDir)).length)
    throw new Error(
      `Fresh scrapes need an empty --output-dir (snapshots are immutable). Use a new folder; --resume is only for recovering an interrupted run: ${outputDir}`,
    );
}

function verifyStartUrl(sourceUrl) {
  const url = new URL(sourceUrl);
  if (url.hostname !== "www.zameen.com" || url.searchParams.get("agent_id") !== AGENT_ID)
    throw new Error(`Refusing source URL without Zameen agent_id=${AGENT_ID}: ${sourceUrl}`);
}

// Transient failures (network blip, timeout, 5xx) get two retries.
const RETRY_DELAYS_MS = [5_000, 20_000];

async function withRetries(label, attempt) {
  for (let i = 0; ; i += 1) {
    try {
      return await attempt();
    } catch (error) {
      if (error instanceof Blocked || error instanceof FormatChanged || i >= RETRY_DELAYS_MS.length)
        throw error;
      console.log(`  Retrying after "${error.message}" (${label})`);
      await sleep(RETRY_DELAYS_MS[i]);
    }
  }
}

async function assertRobotsAllowed(sourceUrl) {
  const origin = new URL(sourceUrl).origin;
  const response = await withRetries(`${origin}/robots.txt`, () =>
    fetch(`${origin}/robots.txt`, {
      headers: { "User-Agent": "TrezKnowledgeBaseCollector/1.0 (+contact-your-own-team)" },
    }),
  );
  if (!response.ok)
    throw new Error(
      `Could not read ${origin}/robots.txt (HTTP ${response.status}). Use --skip-robots-check only with written authorization.`,
    );
  let applies = false;
  const disallowed = [];
  for (const line of (await response.text()).replace(/\r/g, "").split("\n")) {
    const [field, ...rest] = line.split(":");
    const value = rest.join(":").trim();
    if (!field) continue;
    const key = field.trim().toLowerCase();
    if (key === "user-agent")
      applies = value === "*" || value.toLowerCase().includes("trezknowledgebasecollector");
    if (applies && key === "disallow" && value) disallowed.push(value);
  }
  const pathname = new URL(sourceUrl).pathname;
  if (disallowed.some((rule) => pathname.startsWith(rule)))
    throw new Error(`robots.txt disallows ${pathname}. Stop here unless you have permission.`);
}

async function navigate(page, url, options) {
  await sleep(nextDelay(options));
  await withRetries(url, async () => {
    const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 });
    if (!response) throw new Error(`No response while loading ${url}`);
    if ([401, 403, 429].includes(response.status()))
      throw new Blocked(`Zameen answered HTTP ${response.status()} for ${url}. Not pushing past a refusal.`);
    if (response.status() >= 400) throw new Error(`HTTP ${response.status()} while loading ${url}`);
    await page.waitForTimeout(1200);
    const body = await page.locator("body").innerText({ timeout: 15_000 });
    if (/captcha|access denied|unusual traffic|verify you are human/i.test(body))
      throw new Blocked("Zameen presented an access challenge. The script will not try to bypass it.");
  });
}

// --------------------------------------------------------------------------
// Search results: Zameen's search data decides what is a result
// --------------------------------------------------------------------------

async function readSearchPage(page) {
  const data = await page.evaluate(() => {
    const content = window.state?.algolia?.content;
    if (!content || !Array.isArray(content.hits)) return null;
    const hits = JSON.parse(JSON.stringify(content.hits));
    const ids = new Set(hits.map((hit) => String(hit.externalID)));
    // The detail link for each hit, as the page itself renders it.
    const links = {};
    for (const anchor of document.querySelectorAll('a[href*="/Property/"]')) {
      for (const id of ids)
        if (!links[id] && anchor.href.includes(`-${id}-`)) links[id] = anchor.href.split("#")[0];
    }
    return {
      hits,
      links,
      nbHits: content.nbHits,
      nbPages: content.nbPages,
      page: content.page,
      next: document.querySelector('link[rel="next"]')?.href ?? null,
    };
  });
  if (!data || !Number.isInteger(data.nbHits))
    throw new FormatChanged(
      "Zameen's search data (window.state.algolia.content) is missing or changed shape. Stopping instead of guessing.",
    );
  return data;
}

function nextPageUrl(search, requiredParams) {
  // nbPages is authoritative; rel="next" gives the address. Zameen drops our
  // filter params from it, so they are put back.
  if (!search.next || search.page + 1 >= search.nbPages) return null;
  const url = new URL(search.next);
  for (const [name, value] of requiredParams) url.searchParams.set(name, value);
  return url.href;
}

// --------------------------------------------------------------------------
// One listing page
// --------------------------------------------------------------------------

async function readListingPage(page) {
  return page.evaluate(() => {
    let zameen = null;
    try {
      // Only the listing object: the rest of window.state holds session fields.
      zameen = JSON.parse(JSON.stringify(window.state?.property?.data ?? null));
    } catch {
      zameen = null;
    }
    // The photo size: the largest one this page itself uses for the gallery.
    let gallery = null;
    if (zameen?.photos?.length) {
      const ids = new Set(zameen.photos.map((photo) => String(photo.id)));
      for (const node of document.querySelectorAll("img, source")) {
        const urls = [node.currentSrc, node.src, node.getAttribute("data-src"),
          ...(node.getAttribute("srcset") || "").split(",").map((s) => s.trim().split(/\s+/)[0])];
        for (const raw of urls) {
          if (!raw) continue;
          let url;
          try { url = new URL(raw, location.href); } catch { continue; }
          const m = url.pathname.match(/\/(\d+)-(\d+)x(\d+)\.([a-z0-9]+)$/i);
          if (!m || !ids.has(m[1])) continue;
          const pixels = Number(m[2]) * Number(m[3]);
          if (!gallery || pixels > gallery.pixels)
            gallery = { pixels, size: `${m[2]}x${m[3]}`, ext: m[4], example: url.href, id: m[1] };
        }
      }
    }
    const structuredData = [...document.querySelectorAll('script[type="application/ld+json"]')]
      .map((script) => { try { return JSON.parse(script.textContent); } catch { return null; } })
      .filter(Boolean);
    return {
      zameen,
      gallery,
      canonical: document.querySelector('link[rel="canonical"]')?.href ?? location.href,
      structuredData,
      pageText: (document.body?.innerText || "").replace(/\s+/g, " ").trim(),
    };
  });
}

/** The gallery in the agent's order, at the largest size the page uses. */
function galleryOf(zameen, gallery) {
  if (!zameen?.photos?.length) return [];
  if (!gallery)
    throw new FormatChanged(
      `Listing ${zameen.externalID} has ${zameen.photos.length} photos but the page shows none of them. Stopping instead of guessing a photo URL.`,
    );
  return [...zameen.photos]
    .sort((a, b) => (a.orderIndex ?? 0) - (b.orderIndex ?? 0))
    .map((photo) => ({
      type: "image",
      assetId: String(photo.id),
      size: gallery.size,
      // Same address as the page's own photo, for this photo's id.
      url: gallery.example.replace(`/${gallery.id}-${gallery.size}.`, `/${photo.id}-${gallery.size}.`),
      title: photo.title ?? null,
    }));
}

async function downloadMedia(page, listing, outputDir, options) {
  const store = options.mediaStore ?? path.join(outputDir, "media");
  await mkdir(store, { recursive: true });
  const one = async (media) => {
    // Content-addressed: an asset id + size always denotes the same image, so
    // the pool never downloads a file twice.
    const file = `${media.assetId}-${media.size}${path.extname(new URL(media.url).pathname)}`;
    const destination = path.join(store, file);
    const localFile = path.relative(outputDir, destination).replaceAll("\\", "/");
    try {
      const existing = await stat(destination).catch(() => null);
      if (existing?.isFile() && existing.size > 0)
        return { url: media.url, assetId: media.assetId, file, localFile, cached: true };
      const response = await page.request.get(media.url, { timeout: 60_000 });
      if (!response.ok()) throw new Error(`HTTP ${response.status()}`);
      await writeFile(destination, await response.body());
      return { url: media.url, assetId: media.assetId, file, localFile };
    } catch (error) {
      return { url: media.url, assetId: media.assetId, error: error.message };
    }
  };
  const results = [];
  for (let i = 0; i < listing.media.length; i += options.mediaConcurrency)
    results.push(...(await Promise.all(listing.media.slice(i, i + options.mediaConcurrency).map(one))));
  listing.downloadedMedia = results;
}

// --------------------------------------------------------------------------
// Main
// --------------------------------------------------------------------------

async function main() {
  const options = parseArgs(process.argv.slice(2));
  await mkdir(options.outputDir, { recursive: true });
  if (!options.resume) await assertFreshOutputDirectory(options.outputDir);
  await mkdir(path.join(options.outputDir, "listings"), { recursive: true });
  for (const source of options.sources) verifyStartUrl(SOURCE_URLS[source]);
  if (!options.skipRobotsCheck) await assertRobotsAllowed(SOURCE_URLS[options.sources[0]]);

  const browser = await chromium.launch({ headless: !options.headed });
  const page = await (await browser.newContext({ locale: "en-PK", timezoneId: "Asia/Karachi" })).newPage();
  page.setDefaultTimeout(20_000);

  const records = options.resume ? await loadExistingRecords(options.outputDir) : new Map();
  const rejected = [];
  const searchHits = [];
  const expected = {};
  let visited = 0;
  const persist = async () => {
    await atomicJson(path.join(options.outputDir, "listings.json"),
      [...records.values()].sort((a, b) => a.id.localeCompare(b.id)));
    await atomicJson(path.join(options.outputDir, "rejected-listings.json"), rejected);
    await atomicJson(path.join(options.outputDir, "search-hits.json"), searchHits);
  };

  let runError = null;
  try {
    for (const source of options.sources) {
      const requiredParams = [...new URL(SOURCE_URLS[source]).searchParams];
      let pageUrl = SOURCE_URLS[source];
      let pageNumber = 0;
      while (pageUrl && (options.maxPages === 0 || pageNumber < options.maxPages)) {
        pageNumber += 1;
        console.log(`[${source}] Results page ${pageNumber}: ${pageUrl}`);
        await navigate(page, pageUrl, options);
        const search = await readSearchPage(page);
        expected[source] = search.nbHits;
        for (const hit of search.hits) searchHits.push({ source, page: pageNumber, ...hit });
        console.log(`[${source}] ${search.hits.length} results on this page, ${search.nbHits} in total.`);

        for (const hit of search.hits) {
          if (options.maxListings && visited >= options.maxListings) break;
          const id = String(hit.externalID);
          const href = search.links[id];
          if (!href) {
            rejected.push({ id, source, reason: "Zameen listed it as a result but the page had no link to it" });
            console.log(`  ${id}: no link on the results page`);
            continue;
          }
          console.log(`  Listing ${id}: ${href}`);
          visited += 1;
          await navigate(page, href, options);
          const read = await readListingPage(page);
          const zameen = read.zameen;
          if (!zameen)
            throw new FormatChanged(
              `Listing ${id} has no window.state.property.data. Stopping instead of reading page text.`,
            );
          if (String(zameen.externalID) !== id || String(zameen.agency?.externalID) !== AGENT_ID) {
            rejected.push({ id, url: href, source,
              reason: `Embedded data is listing ${zameen.externalID} of agency ${zameen.agency?.externalID}, not ${AGENT_ID}` });
            console.log(`  Skipped: not a ${AGENCY} listing.`);
            continue;
          }
          const listing = {
            id,
            url: href,
            canonicalUrl: read.canonical,
            source,
            scrapedAt: new Date().toISOString(),
            agency: AGENCY,
            zameen,
            media: galleryOf(zameen, read.gallery),
            structuredData: read.structuredData,
            rawPageText: read.pageText,
          };
          if (options.downloadMedia) await downloadMedia(page, listing, options.outputDir, options);
          records.set(id, listing);
          await atomicJson(path.join(options.outputDir, "listings", `${id}.json`), listing);
          await persist();
          console.log(`  Saved ${id} (${listing.media.length} photos at ${read.gallery?.size ?? "-"}).`);
        }
        if (options.maxListings && visited >= options.maxListings) break;
        pageUrl = nextPageUrl(search, requiredParams);
      }
    }
  } catch (error) {
    runError = error;
  } finally {
    await browser.close();
    await persist();
  }
  if (runError) throw runError;

  // A diff may only treat an absent listing as "missing" when this proves the
  // run saw the whole search. A capped or short run never deactivates stock.
  const all = [...records.values()];
  const sources = Object.fromEntries(options.sources.map((source) => {
    const saved = all.filter((r) => r.source === source).length;
    const want = expected[source] ?? null;
    return [source, { expected: want, saved, complete: want !== null && saved >= want }];
  }));
  const complete = !options.resume && options.maxPages === 0 && options.maxListings === 0 &&
    Object.values(sources).every((s) => s.complete);
  await atomicJson(path.join(options.outputDir, "run-summary.json"), {
    finishedAt: new Date().toISOString(), complete, sources, rejected: rejected.length,
  });
  console.log(`Complete: saved ${all.length} listings; rejected ${rejected.length}.`);
  for (const [source, s] of Object.entries(sources))
    console.log(`  [${source}] Zameen reported ${s.expected ?? "?"}, saved ${s.saved}${s.complete ? "" : "  <-- INCOMPLETE"}`);
  if (!complete) console.log("  Run is not complete: absent listings will not be counted as missing.");
}

main().catch((error) => {
  console.error(`Scrape stopped: ${error.message}`);
  process.exitCode = 1;
});
