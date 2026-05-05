# ADR 020 — Web search dropped from v1 (BYOK / paid-tier deferred)

**Status:** Accepted
**Date:** 2026-05-05
**Related:** [ADR 002](002-pure-local-product-shape.md) · [ADR 006](006-offline-signed-license.md) · [`docs/research/web-search-commercial-alternatives.md`](../../research/web-search-commercial-alternatives.md) · [`docs/research/commercial-licensing-audit.md`](../../research/commercial-licensing-audit.md) §"Finding 4"

## Context

The codebase carries a `web_search` tool — a chat-tool affordance that lets the LLM fall back to the open web when local notes don't cover the question. The implementation in [`backend/services/web_search.py`](../../../backend/services/web_search.py) is a thin wrapper over the [`duckduckgo-search`](https://pypi.org/project/duckduckgo-search/) PyPI package, which scrapes DuckDuckGo's public HTML/JSON endpoints with no API key, no commercial agreement, and a README that explicitly states the package is "for educational purposes only."

The commercial-licensing audit (2026-05-04, finding #4) flagged this as a credibility leak. [`docs/research/web-search-commercial-alternatives.md`](../../research/web-search-commercial-alternatives.md) evaluated the four alternatives and recommended Approach A — BYOK with Tavily as the default provider and Brave Search as a configurable second engine. That recommendation, on closer review, has a v1-shape problem: it requires every user to sign up for a Tavily account, generate an API key, and paste it into Settings before web search will work. For a "self-contained from minute zero" product (ADR 002 / ADR 003 driver #4), that's hostile onboarding for a feature whose actual job is "fall back when notes don't have the answer."

The other Approach A variants do not survive scrutiny in the v1 product shape:

- **Vendor-paid managed key.** DeepFilesAI would hold the Tavily key and proxy queries through a thin authenticated relay. Contradicts ADR 002's "no DeepFilesAI servers" stance and ADR 006's offline-signed-license shape. Adds a server-side credential management surface where today there is none. Also turns every user's web-search query into a per-query cost on our side — perverse incentive against a feature whose value is sporadic fallback.
- **Hardcoded free-tier key shipped in the bundle.** Trivial to extract, trivially abused, gets the key revoked, breaks the feature for everyone the moment a script kiddie finds it. Not viable.

This leaves three real options: ship BYOK with Tavily, ship a paid-tier entitlement (Approach B in the research doc), or **drop web search from v1 entirely** (Approach D). This ADR locks in the third option.

## Decision drivers

1. **The "self-contained from minute zero" property is load-bearing.** A buyer's first session should work end-to-end without external account signups. Web search behind a third-party API key violates this; "user types a question, model can answer from notes or refuses cleanly" is the honest v1 contract.
2. **Web search is the lowest-value affordance the product offers.** Every other path — local notes, semantic retrieval, knowledge-graph traversal, Jira ingest, URL paste — has an authoritative use case. Web search is "AI assistant that knows things" generic value, available in every cloud chat tool. Removing it does not erode the differentiator (private knowledge over your local data).
3. **The query-leak risk is structural, not implementation-bound.** Whether DDG, Tavily, or Brave receives the query, the LLM picks the query freely from a context that may include PII, regulated data, or attorney-client material. No provider-swap fixes this; only a UI surface that confirms each query before send fixes it, which is its own design problem we don't have appetite for in v1.
4. **Commercial-feasibility audit cleanup is converging.** Finding #4 is the last licensing-audit issue with a non-trivial implementation tail. Closing it by removal lets the audit converge to "shipped clean" rather than "shipped clean except for one BYOK toggle." A future v1.5+ web-search re-introduction can take the BYOK shape from a position of strength.
5. **No customer evidence demands web search.** Pre-release, no users; no buyer signal that "web fallback is the deal-breaker." Reactivation can be evidence-driven if it ever arises.
6. **Removal shrinks the dep + compliance surface.** `duckduckgo-search` and its scraping behaviour leave the bundle. The privacy module loses one of its three gates. The chat tool taxonomy shrinks by one. Less surface to audit, document, and maintain.

## Decision

**The `web_search` tool is removed from v1.** No BYOK affordance is shipped. The chat model has no fallback to the open web; when the user's notes don't cover a question, the model answers from its training data alone or says it doesn't know.

### What gets removed

- [`backend/services/web_search.py`](../../../backend/services/web_search.py) — file deleted.
- The `web_search` tool spec in [`backend/services/tools/definitions.py`](../../../backend/services/tools/definitions.py).
- The `web_search` dispatch branch in [`backend/services/tools/executor.py`](../../../backend/services/tools/executor.py).
- The `privacy_web_search_enabled` workspace preference and the `web_search_enabled()` getter in [`backend/services/privacy.py`](../../../backend/services/privacy.py). The `_ALLOWED_PRIVACY_KEYS` map drops the `web_search_enabled` slot; an existing workspace preference of that name becomes inert garbage data and is harmlessly ignored on read.
- The `"web_search"` entry from the regex in [`backend/services/entity_extraction.py`](../../../backend/services/entity_extraction.py) that catalogues tool names for skip-filtering.
- The `duckduckgo-search==7.5.1` line in [`backend/requirements.txt`](../../../backend/requirements.txt).
- The "Allow web search" toggle row in [`frontend/app/components/settings/PrivacySection.vue`](../../../frontend/app/components/settings/PrivacySection.vue).

### What stays

- **`url_ingest`** stays. Pasting a specific URL is a different surface (user-initiated, content-specific, fully attributable) and the provider is the URL itself, not a search index. The `privacy_url_ingest_enabled` preference and the master `offline_mode` switch are unaffected.
- **The privacy module's three-layer structure** stays — `JARVIS_OFFLINE_MODE` env lock, master `offline_mode` toggle, and the per-feature `url_ingest_enabled` gate. The web-search slot is the only thing removed.
- **The tool taxonomy**: `search_notes`, `read_note`, `query_graph`, `save_preference`, `create_specialist`, the Jira tools, and `url_ingest` remain. The model still has six tool slots covering the local-knowledge surface.

## Alternatives considered

### A. Ship BYOK web search with Tavily-default + Brave-secondary (the locked research recommendation)

The research doc's pick. Honest commercial contract, ZDR options, SOC 2 attestations, free-tier large enough for realistic users. **Rejected for v1** on the "self-contained from minute zero" criterion — a feature that requires a third-party signup before it works is not first-session-ready, and the value of the feature does not justify the onboarding tax. Reactivation in v1.5+ is reasonable if customer evidence demands it.

### B. Ship a vendor-paid managed-key tier as a license entitlement

Solves the user-facing onboarding question by having DeepFilesAI hold the API key and meter usage via a `web_search_managed` license entitlement. **Rejected** because it requires server-side infrastructure (key holder, relay, billing) that the product architecture explicitly avoids per ADR 002 and ADR 006. The math also doesn't work — a per-query cost on our side, against a feature most users will rarely invoke, is the wrong shape of subsidy.

### C. Ship `duckduckgo-search` as-is

Status quo. **Rejected** — finding #4 is a real commercial-feasibility blocker, not a metadata flag. DDG's ToS forbid commercial scraping; the package's own README disclaims commercial fitness. Untenable for a paid commercial product whose pitch is rigor.

### D. Drop web search from v1 entirely (this ADR)

The decision. Net effect: -45 LoC of service code, -22 LoC of tool spec, -7 LoC of dispatcher branch, -1 dependency, -1 privacy-gate slot, -1 settings UI row, -1 audit finding. The v1 product is honest about its contract — it operates on the user's local knowledge, not the open web.

## Consequences

### Positive

- Audit finding #4 closes by removal. Licensing audit converges.
- The bundle drops `duckduckgo-search` and its scraping behaviour; one fewer outbound network surface to document for buyer compliance review.
- Privacy module simplifies — two gates remain (offline mode + url_ingest), down from three.
- The chat tool taxonomy is smaller and more honest about scope. The system prompt can drop "search notes first before web_search" and replace it with "search notes first; if not found, say so."
- The "self-contained from minute zero" property is preserved: every shipping feature works without external signups.
- A future v1.5+ reactivation, if customer evidence demands it, lands as a deliberate addition with a clean ADR — not as a retrofit on top of a half-implemented BYOK toggle.

### Negative

- The chat model loses its "I don't know — let me look it up" affordance. For questions outside the user's notes and the model's training cutoff, the answer is "I don't know" or stale training data. Users accustomed to cloud chat tools may notice.
- The product loses one row of the "AI assistant capabilities" matrix that buyers may scan. Mitigation: nothing on the matrix was a differentiator anyway; the differentiator is the local knowledge surface.

### What this changes about existing code

- The `services/web_search.py` file is deleted. Future searches for "where does the chat call out to the web" return nothing in `backend/services/`, which matches the new contract.
- The `chat` feature doc removes the `web_search` row from its tool-call table and removes the `services/web_search.py` source-file mention.
- The `preferences-settings` feature doc removes the `web_search_enabled` examples from the API surface and the `privacy.py` gate description.
- [`docs/.registry.json`](../../.registry.json) entries for `chat` and `preferences-settings` get a 2026-05-05 note + `last_updated` bump.
- The licensing-audit doc marks finding #4 closed by removal.
- The web-search-commercial-alternatives research doc gets a "superseded by ADR 019" header — preserved for audit trail and v1.5+ reactivation reference.

## Open follow-ups (non-blocking)

1. **v1.5+ web-search reactivation criteria.** Document what evidence would justify re-introducing the feature: a customer interview signal that web fallback is actually requested; a clean BYOK UX pattern that doesn't violate the first-session contract; a paid tier with margin to absorb a managed-key offering. This ADR is the trigger document for that decision.
2. **System-prompt rewrite.** The chat prompt currently mentions web_search as a tool the model should fall back to. Update to "if notes don't cover it, say so" — a one-line edit but worth tracking so the model's behaviour aligns with the new contract.
3. **Bundle size sanity-check.** Confirm `duckduckgo-search` and any of its transitives leaving the requirements file actually shrink the PyInstaller bundle. Probably modest (~few hundred KB) but worth noting in the build-pipeline diff.
