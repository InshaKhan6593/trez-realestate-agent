#!/usr/bin/env node
// Check a snapshot before anything trusts it. Errors block; warnings don't.
//
// Everything is checked against Zameen's own listing data (record.zameen),
// never against page text. The validator also compares the fields Zameen
// sends with the previous snapshot's, so a field Zameen adds (or stops
// sending) is reported instead of silently missed.

import { createHash } from "node:crypto";
import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";

const AGENCY = "Trez Enterprises";
const AGENT_ID = "200295";

function usage(message) {
  if (message) console.error(`Error: ${message}\n`);
  console.log(`Usage: node validate-zameen-data.mjs --input-dir <snapshot> [options]

  --max-age-hours <number>      fail records older than this (default: 24)
  --require-downloaded-media    require every photo to exist locally
`);
  process.exit(message ? 1 : 0);
}

function parseArgs(argv) {
  const options = { inputDir: null, maxAgeHours: 24, requireDownloadedMedia: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--help" || arg === "-h") usage();
    else if (arg === "--require-downloaded-media") options.requireDownloadedMedia = true;
    else if (arg === "--input-dir" || arg === "--max-age-hours") {
      const value = argv[++i];
      if (!value || value.startsWith("--")) usage(`${arg} requires a value.`);
      if (arg === "--input-dir") options.inputDir = path.resolve(value);
      else options.maxAgeHours = Number(value);
    } else usage(`Unknown option: ${arg}`);
  }
  if (!options.inputDir) usage("--input-dir is required.");
  if (!Number.isFinite(options.maxAgeHours) || options.maxAgeHours < 0)
    usage("--max-age-hours must be a non-negative number.");
  return options;
}

const hash = (value) => createHash("sha256").update(JSON.stringify(value)).digest("hex");
const fileExists = async (p) => (await stat(p).catch(() => null))?.isFile() ?? false;

function isZameenProperty(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" && u.hostname === "www.zameen.com" && u.pathname.startsWith("/Property/");
  } catch {
    return false;
  }
}

/** Every field path Zameen sends, e.g. "installments.monthlyAmount". */
function fieldPaths(value, prefix = "", out = new Set()) {
  if (Array.isArray(value)) {
    for (const item of value) fieldPaths(item, `${prefix}[]`, out);
  } else if (value && typeof value === "object") {
    for (const [key, child] of Object.entries(value)) {
      const p = prefix ? `${prefix}.${key}` : key;
      out.add(p);
      fieldPaths(child, p, out);
    }
  }
  return out;
}

/** The newest earlier snapshot that has Zameen's data, next to this one. */
async function previousSnapshot(inputDir) {
  const parent = path.dirname(inputDir);
  const names = (await readdir(parent).catch(() => []))
    .filter((n) => n < path.basename(inputDir))
    .sort()
    .reverse();
  for (const name of names) {
    try {
      const records = JSON.parse(await readFile(path.join(parent, name, "listings.json"), "utf8"));
      if (records.some((r) => r?.zameen)) return { name, records };
    } catch {
      // not a snapshot folder
    }
  }
  return null;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const listings = JSON.parse(await readFile(path.join(options.inputDir, "listings.json"), "utf8"));
  if (!Array.isArray(listings)) throw new Error("listings.json must contain an array.");

  const errors = [];
  const warnings = [];
  const seenIds = new Set();
  const now = Date.now();

  let summary = null;
  try {
    summary = JSON.parse(await readFile(path.join(options.inputDir, "run-summary.json"), "utf8"));
  } catch {
    errors.push("run-summary.json is missing: the run did not finish");
  }
  if (summary && !summary.complete)
    warnings.push("run is incomplete: it must not be used to decide a listing has gone");

  for (const listing of listings) {
    const label = `listing ${listing?.id ?? "<missing id>"}`;
    if (!listing || typeof listing !== "object") {
      errors.push(`${label}: record is not an object`);
      continue;
    }
    if (!listing.id || seenIds.has(listing.id)) errors.push(`${label}: missing or duplicate id`);
    seenIds.add(listing.id);
    if (!isZameenProperty(listing.canonicalUrl)) errors.push(`${label}: invalid Zameen property URL`);
    if (!["sales", "rentals"].includes(listing.source)) errors.push(`${label}: unknown source`);
    if (listing.agency !== AGENCY) errors.push(`${label}: agency is not ${AGENCY}`);
    const scrapedAt = Date.parse(listing.scrapedAt);
    if (!Number.isFinite(scrapedAt)) errors.push(`${label}: invalid scrapedAt`);
    else if (now - scrapedAt > options.maxAgeHours * 3_600_000)
      errors.push(`${label}: older than ${options.maxAgeHours} hours`);

    const z = listing.zameen;
    if (!z || typeof z !== "object") {
      errors.push(`${label}: Zameen's listing data is missing`);
      continue;
    }
    if (String(z.externalID) !== String(listing.id)) errors.push(`${label}: data is for listing ${z.externalID}`);
    if (String(z.agency?.externalID) !== AGENT_ID) errors.push(`${label}: agency ${z.agency?.externalID}, not ${AGENT_ID}`);
    if (!z.purpose) errors.push(`${label}: no purpose`);
    if (!Array.isArray(z.category) || !z.category.length) errors.push(`${label}: no category`);
    if (!Array.isArray(z.locations) || !z.locations.length) errors.push(`${label}: no location hierarchy`);
    if (!(z.price > 0)) (z.hidePrice ? warnings : errors).push(`${label}: no price${z.hidePrice ? " (hidden by the agent)" : ""}`);
    if (!(z.area > 0)) warnings.push(`${label}: no area`);
    if (!z.geography?.lat || !z.geography?.lng) warnings.push(`${label}: no coordinates`);

    const photos = Array.isArray(z.photos) ? z.photos.length : 0;
    if (!photos) warnings.push(`${label}: no photos on Zameen`);
    if ((listing.media?.length ?? 0) !== photos)
      errors.push(`${label}: ${listing.media?.length ?? 0} photos captured of ${photos} on Zameen`);
    if (options.requireDownloadedMedia && photos) {
      const downloaded = listing.downloadedMedia ?? [];
      if (downloaded.length !== photos) errors.push(`${label}: not every photo was downloaded`);
      for (const media of downloaded)
        if (media.error || !media.localFile || !(await fileExists(path.join(options.inputDir, media.localFile))))
          errors.push(`${label}: photo missing on disk: ${media.url} ${media.error ?? ""}`.trim());
    }
  }

  // Per-listing files must match the aggregate exactly.
  let detailFiles = [];
  try {
    detailFiles = (await readdir(path.join(options.inputDir, "listings"))).filter((f) => f.endsWith(".json"));
  } catch (error) {
    errors.push(`cannot read listings/: ${error.message}`);
  }
  if (detailFiles.length !== listings.length)
    errors.push(`${detailFiles.length} detail files for ${listings.length} listings`);
  const byId = new Map(listings.map((l) => [String(l.id), l]));
  for (const file of detailFiles) {
    const detail = JSON.parse(await readFile(path.join(options.inputDir, "listings", file), "utf8"));
    const aggregate = byId.get(path.basename(file, ".json"));
    if (!aggregate || hash(detail) !== hash(aggregate)) errors.push(`listings/${file} differs from listings.json`);
  }

  // Fields Zameen added or stopped sending since the previous snapshot.
  const previous = await previousSnapshot(options.inputDir);
  if (previous) {
    const fields = (records) => {
      const all = new Set();
      for (const r of records) if (r?.zameen) fieldPaths(r.zameen, "", all);
      return all;
    };
    const now_ = fields(listings);
    const before = fields(previous.records);
    for (const f of [...now_].filter((f) => !before.has(f)).sort())
      warnings.push(`Zameen sends a new field since ${previous.name}: ${f}`);
    for (const f of [...before].filter((f) => !now_.has(f)).sort())
      warnings.push(`Zameen no longer sends a field it sent in ${previous.name}: ${f}`);
  }

  console.log(`Validated ${listings.length} listings in ${options.inputDir}.`);
  for (const w of warnings) console.log(`WARNING: ${w}`);
  for (const e of errors) console.log(`ERROR: ${e}`);
  console.log(`Result: ${errors.length} error(s), ${warnings.length} warning(s).`);
  process.exitCode = errors.length ? 1 : 0;
}

main().catch((error) => {
  console.error(`Validation stopped: ${error.message}`);
  process.exitCode = 1;
});
