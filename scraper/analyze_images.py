#!/usr/bin/env python3
"""
Neemans Visual Style Bible Generator
======================================
Analyses Neemans product imagery and outputs two files:
  • scraped_data/style_bible.md   — full structured analysis
  • scraped_data/style_bible.pdf  — brand document for creative/photographer handoff

Architecture
------------
Vision pass  : per-product, model-images only, extracts shot_type + all style fields
Grouping     : Category × Gender × Color Tone
Synthesis    : per Category+Gender group, broken by Color Tone → 4 Shot Types
Mood section : single call across all categories

Usage (Windows PowerShell):
  $env:ANTHROPIC_API_KEY="sk-ant-..."
  python analyze_images.py
"""

import json, os, sys, re, time, base64, subprocess
import requests
from pathlib import Path
from collections import defaultdict
from datetime import date

# ─── Config ──────────────────────────────────────────────────────────────────

BASE_URL      = "https://www.neemans.com"
BASE_DIR      = Path(__file__).parent
DATA_DIR      = BASE_DIR / "scraped_data"
IMAGES_DIR    = DATA_DIR / "images"
PRODUCTS_JSON = DATA_DIR / "products.json"
CACHE_PATH    = DATA_DIR / "vision_cache.json"
MD_OUTPUT     = DATA_DIR / "style_bible.md"
PDF_OUTPUT    = DATA_DIR / "style_bible.pdf"

CACHE_VERSION             = "v4"
MAX_IMAGES_PER_PRODUCT    = 3    # smart-selected: largest JPEGs first (lifestyle > product)
MAX_PRODUCTS_PER_CATEGORY = 120   # analyse up to 120 products per category+gender combo
SYNTH_REF_IMAGES          = 4    # images attached to each synthesis call

API_DELAY     = 0.8
SHOPIFY_DELAY = 0.4

MODEL_VISION    = "claude-haiku-4-5"
MODEL_SYNTHESIS = "claude-opus-4-5"

# Non-footwear products to skip entirely
SKIP_KEYWORDS = [
    "sock", "insole", "care kit", "lace", "deodorizer",
    "accessory", "accessories", "cleaner", "protector",
]

# Shopify collections that are navigation-only (not real style categories)
SKIP_COLLECTIONS = {
    "all", "frontpage", "home", "featured", "new-arrivals", "new",
    "sale", "best-sellers", "men", "women", "unisex",
}

# Category classifier — first match wins
CATEGORY_RULES = [
    ("Formal",        ["formal", "oxford", "derby", "dress", "brogue", "monk", "wingtip"]),
    ("Open Footwear", ["slide", "flip", "sandal", "clog", "slipper", "open-toe", "thong",
                       "flip-flop", "vibe-flip", "all-vibes"]),
    ("Athleisure",    ["walk", "run", "sport", "athletic", "step-on", "knit", "mesh",
                       "begin-walk", "go-walk", "trainer", "performance", "lite", "active", "neo"]),
    ("Casual",        []),   # catch-all
]

# Shoe color family → broad tone group for synthesis grouping
COLOR_TONE_MAP = {
    "black":      "Black/Charcoal",
    "grey":       "Grey/Silver",
    "white":      "White/Ivory",
    "navy":       "Navy/Blue",
    "brown":      "Brown/Earth",
    "tan":        "Brown/Earth",
    "earth_tone": "Brown/Earth",
    "neon":       "Neon/Bold",
    "pastel":     "Pastel",
    "multicolor": "Multicolor",
    "other":      "Other",
}

CAT_ORDER = ["Casual", "Athleisure", "Formal", "Open Footwear"]

# ─── Helpers ─────────────────────────────────────────────────────────────────

def sanitize(name: str) -> str:
    return re.sub(r"[^a-z0-9_\-]", "_", name.lower())

def media_type(p: Path) -> str:
    return {".jpg":"image/jpeg",".jpeg":"image/jpeg",
            ".png":"image/png",".webp":"image/webp"}.get(p.suffix.lower(),"image/jpeg")

def b64(p: Path) -> str:
    return base64.standard_b64encode(p.read_bytes()).decode()

def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    if data.get("__version__") != CACHE_VERSION:
        print("  Cache version mismatch — starting fresh.")
        return {}
    return data

def save_cache(cache: dict):
    cache["__version__"] = CACHE_VERSION
    CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

def strip_html(raw: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw or "")).strip()

def shopify_get(url, params=None) -> dict:
    r = requests.get(url, params=params,
                     headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                     timeout=15)
    r.raise_for_status()
    return r.json()

# ─── Product classification helpers ──────────────────────────────────────────

def is_non_footwear(product: dict) -> bool:
    text = f"{product.get('handle','')} {product.get('product_name','')}".lower()
    return any(kw in text for kw in SKIP_KEYWORDS)

def classify_category(product: dict) -> str:
    text = " ".join([product.get("handle",""), product.get("product_name",""),
                     " ".join(product.get("categories",[]))]).lower()
    for cat, keywords in CATEGORY_RULES:
        if not keywords:
            return cat
        if any(kw in text for kw in keywords):
            return cat
    return "Casual"

def detect_gender(product: dict) -> str:
    text = " ".join([product.get("handle",""), product.get("product_name",""),
                     " ".join(product.get("categories",[]))]).lower()
    # Handle patterns: -for-men, -men-, men's, /men, etc.
    has_women = any(w in text for w in ["-for-women", "-women-", "women's", "for women", "/women"])
    has_men   = any(w in text for w in ["-for-men", "-men-", "men's", "for men", "/men"])
    if has_women and not has_men:
        return "Women's"
    if has_men and not has_women:
        return "Men's"
    # Check categories
    for cat in product.get("categories", []):
        c = cat.lower()
        if "women" in c:
            return "Women's"
        if "men" in c:
            return "Men's"
    return "Unisex"

def get_color_tone(color_family: str) -> str:
    return COLOR_TONE_MAP.get(color_family, "Other")

# ─── Step 1: Product catalog ──────────────────────────────────────────────────

def fetch_all_products() -> list:
    products, page = [], 1
    while True:
        batch = shopify_get(f"{BASE_URL}/products.json",
                            {"limit": 250, "page": page}).get("products", [])
        if not batch: break
        products.extend(batch)
        print(f"    Page {page}: {len(batch)} ({len(products)} total)")
        if len(batch) < 250: break
        page += 1
        time.sleep(SHOPIFY_DELAY)
    return products

def fetch_collection_map() -> dict:
    h2cats = defaultdict(list)
    colls = shopify_get(f"{BASE_URL}/collections.json", {"limit": 250}).get("collections", [])
    print(f"    {len(colls)} collections found")
    for c in colls:
        if c["handle"].lower() in SKIP_COLLECTIONS: continue
        page = 1
        while True:
            try:
                prods = shopify_get(f"{BASE_URL}/collections/{c['handle']}/products.json",
                                    {"limit": 250, "page": page}).get("products", [])
            except: break
            for p in prods:
                h2cats[p["handle"]].append(c["title"])
            if len(prods) < 250: break
            page += 1
            time.sleep(SHOPIFY_DELAY)
        time.sleep(SHOPIFY_DELAY)
    return dict(h2cats)

def build_or_load_catalog() -> list:
    if PRODUCTS_JSON.exists():
        data = json.loads(PRODUCTS_JSON.read_text(encoding="utf-8"))
        if data and "local_image_dir" in data[0]:
            for p in data:
                p["style_category"] = classify_category(p)
                p["gender"]         = detect_gender(p)
            print(f"  Loaded {len(data)} products.")
            return data
    print("  Fetching from Shopify…")
    raw   = fetch_all_products()
    h2cats = fetch_collection_map()
    records = []
    for p in raw:
        h = p["handle"]
        cats = h2cats.get(h, ["Uncategorized"])
        img_dir = IMAGES_DIR / sanitize(h)
        rec = {
            "handle":          h,
            "product_name":    p["title"],
            "categories":      cats,
            "description":     strip_html(p.get("body_html","")),   # full description, no truncation
            "image_urls":      [i["src"].split("?")[0] for i in p.get("images",[])[:6]],
            "local_image_dir": str(img_dir),
            "has_local_images": img_dir.exists() and bool(list(img_dir.iterdir())),
        }
        rec["style_category"] = classify_category(rec)
        rec["gender"]         = detect_gender(rec)
        records.append(rec)
    PRODUCTS_JSON.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Saved {len(records)} products.")
    return records

# ─── Step 2: Smart image selection ───────────────────────────────────────────

def find_images(product: dict) -> list:
    """
    Return up to MAX_IMAGES_PER_PRODUCT model-likely images.
    Strategy: exclude PNGs (Shopify studio product shots), sort JPEGs by file size desc.
    Larger JPEG = more complex scene = more likely to be a lifestyle/model shot.
    Validated: 230KB JPG = leg shot, 192KB JPG = full model, 303KB PNG = white-bg shot.
    """
    img_dir = Path(product["local_image_dir"])
    if not img_dir.exists():
        return []
    jpegs, pngs = [], []
    for f in img_dir.iterdir():
        if f.suffix.lower() in (".jpg", ".jpeg", ".webp"):
            jpegs.append(f)
        elif f.suffix.lower() == ".png":
            pngs.append(f)
    jpegs_sorted = sorted(jpegs, key=lambda f: f.stat().st_size, reverse=True)
    if len(jpegs_sorted) >= MAX_IMAGES_PER_PRODUCT:
        return jpegs_sorted[:MAX_IMAGES_PER_PRODUCT]
    return (jpegs_sorted + sorted(pngs, key=lambda f: f.stat().st_size, reverse=True))[:MAX_IMAGES_PER_PRODUCT]

# ─── Step 3: Vision analysis ─────────────────────────────────────────────────

VISION_PROMPT = """\
You are a visual analyst documenting how Neemans, an Indian footwear brand, shoots their products.

IMPORTANT FILTERS:
- If the images show ONLY the shoe with no model (flat lay, product-only) set model_present to false
- If the product appears to be socks, insoles, or a non-footwear accessory set is_non_footwear to true

Extract ONLY what is directly observable. No assumptions, no recommendations.

Return ONLY a valid JSON object (no markdown, no extra text):

{
  "is_non_footwear": false,
  "shoe_color_description": "precise e.g. 'chalk white with grey EVA sole'",
  "shoe_color_family": "black|white|grey|navy|brown|tan|earth_tone|neon|pastel|multicolor|other",
  "shoe_style_type": "e.g. 'slip-on knit sneaker', 'penny loafer', 'flip flop', 'oxford'",

  "model_present": true,
  "shot_type": "indoor_leg|indoor_model|outdoor_leg|outdoor_model|product_only",
  "body_framing": "full-body|waist-down|leg-shot|foot-only|no-model",

  "clothing_colors_seen": ["exact colors of clothing visible — only what you observe"],
  "clothing_types_seen": ["exact items e.g. slim chinos, plain white tee, joggers, kurta"],
  "clothing_is_minimal": true,
  "accessories_visible": ["observed accessories e.g. sunglasses, watch — empty list if none"],

  "background_type": "studio_white|studio_grey|outdoor_nature|outdoor_urban|indoor|textured_floor|other",
  "background_color": "specific e.g. 'warm beige sandstone wall', 'cool grey studio floor'",
  "background_surface": "e.g. 'polished marble', 'rough cobblestone', 'grass', 'studio seamless' — null if not applicable",

  "camera_angle": "ground-level|low-angle|eye-level|45-degree|overhead|close-up",
  "shoe_frame_percent": "e.g. '65%'",
  "lighting_mood": "bright-natural|soft-diffused|warm-golden|cool-studio|harsh|null",

  "model_gender": "male|female|mixed|none",
  "model_skin_tone": "fair|wheatish|dusky|dark|none",
  "model_age_range": "teens|20s|30s|40s+|unclear|none",
  "model_build": "lean|athletic|regular|plus|unclear|none",
  "model_expression": "relaxed|candid|serious|laughing|neutral|not-visible|none",
  "face_visible": true,
  "model_energy": "e.g. 'relaxed urban confident' — describe the observable vibe in 3 words",

  "socks_visible": false,
  "sock_color": null,
  "sock_style": "no-show|ankle|crew|null",
  "sock_context": "with shorts|with pants|with trousers|not applicable|no model",

  "sole_color": "e.g. 'white EVA', 'black rubber', 'gum/translucent', 'contrast beige'",
  "sole_pattern": "plain|ribbed|geometric|logo-embossed|honeycomb|waffle|lugged|textured|other",
  "sole_notable": true,
  "upper_material_texture": "e.g. 'smooth leather', 'knit mesh', 'canvas', 'suede', 'perforated leather'",
  "distinctive_design_elements": ["specific visual details that make this shoe stand out — e.g. 'woven ribbon strap across vamp', 'contrast white outsole', 'tonal embroidered logo', 'two-tone lace', 'perforation pattern on toe box', 'metallic aglets'. Empty if nothing distinctive."],

  "visual_inconsistencies": ["anything observably off within this product's own images — lighting mismatch, style clash. Empty if none."]
}"""

def analyze_product(client, product: dict, cache: dict):
    h = product["handle"]
    if h in cache and h != "__version__":
        return cache[h]

    images = find_images(product)
    if not images:
        return None

    # Inject product context so Claude knows what it's analysing
    desc = product.get("description", "").strip()
    product_context = f"Product: {product['product_name']}\n"
    if desc:
        product_context += f"Brand description: {desc[:600]}\n"
    product_context += "---\n"

    content = [{"type": "text", "text": product_context}]

    for img in images:
        try:
            content.append({"type": "image",
                             "source": {"type": "base64",
                                        "media_type": media_type(img),
                                        "data": b64(img)}})
        except Exception as e:
            print(f"\n    [WARN] {img.name}: {e}")

    if len(content) <= 1:   # only context text, no images loaded
        return None

    content.append({"type": "text", "text": VISION_PROMPT})

    try:
        resp = client.messages.create(
            model=MODEL_VISION, max_tokens=900,
            messages=[{"role": "user", "content": content}],
        )
        text = resp.content[0].text.strip()
        s, e = text.find("{"), text.rfind("}") + 1
        result = json.loads(text[s:e])
        result.update({
            "handle":         h,
            "product_name":   product["product_name"],
            "style_category": product["style_category"],
            "gender":         product["gender"],
            "image_paths":    [str(img) for img in images],
        })
        cache[h] = result
        save_cache(cache)
        return result
    except Exception as e:
        print(f"\n    [ERROR] {h}: {e}")
        return None

# ─── Step 4: Group analyses ───────────────────────────────────────────────────

def group_analyses(all_analyses: list):
    """
    Returns nested dict: groups[category][gender][color_tone][shot_type] = [analyses]
    Filters out: non-footwear, product-only shots (no model).
    """
    groups = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list))))

    for a in all_analyses:
        if a.get("is_non_footwear"):
            continue
        if not a.get("model_present", False):
            continue
        shot_type = a.get("shot_type", "product_only")
        if shot_type == "product_only":
            continue

        cat   = a.get("style_category", "Casual")
        gender = a.get("gender", "Unisex")
        tone  = get_color_tone(a.get("shoe_color_family", "other"))

        groups[cat][gender][tone][shot_type].append(a)

    return groups

# ─── Step 5: Category+Gender synthesis ───────────────────────────────────────

SHOT_TYPE_LABELS = {
    "indoor_leg":    "Indoor — Leg Shot",
    "indoor_model":  "Indoor — Full Model Shot",
    "outdoor_leg":   "Outdoor — Leg Shot",
    "outdoor_model": "Outdoor — Full Model Shot",
}

def pick_ref_images(tone_data: dict, all_products: list, n: int = SYNTH_REF_IMAGES) -> list:
    """Pick n representative image paths from across all shot types in a tone group."""
    handle_map = {p["handle"]: p for p in all_products}
    seen_handles, paths = set(), []
    for shot_type in ["outdoor_model", "outdoor_leg", "indoor_model", "indoor_leg"]:
        for a in tone_data.get(shot_type, []):
            if len(paths) >= n:
                break
            h = a["handle"]
            if h in seen_handles:
                continue
            p = handle_map.get(h)
            if not p:
                continue
            imgs = find_images(p)
            if imgs:
                paths.append(imgs[0])
                seen_handles.add(h)
        if len(paths) >= n:
            break
    return paths

def build_group_synthesis(cat: str, gender: str, tone_groups: dict, all_products: list) -> list:
    """
    Build the Claude message content for one Category+Gender synthesis.
    tone_groups: { color_tone: { shot_type: [analyses] } }
    """
    content = []

    # Attach representative reference images across all tones
    handle_map = {p["handle"]: p for p in all_products}
    seen, ref_count = set(), 0
    for tone, tone_data in tone_groups.items():
        for shot_type, analyses in tone_data.items():
            for a in analyses:
                if ref_count >= SYNTH_REF_IMAGES:
                    break
                h = a["handle"]
                if h in seen:
                    continue
                p = handle_map.get(h)
                if not p:
                    continue
                imgs = find_images(p)
                if not imgs:
                    continue
                try:
                    content.append({"type": "text",
                                    "text": f"Reference ({tone} — {shot_type}): {a['product_name']}"})
                    content.append({"type": "image",
                                    "source": {"type": "base64",
                                               "media_type": media_type(imgs[0]),
                                               "data": b64(imgs[0])}})
                    seen.add(h)
                    ref_count += 1
                except Exception:
                    pass
            if ref_count >= SYNTH_REF_IMAGES:
                break
        if ref_count >= SYNTH_REF_IMAGES:
            break

    # Pre-structure condensed data by tone → shot_type
    structured = {}
    for tone, tone_data in tone_groups.items():
        structured[tone] = {}
        for shot_type, analyses in tone_data.items():
            structured[tone][shot_type] = [{
                "product":           a.get("product_name"),
                "shoe_color":        a.get("shoe_color_description"),
                "shoe_style":        a.get("shoe_style_type"),
                "clothing_colors":   a.get("clothing_colors_seen", []),
                "clothing_types":    a.get("clothing_types_seen", []),
                "accessories":       a.get("accessories_visible", []),
                "background_color":  a.get("background_color"),
                "background_surface":a.get("background_surface"),
                "camera_angle":      a.get("camera_angle"),
                "shoe_frame_pct":    a.get("shoe_frame_percent"),
                "lighting":          a.get("lighting_mood"),
                "model_skin_tone":   a.get("model_skin_tone"),
                "model_age":         a.get("model_age_range"),
                "model_build":       a.get("model_build"),
                "model_expression":  a.get("model_expression"),
                "face_visible":      a.get("face_visible"),
                "body_framing":      a.get("body_framing"),
                "model_energy":      a.get("model_energy"),
                "socks_visible":     a.get("socks_visible"),
                "sock_context":             a.get("sock_context"),
                "sole_color":               a.get("sole_color"),
                "sole_pattern":             a.get("sole_pattern"),
                "sole_notable":             a.get("sole_notable"),
                "upper_texture":            a.get("upper_material_texture"),
                "distinctive_design":       a.get("distinctive_design_elements", []),
                "inconsistencies":          a.get("visual_inconsistencies", []),
            } for a in analyses]

    total_products = sum(
        len(analyses)
        for tone_data in tone_groups.values()
        for analyses in tone_data.values()
    )

    prompt = f"""You are producing the Neemans Visual Style Bible — an internal document for \
Neemans creative directors, photographers, and AI image generation briefers.

Neemans is the gold standard. This document tells someone exactly how Neemans shoots \
so they can replicate it without guesswork.

STRICT RULES:
1. PURELY DESCRIPTIVE — document what Neemans DOES. Never say should/consider/recommend/improve.
2. Use real data — specific product names, actual colors, actual observations.
3. The Anti-Patterns section lists what people commonly assume for this category/gender \
   but Neemans specifically does NOT do (e.g. "Casual Men's" does NOT mean Gen Z streetwear, \
   graphic tees, hypebeast models — Neemans shoots relaxed 25–35 professionals).
4. If a shot type has no data, write "Not observed in this dataset."

You have {ref_count} reference images above and data from {total_products} \
{cat} {gender} products, pre-grouped by shoe color tone and shot type below.

Data:
{json.dumps(structured, indent=2)}

Write the full "{cat} — {gender}" section using EXACTLY this format:

---

## {cat} — {gender}

> *[One sentence describing Neemans' overall visual approach for this category+gender combination]*

---
"""
    # Dynamically add a section per color tone
    for tone in tone_groups:
        n_products = sum(len(v) for v in tone_groups[tone].values())
        prompt += f"""
### {tone}

*{n_products} products observed in this tone group*

"""
        for shot_key, shot_label in SHOT_TYPE_LABELS.items():
            prompt += f"""#### {shot_label}

"""
            if tone_groups[tone].get(shot_key):
                prompt += f"""**Clothing:** [exact types and colors observed]
**Camera angle:** [observed angle and framing]
**Background:** [exact background color/surface/environment observed]
**Lighting:** [observed lighting mood]
**Model:** Skin tone — [observed] | Age — [observed] | Build — [observed] | Expression — [observed]
**Face shown:** [yes/no/sometimes — based on data]
**Shoe in frame:** [observed % of frame]
**Accessories:** [observed or "none observed"]
**Socks:** [observed state and context, or "not visible"]
**Model energy:** [observed vibe in a few words]

"""
            else:
                prompt += "Not observed in this dataset.\n\n"

    prompt += f"""
---

### Shoe Design Elements — {cat} — {gender}

*Distinctive physical features of Neemans shoes in this group that photographers and AI prompts must capture accurately.*

**Sole:**
[For each tone group, note sole color and pattern — e.g. "Brown/Earth: gum rubber sole with honeycomb pattern", "Black: plain black EVA with ribbed edge"]
[Flag any soles that are visually striking and should be highlighted in shots — sole_notable = true]

**Upper materials and textures:**
[Exact materials observed — knit mesh, smooth leather, canvas, perforated leather, etc.]

**Distinctive design details:**
[List every recurring distinctive element across products — ribbon strap, contrast outsole, embossed logo, tonal stitching, two-tone lace, perforation pattern, etc.]
[These details matter for photography framing — they justify close-up and angle choices]

---

### Anti-Patterns for {cat} — {gender}

*Common assumptions people make about "{cat.lower()}" + "{gender.lower()}" \
that Neemans specifically does NOT do — drawn from what is absent in the data \
combined with typical industry clichés for this category/gender.*

[Write 6–8 specific, concrete points. Each should name the assumption and contrast \
it with what the data actually shows. Examples of the thinking format:
- "Casual Men's" triggers → graphic tees, Gen Z models, streetwear energy, bold prints
  But Neemans shoots → [what the data shows instead]
- "Formal" triggers → office desk settings, ties, stiff posed looks
  But Neemans shoots → [what the data shows instead]
Ground every point in the actual data.]

---
"""

    content.append({"type": "text", "text": prompt})
    return content

def synthesize_group(client, cat, gender, tone_groups, products) -> str:
    content = build_group_synthesis(cat, gender, tone_groups, products)
    resp = client.messages.create(
        model=MODEL_SYNTHESIS, max_tokens=4000,
        messages=[{"role": "user", "content": content}],
    )
    return resp.content[0].text

# ─── Step 6: Mood/concept synthesis ──────────────────────────────────────────

def build_mood_content(all_analyses: list, all_products: list) -> list:
    handle_map = {p["handle"]: p for p in all_products}
    content = []

    # Attach 6 diverse editorial images (prefer outdoor_model shots)
    seen, count = set(), 0
    outdoor_model = [a for a in all_analyses
                     if a.get("shot_type") == "outdoor_model"
                     and a.get("model_present")]
    for a in outdoor_model:
        if count >= 6: break
        h = a["handle"]
        if h in seen: continue
        p = handle_map.get(h)
        if not p: continue
        imgs = find_images(p)
        if not imgs: continue
        try:
            content.append({"type": "text",
                             "text": f"Editorial reference ({a['style_category']} — {a['gender']}): {a['product_name']}"})
            content.append({"type": "image",
                             "source": {"type": "base64",
                                        "media_type": media_type(imgs[0]),
                                        "data": b64(imgs[0])}})
            seen.add(h)
            count += 1
        except Exception:
            pass

    # Aggregate mood-relevant data
    mood_data = [{
        "product":        a.get("product_name"),
        "category":       a.get("style_category"),
        "gender":         a.get("gender"),
        "shot_type":      a.get("shot_type"),
        "background":     a.get("background_color"),
        "lighting":       a.get("lighting_mood"),
        "model_energy":   a.get("model_energy"),
        "camera_angle":   a.get("camera_angle"),
        "clothing_colors":a.get("clothing_colors_seen", []),
        "accessories":    a.get("accessories_visible", []),
    } for a in all_analyses if a.get("model_present") and a.get("shot_type") != "product_only"]

    prompt = f"""You are documenting the editorial mood and visual language of Neemans \
as observed across their full product catalogue ({len(mood_data)} model images analysed).

You have {count} editorial reference images above.

RULES: Purely observational. Document what Neemans does. No recommendations.

Data (all model images across all categories):
{json.dumps(mood_data, indent=2)}

Write Part 2: Mood & Editorial Direction using EXACTLY this format:

---

## Part 2: Mood & Editorial Direction

> *[One sentence on Neemans' overall visual language across the full catalogue]*

---

### Visual Themes Observed

[List the recurring visual themes seen across the catalogue — \
e.g. "urban architectural settings", "warm natural daylight", "lived-in outdoor environments". \
Back each theme with observations from the data.]

---

### Color Grading & Lighting

**Dominant lighting mood:** [what appears most consistently]
**Color temperature:** [warm/cool/neutral — what the data shows]
**Studio vs natural light split:** [approximate ratio observed]
**Specific lighting setups observed:** [e.g. soft side-lighting, golden hour, overcast diffused]

---

### Men's vs Women's Mood

**Men's visual direction:**
[Describe the observable mood/energy/environment specifically for men's imagery]

**Women's visual direction:**
[Describe the observable mood/energy/environment specifically for women's imagery]

---

### Per Category Mood

**Casual:** [observed mood, environment, energy]
**Athleisure:** [observed mood, environment, energy]
**Formal:** [observed mood, environment, energy]
**Open Footwear:** [observed mood, environment, energy]

---

### What Feels Elevated vs. Commercial

**Elevated shots observed:** [describe what makes certain images feel editorial/premium]
**Commercial shots observed:** [describe what makes certain images feel more catalogue/e-commerce]
**Pattern:** [what distinguishes the two in Neemans' own catalogue]

---
"""

    content.append({"type": "text", "text": prompt})
    return content

def synthesize_mood(client, all_analyses, products) -> str:
    content = build_mood_content(all_analyses, products)
    resp = client.messages.create(
        model=MODEL_SYNTHESIS, max_tokens=3000,
        messages=[{"role": "user", "content": content}],
    )
    return resp.content[0].text

# ─── Step 7: Markdown output ─────────────────────────────────────────────────

def generate_markdown(cat_gender_sections: dict, mood_section: str, total: int) -> str:
    toc_lines = []
    for cat in CAT_ORDER:
        for gender in ["Men's", "Women's", "Unisex"]:
            if (cat, gender) in cat_gender_sections:
                anchor = re.sub(r"[^a-z0-9]+", "-",
                                f"{cat} {gender}".lower()).strip("-")
                toc_lines.append(f"  - [{cat} — {gender}](#{anchor})")
    if mood_section:
        toc_lines.append("- [Part 2: Mood & Editorial Direction](#part-2-mood--editorial-direction)")

    header = f"""# Neemans Visual Style Bible
## How Neemans Shoots — Documented Across {total} Model Images

> **Generated:** {date.today().strftime("%B %d, %Y")}
> **Scope:** {total} model-wearing-shoe images across 4 categories, 2 genders, multiple color tones
> **Purpose:** Internal reference for creative directors, photographers, AI image generation briefs
> **Note:** This document describes what Neemans does — it is not a recommendations document.

---

## Contents

{chr(10).join(toc_lines)}

---

"""
    body_parts = []
    for cat in CAT_ORDER:
        for gender in ["Men's", "Women's", "Unisex"]:
            if (cat, gender) in cat_gender_sections:
                body_parts.append(cat_gender_sections[(cat, gender)])

    if mood_section:
        body_parts.append(mood_section)

    return header + "\n\n".join(body_parts)

# ─── Step 8: PDF generation ──────────────────────────────────────────────────

def ensure_packages():
    for pkg, imp in [("reportlab", "reportlab"), ("Pillow", "PIL")]:
        try:
            __import__(imp)
        except ImportError:
            print(f"  Installing {pkg}…")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

def generate_pdf(cat_gender_sections: dict, groups: dict,
                 mood_section: str, all_analyses: list, all_products: list):
    ensure_packages()

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Image,
                                    Table, TableStyle, PageBreak, HRFlowable,
                                    KeepTogether)
    from io import BytesIO
    from PIL import Image as PILImage

    COAL   = HexColor("#3A3A3A")
    ECRU   = HexColor("#F3F2EE")
    WOOL   = HexColor("#EDD8C3")
    ACCENT = HexColor("#8B7355")
    GREEN  = HexColor("#2E7D32")
    RED    = HexColor("#C62828")
    LGREY  = HexColor("#999999")
    DKGREY = HexColor("#555555")

    def ps(name, **kw):
        return ParagraphStyle(name, **kw)

    S = {
        "cover_h1":  ps("cover_h1",  fontName="Helvetica-Bold",   fontSize=42, textColor=COAL, alignment=TA_CENTER),
        "cover_sub": ps("cover_sub", fontName="Helvetica-Oblique", fontSize=20, textColor=ACCENT, alignment=TA_CENTER, spaceAfter=5),
        "cover_tag": ps("cover_tag", fontName="Helvetica",         fontSize=10, textColor=LGREY, alignment=TA_CENTER),
        "cover_rule":ps("cover_rule",fontName="Helvetica-Oblique", fontSize=11, textColor=COAL, alignment=TA_CENTER, spaceBefore=14),
        "cat_head":  ps("cat_head",  fontName="Helvetica-Bold",    fontSize=28, textColor=COAL, spaceAfter=2),
        "gen_head":  ps("gen_head",  fontName="Helvetica-Bold",    fontSize=18, textColor=ACCENT, spaceAfter=3, spaceBefore=4),
        "tone_head": ps("tone_head", fontName="Helvetica-Bold",    fontSize=13, textColor=COAL, spaceAfter=2, spaceBefore=8),
        "shot_head": ps("shot_head", fontName="Helvetica-Bold",    fontSize=10, textColor=ACCENT, spaceAfter=1, spaceBefore=5),
        "h2":        ps("h2",        fontName="Helvetica-Bold",    fontSize=18, textColor=COAL, spaceAfter=3, spaceBefore=10),
        "h3":        ps("h3",        fontName="Helvetica-Bold",    fontSize=12, textColor=ACCENT, spaceAfter=2, spaceBefore=6),
        "body":      ps("body",      fontName="Helvetica",         fontSize=8.5,textColor=COAL, spaceAfter=2, leading=13),
        "bold":      ps("bold",      fontName="Helvetica-Bold",    fontSize=8.5,textColor=COAL, spaceAfter=2, spaceBefore=3),
        "quote":     ps("quote",     fontName="Helvetica-Oblique", fontSize=10, textColor=ACCENT, spaceAfter=4, leftIndent=10),
        "bullet":    ps("bullet",    fontName="Helvetica",         fontSize=8.5,textColor=COAL, spaceAfter=1, leftIndent=12, leading=13),
        "caption":   ps("caption",   fontName="Helvetica",         fontSize=7,  textColor=LGREY),
        "anti_head": ps("anti_head", fontName="Helvetica-Bold",    fontSize=10, textColor=RED, spaceAfter=2, spaceBefore=6),
    }

    def inline(t: str) -> str:
        return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t or "")

    def md_to_story(text: str) -> list:
        elems = []
        for raw in text.split("\n"):
            line = raw.strip()
            if not line:
                elems.append(Spacer(1, 1.5*mm))
            elif line == "---":
                elems += [Spacer(1,1*mm), HRFlowable(width="100%", thickness=0.5, color=WOOL), Spacer(1,1*mm)]
            elif line.startswith("## "):
                elems.append(Paragraph(inline(line[3:]), S["h2"]))
            elif line.startswith("### "):
                elems.append(Paragraph(inline(line[4:]), S["h3"]))
            elif line.startswith("#### "):
                elems.append(Paragraph(inline(line[5:]), S["shot_head"]))
            elif line.startswith("> "):
                elems.append(Paragraph(line[2:].replace("*",""), S["quote"]))
            elif line.startswith("- "):
                elems.append(Paragraph("•  " + inline(line[2:]), S["bullet"]))
            elif line.startswith("**") and line.endswith("**") and line.count("**") == 2:
                elems.append(Paragraph(line[2:-2], S["bold"]))
            else:
                elems.append(Paragraph(inline(line), S["body"]))
        return elems

    def thumb(img_path: Path, w_pt: float, h_pt: float):
        try:
            bio = BytesIO()
            pil = PILImage.open(img_path).convert("RGB")
            pil.thumbnail((int(w_pt * 2.835), int(h_pt * 2.835)), PILImage.LANCZOS)
            pil.save(bio, format="JPEG", quality=72)
            bio.seek(0)
            return Image(bio, width=w_pt, height=h_pt)
        except Exception:
            return None

    def image_row(paths: list, usable_w: float, max_imgs: int = 4) -> list:
        """Return a Table row of up to max_imgs images."""
        paths = [p for p in paths if p][:max_imgs]
        if not paths:
            return []
        n = len(paths)
        cell_w = (usable_w - (n - 1) * 3*mm) / n
        cell_h = cell_w * 0.62
        cells = []
        for p in paths:
            img = thumb(Path(p), cell_w, cell_h)
            cells.append(img if img else Paragraph("[img]", S["caption"]))
        tbl = Table([cells], colWidths=[cell_w + 1*mm] * n)
        tbl.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 1),
            ("RIGHTPADDING", (0,0), (-1,-1), 1),
        ]))
        return [tbl, Spacer(1, 3*mm)]

    W, H = A4
    MARGIN = 16 * mm
    USABLE_W = W - 2 * MARGIN

    story = []

    # ── Cover ──
    story += [
        Spacer(1, 28*mm),
        Paragraph("NEEMANS", S["cover_h1"]),
        Spacer(1, 3*mm),
        Paragraph("Visual Style Bible", S["cover_sub"]),
        Spacer(1, 6*mm),
        HRFlowable(width="80%", thickness=1.5, color=WOOL),
        Spacer(1, 6*mm),
        Paragraph(f"Photography &amp; Styling Reference for Creative &amp; Production Teams", S["cover_tag"]),
        Spacer(1, 2*mm),
        Paragraph(f"Generated {date.today().strftime('%B %d, %Y')}", S["cover_tag"]),
        Spacer(1, 14*mm),
        Paragraph("<b>The shoe is always the hero.</b><br/>Clothing is context, never competition.",
                  S["cover_rule"]),
        Spacer(1, 14*mm),
        HRFlowable(width="50%", thickness=0.5, color=WOOL),
        Spacer(1, 5*mm),
        Paragraph("Casual  ·  Athleisure  ·  Formal  ·  Open Footwear",
                  ps("cats2", fontName="Helvetica", fontSize=9, textColor=LGREY, alignment=TA_CENTER)),
        PageBreak(),
    ]

    handle_map = {p["handle"]: p for p in all_products}

    # ── Category + Gender sections ──
    for cat in CAT_ORDER:
        first_in_cat = True
        for gender in ["Men's", "Women's", "Unisex"]:
            section_text = cat_gender_sections.get((cat, gender))
            if not section_text:
                continue

            tone_data = groups.get(cat, {}).get(gender, {})

            # Category banner (first gender block in this cat)
            if first_in_cat:
                story += [
                    Paragraph(cat.upper(), S["cat_head"]),
                    HRFlowable(width="100%", thickness=2, color=WOOL, spaceAfter=3),
                    Spacer(1, 2*mm),
                ]
                first_in_cat = False

            story.append(Paragraph(f"{cat} — {gender}", S["gen_head"]))

            # Per color tone: images + insights side-by-side
            for tone, shot_type_dict in tone_data.items():
                story.append(Paragraph(tone, S["tone_head"]))
                story += [HRFlowable(width="100%", thickness=0.5, color=WOOL), Spacer(1, 2*mm)]

                # Collect up to 4 images for this tone
                img_paths = []
                for st in ["outdoor_model", "outdoor_leg", "indoor_model", "indoor_leg"]:
                    for a in shot_type_dict.get(st, []):
                        if len(img_paths) >= 4:
                            break
                        p = handle_map.get(a["handle"])
                        if p:
                            imgs = find_images(p)
                            if imgs:
                                img_paths.append(imgs[0])
                    if len(img_paths) >= 4:
                        break

                if img_paths:
                    story += image_row(img_paths, USABLE_W)

            # Full markdown section
            story += md_to_story(section_text)
            story.append(PageBreak())

    # ── Mood section ──
    if mood_section:
        # Collect editorial images for mood board
        outdoor_analyses = [a for a in all_analyses
                            if a.get("shot_type") == "outdoor_model"
                            and a.get("model_present")][:8]
        mood_img_paths = []
        seen = set()
        for a in outdoor_analyses:
            if len(mood_img_paths) >= 8:
                break
            h = a["handle"]
            if h in seen:
                continue
            p = handle_map.get(h)
            if p:
                imgs = find_images(p)
                if imgs:
                    mood_img_paths.append(imgs[0])
                    seen.add(h)

        story += [
            Paragraph("MOOD & EDITORIAL DIRECTION", S["cat_head"]),
            HRFlowable(width="100%", thickness=2, color=WOOL, spaceAfter=3),
            Spacer(1, 3*mm),
        ]

        # 2-row mood board
        if mood_img_paths:
            story += image_row(mood_img_paths[:4], USABLE_W)
            if len(mood_img_paths) > 4:
                story += image_row(mood_img_paths[4:8], USABLE_W)

        story += md_to_story(mood_section)

    doc = SimpleDocTemplate(
        str(PDF_OUTPUT), pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )
    doc.build(story)
    print(f"  PDF → {PDF_OUTPUT}")

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("\nERROR: Set your API key first:")
        print('  PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."')
        print('  CMD:        set ANTHROPIC_API_KEY=sk-ant-...')
        sys.exit(1)

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    print("\n" + "═"*54)
    print("  Neemans Visual Style Bible Generator")
    print("═"*54)

    # 1. Catalog
    print("\n[1/6] Product catalog…")
    products = build_or_load_catalog()

    # Group by category+gender — include everything with local images
    valid = [p for p in products if Path(p["local_image_dir"]).exists()]
    by_cat_gender = defaultdict(list)
    for p in valid:
        by_cat_gender[(p["style_category"], p["gender"])].append(p)

    print(f"\n  Valid products with local images: {len(valid)}")
    print("  Category × Gender breakdown:")
    for (cat, gender), prods in sorted(by_cat_gender.items()):
        print(f"    {cat:20s} {gender:10s}: {len(prods)}")

    # 2. Vision analysis
    print("\n[2/6] Vision analysis (model images only)…")
    cache = load_cache()
    real_cached = sum(1 for k in cache if k != "__version__")
    print(f"  {real_cached} products already cached.\n")

    all_analyses = []

    for cat in CAT_ORDER:
        for gender in ["Men's", "Women's", "Unisex"]:
            prods = by_cat_gender.get((cat, gender), [])
            if not prods:
                continue
            sample = prods[:MAX_PRODUCTS_PER_CATEGORY]
            print(f"\n  ── {cat} {gender} ({len(sample)} of {len(prods)}) ──")
            for i, product in enumerate(sample):
                label = product["product_name"][:48]
                print(f"  [{i+1:02d}/{len(sample):02d}] {label}…", end=" ", flush=True)
                r = analyze_product(client, product, cache)
                if r:
                    if r.get("is_non_footwear"):
                        print("— non-footwear, skipped")
                    elif not r.get("model_present"):
                        print("— no model")
                    else:
                        all_analyses.append(r)
                        print(f"✓ ({r.get('shot_type','?')})")
                else:
                    print("— no images")
                time.sleep(API_DELAY)

    model_images = len(all_analyses)
    print(f"\n  Model images captured: {model_images}")

    # 3. Group
    print("\n[3/6] Grouping by Category × Gender × Color Tone × Shot Type…")
    groups = group_analyses(all_analyses)
    for cat in CAT_ORDER:
        for gender, tone_data in groups.get(cat, {}).items():
            tones = list(tone_data.keys())
            print(f"  {cat} {gender}: {tones}")

    # 4. Synthesis per category+gender
    print("\n[4/6] Synthesising style rules…")
    cat_gender_sections = {}

    for cat in CAT_ORDER:
        for gender in ["Men's", "Women's", "Unisex"]:
            tone_groups = groups.get(cat, {}).get(gender, {})
            if not tone_groups:
                continue
            total_p = sum(len(a) for td in tone_groups.values() for a in td.values())
            print(f"  {cat} {gender} ({total_p} model images)…", end=" ", flush=True)
            try:
                cat_gender_sections[(cat, gender)] = synthesize_group(
                    client, cat, gender, tone_groups, products)
                print("✓")
            except Exception as e:
                print(f"ERROR: {e}")
            time.sleep(API_DELAY)

    # 5. Mood synthesis
    print("\n[5/6] Mood & editorial synthesis…", end=" ", flush=True)
    mood_section = ""
    try:
        mood_section = synthesize_mood(client, all_analyses, products)
        print("✓")
    except Exception as e:
        print(f"ERROR: {e}")

    # 6. Outputs
    print("\n[6/6] Writing outputs…")
    md = generate_markdown(cat_gender_sections, mood_section, model_images)
    MD_OUTPUT.write_text(md, encoding="utf-8")
    print(f"  Markdown → {MD_OUTPUT}")

    try:
        generate_pdf(cat_gender_sections, groups, mood_section, all_analyses, products)
    except Exception as e:
        print(f"  PDF failed: {e}")
        import traceback; traceback.print_exc()

    print("\n✅  Done!")
    print(f"   Markdown → {MD_OUTPUT}")
    print(f"   PDF      → {PDF_OUTPUT}")

if __name__ == "__main__":
    main()
