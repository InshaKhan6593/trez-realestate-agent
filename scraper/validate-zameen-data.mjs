#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";

const AGENCY = "Trez Enterprises";

function usage(message) {
  if (message) console.error(`Error: ${message}\n`);
  console.log(`Usage: node validate-zameen-data.mjs [options]

  --input-dir <path>            default: ./data
  --max-age-hours <number>      fail records older than this (default: 24)
  --require-downloaded-media    require every selected property image to exist locally
`);
  process.exit(message ? 1 : 0);
}

function parseArgs(argv) {
  const options = {
    inputDir: path.resolve("data"),
    maxAgeHours: 24,
    requireDownloadedMedia: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--help" || argument === "-h") usage();
    if (argument === "--require-downloaded-media")
      options.requireDownloadedMedia = true;
    else if (argument === "--input-dir" || argument === "--max-age-hours") {
      const value = argv[++index];
      if (!value || value.startsWith("--"))
        usage(`${argument} requires a value.`);
      if (argument === "--input-dir") options.inputDir = path.resolve(value);
      else options.maxAgeHours = Number(value);
    } else usage(`Unknown option: ${argument}`);
  }
  if (!Number.isFinite(options.maxAgeHours) || options.maxAgeHours < 0)
    usage("--max-age-hours must be a non-negative number.");
  return options;
}

function hash(value) {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function isHttpsZameenProperty(url) {
  try {
    const parsed = new URL(url);
    return (
      parsed.protocol === "https:" &&
      parsed.hostname === "www.zameen.com" &&
      /^\/Property\//i.test(parsed.pathname)
    );
  } catch {
    return false;
  }
}

function printIssues(label, issues) {
  for (const issue of issues) console.log(`${label}: ${issue}`);
}

async function fileExists(filePath) {
  try {
    return (await stat(filePath)).isFile();
  } catch {
    return false;
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const aggregatePath = path.join(options.inputDir, "listings.json");
  let listings;
  try {
    listings = JSON.parse(await readFile(aggregatePath, "utf8"));
  } catch (error) {
    throw new Error(`Cannot read ${aggregatePath}: ${error.message}`);
  }
  if (!Array.isArray(listings))
    throw new Error("listings.json must contain an array.");

  const errors = [];
  const warnings = [];
  const seenIds = new Set();
  const seenUrls = new Set();
  const now = Date.now();

  for (const listing of listings) {
    const label = `listing ${listing?.id || "<missing id>"}`;
    if (!listing || typeof listing !== "object") {
      errors.push(`${label}: record is not an object`);
      continue;
    }
    if (!listing.id || seenIds.has(listing.id))
      errors.push(`${label}: missing or duplicate ID`);
    seenIds.add(listing.id);
    if (!isHttpsZameenProperty(listing.canonicalUrl))
      errors.push(`${label}: invalid canonical Zameen property URL`);
    if (!listing.canonicalUrl || seenUrls.has(listing.canonicalUrl))
      errors.push(`${label}: missing or duplicate canonical URL`);
    seenUrls.add(listing.canonicalUrl);
    if (!["sales", "rentals"].includes(listing.source))
      errors.push(`${label}: source must be sales or rentals`);
    if (listing.agency !== AGENCY)
      errors.push(`${label}: agency is not ${AGENCY}`);
    if (!listing.title || !listing.description)
      errors.push(`${label}: missing title or description`);
    if (
      !listing.inventory ||
      listing.inventory.sourceStatus !== "listed_on_source_at_scrape_time"
    ) {
      errors.push(`${label}: missing safe inventory status metadata`);
    }
    const scrapedAt = Date.parse(listing.scrapedAt);
    if (!Number.isFinite(scrapedAt))
      errors.push(`${label}: invalid scrapedAt timestamp`);
    else if (now - scrapedAt > options.maxAgeHours * 3_600_000)
      errors.push(`${label}: older than ${options.maxAgeHours} hours`);

    const facts = listing.facts;
    if (!facts?.propertyType || !facts?.purpose || !facts?.location)
      errors.push(`${label}: incomplete property type, purpose, or location`);
    if (
      !Number.isInteger(facts?.price?.amount) ||
      facts.price.amount <= 0 ||
      facts.price.currency !== "PKR"
    ) {
      errors.push(`${label}: invalid normalized PKR price`);
    }
    if (
      !Number.isFinite(facts?.area?.value) ||
      facts.area.value <= 0 ||
      !facts.area.unit
    )
      errors.push(`${label}: invalid normalized area`);
    if (
      facts?.propertyType !== "Residential Plot" &&
      !Number.isInteger(facts?.bedrooms)
    )
      warnings.push(`${label}: bedroom count is absent`);
    if (
      facts?.propertyType !== "Residential Plot" &&
      !Number.isInteger(facts?.bathrooms)
    )
      warnings.push(`${label}: bathroom count is absent`);
    if (
      !Array.isArray(facts?.locationHierarchy) ||
      facts.locationHierarchy.length === 0
    )
      warnings.push(`${label}: no location hierarchy from breadcrumbs`);

    if (!Array.isArray(listing.media) || listing.media.length === 0)
      warnings.push(
        `${label}: no selected property media; keep it out of WhatsApp gallery replies until photos are added`,
      );
    const mediaUrls = new Set();
    for (const media of listing.media || []) {
      if (!media?.url || mediaUrls.has(media.url))
        errors.push(`${label}: missing or duplicate media URL`);
      mediaUrls.add(media.url);
      try {
        const parsed = new URL(media.url);
        if (
          parsed.hostname !== "media.zameen.com" ||
          !/\/thumbnails\/\d+-\d+x\d+\.(?:jpe?g|png|webp)$/i.test(
            parsed.pathname,
          )
        ) {
          errors.push(`${label}: non-gallery media was included`);
        }
      } catch {
        errors.push(`${label}: invalid media URL`);
      }
    }
    if (options.requireDownloadedMedia && listing.media.length > 0) {
      if (
        !Array.isArray(listing.downloadedMedia) ||
        listing.downloadedMedia.length !== listing.media.length
      ) {
        errors.push(`${label}: not every selected image was downloaded`);
      } else {
        for (const media of listing.downloadedMedia) {
          if (
            media.error ||
            !media.localFile ||
            !(await fileExists(path.join(options.inputDir, media.localFile)))
          ) {
            errors.push(
              `${label}: missing downloaded image for ${media.url || "<unknown URL>"}`,
            );
          }
        }
      }
    }
  }

  let detailFiles = [];
  try {
    detailFiles = (
      await readdir(path.join(options.inputDir, "listings"))
    ).filter((file) => file.endsWith(".json"));
  } catch (error) {
    errors.push(`Cannot read listing detail directory: ${error.message}`);
  }
  if (detailFiles.length !== listings.length)
    errors.push(
      `Detail file count (${detailFiles.length}) does not match aggregate count (${listings.length})`,
    );
  const aggregateById = new Map(
    listings.map((listing) => [String(listing.id), listing]),
  );
  for (const file of detailFiles) {
    const id = path.basename(file, ".json");
    const filePath = path.join(options.inputDir, "listings", file);
    try {
      const detail = JSON.parse(await readFile(filePath, "utf8"));
      const aggregate = aggregateById.get(id);
      if (!aggregate)
        errors.push(`Detail file ${file} has no aggregate record`);
      else if (hash(detail) !== hash(aggregate))
        errors.push(`Detail file ${file} differs from aggregate record`);
    } catch (error) {
      errors.push(`Cannot parse detail file ${file}: ${error.message}`);
    }
  }

  console.log(
    `Validated ${listings.length} listing records in ${options.inputDir}.`,
  );
  printIssues("WARNING", warnings);
  printIssues("ERROR", errors);
  console.log(
    `Result: ${errors.length} error(s), ${warnings.length} warning(s).`,
  );
  process.exitCode = errors.length ? 1 : 0;
}

main().catch((error) => {
  console.error(`Validation stopped: ${error.message}`);
  process.exitCode = 1;
});
