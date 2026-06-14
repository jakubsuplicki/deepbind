# ADR 018 — v1 ships English-only

**Status:** Accepted
**Date:** 2026-05-05
**Related:** [ADR 002](002-pure-local-product-shape.md), [ADR 005](005-hardware-tiered-model-stack-and-first-run-policy.md), [ADR 009](009-context-overflow-compaction.md), [ADR 015](015-single-target-local-only-stack.md)
**Supersedes language assumptions in:** the multilingual-reranker and Polish-NER model evaluations (both amended under "Scope amendment 2026-05-05").

## Context

The product's retrieval pipeline runs three ML components, each currently selected on a *multilingual* axis:

- **Embedding model**: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (50+ languages, ~252 MB ONNX in the bundle)
- **Reranker**: `jinaai/jina-reranker-v2-base-multilingual` (50+ languages, downloaded at runtime — being swapped per the in-flight commercial-licensing audit)
- **NER**: `pl_core_news_sm` (Polish-specific, GPL-3.0 — being swapped to `xx_ent_wiki_sm` per the audit)

The original product framing assumed Polish was a primary target language, which forced multilingual model picks across the pipeline. The 2026-05-05 scope amendment in the Polish-NER evaluation dropped Polish from v1 target languages, but that change was scoped to one model. The implication for the *rest* of the pipeline wasn't formalized — and the English-first reranker re-research surfaced the question: if Polish is out, why are we still picking multilingual everywhere?

The pattern that emerged: **multilingual models trade English quality for language breadth.** A multilingual MiniLM scores 1–3 nDCG@10 points lower on English BEIR than an English specialist of the same size. Same shape across the cross-encoder reranker family — `bge-reranker-v2-m3` (multilingual, ~600 MB INT8) is roughly tied with `bge-reranker-large` (EN+ZH, ~2.2 GB) on English BEIR while costing real integration work. Picking multilingual without a user demographic that justifies it is paying a quality tax for breadth we don't market and won't ship support for.

## Decision drivers

1. **English is the only target user demographic for v1.** Target user profile (US/UK/AU compliance / knowledge workers) is English-first. Polish was the only non-English language with a confirmed user; that user dropped Polish from scope.
2. **Quality is the product.** A compliance-sensitive product must embody the rigor auditors look for. Shipping a multilingual reranker that's mediocre on the user's actual content (English) to preserve breadth nobody will use is a credibility leak.
3. **Honest product positioning.** Half-baked multilingual support is worse than no multilingual support. If we say "supports 50 languages" but the precision filter is tuned for cross-lingual averages instead of English-specific quality, we under-deliver to *every* user group: English users get suboptimal English, multilingual users get a hint of their language without audit-grade quality.
4. **Smaller architectural surface.** One language axis to optimize for at every layer — embeddings, reranker, NER, system prompts, tokenizers — instead of N. Future-multilingual is then a deliberate v1.5+ investment, not an accidental side-effect of "we picked multilingual base models."
5. **License + offline-first cleanup is happening anyway.** The commercial-licensing audit forces every retrieval-pipeline model to be re-picked. Doing that under an English-first constraint is the same effort as doing it under a multilingual constraint, with strictly better English outcomes.

## Decision

**v1 ships English-only across the entire ML pipeline.** Every model selection optimizes for best English quality within the existing license + local-runnable + bundle-friendly constraints.

**What this means concretely:**

| Component | Current | v1 selection criterion |
|---|---|---|
| Embedding model | Multilingual MiniLM (50+ langs) | Best English embedder under Apache/MIT, ≤ ~500 MB ONNX, fastembed-compatible. Likely candidate: `BAAI/bge-{small,base,large}-en-v1.5` |
| Reranker | Jina v2 multilingual (CC-BY-NC) | Best English cross-encoder under Apache/MIT, ≤ ~150 ms top-20 on Apple Silicon CPU. Likely candidate: `BAAI/bge-reranker-large` or `BAAI/bge-reranker-base` |
| NER | `pl_core_news_sm` (GPL) | Locked to `xx_ent_wiki_sm`. Multilingual-by-side-effect (the model just happens to cover 9 languages); the choice is driven by license + spaCy compatibility, not by language coverage. Acceptable; revisit only if English NER quality regresses |
| System prompt language | English-only | Already the case; no change |
| Chat models (Ollama catalog) | Mostly multilingual-capable backbones (Qwen3, etc.) | Unchanged. Chat models are the user's choice from Ollama's model registry; we don't gate them on language. The product UI is English; whatever the user pulls is what they get. |
| Tokenizer counting | HuggingFace `tokenizers` per-model | Unchanged — accurate per chat-model regardless of language |
| Web search (BYOK) | Locked to Tavily/Brave | Unchanged — both providers are multilingual but we surface results in whatever language the index returns. No constraint here. |

## Trade-offs

| Choice | Benefit | Cost |
|---|---|---|
| English-specialist embedding model | +1–3 nDCG points on English content vs multilingual baseline of same size | Users with non-English notes get worse semantic recall. Acceptable: that user demographic isn't v1 ICP |
| English-specialist reranker | Allows picking the best EN cross-encoder regardless of language coverage; opens up `bge-reranker-large` as a candidate that was filtered out before | Loses the precision-filter on non-English content. The hybrid pipeline still finds those notes via BM25 + embeddings; they just don't get the cross-encoder reordering boost |
| Single language axis across the stack | Simpler architecture, simpler audit story, simpler eval harness | Multilingual support becomes a deliberate v1.5+ project: re-pick embedding + reranker + NER under multilingual constraint, ship as opt-in model pack |
| Honest "English-only" product copy | No expectations gap when a multilingual user tries the app | Closes the door on a "secretly multilingual via base model side effects" positioning claim |

## Alternatives considered

### A. Keep multilingual baseline everywhere (status quo)
Reject. The multilingual constraint forced sub-optimal English picks (multilingual MiniLM, multilingual reranker) without a user demographic that benefits.

### B. English specialists for reranker only, multilingual elsewhere
Asymmetric and half-baked. The embedding model would still under-perform on English; the reranker boost would be partly wasted on top of weaker embeddings.

### C. Drop the reranker entirely (Signal 5 deletion)
Considered earlier in the conversation. Rejected — the precision lift is real (~5–15% nDCG@10) and the implementation already exists. Cheaper to swap models than to delete and re-add later.

### D. Future-proof for multilingual v1.5 by keeping multilingual everything (chosen baseline before this ADR)
Rejected. v1.5 multilingual is its own deliberate project, not a side-effect of v1 choices. Future-proofing v1 with multilingual baggage trades v1 quality for hypothetical v1.5 scope.

## Migration path

**Already in flight:**
- NER swap to `xx_ent_wiki_sm`: locked in the Polish-NER evaluation. Implementation pending.

**Triggered by this ADR:**
- Reranker re-research under English-first constraint: in flight. User-paced.
- Embedding-model re-research under English-first constraint: pending. Same shape as reranker re-research.

**No-op for v1:**
- Chat-model catalog: stays as-is. Chat models are user-pulled from Ollama's registry; the product surfaces whatever the user installs.
- System prompt: already English.
- Web search providers (Tavily / Brave): unchanged.

**v1.5+ scope (out of v1):**
- Multilingual model pack (alternative embedder + reranker + NER bundled separately, opt-in via Settings).
- Polish-specific model pack if a user requests it.

## Consequences

- **Documentation:** product-facing copy must say "English-only" explicitly. Onboarding flow, marketing site, app store descriptions. Inconsistent multilingual marketing would be a credibility leak.
- **Eval harness:** the conversation-replay eval set ([`backend/tests/eval/conversations/`](../../../backend/tests/eval/conversations/)) is already English-dominant. No change required, but a small task: explicitly tag each fixture's language as `en` so multilingual support's later return doesn't silently regress on English.
- **Contributor conventions:** record an English-only scope rule in the project's engineering conventions so future contributors don't pick multilingual base models out of habit.
- **Research docs:** the existing v1 research docs (multilingual reranker, multilingual NER) keep their audit-trail value. Their conclusions are amended (see "Scope amendment 2026-05-05" sections), not deleted.
- **Future PRs that re-introduce multilingual concerns** must explicitly cite this ADR and either justify the deviation as v1-essential or defer to v1.5.

## Open questions

- **Embedding-model re-research has not started yet.** That's a separate research doc, same shape as the reranker re-research. Pending user direction.
- **Should `xx_ent_wiki_sm`'s incidental 9-language coverage be marketed as "supports those languages" in any user-facing copy?** No — would contradict the "English-only" framing. The model picks PERSON / ORG / LOC entities reasonably across those languages but we don't test or guarantee anything beyond English.
- **What does "English-only" mean for a user whose chat model is Qwen3 (multilingual backbone)?** The chat model can still respond in any language the user prompts in — that's a runtime behavior, not a product capability we market. We just don't optimize for it. A non-English query will work; precision will be lower.
