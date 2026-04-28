---
title: Profiles (ProfilePack)
status: scaffold
type: feature
sources:
	- backend/services/profile_service.py
	- backend/tests/test_inference_router.py
depends_on: []
last_reviewed: 2026-04-28
last_updated: 2026-04-28
---

# Profiles (ProfilePack)

Per-vertical model stack manifests ([ADR 005](../architecture/decisions/005-profile-driven-model-stacks.md)).

## Status: scaffold

This implements the ADR 005 schema and a **3-profile starter catalog** —
enough variety to validate the schema works for real shapes (no coder for
patent, coder ladder for developer, smallest install for generic). The
remaining six profiles in ADR 005's catalog table are deferred until
someone close to each vertical signs off on the specialist defaults, tool
defaults, and ingest defaults (per ADR 005 §"Open follow-ups" #1).

| In scaffold | Deferred |
|---|---|
| `ProfilePack` Pydantic schema (matches ADR 005 §"Schema") | Tauri-side onboarding picker UI |
| `SlotSpec` / `SlotLadder` / `ProfileStack` shapes | Profile change with delta downloads + 30-day GC |
| 3 starter profiles (generic, developer, patent-prosecutor) | The other 6 profiles in ADR 005's catalog table |
| `get_active_profile()` reads `app/config.json:active_profile_id` | License `allowed_profiles` cross-check |
| `set_active_profile()` persists to config | Specialist defaults / tool defaults / ingest defaults / UI strings per profile |

## How It Works

### Schema

`ProfilePack` matches ADR 005 §"Schema":

```python
class ProfilePack(BaseModel):
    id: str                      # stable identifier ("patent-prosecutor")
    display_name: str
    version: int = 1
    stack: ProfileStack          # the slot table
    specialists: list[str] = []
    tools: dict[str, bool] = {}
    ingest_defaults: dict[str, object] = {}
    ui_strings: dict[str, str] = {}
    context_recent_n: int = 4    # default per ADR 009
    context_perf_mode_default: str = "balanced"  # "balanced" | "quality" | "lightweight"
```

`ProfileStack` carries the slot table. `embeddings`, `plumbing`,
`conversational`, and `reasoning` are required. `coder`, `vision`,
`long_context`, and `tts` are optional (`None` means the slot is not
provisioned for this profile — the router never dispatches to a `None` slot).

### Slot specs and ladders

```python
class SlotSpec(BaseModel):
    model_id: str                # references ModelCatalogEntry.id
    quant: str | None
    pinned_sha: str | None       # ADR 003 pinning placeholder
    expected_footprint_at_default_ctx_gb: float | None

class SlotLadder(BaseModel):
    preferred: SlotSpec
    downgrade_ladder: list[SlotSpec] = []
```

`SlotLadder` is used for slots that have a downgrade path (conversational +
coder). The router's `dispatch()` selects `preferred` today; the ladder is
held in the schema so when the memory-pressure-driven downgrade logic lands
(per [ADR 004](../architecture/decisions/004-inference-router-architecture.md)
§"Blocked by upstream ADRs"), it's a one-line change in `dispatch()`, not a
schema migration.

### The 3 starter profiles

| Profile | Coder | Vision | Long-context | Notes |
|---|---|---|---|---|
| `generic-knowledge-worker` (default) | — | — | ministral-3-8b | Smallest install footprint; safe default |
| `developer-devops` | devstral-small-2-24b → qwen3-8b | — | ministral-3-8b | The original implicit profile |
| `patent-prosecutor` | — | granite-vision-3-2b *(placeholder)* | ministral-3-8b | No coder — the whole point of profile-driven stacks |

Common to every profile: `qwen3-embedding-0.6b` (embeddings), `granite-4-h-micro`
(plumbing), `qwen3-8b` chat slot, `gemma4-e4b` reasoning, `kokoro-82m` TTS.

The embedding / TTS / vision references are **name-only placeholders** today —
they don't yet exist in the [`ollama_service.py` catalog](local-models.md)
because the embedding, TTS, and vision *slot consumers* haven't been wired
yet. The router never dispatches to a `None` slot, and for placeholder slots
in the conversational fall-through it logs a warning and falls back to
`qwen3-8b` rather than crashing.

### `get_active_profile()` resolution

```
config_path = workspace/app/config.json
  ↓
  profile_id = config["active_profile_id"]
  ↓
  if profile_id in PROFILE_CATALOG → return that profile
  else → return PROFILE_CATALOG["generic-knowledge-worker"]  # safe fallback
```

A stale config from a removed profile (or no config at all) doesn't crash
the dispatcher — fall back to the default. The right time to fail-fast on
unknown profiles is at `set_active_profile()` (which validates against the
catalog and raises `ValueError`), not at read.

## Key Files

| File | Purpose |
|------|---------|
| [profile_service.py](../../backend/services/profile_service.py) | Schema + 3-profile starter catalog + `get_active_profile` / `set_active_profile` |
| [test_inference_router.py](../../backend/tests/test_inference_router.py) | Profile catalog tests (presence, schema shape, default fallback, slot ladder integrity) |

## Behavior under code-review hardening (2026-04-28)

- **`get_active_profile()` is mtime-cached.** The router calls it on every dispatch (i.e. every WS chat message). The cache is keyed on `(config_path, mtime)` — invalidated when the file's mtime changes (covers external edits) and explicitly when `set_active_profile()` runs. `invalidate_active_profile_cache()` is exposed for tests that manipulate config files directly without going through `set_active_profile()`.
- **Multi-worker note.** The cache is *process-local*. A `uvicorn --workers N` deployment has N independent caches; a `set_active_profile()` call in one worker only invalidates that worker's cache. Other workers eventually pick up the new profile when their next `get_active_profile()` sees a changed mtime — eventual consistency, not strong. Today's deployment target per [ADR 003](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md) is a single-worker desktop binary, where this is moot. If/when a server deployment with multiple workers ships, switch to a process-shared signal (`fcntl`-locked sentinel file or pub-sub) to close the consistency gap.
- **Config writes are crash-safe and serialized across writers.** All three sibling functions (`set_active_profile`, `set_active_local_model`, `clear_active_local_model`) go through `locked_config_update()` from [`services/_config_io.py`](../../backend/services/_config_io.py), which wraps the read-modify-write in a POSIX `fcntl.flock` (no-op on Windows — see [the helper docstring](../../backend/services/_config_io.py) for the Windows narrow-race caveat) and writes via `atomic_write_json()` (temp-file + fsync + `os.replace` + parent-dir fsync). The lock prevents the lost-update race where two writers concurrently load the same starting state and one overwrites the other; the atomic write defends against truncated config files on process crash. Skip-on-unchanged keeps the file's mtime stable when a writer's block doesn't actually mutate the dict — important so the `get_active_profile()` mtime cache doesn't invalidate on no-op writes.
- **`validate_profile_catalog()` runs in CI.** Returns a dict of `profile_id → list of unresolved model_ids`; the test suite asserts the dict is empty. A typo (`devstal` vs `devstral`) trips this in CI rather than silently degrading to `qwen3-8b` at runtime via the dispatcher's safety fallback. The set of *intentional* placeholders (`qwen3-embedding-0.6b`, `kokoro-82m`, `granite-vision-3-2b`) is `KNOWN_PLACEHOLDER_MODEL_IDS` — these don't trigger validation.
- **Schema-drift defense.** `_walk_slot_specs()` walks every slot in `ProfileStack` for validation. A test (`TestWalkSlotSpecs`) asserts the explicit field set of `ProfileStack` matches what `_walk_slot_specs()` yields — if someone adds a new optional slot (`audio: Optional[SlotSpec]`) without updating the walker, the test fails with a pointer to the two places that need updating. Catches the silent-drift bug where a new slot escapes validation.

## Gotchas

- **`active_profile_id` lives in `app/config.json` alongside the legacy
  `local_model.active`.** Per ADR 005, the canonical multi-slot config will
  eventually replace `local_model.active` with `local_stack.{slots}` — but
  that lossless migration ships with the Tauri-side onboarding picker.
  Today both keys exist independently; the router's user-override
  short-circuit reads `local_model.active`, and the profile-driven fallback
  reads `active_profile_id`. If both are set, `local_model.active` wins
  (legacy single-model behavior preservation).

- **Profile change is a no-op without UI.** `set_active_profile()` exists and
  is correct, but there's no surface that calls it today. The Tauri-side
  picker is the consumer; the function is shipped now so the router has
  something honest to read.

- **License `allowed_profiles` enforcement is deferred.** ADR 005 §"License
  interaction" specifies that a license can scope `allowed_profiles`. The
  field exists in [`LicenseClaims`](licensing.md), but the cross-check
  ("refuse `set_active_profile(id)` if id ∉ license.allowed_profiles") wires
  in once the Tauri-side license loader from ADR 006 lands. Today,
  `set_active_profile()` validates only against the catalog.

- **The 3-profile scaffold is intentional.** Per ADR 005 §"Open follow-ups"
  #1, the full 9-profile catalog needs domain validation (mining /
  architecture / medical / etc.) by someone close to each vertical. Shipping
  guesses-as-defaults for domains we haven't validated would be worse than
  shipping fewer, honestly-scoped profiles. The schema is locked; the
  catalog will fill in as domain reviewers sign off.

## Related ADRs

- [ADR 005 — Profile-driven model stacks](../architecture/decisions/005-profile-driven-model-stacks.md) — this scaffold implements the schema piece.
- [ADR 004 — Multi-model InferenceRouter](../architecture/decisions/004-inference-router-architecture.md) — the consumer that reads ProfilePack to decide which slot serves a request.
- [ADR 006 — Ed25519-signed offline license](../architecture/decisions/006-offline-signed-license.md) — `LicenseClaims.allowed_profiles` will eventually gate which profiles a customer can switch to.
- [ADR 003 — Desktop distribution: Tauri shell](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md) — gates the onboarding picker UI surface.
