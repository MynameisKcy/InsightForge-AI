# Hallmark Design Skill — Installation Record

## Summary

The Hallmark design skill (from `Nutlope/hallmark` on GitHub) has been installed to the user-level Claude skills directory.

## Install Method

**Manual GitHub-raw fallback** (npx unavailable on this machine).

The primary install method `npx skills add nutlope/hallmark` does not work here because the `skills` npm package requires Node >= 22.20.0, but this machine has Node v18.20.0 (npx fails with `EBADENGINE`). Per Hallmark's own README, the documented fallback is to copy `SKILL.md` + `references/` into `~/.claude/skills/hallmark/`. GitHub raw (`raw.githubusercontent.com`) is reachable, so the tree was downloaded recursively using the GitHub Contents API to enumerate directories and `raw.githubusercontent.com` to fetch each file.

## Installed Location

`C:/Users/86131/.claude/skills/hallmark/`

(Outside the repository — not committed; this note is the only repo-side record.)

## File Count

**106 files** total across **7 directories** (root + `references/` + 5 nested subdirs):

- `SKILL.md` (67444 bytes) — top-level skill definition
- `references/` — 24 top-level `.md` files (anti-patterns, assets, color, component-cookbook, contract, copy, custom-craft, custom-theme, design-md, export-formats, floating-nav, hero-enrichment, imagery-kit, interaction-and-states, layout-and-space, macrostructures, microinteractions, motion, preview-examples, responsive, slop-test, structure, **study**, typography)
- `references/components/` — 50 component spec files
- `references/genres/` — 4 genre files (atmospheric, editorial, modern-minimal, playful)
- `references/macrostructures/` — 21 macrostructure files (01 through 21)
- `references/themes/` — 4 theme files (carnival, cobalt, hum, lumen)
- `references/verbs/` — 2 verb files (audit, redesign)

## Verification

- `ls ~/.claude/skills/hallmark/` shows `SKILL.md` and `references/`
- `ls ~/.claude/skills/hallmark/references/` shows all 24 `.md` files plus the 5 subdirs (components, genres, macrostructures, themes, verbs)
- `SKILL.md`: 67444 bytes (non-empty)
- `references/study.md`: 42639 bytes (non-empty — required by downstream tasks)

## Usage

With the skill installed, the Hallmark verbs become available: `hallmark study` (analyze/audit a design) and `hallmark redesign` (propose a redesign).
