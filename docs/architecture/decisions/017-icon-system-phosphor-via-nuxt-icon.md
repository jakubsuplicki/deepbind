# ADR 017 — Icon system: Phosphor + Simple Icons via @nuxt/icon (offline-bundled)

**Status:** Accepted
**Date:** 2026-05-04 (initial); follow-up scope landed 2026-05-05
**Related:** [ADR 003](003-desktop-distribution-tauri-and-sidecars.md), [ADR 014](014-desktop-bundle-excludes-cloud-providers.md), [ADR 015](015-single-target-local-only-stack.md)

## Context

Before this decision, the frontend had **no icon system**. Every component author hand-rolled inline `<svg>` strokes (38 SVGs across 19 files), supplemented by emoji glyphs (`✅ ⚠️ ❌ 🔒 ⚡ 🟢 🔴 🔍 ⚙️ 🛡 🖥 ✨` and the file-type set `📄 📝 📃 📊 📋 📁`) used as functional UI chrome — runtime status indicators, banner heads, button affordances, search-mode badges. [`providerIcons.ts`](../../../frontend/app/composables/providerIcons.ts) carried four hand-pasted Simple-Icons brand SVGs plus four utility shapes (eye-open/closed, lock, info), but the file was no longer imported from anywhere — the two call-sites that referenced its `providerIcon()` shape had been collapsed to empty-string stubs after [ADR 015](015-single-target-local-only-stack.md) removed the multi-provider dispatcher.

### Why the status quo had to change

This is a compliance/security product whose value proposition is rigour-as-craft (CLAUDE.md: *"Quality is the product"*). Three concrete problems:

1. **Visual incoherence.** Each inline SVG was drawn to a different stroke width, viewport, and idiomatic Feather/Heroicons pastiche, so rows that should read as one toolbar (e.g. SpecialistCard's edit/delete/expand) looked subtly mismatched. Hand-rolled provider marks in [`providerIcons.ts`](../../../frontend/app/composables/providerIcons.ts) (Anthropic, OpenAI, Google, Ollama) were lifted from Simple Icons but only as ad-hoc paste-ins, with no central catalog or update path.
2. **Emoji as functional UI is platform-rendered.** `🟢/🔴` next to the active model in [StatusBar.vue](../../../frontend/app/components/StatusBar.vue) renders as Apple Color Emoji on macOS, Segoe UI Emoji on Windows, Noto on Linux — three different visual languages for a single status signal — and none of them respond to theming. Emoji also cannot be re-coloured to match the cyan/green/red palette in [main.css](../../../frontend/app/assets/css/main.css) (`--neon-*` tokens).
3. **Offline-first constraint.** ADR 003 runs the frontend inside a Tauri WKWebView with no Node server (`nuxt generate` static SPA). ADR 014 explicitly excludes any cloud dependency from the bundle. An icon system that resolves SVGs over the network at runtime — Iconify's `api.iconify.design` default — would break the offline guarantee on first launch with no connectivity.

A fourth, minor issue: every author writing a new affordance had to either find a paste-in source or hand-draw the shape, which is friction the project can't afford pre-v1.

## Decision

Adopt **Phosphor Icons** (regular weight default, fill weight reserved for active/selected/success states) as the UI vocabulary, **Simple Icons** as the brand-mark catalogue (provider/runtime logos only), and **`@nuxt/icon`** as the integration layer with **client-side bundling** so all icons are baked into the static SPA — no runtime fetches, no network dependency.

### Why Phosphor (and not Lucide / Heroicons / Tabler / Material Symbols)

- **Character without weirdness.** Phosphor's geometric construction reads as "designed product" rather than "default Tailwind landing page". A compliance product whose buyers will audit the visual language for rigour should not adopt the icon set every starter template ships with — that's a credibility leak.
- **Multiple weights map to UI state without a second set.** `regular` for inactive/default, `fill` for active/selected/success, `bold` reserved for high-emphasis tokens (X-close, check-confirm). Lucide's single weight forces a state vocabulary built from colour alone, which clashes with the existing semantic-colour system in [main.css](../../../frontend/app/assets/css/main.css).
- **MIT licensed**, ~1500 icons, used in production by Linear, Cal.com, Plausible — not exotic.

Skipped:
- **Lucide / Heroicons** — too generic for a brand-conscious surface; they encode "I built this in a weekend" by sheer ubiquity.
- **Tabler** — 5800 icons (more than Phosphor) but a more utilitarian, less editorial style. Reach for it only if Phosphor's catalogue leaves a real gap.
- **Material Symbols** — reads as Google product. Wrong tone.
- **Font Awesome** — Free/Pro split creates licensing friction the project doesn't need to inherit.

### Why Simple Icons specifically for brand marks

Brand logos (Anthropic, OpenAI, Ollama, Tauri, GitHub, etc.) belong to their owners — using Simple Icons (CC0 for the SVGs themselves; trademarks remain with the brand owner) gives a curated, consistently-styled catalogue without freehand redraws. The previous hand-pasted approach in [`providerIcons.ts`](../../../frontend/app/composables/providerIcons.ts) was already pulling from Simple Icons but informally; this decision formalises that boundary.

The split is intentional: **brand marks come from `simple-icons:*`, everything else from `ph:*`**. Mixing brand marks into Phosphor would force redraws of marks that have authoritative versions elsewhere.

### Why @nuxt/icon (not unplugin-icons or direct iconify-vue)

- It's the official Nuxt module — one install, one `modules: ['@nuxt/icon']` line, no manual auto-import config.
- The `clientBundle.scan: true` mode statically analyses every `<Icon name="..." />` reference in the source tree at build time and includes only those icons in the SPA bundle. Tree-shaken, deterministic, no network call at runtime.
- Bundling [@iconify-json/ph](https://www.npmjs.com/package/@iconify-json/ph) and [@iconify-json/simple-icons](https://www.npmjs.com/package/@iconify-json/simple-icons) as devDependencies gives the scanner local SVG sources — Iconify's `api.iconify.design` is never reached, satisfying ADR 014's offline-first stance.

## Configuration

[nuxt.config.ts](../../../frontend/nuxt.config.ts):

```ts
modules: ['@nuxt/icon'],

icon: {
  provider: 'iconify',
  serverBundle: false,           // SPA build — no Nitro, no server bundle
  clientBundle: {
    scan: true,                  // static analysis of <Icon name="..." />
    includeCustomCollections: true,
    sizeLimitKb: 512,
  },
  collections: ['ph', 'simple-icons'],
  mode: 'svg',                   // inline SVG; themes via currentColor
  class: 'icon',                 // every <Icon> gets the .icon base class
},
```

[main.css](../../../frontend/app/assets/css/main.css) defines the design tokens — see [docs/concepts/icon-system.md](../../concepts/icon-system.md) for the full set and the rules governing weights, sizes, and semantic colours.

## Migration scope (this chunk)

- **Infrastructure:** add `@nuxt/icon`, `@iconify-json/ph`, `@iconify-json/simple-icons` to the frontend; configure offline client bundle in [nuxt.config.ts](../../../frontend/nuxt.config.ts).
- **Design tokens + rules:** add the icon CSS layer to [main.css](../../../frontend/app/assets/css/main.css) — sizes (`xs`/`sm`/`md`/`lg`/`xl`/`2xl`), semantic colours (`success`/`warning`/`danger`/`info`/`accent`/`muted`/`subtle`), brand-mark hover, glow filter, spin/pulse animations.
- **Migrate all 19 components** with inline SVGs to `<Icon name="ph:...">`. JarvisSelfCard's bespoke orb art and Orb.vue's main HUD orb are explicitly preserved as inline `<svg>` — they are brand illustrations, not icons.
- **Replace functional emoji** in 14 sites (`✅ ⚠️ ❌ 🔒 ⚡ 🟢 🔴 🔍 ⚙️ 🛡 🖥 ✨ 🎬 📄`) with semantic Phosphor icons + colour classes.
- **Delete `providerIcons.ts`** entirely. It's been dead since ADR 015 collapsed the per-provider dispatch surface, and the two consumer stubs returning `''` are removed in this chunk. Per CLAUDE.md "no backwards-compat hacks", we do not preserve the file as a re-export shim.
- **Convert the chat-bubble model badge** from a hand-rolled `<span v-html="providerIcon(...)" />` (which always rendered nothing) to `<Icon name="ph:hard-drives" />` — making explicit that every model in the local-only stack is local hardware.
- **Specialist user emoji left untouched.** The icon picker in [SpecialistWizard.vue](../../../frontend/app/components/SpecialistWizard.vue) (`form.icon`, `iconOptions[]`) is end-user content — users choose an emoji to represent each specialist. That's user data, not chrome, and isn't governed by the icon system.

### Deliberately out of scope

- (Initially deferred but landed in a follow-up chunk on 2026-05-05) Decorative icons on settings section headers, top-nav links, and page H1s. See follow-up section below.

### 2026-05-05 follow-up — decorative icon pass

- `SettingsSection.vue` gained `icon` / `iconActive` props. All 10 settings sections wired with section-specific Phosphor glyphs (regular → fill swap when the section is open), giving the long collapsible list a scannable left gutter.
- Status-bar top-nav links got route-aware icons: regular weight on inactive routes, `-fill` on the active route, with a cyan drop-shadow on the active glyph. Driven from a single `navLinks` config in [`StatusBar.vue`](../../../frontend/app/components/StatusBar.vue).
- Settings, Specialists, and Memory page titles got decorative H1/H2 icons (`ph:gear-six-fill`, `ph:users-three-fill`, `ph:books-fill`). These are pure decoration — no state, no interaction — and exist to make the page identity legible without leaning on the page title text alone.
- `PerformanceSection.vue` had a stray 🪶 emoji in its badge that the original migration missed (the badge is rendered in a `#suffix` slot which the SVG sweep didn't fully cover). Replaced with `ph:feather`.
- `DuelScoreBar.vue` deleted. The component had no consumers post-ADR 015 and only carried stray emoji + duel-specific CSS. Same chunk also cleaned the dead `.chat-panel__icon-btn--duel` CSS block, the silent `/duel` slash-command absorber, and the stale "duel types deleted" comment in [`types/index.ts`](../../../frontend/app/types/index.ts). Backend [`ollama_service.py`](../../../backend/services/ollama_service.py) had two model-description strings ("duel-mode opt-in" / "duel mode") referencing the removed feature; cleaned up. README's Duel Mode marketing sections + the `docs/assets/duel.png` screenshot also removed.

After the pass: bundle grew from 46 to 67 icons / 29 KB uncompressed — still trivially small, still 100% offline-bundled.

### 2026-05-05 follow-up — review-driven fixes

A code review of the decorative-icon pass surfaced three medium-priority issues plus one doc gap; all four landed in the same chunk:

1. **Single source of truth for nav active state.** The earlier `route.path.startsWith(link.to)` check duplicated state Vue Router already manages via `router-link-active`. They aligned for the current route set but would silently desync if a future route conflicted by prefix (e.g. `/main` vs `/mainframe`). [`StatusBar.vue`](../../../frontend/app/components/StatusBar.vue) now uses NuxtLink's `custom` slot pattern: `v-slot="{ href, navigate, isActive }"` with the `<a>` rendered manually. `isActive` becomes the only authority on which link is active and which icon weight to show.
2. **SettingsSection chevron migrated to Phosphor.** The chevron was a CSS-drawn diamond (`border-right`/`border-bottom` + `transform: rotate(-45deg)`); now it's `<Icon name="ph:caret-right">` with the rotate-on-open rule simplified to `transform: rotate(90deg)`. The two glyphs in the section header (chevron + content icon) now render with the same anti-aliasing and stroke character.
3. **Dropped no-op ternary on the Ollama status dot.** Both branches of `:name="ollamaReachable ? 'ph:circle-fill' : 'ph:circle-fill'"` returned the same string. Simplified to `name="ph:circle-fill"`; the dynamic part (`icon--success` vs `icon--danger`) lives in the class binding where it belongs.
4. **Documented the `-bold` fallback** in [docs/concepts/icon-system.md](../../concepts/icon-system.md) for glyphs without a `-fill` variant (`ph:link`, `ph:hash`, `ph:caret-*`, etc.). [`SmartConnectSection.vue`](../../../frontend/app/components/settings/SmartConnectSection.vue) was already pairing `ph:link` with `ph:link-bold`; the doc now describes this as a sanctioned exception so future contributors don't reinvent it ad-hoc.

Bundle unchanged at 67 icons / 29 KB — `ph:caret-right` was already pulled in by other surfaces (NoteList, SuggestionsPanel), so the chevron migration cost zero additional icons.

## Consequences

### Good

- One `<Icon name="..." />` is the only way to reach an icon. No new inline SVG enters the codebase.
- Theming is real: every icon inherits `currentColor` and responds to the `.icon--success`/`--warning`/`--danger`/`--info`/`--accent`/`--muted`/`--subtle` modifier classes, which map directly to the existing `--neon-*` tokens.
- Offline guarantee preserved: the static SPA bundle contains the icon SVGs; `api.iconify.design` is never contacted.
- The design system can extend without per-component CSS — adding a new affordance is `<Icon name="ph:something">` + a class, not a hand-drawn SVG.
- Brand marks have one authoritative source (`simple-icons:*`) — when a new provider/runtime needs a logo, no redraw, no paste.
- Frontend bundle stays small: `clientBundle.scan` only includes referenced icons. Migration adds roughly 60 distinct Phosphor glyphs and 3-4 brand marks; the JSON sets ship as devDependencies and are consumed by the build only.

### Bad / accepted trade-offs

- **Phosphor's six weights are a discipline problem if not bounded.** Without a rule, a row will mix `thin` and `bold` and look amateurish. The rule in [docs/concepts/icon-system.md](../../concepts/icon-system.md) is **regular default, fill for active/selected/success, bold sparingly for X/check; nothing else without justification**. New code must follow it.
- **`<Icon>` is a runtime component**, not a compile-time SVG inline like the previous hand-rolled approach. There is a tiny per-icon mount cost. For the volumes in play here (a few dozen icons per page max), it's invisible. If a future hot path renders thousands of icons, switch to a precomputed `<svg>` inline at that callsite.
- **Hand-edited inline SVGs become a code-review failure mode.** A future contributor who reflexively pastes a Feather SVG into a component is now violating the system. The concept doc spells this out, and the migration leaves zero hand-rolled `<svg>` in components that should adopt the system — Orb.vue and JarvisSelfCard's orb-art remain as the only sanctioned exceptions.

### Reversibility

The Icon component is a thin Vue wrapper; reverting to inline SVG is a mechanical replacement, not a structural rollback. Removing `@nuxt/icon` deletes one module config block. The decision is low-cost to reverse if a real performance issue surfaces.

## Notes for future contributors

- Adding a new icon: pick a Phosphor name from [phosphoricons.com](https://phosphoricons.com), reference it as `<Icon name="ph:xxx">`. The build picks it up automatically; no registration step.
- Adding a new brand mark: check [simpleicons.org](https://simpleicons.org), use as `<Icon name="simple-icons:xxx">`. Same — no registration.
- Adding a new size or semantic colour: extend [main.css](../../../frontend/app/assets/css/main.css)'s `.icon--*` modifiers; do not add per-component sizing rules.
- Adding **decorative bespoke art** (like the orb): inline `<svg>` is fine. The rule applies only to *icons* — small functional glyphs that participate in the visual vocabulary.
