"""Per-category CSS for visual reports (ADR-051, Phase 2.2).

_category_css builds the per-category palette + structural CSS for the visual
report, split verbatim out of src/visual_report.py (where it was a ~516-line
template function). Re-imported there so generate_visual_report keeps calling it.
"""

from typing import Optional


def _category_css(category: Optional[str]) -> str:
    if not category:
        return ""
    # Per-category palette overrides — applied BEFORE the structural rules so
    # everything that reads --accent / --aurora-* automatically retints. The
    # default (no category) keeps the warm terracotta defined in :root.
    palettes = """
/* ── Category palettes ───────────────────────────────────
   Override the accent + aurora vars per category so each report
   type has a distinct visual identity. */
body.category-product {
  --accent: #2a8a8c;
  --accent-light: #4ab0b2;
  --accent-bg: rgba(42,138,140,0.07);
  --aurora-a: rgba(42,138,140,0.11);
  --aurora-b: rgba(201,149,46,0.06);
  --aurora-c: rgba(64,98,128,0.06);
}
body.category-comparison {
  --accent: #7a4cb8;
  --accent-light: #9d76d0;
  --accent-bg: rgba(122,76,184,0.07);
  --aurora-a: rgba(122,76,184,0.11);
  --aurora-b: rgba(184,84,58,0.05);
  --aurora-c: rgba(64,98,128,0.07);
}
body.category-howto {
  --accent: #3d8a3d;
  --accent-light: #62b162;
  --accent-bg: rgba(61,138,61,0.07);
  --aurora-a: rgba(61,138,61,0.11);
  --aurora-b: rgba(201,149,46,0.07);
  --aurora-c: rgba(42,138,140,0.05);
}
body.category-landscape {
  --accent: #b88a2e;
  --accent-light: #d4a955;
  --accent-bg: rgba(184,138,46,0.08);
  --aurora-a: rgba(184,138,46,0.13);
  --aurora-b: rgba(184,84,58,0.06);
  --aurora-c: rgba(122,76,184,0.05);
}
@media (prefers-color-scheme: dark) {
  body.category-product {
    --accent: #5cc8cb; --accent-light: #8fdde0;
    --accent-bg: rgba(92,200,203,0.10);
    --aurora-a: rgba(92,200,203,0.13);
    --aurora-b: rgba(232,192,90,0.07);
    --aurora-c: rgba(125,180,224,0.08);
  }
  body.category-comparison {
    --accent: #b896e8; --accent-light: #d0b8f0;
    --accent-bg: rgba(184,150,232,0.10);
    --aurora-a: rgba(184,150,232,0.13);
    --aurora-b: rgba(232,143,115,0.06);
    --aurora-c: rgba(125,180,224,0.08);
  }
  body.category-howto {
    --accent: #82c882; --accent-light: #a8dba8;
    --accent-bg: rgba(130,200,130,0.09);
    --aurora-a: rgba(130,200,130,0.12);
    --aurora-b: rgba(232,192,90,0.07);
    --aurora-c: rgba(92,200,203,0.07);
  }
  body.category-landscape {
    --accent: #e6c069; --accent-light: #f0d390;
    --accent-bg: rgba(230,192,105,0.10);
    --aurora-a: rgba(230,192,105,0.15);
    --aurora-b: rgba(232,143,115,0.07);
    --aurora-c: rgba(184,150,232,0.06);
  }
}

/* ── Per-category font pairings ───────────────────────
   Body font shifts between serif (long-form categories) and sans
   (practical/data categories) so each report reads as a different
   publication, not just a re-tinted version of the same template. */

/* Long-form: literary serif for both display and body */
body:not([class*="category-"]),
body.category-landscape {
  --font-body: 'Source Serif 4', 'Iowan Old Style', Georgia, serif;
}

/* Comparison: analytical serif display + clean sans body */
body.category-comparison {
  --font-display: 'Playfair Display', Georgia, serif;
  --font-body: 'Inter', system-ui, sans-serif;
}

/* How-to: friendly geometric sans, top to bottom */
body.category-howto {
  --font-display: 'Manrope', system-ui, sans-serif;
  --font-body: 'Inter', system-ui, sans-serif;
}

/* Product: techy/engineery — IBM Plex Sans display + Inter body */
body.category-product {
  --font-display: 'IBM Plex Sans', system-ui, sans-serif;
  --font-body: 'Inter', system-ui, sans-serif;
}

/* Source Serif sits visually larger than Inter at the same px — pull it
   back one notch for the categories that use it as body so line length
   and rhythm stay comparable across categories. */
body:not([class*="category-"]) body, /* no-op selector, kept for clarity */
body.category-landscape { font-size: 16.5px; }

/* Drop cap looks bad on geometric sans — kill it for those categories */
body.category-product   .content > p:first-of-type::first-letter,
body.category-howto     .content > p:first-of-type::first-letter,
body.category-comparison .content > p:first-of-type::first-letter,
body.category-product   .content > h2:first-child + p::first-letter,
body.category-howto     .content > h2:first-child + p::first-letter,
body.category-comparison .content > h2:first-child + p::first-letter {
  font-size: 1em; float: none; margin: 0; color: inherit;
  font-family: inherit; font-weight: inherit;
}

/* ── Per-category background effects ───────────────
   Each category overrides body::before so the page reads as a
   distinctly-textured surface. Aurora stays the default. */

/* Product → blueprint grid that slowly pans */
body.category-product::before {
  background:
    linear-gradient(to right, var(--aurora-a) 1px, transparent 1px),
    linear-gradient(to bottom, var(--aurora-a) 1px, transparent 1px),
    radial-gradient(70vw 60vh at 50% 50%, var(--aurora-a) 0%, transparent 75%);
  background-size: 56px 56px, 56px 56px, 100% 100%;
  filter: none;
  animation: cat-grid-pan 60s linear infinite;
}
@keyframes cat-grid-pan {
  to { background-position: 56px 56px, 56px 56px, 0 0; }
}

/* Comparison → dot grid + slow opacity pulse */
body.category-comparison::before {
  background:
    radial-gradient(circle, var(--aurora-a) 1.4px, transparent 1.8px),
    radial-gradient(60vw 55vh at 25% 25%, var(--aurora-b) 0%, transparent 65%),
    radial-gradient(60vw 55vh at 75% 75%, var(--aurora-c) 0%, transparent 65%);
  background-size: 26px 26px, 100% 100%, 100% 100%;
  filter: none;
  animation: cat-dot-pulse 14s ease-in-out infinite alternate;
}
@keyframes cat-dot-pulse {
  from { opacity: 0.65; }
  to   { opacity: 1; }
}

/* How-to → flat surface with a very subtle vignette. Drop the flow-lines
   pattern — it competes visually with the step number rails on the
   right-hand side of each H2. The reading should feel like an O'Reilly
   procedure: clean, scannable, no decoration in the way. */
body.category-howto::before {
  background:
    radial-gradient(70vw 70vh at 50% 0%, var(--aurora-a) 0%, transparent 60%),
    radial-gradient(50vw 50vh at 50% 100%, var(--aurora-b) 0%, transparent 65%);
  filter: blur(40px);
  animation: none;
}

/* Landscape → horizontal horizon bands that slowly shift sideways */
body.category-landscape::before {
  background:
    linear-gradient(
      180deg,
      transparent 0%,
      var(--aurora-a) 22%,
      transparent 35%,
      var(--aurora-b) 55%,
      transparent 68%,
      var(--aurora-c) 85%,
      transparent 100%
    );
  background-size: 100% 200%;
  filter: blur(40px);
  animation: cat-horizon-drift 36s ease-in-out infinite alternate;
}
@keyframes cat-horizon-drift {
  0%   { background-position: 0 0; }
  100% { background-position: 0 100%; }
}

@media (prefers-reduced-motion: reduce) {
  body.category-product::before,
  body.category-comparison::before,
  body.category-howto::before,
  body.category-landscape::before {
    animation: none;
  }
}

/* ─────────────────────────────────────────────────────
   PER-CATEGORY STRUCTURAL TREATMENTS
   Each category gets distinctive structural CSS so the page
   reads as a different publication — not just retinted.
   ───────────────────────────────────────────────────── */

/* ── HOWTO: O'Reilly-style numbered procedure ─────── */
body.category-howto .content { counter-reset: howto-step; }
body.category-howto .content h2 {
  counter-increment: howto-step;
  display: flex; align-items: center; gap: 14px;
  border-bottom: none;
  padding-left: 0;
  margin-top: 3.5rem;
}
body.category-howto .content h2::before {
  content: counter(howto-step);
  display: inline-flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  width: 40px; height: 40px;
  border-radius: 12px;
  background: var(--accent);
  color: #fff;
  font-family: var(--font-display);
  font-size: 1.15rem;
  font-weight: 700;
  letter-spacing: 0;
  box-shadow: 0 4px 12px color-mix(in srgb, var(--accent) 30%, transparent);
}
/* Step body gets a colored left rail so you can scan "this is step 1's stuff" */
body.category-howto .content h2 ~ p,
body.category-howto .content h2 ~ ul,
body.category-howto .content h2 ~ ol,
body.category-howto .content h2 ~ pre,
body.category-howto .content h2 ~ blockquote {
  border-left: 2px solid color-mix(in srgb, var(--accent) 25%, transparent);
  padding-left: 1rem;
  margin-left: 4px;
}
body.category-howto .content h2:has(+ *) ~ h2 ~ * { border-left: none; padding-left: 0; margin-left: 0; }
/* Terminal-style code blocks — green $ prompt, monospaced, dark surface */
body.category-howto .content pre {
  background: #1a1a1e;
  color: #d4e4d4;
  border: 1px solid color-mix(in srgb, var(--accent) 20%, transparent);
  border-radius: 8px;
  position: relative;
  padding-left: 2.6rem;
}
body.category-howto .content pre::before {
  content: '$';
  position: absolute;
  left: 1.1rem; top: 1.15rem;
  color: var(--accent);
  font-family: var(--font-mono);
  font-weight: 700;
  font-size: 0.86rem;
  opacity: 0.85;
}
body.category-howto .content pre code { color: inherit; }

/* ── LANDSCAPE: editorial briefing with H3 player cards ─ */
body.category-landscape .content h3 {
  /* Each H3 in landscape = a "player" in the field — give it a card frame */
  margin-top: 2.5rem;
  padding: 14px 18px 4px;
  border-left: 3px solid var(--accent);
  background: color-mix(in srgb, var(--accent) 4%, transparent);
  border-radius: 0 8px 8px 0;
  font-family: var(--font-display);
  font-size: 1.18rem;
}
body.category-landscape .content h3 + p {
  margin-top: 0;
  padding: 0 18px 14px;
  background: color-mix(in srgb, var(--accent) 4%, transparent);
  border-left: 3px solid var(--accent);
  margin-left: 0;
  border-radius: 0 0 8px 0;
}
/* Pull-quote treatment for any standalone blockquote */
body.category-landscape .content blockquote {
  font-size: 1.2rem;
  line-height: 1.5;
  max-width: 90%;
  margin: 2rem auto;
  text-align: center;
  border-left: none;
  border-top: 1px solid color-mix(in srgb, var(--accent) 40%, transparent);
  border-bottom: 1px solid color-mix(in srgb, var(--accent) 40%, transparent);
  background: transparent;
  border-radius: 0;
  padding: 1.5rem 1rem;
  font-style: italic;
}
body.category-landscape .content blockquote::before {
  display: none;
}

/* ── COMPARISON: lab-report tables with winner badges ─ */
body.category-comparison .content {
  font-feature-settings: 'tnum' on, 'ss01';  /* tabular numerals for tables */
}
body.category-comparison .content table {
  font-size: 0.92rem;
  box-shadow: 0 6px 20px rgba(0,0,0,0.06);
}
body.category-comparison .content th {
  background: color-mix(in srgb, var(--accent) 18%, var(--bg-surface));
  color: var(--text);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  font-size: 0.72rem;
  font-weight: 700;
}
body.category-comparison .content td:first-child {
  font-weight: 600;
  background: color-mix(in srgb, var(--accent) 6%, transparent);
}
/* The first H3 inside a comparison report often names the recommended pick */
body.category-comparison .content h3:first-of-type::after {
  content: 'Pick';
  display: inline-block;
  margin-left: 10px;
  padding: 2px 10px;
  background: var(--accent);
  color: #fff;
  font-family: var(--font-body);
  font-size: 0.65rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  border-radius: 999px;
  vertical-align: middle;
}

/* ── PRODUCT: spec-sheet cards for each H3 ─────────── */
body.category-product .content h3 {
  /* Each product gets a spec-card frame — bordered, slight bg lift */
  margin-top: 2.4rem;
  padding: 16px 18px;
  border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border));
  background: var(--bg-surface);
  border-radius: 10px;
  display: flex; align-items: baseline; gap: 10px;
  font-family: var(--font-display);
  letter-spacing: -0.01em;
  box-shadow: 0 2px 8px rgba(0,0,0,0.04);
}
body.category-product .content h3::after {
  /* small "spec" tag on each product heading */
  content: 'SPEC';
  margin-left: auto;
  font-family: var(--font-body);
  font-size: 0.6rem;
  font-weight: 700;
  letter-spacing: 0.18em;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  padding: 3px 8px;
  border-radius: 4px;
}
body.category-product .content h3 + p,
body.category-product .content h3 + ul,
body.category-product .content h3 + table {
  margin-top: 0.8rem;
  padding-left: 4px;
}
"""
    styles = {
        "product": """
/* Product category */
.category-product .content h3 {
  display:flex; align-items:baseline; gap:8px;
  border-bottom:1px solid var(--border); padding-bottom:6px;
}
.category-product .content table {
  width:100%; border-collapse:collapse; margin:1.2em 0; font-size:0.92em;
}
.category-product .content table th {
  background:var(--accent); color:#fff; padding:8px 12px; text-align:left;
}
.category-product .content table td { padding:8px 12px; border-bottom:1px solid var(--border); }
.category-product .content table tr:nth-child(even) td { background:var(--bg-surface); }
.category-product .content ul { columns:2; column-gap:2em; }
@media (max-width:600px) { .category-product .content ul { columns:1; } }
.category-product .content a[href*="amazon"],
.category-product .content a[href*="ebay"],
.category-product .content a[href*="shop"],
.category-product .content a[href*="buy"] {
  display:inline-block; padding:3px 10px; border-radius:4px;
  background:var(--accent); color:#fff; text-decoration:none; font-size:0.85em; margin:2px 4px;
}
.quick-links-bar {
  display:flex; flex-wrap:wrap; gap:6px; padding:12px 0; margin-bottom:12px;
  border-bottom:1px solid var(--border);
}
.quick-link {
  padding:5px 12px; border-radius:16px; font-size:0.82em; text-decoration:none;
  border:1px solid var(--border); color:var(--text); transition:all 0.15s;
  white-space:nowrap;
}
.quick-link:hover {
  background:var(--accent); color:#fff; border-color:var(--accent);
}
""",
        "comparison": """
/* Comparison category */
.category-comparison .content table {
  width:100%; border-collapse:collapse; margin:1.2em 0;
}
.category-comparison .content table th {
  background:var(--accent); color:#fff; padding:10px 14px;
  text-align:center; font-weight:600; position:sticky; top:0;
}
.category-comparison .content table td {
  padding:10px 14px; border-bottom:1px solid var(--border); text-align:center;
}
.category-comparison .content table tr:nth-child(even) td { background:var(--bg-surface); }
.category-comparison .content table td:first-child {
  text-align:left; font-weight:500; background:color-mix(in srgb, var(--accent) 8%, transparent);
}
.category-comparison .content table td.cmp-pos {
  color:#2e7d32; font-weight:600;
  background:color-mix(in srgb, #4caf50 10%, transparent);
}
.category-comparison .content table td.cmp-neg {
  color:#c62828; font-weight:600;
  background:color-mix(in srgb, #f44336 8%, transparent);
}
.category-comparison .content table td.cmp-mid {
  color:#e68a00;
  background:color-mix(in srgb, #ffa726 8%, transparent);
}
.category-comparison .content h2 ~ p strong:first-child {
  display:inline-block; padding:2px 8px; border-radius:3px;
  background:color-mix(in srgb, var(--accent) 15%, transparent); font-size:0.9em;
}
""",
        "howto": """
/* How-to category */
.category-howto .content h2 {
  counter-increment:step-counter;
}
.category-howto .content h2::before {
  content:counter(step-counter);
  display:inline-flex; align-items:center; justify-content:center;
  width:28px; height:28px; border-radius:50%;
  background:var(--accent); color:#fff; font-size:0.8em; font-weight:700;
  margin-right:10px; flex-shrink:0;
}
.category-howto .content { counter-reset:step-counter; }
.category-howto .content blockquote {
  border-left:3px solid var(--accent); background:color-mix(in srgb, var(--accent) 8%, transparent);
  padding:12px 16px; border-radius:0 6px 6px 0; margin:1em 0;
}
.category-howto .content blockquote strong:first-child {
  display:inline-block; margin-bottom:4px; text-transform:uppercase;
  font-size:0.82em; letter-spacing:0.5px;
}
.category-howto .content h2#quick-guide + ol,
.category-howto .content h2#quick-guide ~ ol:first-of-type {
  background:color-mix(in srgb, var(--accent) 8%, transparent);
  border:1px solid color-mix(in srgb, var(--accent) 20%, transparent);
  border-radius:8px; padding:14px 14px 14px 32px; font-size:0.95em; line-height:1.8;
}
.category-howto .content h2#quick-guide {
  counter-increment:none;
}
.category-howto .content h2#quick-guide::before {
  content:'\\26A1'; background:none; width:auto; height:auto; margin-right:6px;
}
""",
        "landscape": """
/* Landscape category */
.category-landscape .content h3 {
  display:flex; align-items:center; gap:8px;
  padding:8px 0; border-bottom:1px solid var(--border);
}
.category-landscape .content table {
  width:100%; border-collapse:collapse; margin:1em 0; font-size:0.92em;
}
.category-landscape .content table th {
  background:var(--accent); color:#fff; padding:8px 12px; text-align:left;
}
.category-landscape .content table td { padding:8px 12px; border-bottom:1px solid var(--border); }
.category-landscape .content table tr:nth-child(even) td { background:var(--bg-surface); }
.category-landscape .content blockquote {
  border-left:3px solid var(--gold, #d4a73a);
  background:color-mix(in srgb, var(--gold, #d4a73a) 8%, transparent);
  padding:10px 14px; border-radius:0 6px 6px 0;
}
""",
        "factcheck": """
/* Fact-check category */
.category-factcheck .hero {
  background:linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
}
.category-factcheck .content h2:first-of-type {
  font-size:1.4em; text-align:center; padding:16px 0; border:none;
  background:color-mix(in srgb, var(--accent) 8%, transparent);
  border-radius:8px; margin:1em 0;
}
.category-factcheck .content blockquote {
  position:relative; padding-left:20px;
}
.category-factcheck .content h2 ~ h3 {
  padding:6px 10px; border-radius:4px;
  border-left:3px solid var(--accent);
}
.category-factcheck .content strong:only-child {
  display:inline-block; padding:4px 12px; border-radius:4px;
  font-size:1.1em;
}
""",
    }
    # Always emit the per-category palette block when ANY category is set —
    # it contains body.category-X scoped rules so it only re-skins the page
    # for the matching category. The legacy `styles[category]` block adds
    # structural CSS specific to that one type.
    return palettes + styles.get(category, "")
