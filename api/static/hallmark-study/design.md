<!-- Hallmark · study-emit · studied: yes · DNA-source: url (live page unreachable — extracted from Hum theme spec + Narrative Workflow macrostructure spec)
     source-url: https://www.usehallmark.com/examples/hum-07/
     observed-fonts: Plus Jakarta Sans + JetBrains Mono (from Hum theme spec — hum-07 is the Hum theme applied to a guided-sourdough brief)
     observed-accent: multi — oklch(86% 0.18 95) pear + oklch(66% 0.18 235) cyan + oklch(68% 0.24 18) coral (from Hum theme spec)
     rhythm: unknown (URL-mode blind spot compounded by live-page fetch failure)
-->

# Design — Hallmark hum-07 study (structural reference only)

Locked design DNA extracted from the Hallmark **hum-07** example
(https://www.usehallmark.com/examples/hum-07/) — a "Bubble guided sourdough
app hero" built on Hallmark's **Hum** theme. Future Hallmark runs read this
file first; pages defer to it. Amend intentionally — the file is the rule.

> **CRITICAL — structural reference only.** This file captures hum-07's
> **macrostructure, type-pairing, and color anchor** as a structural skeleton.
> The warm multi-accent palette below (cream + pear + cyan + coral) is
> **hum-07's dress, NOT the project's destination palette.** Task 9 overrides
> the `## Tokens` color block with a sci-tech palette while preserving the
> macrostructure, type-pairing roles, archetype composition, and motion stance
> recorded here. Treat the color values in `## Tokens` as "what hum-07 uses,"
> not "what the project ships."

## System
- Genre · playful (Hum is the catalog's only playful theme)
- Macrostructure · Narrative Workflow (F4 Step Sequence — numbered stages `1.0 → 2.0 → 3.0 → 4.0`)
- Theme · catalog: Hum · studied-DNA from hum-07 example
- Axes · light (paper L 97%) / rounded-sans (Plus Jakarta Sans) / multi-accent (pear + cyan + coral)

### Macrostructure fingerprint (the landing-page layout)
hum-07 is a guided-sourdough app hero. The Hum theme spec
(`references/themes/hum.md`) explicitly names guided-sourdough as the canonical
Narrative Workflow use case, and the Narrative Workflow macrostructure spec's
sample opening line is verbatim sourdough process language:
*"01 · sourdough overnight · 02 · score at dawn · 03 · pull at seven."*

The macrostructure is a **process timeline** — the page IS the numbered
sequence of baking stages, not a marketing stack:

- **Hero (H1 Marquee, off-centre per Hum anti-slop rule)** — a single bold
  statement fills the fold. No centred eyebrow/badge/H1/subhead/CTA stack
  (that is the deadliest Hum tell). Headline left-biased or split-screen;
  one global "Start at stage 1 →" CTA deferred to the foot, not the fold.
  A small CSS-built character mark (a pear-yellow dot that pulses at rest,
  bursts a 4-point star on the primary CTA click) anchors one side — Hum's
  mandatory character moment.
- **Stage sequence (F4 Step Sequence body)** — each stage is a phase:
  `1.0 · overnight` → `2.0 · dawn` → `3.0 · pull`. Large numbered stage
  labels in JetBrains Mono uppercase (`01 · TODAY`, `02 · YOUR STREAK` voice).
  Each stage has a short declarative explanation and a small annotated
  product capture. Thick numbered rule divides stages. Reveal: horizontal
  sweep as stages enter viewport.
- **Feature block** — one non-default shape: a numbered narrative or
  big+small split, never the banned 3-identical-accent-cards row. Each
  stage's tile owns a different accent tint (color-shift card grid).
- **Auth entry** — sign-in folded into the nav (N1b sign-in + filled CTA
  right) and/or a C2 Inline-form-as-CTA at the foot ("Start your starter →"
  email field). No separate landing for sign-up.
- **Footer (Ft5 Statement)** — one large display sentence dominates
  (a closing line, not a sitemap). Wordmark + minimal links + copyright
  beneath in muted small type. Avoids Ft3 (the 4-column index AI fingerprint).

### Archetype picks (component cookbook)
- Hero · **H1 Marquee** (knobs: size=xl, alignment=left-bias, underlay=single-rule-above)
- Section head · **S1 Left-margin numbered** (the numbered rail IS the structure)
- Feature · **F4 Step sequence** (knobs: numbering=01/02/03, layout=vertical-stack, connector=thick-rule)
- CTA · **C2 Inline form as CTA** (knobs: fields=1, submit=end-of-row, helper=below) + one **C1 Outlined chip** per stage
- Proof · **T4 Numbered stat strip** (knobs: 3-up, display weight, qualifier=under) — streak counters tick up
- Nav · **N1b SaaS three-section** (playful genre default; knobs: centre-links=4, dropdowns=none, scroll=frost-on-scroll)
- Footer · **Ft5 Statement** (knobs: sentence=38ch, wordmark=under, rule-above=hairline)

## Provenance
- Source mode · url (intended) — live page at https://www.usehallmark.com/examples/hum-07/
- Live-page fetch · **failed** — WebFetch to usehallmark.com was blocked by the
  execution environment's network policy ("Unable to verify if domain is safe
  to fetch"). Raw.githubusercontent.com and api.github.com were also blocked,
  so the hum-07 source HTML could not be pulled from the Hallmark GitHub repo.
- Fallback source · the Hallmark skill's authoritative theme + macrostructure
  references installed at `~/.claude/skills/hallmark/`:
  - `references/themes/hum.md` — the Hum theme spec. hum-07 is the Hum theme
    applied to a guided-sourdough brief, so this file carries hum-07's exact
    palette (OKLCH values), exact type-pairing (Plus Jakarta Sans + JetBrains
    Mono), motion stack, signature moves, and macrostructure affinity. It
    explicitly states Narrative Workflow "Works well for a guided-sourdough-style
    build."
  - `references/macrostructures/14-narrative-workflow.md` — the Narrative
    Workflow macrostructure spec, whose sample opening line is verbatim
    sourdough process language.
  - `references/component-cookbook.md` — archetype + variation-knob catalogue.
- Extraction date · 2026-07-22
- Attestation · (b) public reference for the user's own brand — hum-07 is a
  public Hallmark example page, studied as a structural reference for the
  project's own welcome-page UI. The DNA is structural; the specific warm
  tokens are NOT carried forward (Task 9 regenerates a sci-tech palette).
- Confidence note · **Tokens are exact** (extracted from the Hum theme spec's
  canonical OKLCH values — hum-07 IS the Hum theme). **Fonts are exact**
  (the Hum theme spec declares Plus Jakarta Sans + JetBrains Mono via Google
  Fonts). **Rhythm is unknown** — URL-mode rhythm blind spot compounded by
  the live-page fetch failure; density/asymmetry judgements are inferred from
  the theme spec's "Not-AI discipline" levers, not observed from a screenshot.

## Tokens (hum-07's dress — STRUCTURAL REFERENCE ONLY; Task 9 overrides color)

> The values below are hum-07's actual Hum-theme tokens (per
> `references/themes/hum.md`). They are recorded here so the structural DNA is
> complete. **Task 9 replaces every `--color-*` value with a sci-tech palette
> while keeping `--font-*`, the 4-pt spacing scale, easings, durations, and
> radii.** Do not ship these warm colors in the project's welcome page.

```css
:root {
  /* === hum-07's warm multi-accent palette (REFERENCE ONLY — Task 9 swaps these) === */
  --color-paper:      oklch(97% 0.012 95);   /* cream, pear-yellow pull — NEVER pure white in Hum */
  --color-paper-2:    oklch(94% 0.016 95);   /* tinted band (yellower) */
  --color-paper-3:    oklch(91% 0.020 95);   /* deeper hover */
  --color-ink:        oklch(20% 0.012 250);  /* near-black with cool tilt — NEVER pure black */
  --color-ink-2:      oklch(40% 0.012 250);  /* secondary ink */
  --color-rule:       oklch(85% 0.010 95);   /* hairline rule on cream */
  --color-accent:     oklch(86% 0.18 95);    /* pear-yellow — primary CTA, streaks, character mark */
  --color-accent-2:   oklch(66% 0.18 235);   /* sky-cyan — links, hover tints, illustrations */
  --color-accent-3:   oklch(68% 0.24 18);    /* coral-red — single high-energy moment per page */
  --color-mint:       oklch(80% 0.16 150);   /* soft green — success states (sparingly) */
  --color-lavender:   oklch(74% 0.16 305);   /* tag chips (sparingly) */
  --color-focus:      oklch(60% 0.18 235);   /* focus ring */

  /* === Type-pairing (CARRIES FORWARD into Task 9 — roles preserved) === */
  --font-display: "Plus Jakarta Sans", Geist, system-ui, sans-serif;  /* rounded humanist sans, weights 400/500/600/700 */
  --font-body:    "Plus Jakarta Sans", Geist, system-ui, sans-serif;  /* same family — Hum uses one sans throughout */
  --font-mono:    "JetBrains Mono", ui-monospace, monospace;          /* uppercase labels, tabular nums, streak counters */

  /* 4-pt spacing scale, named: --space-3xs … --space-4xl. See tokens.css. */
  /* Type scale, 1.25 (major-third) ratio: --text-xs … --text-display. */

  /* === Motion (CARRIES FORWARD into Task 9) === */
  --ease-spring: cubic-bezier(0.34, 1.56, 0.64, 1);  /* bouncy overshoot — Hum's canonical easing */
  --ease-snap:   cubic-bezier(0.22, 1, 0.36, 1);      /* easeOutExpo — tick-ups, reveals */
  --ease-out:    cubic-bezier(0.16, 1, 0.3, 1);
  --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
  --dur-fast: 140ms;  --dur-base: 220ms;  --dur-slow: 320ms;  --dur-counter: 1200ms;

  /* === Radii (CARRIES FORWARD into Task 9 — Hum is the rounded theme) === */
  --radius-card: 20px;  --radius-pill: 999px;  --radius-input: 12px;
}
```

## CTA voice
- Primary · push button — `--color-accent` fill · `--radius-pill` · padding `0.8rem 1.4rem` · a full-width solid colour edge (`box-shadow: 0 4px 0 0 var(--btn-edge)`) + a separate soft ground shadow. Lift 2px on hover (edge grows to 6px), **press DOWN 3px on `:active`** (edge shrinks to 1px) — the press IS the feedback. Snappy `cubic-bezier(0.2,0.7,0.3,1)`, 140ms hover / 70ms active. No `scale()`, no spring overshoot on buttons.
- Secondary · soft (`.btn--soft`) — flat-lift, soft shadow, no colour edge.
- Tertiary · outline (`.btn--outline`) — hairline + accent fill sweeps up on hover.
- One push button per primary moment; do not stack three push buttons in a row.

## Motion stance
- Motion-on (Hum has the loudest motion stack in the catalog). Mandatory
  hover-and-on-paint motion on every interactive element.
- Reveal primitives · fade-up stagger (section headings, `translateY(12px→0)` + opacity, 600ms, 80ms stagger) · horizontal sweep (stages entering viewport) · counter tick-up (`@property --num`, 1200ms, `--ease-snap`, scale 1→1.06→1 on completion).
- Character mark · pulse at rest (4s gentle scale 1→1.04→1); star-burst (420ms, coral-red, fires once) on primary CTA click.
- Reduced-motion fallback · ≤150ms opacity crossfade. Spring hovers collapse to opacity/colour only; counters render at final value instantly; character stops pulsing; star-burst disabled.

## Notes — anti-patterns to NOT carry over (from the Hum theme spec)
These are hum-07's theme-level anti-patterns. They are part of the system's
identity and future Hallmark runs reading this file must respect them —
**even after Task 9 swaps the palette to sci-tech.**

- **NEVER serif anywhere.** Display, body, captions — all rounded sans. If a
  serif appears, the theme is misapplied. (Task 9 keeps Plus Jakarta Sans.)
- **NEVER pure white paper.** Hum's ground is cream. (Task 9's sci-tech
  palette should still avoid pure `#fff` — use a tinted near-white.)
- **NEVER pure black ink.** Minimum lightness L 20% with a slight tilt.
- **NEVER square corners** on cards / pills / buttons. Cards 20px, pills 999px, inputs 12px.
- **NEVER single-accent palette.** Hum is multi-accent — each accent owns its
  own surface. (Task 9's sci-tech palette should preserve the multi-surface
  discipline even if it reduces the hue count.)
- **NEVER gradients between accents.** Pear-to-cyan or cyan-to-coral banned.
  Each accent owns its own surface; they sit next to each other in defined regions.
- **NEVER `font-style: italic` for emphasis.** Carry emphasis with weight (500)
  or accent colour, not italics. (Universal Hallmark rule — italic headers are
  an AI tell.)
- **NEVER the centred hero stack** (eyebrow → centred H1 → two-line subhead →
  filled + ghost CTA, dead-centre). Default to off-centre or split-screen.
- **NEVER a badge-pill directly above the H1** ("✨ now with…").
- **NEVER the 3-identical-accent-cards row** (icon-tile → title → two grey
  lines, ×3). If there are three things to say, say them in a different shape.
- **NEVER emoji standing in for icons.** Draw marks in CSS/SVG.
- **NEVER invented metrics.** Streak counts must be honest. Marketing stats real.
- **NEVER an accent stripe on a card's top/left edge** — reads as AI.
- **NEVER `transition: all` or `transform: scale(1.05)` on `:hover`** (URL-mode
  motion anti-patterns — if hum-07's CSS contained them, drop them).
- **One character moment per page. One CTA wobble per page.** No more.

## Exports
`tokens.css` (to be produced in the project) is the source of truth. For
Tailwind v4 `@theme`, DTCG `tokens.json`, or shadcn/ui CSS variables, ask
*"extend design.md with Tailwind exports"* (or the format you want) — Hallmark
will append them per `references/export-formats.md`.
