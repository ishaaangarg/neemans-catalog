/**
 * Neemans.com Product Scraper
 *
 * Neemans runs on Shopify, so this scraper uses the Shopify JSON API endpoints
 * (/collections.json, /products.json) which are publicly available and more
 * reliable than parsing HTML. Cheerio is used for HTML fallback parsing where
 * needed (e.g. extracting category links from the homepage nav).
 *
 * Usage:
 *   npm install
 *   node scraper.js
 *
 * Output:
 *   scraped_data/products.json     — full structured data
 *   scraped_data/products.csv      — flat CSV for spreadsheet use
 *   scraped_data/images/<handle>/  — downloaded product images
 */

'use strict';

const axios = require('axios');
const cheerio = require('cheerio');
const fs = require('fs');
const path = require('path');
const https = require('https');
const { createObjectCsvWriter } = require('csv-writer');

// ─── Config ──────────────────────────────────────────────────────────────────

const BASE_URL = 'https://www.neemans.com';
const OUTPUT_DIR = path.join(__dirname, 'scraped_data');
const IMAGES_DIR = path.join(OUTPUT_DIR, 'images');

const REQUEST_DELAY_MS = 1200;   // pause between requests (be polite)
const RETRY_ATTEMPTS = 3;
const RETRY_DELAY_MS = 3000;
const PRODUCTS_PER_PAGE = 250;   // Shopify max

const HEADERS = {
  'User-Agent':
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
  Accept: 'application/json, text/html, */*',
  'Accept-Language': 'en-US,en;q=0.9',
};

// ─── Utilities ───────────────────────────────────────────────────────────────

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function ensureDir(dir) {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function sanitizeFilename(name) {
  return name.replace(/[^a-z0-9_\-]/gi, '_').toLowerCase();
}

async function get(url, params = {}, retries = RETRY_ATTEMPTS) {
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const res = await axios.get(url, {
        params,
        headers: HEADERS,
        timeout: 15000,
        httpsAgent: new https.Agent({ rejectUnauthorized: false }),
      });
      return res;
    } catch (err) {
      const status = err.response?.status;
      const isRetryable = !status || status === 429 || status >= 500;
      console.warn(
        `  [WARN] GET ${url} failed (attempt ${attempt}/${retries}): ${err.message}`
      );
      if (attempt < retries && isRetryable) {
        const backoff = RETRY_DELAY_MS * attempt;
        console.warn(`  Retrying in ${backoff}ms…`);
        await sleep(backoff);
      } else {
        throw err;
      }
    }
  }
}

async function downloadImage(imageUrl, destPath) {
  if (fs.existsSync(destPath)) return; // skip already downloaded
  try {
    const res = await axios.get(imageUrl, {
      responseType: 'arraybuffer',
      headers: HEADERS,
      timeout: 30000,
    });
    fs.writeFileSync(destPath, res.data);
  } catch (err) {
    console.warn(`  [WARN] Could not download image ${imageUrl}: ${err.message}`);
  }
}

// ─── Step 1: Discover collections (categories) ───────────────────────────────

/**
 * Primary method: Shopify /collections.json API
 */
async function fetchCollectionsViaApi() {
  const collections = [];
  let page = 1;

  while (true) {
    const url = `${BASE_URL}/collections.json`;
    const res = await get(url, { limit: 250, page });
    const data = res.data?.collections ?? [];
    if (!data.length) break;
    collections.push(...data);
    if (data.length < 250) break;
    page++;
    await sleep(REQUEST_DELAY_MS);
  }

  return collections.map((c) => ({
    handle: c.handle,
    title: c.title,
    url: `${BASE_URL}/collections/${c.handle}`,
  }));
}

/**
 * Fallback: scrape homepage navigation for category links with Cheerio
 */
async function fetchCollectionsViaHtml() {
  console.log('  Falling back to HTML nav scraping for categories…');
  const res = await get(BASE_URL);
  const $ = cheerio.load(res.data);

  const seen = new Set();
  const collections = [];

  $('a[href*="/collections/"]').each((_, el) => {
    const href = $(el).attr('href') || '';
    const match = href.match(/\/collections\/([a-z0-9_-]+)/i);
    if (!match) return;
    const handle = match[1];
    if (seen.has(handle) || handle === 'all') return;
    seen.add(handle);
    collections.push({
      handle,
      title: $(el).text().trim() || handle,
      url: `${BASE_URL}/collections/${handle}`,
    });
  });

  return collections;
}

async function discoverCollections() {
  console.log('\n[1/4] Discovering collections…');
  try {
    const collections = await fetchCollectionsViaApi();
    console.log(`  Found ${collections.length} collections via API.`);
    return collections;
  } catch (err) {
    console.warn(`  API failed: ${err.message}`);
    const collections = await fetchCollectionsViaHtml();
    console.log(`  Found ${collections.length} collections via HTML.`);
    return collections;
  }
}

// ─── Step 2: Fetch all products per collection ───────────────────────────────

/**
 * Shopify /collections/<handle>/products.json supports ?limit=250&page=N
 */
async function fetchProductsForCollection(collection) {
  const products = [];
  let page = 1;

  while (true) {
    const url = `${BASE_URL}/collections/${collection.handle}/products.json`;
    let res;
    try {
      res = await get(url, { limit: PRODUCTS_PER_PAGE, page });
    } catch (err) {
      console.warn(
        `  [WARN] Could not fetch products for "${collection.title}": ${err.message}`
      );
      break;
    }

    const batch = res.data?.products ?? [];
    if (!batch.length) break;
    products.push(...batch);
    if (batch.length < PRODUCTS_PER_PAGE) break;
    page++;
    await sleep(REQUEST_DELAY_MS);
  }

  return products;
}

// ─── Step 3: Parse Shopify product objects ───────────────────────────────────

/**
 * Extract structured data from a raw Shopify product object.
 * Returns one record per color variant (so each row in the CSV is one color).
 */
function parseProduct(rawProduct, collectionTitle) {
  const { handle, title, body_html, variants, images, options } = rawProduct;

  // Strip HTML tags from description
  const description = body_html
    ? cheerio.load(body_html).text().replace(/\s+/g, ' ').trim()
    : '';

  // Identify which option index corresponds to "color" (case-insensitive)
  const colorOptionIndex = (options ?? []).findIndex((o) =>
    /colou?r/i.test(o.name)
  );

  // Collect all image URLs (use 1200px variant if available)
  const allImageUrls = (images ?? []).map((img) => {
    // Shopify image URL pattern: .../filename_<size>.jpg — request 1200px
    const src = img.src.split('?')[0]; // strip query params
    return src.replace(/(_\d+x\d*)?(\.[a-z]+)$/i, '_1200x$2');
  });

  // Group variants by color
  const colorMap = new Map(); // color -> { variantIds, sizes }

  for (const variant of variants ?? []) {
    const color =
      colorOptionIndex >= 0
        ? variant[`option${colorOptionIndex + 1}`] ?? 'Default'
        : variant.option1 ?? 'Default';

    if (!colorMap.has(color)) {
      colorMap.set(color, { sizes: [], available: false });
    }
    const entry = colorMap.get(color);
    entry.sizes.push(variant.option2 ?? variant.title);
    if (variant.available) entry.available = true;
  }

  // If no color variants found, treat as single "Default" record
  if (!colorMap.size) colorMap.set('Default', { sizes: [], available: true });

  // Build one record per color
  const records = [];
  for (const [color, { sizes, available }] of colorMap) {
    records.push({
      product_name: title,
      handle,
      category: collectionTitle,
      color,
      sizes: [...new Set(sizes)].join(' | '),
      available,
      description,
      image_urls: allImageUrls,
      image_paths: [], // filled in after download
      product_url: `${BASE_URL}/products/${handle}`,
    });
  }

  return records;
}

// ─── Step 4: Deduplicate across collections ──────────────────────────────────

/**
 * Products can appear in multiple collections. Deduplicate by handle+color,
 * but preserve all category names as a joined string.
 */
function deduplicateRecords(records) {
  const map = new Map();

  for (const r of records) {
    const key = `${r.handle}::${r.color}`;
    if (map.has(key)) {
      const existing = map.get(key);
      const cats = new Set(existing.category.split(' | '));
      cats.add(r.category);
      existing.category = [...cats].join(' | ');
    } else {
      map.set(key, { ...r });
    }
  }

  return [...map.values()];
}

// ─── Step 5: Download images ─────────────────────────────────────────────────

async function downloadImagesForRecords(records) {
  console.log('\n[3/4] Downloading images…');
  let total = 0;

  for (const record of records) {
    const productDir = path.join(IMAGES_DIR, sanitizeFilename(record.handle));
    ensureDir(productDir);

    const localPaths = [];
    for (let i = 0; i < record.image_urls.length; i++) {
      const url = record.image_urls[i];
      const ext = url.match(/\.([a-z]+)(\?|$)/i)?.[1] ?? 'jpg';
      const filename = `${i + 1}.${ext}`;
      const destPath = path.join(productDir, filename);

      await downloadImage(url, destPath);
      // Store a relative path for portability
      localPaths.push(path.relative(OUTPUT_DIR, destPath).replace(/\\/g, '/'));
      total++;
    }
    record.image_paths = localPaths;
    await sleep(200); // small pause between products
  }

  console.log(`  Downloaded ${total} images.`);
}

// ─── Step 6: Save outputs ────────────────────────────────────────────────────

function saveJson(records) {
  const outPath = path.join(OUTPUT_DIR, 'products.json');
  fs.writeFileSync(outPath, JSON.stringify(records, null, 2));
  console.log(`  JSON saved → ${outPath}`);
}

async function saveCsv(records) {
  const outPath = path.join(OUTPUT_DIR, 'products.csv');
  const writer = createObjectCsvWriter({
    path: outPath,
    header: [
      { id: 'product_name', title: 'product_name' },
      { id: 'category', title: 'category' },
      { id: 'color', title: 'color' },
      { id: 'sizes', title: 'sizes' },
      { id: 'available', title: 'available' },
      { id: 'description', title: 'description' },
      { id: 'image_paths_str', title: 'image_paths' },
      { id: 'image_urls_str', title: 'image_urls' },
      { id: 'product_url', title: 'product_url' },
    ],
  });

  const rows = records.map((r) => ({
    ...r,
    image_paths_str: r.image_paths.join(' | '),
    image_urls_str: r.image_urls.join(' | '),
  }));

  await writer.writeRecords(rows);
  console.log(`  CSV  saved → ${outPath}`);
}

// ─── Main ─────────────────────────────────────────────────────────────────────

async function main() {
  console.log('═══════════════════════════════════════');
  console.log('  Neemans.com Product Scraper');
  console.log('═══════════════════════════════════════');

  ensureDir(OUTPUT_DIR);
  ensureDir(IMAGES_DIR);

  // 1. Discover collections
  const collections = await discoverCollections();
  if (!collections.length) {
    console.error('No collections found. Aborting.');
    process.exit(1);
  }

  // 2. Fetch products for every collection
  console.log('\n[2/4] Fetching products for each collection…');
  const allRawRecords = [];
  let collectionsDone = 0;

  for (const collection of collections) {
    process.stdout.write(
      `  [${++collectionsDone}/${collections.length}] ${collection.title}… `
    );
    const rawProducts = await fetchProductsForCollection(collection);
    process.stdout.write(`${rawProducts.length} products\n`);

    for (const rawProduct of rawProducts) {
      const records = parseProduct(rawProduct, collection.title);
      allRawRecords.push(...records);
    }

    await sleep(REQUEST_DELAY_MS);
  }

  // 3. Deduplicate (products appear in multiple collections)
  const records = deduplicateRecords(allRawRecords);
  console.log(
    `\n  Total unique product-color records: ${records.length} ` +
    `(from ${allRawRecords.length} before dedup)`
  );

  // 4. Download images
  await downloadImagesForRecords(records);

  // 5. Save outputs
  console.log('\n[4/4] Saving outputs…');
  saveJson(records);
  await saveCsv(records);

  console.log('\n✓ Done.\n');
}

main().catch((err) => {
  console.error('\n[FATAL]', err.message);
  process.exit(1);
});
