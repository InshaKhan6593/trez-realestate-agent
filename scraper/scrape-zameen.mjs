#!/usr/bin/env node

import { createHash } from "node:crypto";
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
  // Same params as Zameen's own "View All" link: without them the rentals page
  // renders no "1 to N of M Properties" counter, so completeness can't be proven.
  rentals: `https://www.zameen.com/Rentals/Pakistan-1521-1.html?agent_id=${AGENT_ID}&types=all&property_status=available`,
};
const MEDIA_EXTENSIONS =
  /\.(?:avif|gif|jpe?g|png|webp|mp4|webm|mov)(?:[?#].*)?$/i;
const VIDEO_HOSTS = /(?:youtube\.com|youtu\.be|vimeo\.com)/i;
const PROPERTY_MEDIA_HOST = "media.zameen.com";

function usage(message) {
  if (message) console.error(`Error: ${message}\n`);
  console.log(`Usage: node scrape-zameen.mjs [options]

  --headed
  --max-pages <number>        0 follows every results page (default: 0)
  --max-listings <number>     0 follows every listing (default: 0)
  --min-delay-ms <number>     default: 2500
  --max-delay-ms <number>     default: 4500
  --download-media
  --media-concurrency <number>  default: 4
  --media-store <path>        Shared content-addressed media pool reused by every
                              run (default: <output-dir>/media)
  --resume                  Merge with an existing output directory (off by default)
  --output-dir <path>         default: ./data
  --skip-robots-check
  --sources <sales,rentals>   default: sales,rentals
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
    outputDir: path.resolve("data"),
    skipRobotsCheck: false,
    sources: ["sales", "rentals"],
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--help" || arg === "-h") usage();
    if (arg === "--headed") options.headed = true;
    else if (arg === "--download-media") options.downloadMedia = true;
    else if (arg === "--resume") options.resume = true;
    else if (arg === "--skip-robots-check") options.skipRobotsCheck = true;
    else if (
      [
        "--max-pages",
        "--max-listings",
        "--min-delay-ms",
        "--max-delay-ms",
        "--media-concurrency",
        "--media-store",
        "--output-dir",
        "--sources",
      ].includes(arg)
    ) {
      const value = argv[++i];
      if (!value || value.startsWith("--")) usage(`${arg} requires a value.`);
      if (arg === "--max-pages") options.maxPages = Number(value);
      if (arg === "--max-listings") options.maxListings = Number(value);
      if (arg === "--min-delay-ms") options.minDelayMs = Number(value);
      if (arg === "--max-delay-ms") options.maxDelayMs = Number(value);
      if (arg === "--media-concurrency")
        options.mediaConcurrency = Number(value);
      if (arg === "--media-store") options.mediaStore = path.resolve(value);
      if (arg === "--output-dir") options.outputDir = path.resolve(value);
      if (arg === "--sources")
        options.sources = value
          .split(",")
          .map((part) => part.trim())
          .filter(Boolean);
    } else usage(`Unknown option: ${arg}`);
  }
  if (!Number.isInteger(options.maxPages) || options.maxPages < 0)
    usage("--max-pages must be a whole number of 0 or greater.");
  if (!Number.isInteger(options.maxListings) || options.maxListings < 0)
    usage("--max-listings must be a whole number of 0 or greater.");
  if (
    ![options.minDelayMs, options.maxDelayMs].every(
      (n) => Number.isFinite(n) && n >= 0,
    )
  )
    usage("Delays must be non-negative numbers.");
  if (options.maxDelayMs < options.minDelayMs)
    usage("--max-delay-ms must be at least --min-delay-ms.");
  if (
    !Number.isInteger(options.mediaConcurrency) ||
    options.mediaConcurrency < 1 ||
    options.mediaConcurrency > 8
  )
    usage("--media-concurrency must be a whole number from 1 to 8.");
  if (
    !options.sources.length ||
    options.sources.some((source) => !(source in SOURCE_URLS))
  )
    usage("--sources may only contain sales and rentals.");
  return options;
}

function normalize(text) {
  return String(text ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function normalizeWhitespace(text) {
  return String(text ?? "")
    .replace(/\s+/g, " ")
    .trim();
}

function containsAgency(text) {
  return normalize(text).includes(normalize(AGENCY));
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function resolveUrl(raw, base) {
  try {
    const url = new URL(raw, base);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function canonicalizeUrl(raw, base) {
  const resolved = resolveUrl(raw, base);
  if (!resolved) return null;
  const url = new URL(resolved);
  url.hash = "";
  for (const key of [...url.searchParams.keys()]) {
    if (/^(?:utm_.+|fbclid|gclid)$/i.test(key)) url.searchParams.delete(key);
  }
  return url.href;
}

function slug(value) {
  return (
    String(value || "unknown")
      .replace(/[^a-z0-9_-]+/gi, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 90) || "unknown"
  );
}

function listingId(url) {
  const pathname = new URL(url).pathname;
  // Zameen property paths end with a numeric triplet. The first number is the
  // listing identifier; the final `-1.html` is a page/version suffix.
  const match = pathname.match(/-(\d+)(?:-\d+)+\.html$/i);
  return (
    match?.[1] || createHash("sha256").update(url).digest("hex").slice(0, 16)
  );
}

function parsePkrAmount(display) {
  if (!display) return null;
  const match = normalizeWhitespace(display).match(
    /([\d,.]+)\s*(arab|crore|cr\.?|lakh|lac|thousand|million|m\b|k\b)?/i,
  );
  if (!match) return null;
  const value = Number(match[1].replaceAll(",", ""));
  if (!Number.isFinite(value) || value <= 0) return null;
  const unit = (match[2] || "").toLowerCase().replace(".", "");
  const multiplier =
    unit === "arab"
      ? 1_000_000_000
      : unit.startsWith("crore") || unit === "cr"
        ? 10_000_000
        : unit === "lakh" || unit === "lac"
          ? 100_000
          : unit === "million" || unit === "m"
            ? 1_000_000
            : unit === "thousand" || unit === "k"
              ? 1_000
              : 1;
  return Math.round(value * multiplier);
}

function extractBreadcrumbLocations(structuredData) {
  const documents = Array.isArray(structuredData)
    ? structuredData
    : [structuredData];
  const names = [];
  for (const document of documents) {
    const nodes = Array.isArray(document) ? document : [document];
    for (const node of nodes) {
      if (
        node?.["@type"] !== "BreadcrumbList" ||
        !Array.isArray(node.itemListElement)
      )
        continue;
      for (const item of node.itemListElement) {
        if (typeof item?.name !== "string") continue;
        const name = normalizeWhitespace(item.name)
          .replace(/\s+Houses?$/i, "")
          .replace(/^(?:House|Flat|Plot)\s+\d+$/i, "")
          .trim();
        if (name && !/^zameen$/i.test(name)) names.push(name);
      }
    }
  }
  return unique(names);
}

// Short descriptions have no "Read More" button and run straight into the next
// heading. Requiring "Read More" made 32 of 74 listings fall back to the
// 160-character meta summary on 2026-10-06.
function extractDescription(pageText, metaDescription) {
  const match = pageText.match(
    /\bDescription\s+([\s\S]*?)\s+(?:Read More\s+)?(?:Amenities|Location & Nearby)\b/i,
  );
  return normalizeWhitespace(match?.[1] || metaDescription) || null;
}

function extractFacts(pageText, structuredData) {
  const detailsStart = pageText.search(/\bOverview\s+Details\b/i);
  const descriptionStart =
    detailsStart >= 0
      ? pageText.slice(detailsStart).search(/\bDescription\b/i)
      : -1;
  const details =
    detailsStart >= 0
      ? pageText.slice(
          detailsStart,
          descriptionStart >= 0
            ? detailsStart + descriptionStart
            : detailsStart + 2_000,
        )
      : pageText;
  const valueBefore = (pattern, nextFieldPattern) => {
    const match = details.match(pattern);
    if (!match) return null;
    const value = normalizeWhitespace(match[1]);
    return value.replace(nextFieldPattern, "").trim() || null;
  };
  const propertyType = valueBefore(/\bType\s+(.+?)(?=\s+Price\s+PKR\b)/i, /$/);
  const priceDisplay = valueBefore(
    /\bPrice\s+PKR\s+(.+?)(?=\s+(?:Bath\(s\)|Area|Purpose|Bedroom\(s\)|Added|Location)\b)/i,
    /$/,
  );
  const areaMatch = details.match(
    /\bArea\s+([\d,.]+)\s*(Sq\.?\s*(?:Yd\.?|Ft\.?)|Marla|Kanal|Acre)\b/i,
  );
  const purpose = valueBefore(
    /\bPurpose\s+(For\s+(?:Sale|Rent|Lease))\b/i,
    /$/,
  );
  const bedrooms =
    Number(details.match(/\bBedroom\(s\)\s+(\d+)\b/i)?.[1] || 0) || null;
  const bathrooms =
    Number(details.match(/\bBath\(s\)\s+(\d+)\b/i)?.[1] || 0) || null;
  const addedText = valueBefore(/\bAdded\s+(.+?)(?=\s+Location\b)/i, /$/);
  const location = valueBefore(/\bLocation\s+(.+)$/i, /$/);
  const amount = parsePkrAmount(priceDisplay);
  return {
    propertyType,
    purpose,
    price:
      amount && priceDisplay
        ? {
            amount,
            currency: "PKR",
            display: `PKR ${priceDisplay}`,
            source: "overview_details",
          }
        : null,
    area: areaMatch
      ? {
          value: Number(areaMatch[1].replaceAll(",", "")),
          unit: normalizeWhitespace(areaMatch[2]).replace(/\.$/, ""),
          display: normalizeWhitespace(areaMatch[0]),
        }
      : null,
    bedrooms,
    bathrooms,
    location,
    locationHierarchy: extractBreadcrumbLocations(structuredData),
    addedText,
  };
}

function mediaAssetId(url) {
  const match = new URL(url).pathname.match(/\/thumbnails\/(\d+)-/i);
  return match?.[1] || null;
}

// The gallery lazy-loads, so most images on the page exist only as 120x90
// strip thumbnails: on 2026-10-06, 525 of 715 selected photos were 120x90,
// useless on WhatsApp. Zameen serves every asset at 800x1200 (the largest size
// the page itself uses; other sizes such as 1200x900 return 403), so always
// ask for that.
const FULL_SIZE = "800x1200";
function fullSizeUrl(url) {
  const parsed = new URL(url);
  parsed.pathname = parsed.pathname.replace(
    /(\/thumbnails\/\d+)-\d+x\d+(\.(?:jpe?g|png|webp))$/i,
    `$1-${FULL_SIZE}$2`,
  );
  return parsed.href;
}

function mediaQuality(media) {
  const dimensions = new URL(media.url).pathname.match(
    /-(\d+)x(\d+)\.(?:jpe?g|png|webp)$/i,
  );
  const pixels = dimensions ? Number(dimensions[1]) * Number(dimensions[2]) : 0;
  const jpegBonus = /\.jpe?g(?:$|[?#])/i.test(media.url) ? 1 : 0;
  return pixels * 10 + jpegBonus;
}

function selectPropertyMedia(rawMedia, title) {
  const normalizedTitle = normalize(title);
  const candidates = rawMedia
    .map((item) => ({ ...item, url: resolveUrl(item.url) }))
    .filter((item) => item.type === "image" && item.url)
    .filter((item) => new URL(item.url).hostname === PROPERTY_MEDIA_HOST)
    .filter((item) =>
      /\/thumbnails\/\d+-\d+x\d+\.(?:jpe?g|png|webp)(?:[?#].*)?$/i.test(
        new URL(item.url).pathname,
      ),
    )
    .filter(
      (item) =>
        normalize(item.alt) === normalizedTitle ||
        /^\s*\d+\s*$/.test(item.alt || ""),
    )
    .map((item) => ({
      ...item,
      url: fullSizeUrl(item.url),
      assetId: mediaAssetId(item.url),
    }))
    .filter((item) => item.assetId);
  const bestByAsset = new Map();
  for (const item of candidates) {
    const current = bestByAsset.get(item.assetId);
    if (!current || mediaQuality(item) > mediaQuality(current))
      bestByAsset.set(item.assetId, item);
  }
  return [...bestByAsset.values()]
    .sort((left, right) => Number(left.assetId) - Number(right.assetId))
    .map(({ assetId, ...item }) => item);
}

function listingValidationErrors(listing) {
  const errors = [];
  if (
    !/^https:\/\/www\.zameen\.com\/Property\//i.test(listing.canonicalUrl || "")
  )
    errors.push("invalid canonical Zameen property URL");
  if (!listing.id || !listing.title) errors.push("missing listing ID or title");
  if (listing.agency !== AGENCY) errors.push("agency could not be verified");
  if (!listing.facts?.propertyType) errors.push("missing property type");
  if (!listing.facts?.purpose) errors.push("missing purpose");
  if (!listing.facts?.price?.amount) errors.push("missing numeric PKR price");
  if (!listing.facts?.area?.value || !listing.facts?.area?.unit)
    errors.push("missing normalized area");
  if (!listing.facts?.location) errors.push("missing location");
  return errors;
}

function listingValidationWarnings(listing) {
  const warnings = [];
  if (!Array.isArray(listing.media) || listing.media.length === 0)
    warnings.push(
      "no target-property media; a human must add photos before WhatsApp sharing",
    );
  return warnings;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function nextDelay(options) {
  return Math.round(
    options.minDelayMs +
      Math.random() * (options.maxDelayMs - options.minDelayMs),
  );
}

async function atomicJson(filePath, value) {
  const temp = `${filePath}.${process.pid}.tmp`;
  await writeFile(temp, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  await rename(temp, filePath);
}

async function loadExistingRecords(outputDir) {
  const aggregatePath = path.join(outputDir, "listings.json");
  try {
    const aggregate = JSON.parse(await readFile(aggregatePath, "utf8"));
    return new Map(aggregate.map((listing) => [listing.canonicalUrl, listing]));
  } catch {
    // A previous run can be interrupted before the aggregate file is written.
    // In that case, rebuild the in-memory index from its already-saved records.
    try {
      const files = await readdir(path.join(outputDir, "listings"));
      const records = await Promise.all(
        files
          .filter((file) => file.endsWith(".json"))
          .map(async (file) =>
            JSON.parse(
              await readFile(path.join(outputDir, "listings", file), "utf8"),
            ),
          ),
      );
      return new Map(records.map((listing) => [listing.canonicalUrl, listing]));
    } catch {
      return new Map();
    }
  }
}

async function assertFreshOutputDirectory(outputDir) {
  const entries = await readdir(outputDir);
  if (entries.length) {
    throw new Error(
      `Fresh scrapes require an empty --output-dir. Use a new staging directory, validate it, then promote it. Use --resume only for an intentional merge: ${outputDir}`,
    );
  }
}

function verifyStartUrl(sourceUrl) {
  const url = new URL(sourceUrl);
  if (
    url.hostname !== "www.zameen.com" ||
    url.searchParams.get("agent_id") !== AGENT_ID
  ) {
    throw new Error(
      `Refusing source URL without Zameen agent_id=${AGENT_ID}: ${sourceUrl}`,
    );
  }
}

async function assertRobotsAllowed(sourceUrl) {
  const origin = new URL(sourceUrl).origin;
  const response = await fetch(`${origin}/robots.txt`, {
    headers: {
      "User-Agent": "TrezKnowledgeBaseCollector/1.0 (+contact-your-own-team)",
    },
  });
  if (!response.ok)
    throw new Error(
      `Could not read ${origin}/robots.txt (HTTP ${response.status}). Use --skip-robots-check only if you have written authorization.`,
    );
  const lines = (await response.text()).replace(/\r/g, "").split("\n");
  let applies = false;
  const disallowed = [];
  for (const line of lines) {
    const [field, ...rest] = line.split(":");
    const value = rest.join(":").trim();
    if (!field) continue;
    if (field.trim().toLowerCase() === "user-agent")
      applies =
        value === "*" ||
        value.toLowerCase().includes("trezknowledgebasecollector");
    if (applies && field.trim().toLowerCase() === "disallow" && value)
      disallowed.push(value);
  }
  const pathname = new URL(sourceUrl).pathname;
  if (disallowed.some((rule) => pathname.startsWith(rule))) {
    throw new Error(
      `robots.txt disallows ${pathname}. Stop here or run with --skip-robots-check only if you have permission.`,
    );
  }
}

// Zameen appends "Discover more properties for rent" cards from OTHER agencies
// to the same <ul> as the real results once the agency's own results run out.
// On 2026-10-06 the rentals page held 2 Trez cards + 23 recommendations, and
// every one of those 23 cost a detail-page visit before the agency check
// rejected it. Only cards that come before that heading are results.
async function collectResultLinks(page) {
  return page.evaluate(() => {
    const marker = [
      ...document.querySelectorAll("h1,h2,h3,h4,h5,div,span,p"),
    ].find(
      (node) =>
        node.children.length === 0 &&
        /^Discover more properties/i.test((node.textContent || "").trim()),
    );
    const beforeMarker = (node) =>
      !marker ||
      Boolean(
        node.compareDocumentPosition(marker) & Node.DOCUMENT_POSITION_FOLLOWING,
      );
    const cards = [...document.querySelectorAll('li[role="article"]')];
    const anchors = cards.length
      ? cards
          .filter(beforeMarker)
          .flatMap((card) => [...card.querySelectorAll("a[href]")])
      : [...document.querySelectorAll("a[href]")].filter(beforeMarker);
    // "26 to 50 of 72 Properties": the search's own count, used after the
    // run to prove every result was visited.
    // textContent, not innerText: the counter is hidden at some viewport
    // widths and innerText skips hidden elements.
    const range = (document.body?.textContent || "").match(
      /(\d+)\s+to\s+(\d+)\s+of\s+(\d+)\s+Propert/i,
    );
    const candidates = anchors
      .map((node) => {
        const url = new URL(node.href);
        return {
          href: url.href,
          pathname: url.pathname,
          hostname: url.hostname,
        };
      })
      .filter(
        ({ hostname, pathname }) =>
          hostname === location.hostname &&
          /^\/Property\//i.test(pathname) &&
          /-\d+(?:-\d+)+\.html$/i.test(pathname),
      );
    return {
      links: [...new Map(candidates.map((item) => [item.href, item])).values()],
      total: range ? Number(range[3]) : null,
      droppedRecommendations: Boolean(marker),
    };
  });
}

async function getNextPageUrl(page, currentUrl, requiredSearchParams) {
  const currentPage = Number(
    new URL(currentUrl).pathname.match(/-(\d+)\.html$/i)?.[1] || 1,
  );
  const nextHref = await page.evaluate((current) => {
    const candidates = [...document.querySelectorAll("a[href]")]
      .map((anchor) => {
        const match = (anchor.getAttribute("title") || "").match(
          /^Page\s+(\d+)$/i,
        );
        return match ? { page: Number(match[1]), href: anchor.href } : null;
      })
      .filter(Boolean)
      .filter((candidate) => candidate.page > current)
      .sort((a, b) => a.page - b.page);
    return candidates[0]?.href || null;
  }, currentPage);
  if (!nextHref) return null;
  const nextUrl = new URL(nextHref);
  for (const [name, value] of requiredSearchParams) {
    if (value && !nextUrl.searchParams.has(name))
      nextUrl.searchParams.set(name, value);
  }
  return nextUrl.href;
}

async function extractListing(page, url, source) {
  const payload = await page.evaluate(() => {
    const text = document.body?.innerText || "";
    const valueFrom = (selector, attr = "content") =>
      document.querySelector(selector)?.getAttribute(attr)?.trim() || null;
    const title =
      document.querySelector("h1")?.textContent?.trim() ||
      valueFrom('meta[property="og:title"]') ||
      document.title ||
      null;
    const description =
      valueFrom('meta[name="description"]') ||
      valueFrom('meta[property="og:description"]') ||
      null;
    const canonical =
      valueFrom('link[rel="canonical"]', "href") || location.href;
    const media = [];
    for (const image of document.querySelectorAll("img")) {
      const urls = [
        image.currentSrc,
        image.src,
        image.getAttribute("data-src"),
        image.getAttribute("data-original"),
        ...["srcset", "data-srcset"].flatMap((attribute) =>
          (image.getAttribute(attribute) || "")
            .split(",")
            .map((candidate) => candidate.trim().split(/\s+/)[0]),
        ),
      ];
      for (const url of urls)
        media.push({ type: "image", url, alt: image.alt || null });
    }
    for (const source of document.querySelectorAll(
      "video[src], video source[src], audio[src]",
    )) {
      media.push({
        type: "video",
        url: source.src || source.getAttribute("src"),
        alt: null,
      });
    }
    for (const anchor of document.querySelectorAll("a[href]")) {
      const href = anchor.href;
      if (
        /\.(?:mp4|webm|mov)(?:[?#].*)?$/i.test(href) ||
        /(?:youtube\.com|youtu\.be|vimeo\.com)/i.test(href)
      ) {
        media.push({
          type: "video",
          url: href,
          alt: (anchor.textContent || "").trim() || null,
        });
      }
    }
    const structuredData = [
      ...document.querySelectorAll('script[type="application/ld+json"]'),
    ]
      .map((script) => {
        try {
          return JSON.parse(script.textContent);
        } catch {
          return null;
        }
      })
      .filter(Boolean);
    const attributes = [...document.querySelectorAll("li, tr, [data-testid]")]
      .map((node) => (node.textContent || "").replace(/\s+/g, " ").trim())
      .filter(
        (value) =>
          /(?:beds?|baths?|area|price|location|purpose|type|added|reference|property id)/i.test(
            value,
          ) && value.length < 300,
      )
      .slice(0, 150);
    return {
      text,
      title,
      description,
      canonical,
      media,
      structuredData,
      attributes,
    };
  });
  const pageText = normalizeWhitespace(payload.text);
  const media = selectPropertyMedia(
    payload.media.map((item) => ({ ...item, url: resolveUrl(item.url, url) })),
    payload.title,
  );
  return {
    id: listingId(url),
    url,
    canonicalUrl:
      canonicalizeUrl(payload.canonical, url) || canonicalizeUrl(url),
    source,
    scrapedAt: new Date().toISOString(),
    agency: containsAgency(pageText) ? AGENCY : null,
    inventory: {
      sourceStatus: "listed_on_source_at_scrape_time",
      lastSourceCheckAt: new Date().toISOString(),
    },
    title: payload.title,
    description: extractDescription(pageText, payload.description),
    facts: extractFacts(pageText, payload.structuredData),
    media,
    structuredData: payload.structuredData,
    rawAttributes: unique(payload.attributes),
    rawPageText: pageText,
  };
}

// Content-addressed name for one media asset. A Zameen asset id always denotes
// the same image, so the file name is stable across runs even if the agency
// reorders the gallery. The size is part of the name: the store is never
// re-downloaded, so a bare "<id>.jpeg" saved as a 120x90 thumbnail would
// otherwise be reused as if it were the full photo. Falls back to a hash when
// no asset id is present.
function mediaFileName(media) {
  const extension =
    path
      .extname(new URL(media.url).pathname)
      .replace(/[^.a-z0-9]/gi, "")
      .slice(0, 8) || (media.type === "video" ? ".mp4" : ".jpg");
  const assetId =
    mediaAssetId(media.url) ||
    createHash("sha1").update(media.url).digest("hex").slice(0, 16);
  const size = new URL(media.url).pathname.match(/-(\d+x\d+)\.[a-z]+$/i)?.[1];
  return {
    assetId,
    fileName: size ? `${assetId}-${size}${extension}` : `${assetId}${extension}`,
  };
}

async function downloadMediaFiles(page, listing, outputDir, options) {
  // Default store lives inside the run directory so a raw snapshot stays
  // self-contained; --media-store points every run at one shared, immutable
  // pool so each photo is fetched exactly once, ever.
  const store = options.mediaStore
    ? path.resolve(options.mediaStore)
    : path.join(outputDir, "media");
  await mkdir(store, { recursive: true });
  const downloadOne = async (media) => {
    try {
      const { assetId, fileName } = mediaFileName(media);
      const destination = path.join(store, fileName);
      const relative = path
        .relative(outputDir, destination)
        .replaceAll("\\", "/");
      // Never re-download a file already on disk and non-empty.
      const existing = await stat(destination).catch(() => null);
      if (existing?.isFile() && existing.size > 0)
        return {
          url: media.url,
          assetId,
          file: fileName,
          localFile: relative,
          cached: true,
        };
      const response = await page.request.get(media.url, { timeout: 60_000 });
      if (!response.ok()) throw new Error(`HTTP ${response.status()}`);
      await writeFile(destination, await response.body());
      return { url: media.url, assetId, file: fileName, localFile: relative };
    } catch (error) {
      return { url: media.url, error: error.message };
    }
  };
  const mediaToDownload = listing.media.filter(
    (media) => !VIDEO_HOSTS.test(media.url) && MEDIA_EXTENSIONS.test(media.url),
  );
  const downloaded = [];
  for (
    let start = 0;
    start < mediaToDownload.length;
    start += options.mediaConcurrency
  ) {
    const batch = mediaToDownload.slice(
      start,
      start + options.mediaConcurrency,
    );
    downloaded.push(
      ...(await Promise.all(
        batch.map((media) => downloadOne(media)),
      )),
    );
  }
  listing.downloadedMedia = downloaded;
}

async function navigate(page, url, options) {
  await sleep(nextDelay(options));
  const response = await page.goto(url, {
    waitUntil: "domcontentloaded",
    timeout: 60_000,
  });
  if (!response) throw new Error(`No response while loading ${url}`);
  if (response.status() >= 400)
    throw new Error(`HTTP ${response.status()} while loading ${url}`);
  await page.waitForTimeout(1200);
  const body = await page.locator("body").innerText({ timeout: 15_000 });
  if (
    /captcha|access denied|unusual traffic|verify you are human/i.test(body)
  ) {
    throw new Error(
      "Zameen presented an access challenge. The script will not try to bypass it. Try again later or contact Zameen for approved access.",
    );
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  await mkdir(options.outputDir, { recursive: true });
  if (!options.resume) await assertFreshOutputDirectory(options.outputDir);
  await mkdir(path.join(options.outputDir, "listings"), { recursive: true });
  if (!options.skipRobotsCheck)
    await assertRobotsAllowed(SOURCE_URLS[options.sources[0]]);
  for (const source of options.sources) verifyStartUrl(SOURCE_URLS[source]);

  const browser = await chromium.launch({ headless: !options.headed });
  const context = await browser.newContext({
    locale: "en-PK",
    timezoneId: "Asia/Karachi",
  });
  const page = await context.newPage();
  page.setDefaultTimeout(20_000);
  const records = options.resume
    ? await loadExistingRecords(options.outputDir)
    : new Map();
  if (!options.resume)
    console.log(
      "Starting a fresh catalog snapshot; use --resume only for an intentional merge.",
    );
  const rejected = [];
  const searchTotals = {};
  let listingsVisited = 0;
  const persistProgress = async () => {
    const output = [...records.values()].sort((a, b) =>
      a.id.localeCompare(b.id),
    );
    await atomicJson(path.join(options.outputDir, "listings.json"), output);
    await atomicJson(
      path.join(options.outputDir, "rejected-listings.json"),
      rejected,
    );
  };
  let runError = null;
  try {
    for (const source of options.sources) {
      let pageUrl = SOURCE_URLS[source];
      const requiredSearchParams = new URL(SOURCE_URLS[source]).searchParams;
      let pageNumber = 0;
      const seenPages = new Set();
      while (
        pageUrl &&
        !seenPages.has(pageUrl) &&
        (options.maxPages === 0 || pageNumber < options.maxPages)
      ) {
        seenPages.add(pageUrl);
        pageNumber += 1;
        console.log(`[${source}] Results page ${pageNumber}: ${pageUrl}`);
        await navigate(page, pageUrl, options);
        const results = await collectResultLinks(page);
        const links = results.links;
        if (results.total !== null) searchTotals[source] = results.total;
        const nextPageUrl = await getNextPageUrl(
          page,
          pageUrl,
          requiredSearchParams,
        );
        console.log(
          `[${source}] Found ${links.length} candidate detail URLs${results.droppedRecommendations ? " (ignored Zameen's recommendations from other agencies)" : ""}.`,
        );
        for (const candidate of links) {
          if (options.maxListings && listingsVisited >= options.maxListings)
            break;
          console.log(`  Listing: ${candidate.href}`);
          listingsVisited += 1;
          await navigate(page, candidate.href, options);
          const listing = await extractListing(page, candidate.href, source);
          if (listing.agency !== AGENCY) {
            rejected.push({
              url: candidate.href,
              source,
              reason: `Visible page text did not identify ${AGENCY}`,
              scrapedAt: listing.scrapedAt,
            });
            await persistProgress();
            console.log(
              `  Skipped: agency could not be verified as ${AGENCY}.`,
            );
            continue;
          }
          const validationErrors = listingValidationErrors(listing);
          if (validationErrors.length) {
            rejected.push({
              url: candidate.href,
              source,
              reason: `Validation failed: ${validationErrors.join("; ")}`,
              scrapedAt: listing.scrapedAt,
            });
            await persistProgress();
            console.log(`  Skipped: ${validationErrors.join("; ")}.`);
            continue;
          }
          const validationWarnings = listingValidationWarnings(listing);
          if (options.downloadMedia)
            await downloadMediaFiles(page, listing, options.outputDir, options);
          records.set(listing.canonicalUrl, listing);
          await atomicJson(
            path.join(
              options.outputDir,
              "listings",
              `${slug(listing.id)}.json`,
            ),
            listing,
          );
          await persistProgress();
          console.log(
            `  Saved ${listing.id} (${listing.media.length} media URLs)${validationWarnings.length ? `; warning: ${validationWarnings.join("; ")}` : ""}.`,
          );
        }
        if (options.maxListings && listingsVisited >= options.maxListings)
          break;
        pageUrl = nextPageUrl;
      }
    }
  } catch (error) {
    runError = error;
  } finally {
    await browser.close();
    await persistProgress();
  }
  if (runError) throw runError;
  const output = [...records.values()].sort((a, b) => a.id.localeCompare(b.id));
  // The ETL only treats an absent listing as "missing" when this says the run
  // saw the whole search. A capped or short run must never deactivate stock.
  const sources = Object.fromEntries(
    options.sources.map((source) => {
      const saved = output.filter((r) => r.source === source).length;
      const expected = searchTotals[source] ?? null;
      return [source, { expected, saved, complete: expected !== null && saved >= expected }];
    }),
  );
  const complete =
    !options.resume &&
    options.maxPages === 0 &&
    options.maxListings === 0 &&
    Object.values(sources).every((s) => s.complete);
  await atomicJson(path.join(options.outputDir, "run-summary.json"), {
    finishedAt: new Date().toISOString(),
    complete,
    sources,
    rejected: rejected.length,
  });
  console.log(
    `Complete: saved ${output.length} verified listings; rejected ${rejected.length} unverified listings.`,
  );
  for (const [source, s] of Object.entries(sources))
    console.log(
      `  [${source}] search reported ${s.expected ?? "?"}, saved ${s.saved}${s.complete ? "" : "  <-- INCOMPLETE"}`,
    );
  if (!complete)
    console.log(
      "  Run is not complete: the ETL will not mark absent listings as missing.",
    );
}

main().catch((error) => {
  console.error(`Scrape stopped: ${error.message}`);
  process.exitCode = 1;
});
