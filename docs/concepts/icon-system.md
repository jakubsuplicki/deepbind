# Icon system

**Status:** Active
**Owner:** Frontend
**Last updated:** 2026-05-05
**ADR:** [017 — Icon system: Phosphor + Simple Icons via @nuxt/icon](../architecture/decisions/017-icon-system-phosphor-via-nuxt-icon.md)

The frontend has one icon system. Reach for it instead of writing inline `<svg>` and instead of using emoji for functional UI.

## What goes where

| Need | Source | Example |
|---|---|---|
| Functional UI affordance (button, status, badge, banner head, toolbar action…) | Phosphor | `<Icon name="ph:check-circle-fill">` |
| Brand mark (provider, runtime, integration, third-party logo) | Simple Icons | `<Icon name="simple-icons:anthropic">` |
| Bespoke product illustration (Jarvis orb, ambient HUD elements) | Inline `<svg>` | See [`Orb.vue`](../../frontend/app/components/Orb.vue), [`JarvisSelfCard.vue`](../../frontend/app/components/JarvisSelfCard.vue) |
| User-supplied content (specialist avatar emoji) | Whatever the user picks | Specialist `form.icon` is user data, not chrome |

If you can't decide whether something is a Phosphor icon or bespoke art, ask: *"Is this part of the visual vocabulary, or is it brand identity?"* Visual vocabulary → Phosphor. Brand identity → bespoke.

## Sizing

Icons inherit their size from the parent's `font-size`. Default is `1em`. Use the modifier classes for explicit sizes:

| Class | Size |
|---|---|
| `icon--xs` | 12px |
| `icon--sm` | 14px |
| `icon--md` | 18px |
| `icon--lg` | 22px |
| `icon--xl` | 28px |
| `icon--2xl` | 40px |

Setting `font-size` on an icon's wrapper also works — the icon scales to match — and is preferable when the icon has to align with adjacent text (e.g. a 0.78rem chip with an inline glyph).

## Semantic colour classes

Map directly to `--neon-*` tokens in [main.css](../../frontend/app/assets/css/main.css):

| Class | Use for | Token |
|---|---|---|
| `icon--success` | OK / done / passing | `--neon-green` |
| `icon--warning` | Attention / re-test recommended | `--neon-yellow` |
| `icon--danger` | Error / destructive / failed | `--neon-red` |
| `icon--info` | Informational | `--neon-cyan` |
| `icon--accent` | Highlight / active surface | `--neon-cyan` |
| `icon--muted` | Secondary chrome | `--text-secondary` |
| `icon--subtle` | Tertiary chrome | `--text-muted` |

These exist so you don't write per-component CSS for every icon colour. Reach for the modifier; only add a per-component rule if you have a genuinely one-off requirement (e.g. an icon that has to sit on a custom-coloured surface and needs its own contrast-tuned tone).

## Weights and the discipline rule

Phosphor ships six weights: `thin`, `light`, `regular`, `bold`, `fill`, `duotone`. We use **three**:

- **`regular`** — default for every icon. The unmarked Phosphor name (e.g. `ph:check-circle`) gives you regular weight.
- **`-fill` suffix** — for active/selected/success states. Pairs with `icon--success`/`icon--accent` for emphasis (e.g. a check after a passing probe, an active toggle, a success badge).
- **`-bold` suffix** — sparingly. Used for high-emphasis tokens that need to read clearly at small sizes: `ph:check-bold` for confirmations, `ph:x-bold` for close/dismiss in tight chrome (8-14px), `ph:plus-bold` for primary action buttons.

**Do not mix `thin`/`light`/`duotone` into the system.** A row that mixes weights looks amateurish. The Definition of Done requires you stay within {regular, -fill, -bold} or justify the choice in a code comment.

### Glyphs without a `-fill` variant

Some Phosphor glyphs are stroke-only by design and have no `-fill` equivalent — `ph:link`, `ph:hash`, `ph:arrow-*`, `ph:caret-*`, etc. When you need an active state for one of these, **use `-bold` as the active variant** instead of forcing a fill that doesn't exist. The visual progression (regular → bold) reads as "this is selected" similarly to (regular → fill) for filled-capable icons. Example: [`SmartConnectSection.vue`](../../frontend/app/components/settings/SmartConnectSection.vue) pairs `icon: "ph:link"` with `iconActive: "ph:link-bold"`. Don't substitute a different glyph just to get a fill variant — the icon's identity should not change between states.

## Common patterns

### Status pill (inline icon + label)

```vue
<span class="badge" :class="{ 'badge--ok': isOk }">
  <Icon
    :name="isOk ? 'ph:check-circle-fill' : 'ph:warning-fill'"
    :class="['icon--sm', isOk ? 'icon--success' : 'icon--warning']"
  />
  {{ isOk ? 'Connected' : 'Disconnected' }}
</span>
```

### Banner head with semantic colour

```vue
<div class="banner banner--warn">
  <Icon name="ph:warning-fill" class="icon--md icon--warning" />
  <span>Re-test recommended</span>
</div>
```

### Active vs inactive toggle (weight-driven, not colour-driven)

```vue
<Icon :name="active ? 'ph:stop-circle-fill' : 'ph:play-fill'" class="icon--md" />
```

### Brand mark in a list row

```vue
<Icon name="simple-icons:ollama" class="icon--md icon--brand" />
<span>Ollama runtime</span>
```

`icon--brand` softens the mark to 90% opacity and lifts it to full opacity + cyan on hover — same hover lift the rest of the chrome uses, applied to brand marks as a class instead of per-component CSS.

### Spinning / pulsing utilities

Use `icon--spin` with `ph:spinner-gap` or `ph:circle-notch` for loaders; `icon--pulse` with `ph:circle-fill` for live-status dots. Don't write a fresh `@keyframes` per component.

## Anti-patterns

- **Hand-rolled inline `<svg>` for new affordances.** The migration left zero of these in component code (Orb.vue and JarvisSelfCard's orb art are the only sanctioned exceptions). If you find yourself pasting a Feather SVG, stop — there's a Phosphor equivalent.
- **Emoji as functional UI.** ✅ ⚠️ 🟢 🔴 ⚡ ⚙️ 🔍 etc. render differently across operating systems and don't theme. They're banned for any icon-shaped role: status, badges, banner heads, button glyphs, search-mode markers. User-chosen avatars (specialist icons) are an exception — that's user data.
- **Mixing weights without intent.** A toolbar with one `thin` icon next to a `bold` icon next to a `regular` icon reads as broken design. Stay within the three-weight rule above.
- **Per-component icon size rules.** If you write `.foo__icon { width: 14px; height: 14px; }` you are reinventing the modifier system. Use `class="icon--sm"`, or set `font-size` on the wrapper if it has to align with surrounding text.
- **Reaching for `api.iconify.design` in code.** Never. The bundle is offline-first ([ADR 014](../architecture/decisions/014-desktop-bundle-excludes-cloud-providers.md)). The build picks icons from the local `@iconify-json/*` packages.

## Section-header icons

[`SettingsSection.vue`](../../frontend/app/components/settings/SettingsSection.vue) takes optional `icon` and `iconActive` props (Phosphor names). The icon renders in the section header gutter, before the title. When the section is open, the `iconActive` weight (typically `-fill`) replaces the regular weight and a cyan glow is applied via [main.css](../../frontend/app/assets/css/main.css) — same hover rule the rest of the chrome uses.

Convention: pair `icon: "ph:foo"` with `iconActive: "ph:foo-fill"` and let the component swap them automatically. Don't try to fold both into one prop or hard-code the suffix in the component — that breaks the rule that "fill = active state" is a system-level concept, not section-specific.

When wiring a new section, pick a Phosphor name that telegraphs the section's content (e.g. `ph:hard-drives` for Local Models, `ph:plugs` for MCP, `ph:microphone` for Voice). The current set is documented inline in each section component's `<SettingsSection ... icon="ph:..." />` invocation.

## Top-nav active state

The status-bar nav links in [`StatusBar.vue`](../../frontend/app/components/StatusBar.vue) drive `regular` → `fill` weight off `route.path.startsWith(link.to)`. Active link gets a soft cyan drop-shadow on the icon (see `.status-bar__link.router-link-active .status-bar__link-icon` in the same component's scoped CSS). This is the canonical example for any future "regular default, fill on active" navigation.

## Adding a new icon

1. Find the icon at [phosphoricons.com](https://phosphoricons.com) (or [simpleicons.org](https://simpleicons.org) for brand marks).
2. Reference it: `<Icon name="ph:foo">` or `<Icon name="simple-icons:foo">`.
3. Build picks it up automatically — no registration, no central catalogue update.

If a Phosphor name is missing for what you need, file the gap before reaching for another icon set. We accept Tabler as a documented escape hatch for missing glyphs but the bias is to stay in Phosphor.

## Adding a new size, colour, or utility class

Edit [main.css](../../frontend/app/assets/css/main.css)'s `.icon--*` block. Do not add a per-component CSS rule that duplicates a token that should be system-level.

## Bundle and offline guarantee

`@nuxt/icon` is configured in [nuxt.config.ts](../../frontend/nuxt.config.ts) with `clientBundle.scan: true`. At build time, the scanner walks the source tree, collects every `<Icon name="..." />` reference (and `:name="..."` bindings whose values are statically resolvable), and bakes the matching SVGs into the SPA bundle from the local `@iconify-json/ph` and `@iconify-json/simple-icons` JSON sets.

The runtime never reaches `api.iconify.design`. This is required for [ADR 014](../architecture/decisions/014-desktop-bundle-excludes-cloud-providers.md)'s offline-first guarantee — first launch with no network must not produce missing-icon placeholders.

If a binding is dynamic enough that the scanner can't resolve it (e.g. a fully runtime-computed name string), the build will skip it and the icon will be missing at runtime. Keep icon names as static literals or as values from a small static lookup table (see [`SpecialistKnowledgePanel.vue`](../../frontend/app/components/SpecialistKnowledgePanel.vue)'s `fileIcon()` for the pattern: a `switch` over file extensions returning hard-coded `ph:*` strings).
