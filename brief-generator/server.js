'use strict';

const express     = require('express');
const multer      = require('multer');
const Anthropic   = require('@anthropic-ai/sdk');
const PDFDocument = require('pdfkit');
const fs          = require('fs');
const path        = require('path');

const app  = express();
const PORT = 3000;

const BIBLE_PATH = path.join(__dirname, '../scraper/scraped_data/style_bible.md');

// ─── Multer: memory storage, images only, max 6 files / 15 MB each ──────────

const upload = multer({
  storage: multer.memoryStorage(),
  limits:  { files: 6, fileSize: 15 * 1024 * 1024 },
  fileFilter: (_req, file, cb) => {
    if (file.mimetype.startsWith('image/')) cb(null, true);
    else cb(new Error('Images only please.'));
  },
});

// ─── Style bible helpers ─────────────────────────────────────────────────────

function loadBibleSection(category, gender) {
  if (!fs.existsSync(BIBLE_PATH)) return null;
  const content = fs.readFileSync(BIBLE_PATH, 'utf-8');
  const header  = `## ${category} — ${gender}`;
  const start   = content.indexOf(header);
  if (start === -1) return null;
  const after   = content.slice(start + header.length);
  const nextSec = after.search(/\n## /);
  return content.slice(start, nextSec === -1 ? undefined : start + header.length + nextSec).trim();
}

function loadMoodSection() {
  if (!fs.existsSync(BIBLE_PATH)) return null;
  const content = fs.readFileSync(BIBLE_PATH, 'utf-8');
  const start   = content.indexOf('## Part 2: Mood');
  return start === -1 ? null : content.slice(start).trim();
}

function bibleAvailable() { return fs.existsSync(BIBLE_PATH); }

// ─── Routes ──────────────────────────────────────────────────────────────────

app.use(express.static(path.join(__dirname, 'public')));
app.use(express.json({ limit: '2mb' }));

// Health / bible status check
app.get('/status', (_req, res) => {
  res.json({ bibleLoaded: bibleAvailable() });
});

// Main generation endpoint
app.post('/generate', upload.array('images', 6), async (req, res) => {
  try {
    const { category, gender } = req.body;
    const files = req.files;

    if (!files || files.length === 0)
      return res.status(400).json({ error: 'Upload at least one shoe image.' });
    if (!category || !gender)
      return res.status(400).json({ error: 'Select a category and gender.' });

    const apiKey = process.env.ANTHROPIC_API_KEY;
    if (!apiKey)
      return res.status(500).json({ error: 'ANTHROPIC_API_KEY not set on the server.' });

    const client = new Anthropic({ apiKey });

    // ── Step 1: Analyse shoe images with Claude Vision ────────────────────
    const imageBlocks = files.map(f => ({
      type: 'image',
      source: { type: 'base64', media_type: f.mimetype, data: f.buffer.toString('base64') },
    }));

    const visionPrompt = `You are analysing shoe product images for Neemans, an Indian lifestyle footwear brand.
Extract ONLY what is directly observable. Return ONLY valid JSON (no extra text):

{
  "shoe_color_primary": "e.g. 'chalk white'",
  "shoe_color_secondary": "e.g. 'grey EVA sole' or null",
  "shoe_color_family": "black|white|grey|navy|brown|tan|earth_tone|neon|pastel|multicolor",
  "shoe_style_type": "e.g. 'slip-on knit sneaker', 'penny loafer', 'flip flop', 'oxford'",
  "upper_material": "e.g. 'knit mesh', 'smooth leather', 'canvas', 'suede'",
  "sole_color": "e.g. 'white EVA', 'gum rubber', 'black rubber'",
  "sole_pattern": "plain|ribbed|geometric|logo-embossed|honeycomb|waffle|lugged|textured",
  "sole_notable": true,
  "distinctive_elements": ["specific visual details — e.g. 'woven ribbon strap', 'contrast white outsole', 'tonal embroidered logo', 'two-tone lace', 'perforated toe box'. Empty if none."],
  "overall_colorway_vibe": "e.g. 'clean minimal monochrome', 'warm earth palette', 'bold contrast'"
}`;

    const visionResp = await client.messages.create({
      model: 'claude-haiku-4-5',
      max_tokens: 600,
      messages: [{ role: 'user', content: [...imageBlocks, { type: 'text', text: visionPrompt }] }],
    });

    let shoeAnalysis = {};
    try {
      const raw  = visionResp.content[0].text.trim();
      const s    = raw.indexOf('{'), e = raw.lastIndexOf('}') + 1;
      shoeAnalysis = JSON.parse(raw.slice(s, e));
    } catch (_) {
      shoeAnalysis = { raw_description: visionResp.content[0].text };
    }

    // ── Step 2: Load style bible context ─────────────────────────────────
    const styleGuide = loadBibleSection(category, gender);
    const moodGuide  = loadMoodSection();

    const bibleContext = styleGuide
      ? `\n\nNEEMANS STYLE BIBLE — ${category} ${gender}:\n${styleGuide.slice(0, 4000)}`
      : '\n\n(No style bible loaded — use general Neemans brand principles: shoe is always the hero, minimal clothing, relaxed urban confidence.)';

    const moodContext = moodGuide
      ? `\n\nNEEMANS MOOD DIRECTION:\n${moodGuide.slice(0, 1500)}`
      : '';

    // ── Step 3: Generate 4 briefs ─────────────────────────────────────────
    const briefPrompt = `You are a creative director writing photography briefs for Neemans, an Indian lifestyle footwear brand.

SHOE BEING SHOT:
${JSON.stringify(shoeAnalysis, null, 2)}

CATEGORY: ${category} | GENDER: ${gender}
${bibleContext}
${moodContext}

Write exactly 4 photography briefs for this shoe. Ground EVERY detail in:
1. The specific shoe's color, material, and design elements above
2. The style bible patterns for this category and gender
3. Neemans' core rule: THE SHOE IS ALWAYS THE HERO — clothing is minimal and complementary

Return as a JSON array of 4 objects, each with these exact keys:
[
  {
    "type": "Indoor Lifestyle",
    "headline": "5-7 word punchy brief title",
    "scene": "One sentence describing the scene/setting",
    "clothing": { "type": "exact clothing item", "color": "exact color", "fit": "slim/relaxed/etc." },
    "model": { "skin_tone": "fair/wheatish/dusky/dark", "age": "e.g. mid-20s", "build": "lean/regular/athletic", "expression": "relaxed/candid/neutral", "framing": "full-body/waist-down/leg-shot" },
    "camera": { "angle": "ground-level/low-angle/eye-level/45-degree", "shoe_in_frame": "e.g. 70% of frame" },
    "background": "specific description — color, surface, texture",
    "lighting": "soft-diffused/warm-golden/bright-natural/cool-studio",
    "accessories": "e.g. minimal — sunglasses only | none",
    "socks": "not visible / no-show / ankle socks with shorts only",
    "design_highlight": "which distinctive shoe element to feature prominently",
    "ai_prompt": "Complete copy-paste-ready AI image generation prompt incorporating all of the above"
  },
  { "type": "Leg / Close-up", ... },
  { "type": "Outdoor / Editorial", ... },
  { "type": "Mood / Concept", ... }
]

The ai_prompt field must be a complete, detailed, copy-paste-ready prompt — specific enough to hand to a photographer or paste directly into Midjourney/DALL-E. Include all style details, no placeholders.`;

    const briefResp = await client.messages.create({
      model:      'claude-opus-4-5',
      max_tokens: 3000,
      messages:   [{ role: 'user', content: briefPrompt }],
    });

    let briefs = [];
    try {
      const raw = briefResp.content[0].text.trim();
      const s   = raw.indexOf('['), e = raw.lastIndexOf(']') + 1;
      briefs    = JSON.parse(raw.slice(s, e));
    } catch (_) {
      return res.status(500).json({ error: 'Failed to parse briefs. Please try again.', raw: briefResp.content[0].text });
    }

    // ── Step 4: Build markdown output ────────────────────────────────────
    const markdown = buildMarkdown(briefs, shoeAnalysis, category, gender);

    res.json({ briefs, markdown, shoeAnalysis });

  } catch (err) {
    console.error(err);
    res.status(500).json({ error: err.message || 'Something went wrong.' });
  }
});

// ─── Markdown builder ────────────────────────────────────────────────────────

function buildMarkdown(briefs, shoe, category, gender) {
  const date = new Date().toLocaleDateString('en-GB', { day:'numeric', month:'long', year:'numeric' });
  let md = `# Neemans Photography Briefs\n`;
  md    += `**${category} — ${gender}** | Generated ${date}\n\n`;
  md    += `---\n\n`;
  md    += `## Shoe Analysed\n\n`;
  md    += `- **Style:** ${shoe.shoe_style_type || '—'}\n`;
  md    += `- **Color:** ${shoe.shoe_color_primary || '—'}`;
  if (shoe.shoe_color_secondary) md += ` / ${shoe.shoe_color_secondary}`;
  md    += `\n`;
  md    += `- **Upper material:** ${shoe.upper_material || '—'}\n`;
  md    += `- **Sole:** ${shoe.sole_color || '—'} (${shoe.sole_pattern || '—'})\n`;
  if (shoe.distinctive_elements && shoe.distinctive_elements.length)
    md  += `- **Distinctive details:** ${shoe.distinctive_elements.join(', ')}\n`;
  md    += `\n---\n\n`;

  briefs.forEach((b, i) => {
    md  += `## Brief ${i + 1}: ${b.type}\n\n`;
    md  += `### ${b.headline}\n\n`;
    md  += `**Scene:** ${b.scene}\n\n`;
    md  += `**Clothing:**\n`;
    md  += `- Type: ${b.clothing?.type || '—'}\n`;
    md  += `- Color: ${b.clothing?.color || '—'}\n`;
    md  += `- Fit: ${b.clothing?.fit || '—'}\n\n`;
    md  += `**Model:**\n`;
    md  += `- Skin tone: ${b.model?.skin_tone || '—'}\n`;
    md  += `- Age: ${b.model?.age || '—'}\n`;
    md  += `- Build: ${b.model?.build || '—'}\n`;
    md  += `- Expression: ${b.model?.expression || '—'}\n`;
    md  += `- Framing: ${b.model?.framing || '—'}\n\n`;
    md  += `**Camera:**\n`;
    md  += `- Angle: ${b.camera?.angle || '—'}\n`;
    md  += `- Shoe in frame: ${b.camera?.shoe_in_frame || '—'}\n\n`;
    md  += `**Background:** ${b.background || '—'}\n\n`;
    md  += `**Lighting:** ${b.lighting || '—'}\n\n`;
    md  += `**Accessories:** ${b.accessories || '—'}\n\n`;
    md  += `**Socks:** ${b.socks || '—'}\n\n`;
    if (b.design_highlight)
      md += `**Design highlight:** ${b.design_highlight}\n\n`;
    md  += `### AI Generation Prompt\n\n`;
    md  += `> ${b.ai_prompt}\n\n`;
    md  += `---\n\n`;
  });

  return md;
}

// ─── PDF generation endpoint ─────────────────────────────────────────────────

app.post('/generate-pdf', (req, res) => {
  const { briefs, shoeAnalysis, category, gender } = req.body || {};
  if (!Array.isArray(briefs) || !briefs.length)
    return res.status(400).json({ error: 'No briefs data.' });

  const slug  = (category || 'brief').toLowerCase().replace(/[\s']+/g, '-');
  const fname = `neemans-briefs-${slug}.pdf`;
  res.setHeader('Content-Type', 'application/pdf');
  res.setHeader('Content-Disposition', `attachment; filename="${fname}"`);

  const doc = new PDFDocument({ size: 'A4', margin: 0, autoFirstPage: false });
  doc.on('error', err => console.error('PDF stream error:', err));
  doc.pipe(res);

  try {
    buildBriefsPDF(doc, briefs, shoeAnalysis || {}, category || '', gender || '');
  } catch (err) {
    console.error('PDF build error:', err);
  }

  doc.end();
});

// ─── PDF builder ─────────────────────────────────────────────────────────────

function buildBriefsPDF(doc, briefs, shoe, category, gender) {
  const COAL   = '#3A3A3A';
  const ECRU   = '#F3F2EE';
  const WOOL   = '#EDD8C3';
  const ACCENT = '#8B7355';
  const WHITE  = '#FFFFFF';

  const TYPE_COLORS = {
    'Indoor Lifestyle':    '#1976D2',
    'Leg / Close-up':      '#7B1FA2',
    'Outdoor / Editorial': '#2E7D32',
    'Mood / Concept':      '#BF360C',
  };

  const PW = 595.28;   // A4 width in points
  const PH = 841.89;   // A4 height in points
  const M  = 50;       // margin
  const CW = PW - M * 2;

  const date = new Date().toLocaleDateString('en-GB', {
    day: 'numeric', month: 'long', year: 'numeric',
  });

  // ── Helpers ────────────────────────────────────────────────────────────────

  function row(label, value, y, idx) {
    doc.rect(M, y, CW, 24).fill(idx % 2 === 0 ? ECRU : WHITE);
    doc.fillColor(ACCENT).font('Helvetica-Bold').fontSize(8)
       .text(label, M + 10, y + 8, { width: 95, lineBreak: false, characterSpacing: 0.5 });
    doc.fillColor(COAL).font('Helvetica').fontSize(8)
       .text(String(value || '—'), M + 112, y + 8, { width: CW - 120, lineBreak: false });
    return y + 24;
  }

  function sectionHeader(text, y) {
    doc.rect(M, y, CW, 22).fill(COAL);
    doc.fillColor(ECRU).font('Helvetica-Bold').fontSize(8)
       .text(text, M + 10, y + 7, { characterSpacing: 1.5, lineBreak: false });
    return y + 22;
  }

  // ── Cover page ─────────────────────────────────────────────────────────────
  doc.addPage();

  // Header bar
  doc.rect(0, 0, PW, 70).fill(COAL);
  doc.fillColor(ECRU).font('Helvetica-Bold').fontSize(22)
     .text('NEEMANS', M, 19, { lineBreak: false, characterSpacing: 5 });
  doc.fillColor(ACCENT).font('Helvetica').fontSize(10)
     .text('BRIEF GENERATOR', 202, 25, { lineBreak: false, characterSpacing: 2 });

  // Title
  doc.fillColor(COAL).font('Helvetica-Bold').fontSize(20)
     .text('Photography Briefs', M, 96);
  doc.fillColor(ACCENT).font('Helvetica').fontSize(11)
     .text(`${category} — ${gender}  ·  ${date}`, M, 124);

  // Rule
  doc.strokeColor(WOOL).lineWidth(1.5).moveTo(M, 150).lineTo(PW - M, 150).stroke();

  // Shoe analysis
  let y = 168;
  y = sectionHeader('SHOE ANALYSED', y);

  const colorVal = [shoe.shoe_color_primary, shoe.shoe_color_secondary]
    .filter(Boolean).join(' / ');
  const soleVal = shoe.sole_color
    ? `${shoe.sole_color}${shoe.sole_pattern ? ` (${shoe.sole_pattern})` : ''}`
    : '—';
  const detailsVal = Array.isArray(shoe.distinctive_elements)
    ? shoe.distinctive_elements.join(', ')
    : (shoe.distinctive_elements || null);

  [
    ['STYLE',   shoe.shoe_style_type],
    ['COLOR',   colorVal],
    ['UPPER',   shoe.upper_material],
    ['SOLE',    soleVal],
    ['DETAILS', detailsVal],
    ['VIBE',    shoe.overall_colorway_vibe],
  ].forEach(([lbl, val], i) => { y = row(lbl, val || '—', y, i); });

  y += 20;
  y = sectionHeader('BRIEF OVERVIEW', y);

  briefs.forEach((b, i) => {
    const tc = TYPE_COLORS[b.type] || COAL;
    doc.rect(M,     y, 6,      22).fill(tc);
    doc.rect(M + 6, y, CW - 6, 22).fill(i % 2 === 0 ? ECRU : WHITE);
    doc.fillColor(tc).font('Helvetica-Bold').fontSize(8)
       .text((b.type || '').toUpperCase(), M + 14, y + 7, { width: 142, lineBreak: false });
    doc.fillColor(COAL).font('Helvetica').fontSize(8)
       .text(b.headline || '', M + 164, y + 7, { width: CW - 170, lineBreak: false });
    y += 22;
  });

  // ── Brief pages ────────────────────────────────────────────────────────────
  briefs.forEach((b, i) => {
    doc.addPage();
    const tc = TYPE_COLORS[b.type] || COAL;

    // Header bar
    doc.rect(0, 0, PW, 72).fill(COAL);
    doc.fillColor(ECRU).font('Helvetica').fontSize(8)
       .text(`NEEMANS  ·  BRIEF ${i + 1} OF ${briefs.length}`, M, 16,
         { lineBreak: false, characterSpacing: 1 });

    // Type badge (in header)
    const bText = (b.type || '').toUpperCase();
    doc.font('Helvetica-Bold').fontSize(8);
    const bW = doc.widthOfString(bText) + 18;
    doc.rect(M, 32, bW, 18).fill(tc);
    doc.fillColor(WHITE).text(bText, M + 9, 37, { lineBreak: false });

    // Headline (in header)
    doc.fillColor(WHITE).font('Helvetica-Bold').fontSize(15)
       .text(b.headline || '', M + bW + 12, 30,
         { width: CW - bW - 12, lineBreak: false });

    y = 90;

    // Scene (italic)
    doc.fillColor(COAL).font('Helvetica-Oblique').fontSize(11)
       .text(b.scene || '', M, y, { width: CW, lineGap: 3 });
    y = doc.y + 16;

    // Rule
    doc.strokeColor(WOOL).lineWidth(1).moveTo(M, y).lineTo(PW - M, y).stroke();
    y += 14;

    // Specs
    const specs = [
      ['CLOTHING',   `${b.clothing?.type || '—'} · ${b.clothing?.color || ''} · ${b.clothing?.fit || ''}`],
      ['MODEL',      `${b.model?.skin_tone || '—'} · ${b.model?.age || '—'} · ${b.model?.build || '—'}`],
      ['FRAMING',    b.model?.framing || '—'],
      ['EXPRESSION', b.model?.expression || '—'],
      ['CAMERA',     `${b.camera?.angle || '—'} · Shoe: ${b.camera?.shoe_in_frame || '—'}`],
      ['BACKGROUND', b.background || '—'],
      ['LIGHTING',   b.lighting || '—'],
      ['ACCESSORIES',b.accessories || '—'],
      ['SOCKS',      b.socks || '—'],
    ];
    if (b.design_highlight) specs.push(['HIGHLIGHT', b.design_highlight]);

    specs.forEach(([lbl, val], idx) => { y = row(lbl, val, y, idx); });

    y += 20;

    // AI Prompt — ensure enough space remains, otherwise start on new page
    if (y + 80 > PH - M) {
      doc.addPage();
      y = M;
    }

    // Section header
    y = sectionHeader('AI GENERATION PROMPT', y);

    // Calculate prompt box height
    const promptText = b.ai_prompt || '';
    doc.font('Helvetica-Oblique').fontSize(10);
    const promptH = doc.heightOfString(promptText, { width: CW - 24, lineGap: 2 });

    // Draw background — PDFKit clips at page edge; text auto-paginates
    doc.rect(M, y, CW, promptH + 24).fill(ECRU);

    // Prompt text
    doc.fillColor(COAL).font('Helvetica-Oblique').fontSize(10)
       .text(promptText, M + 12, y + 12, { width: CW - 24, lineGap: 2 });
  });
}

// ─── Start ───────────────────────────────────────────────────────────────────

app.listen(PORT, () => {
  console.log(`\n  Neemans Brief Generator`);
  console.log(`  Running at http://localhost:${PORT}`);
  console.log(`  Style bible: ${bibleAvailable() ? '✓ loaded' : '✗ not found (will use defaults)'}\n`);
});
