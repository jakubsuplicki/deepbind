"""Tests for InferenceRouter, ProfilePack, keep_alive policy, KV-aware footprint.

Covers ADR 004's "Buildable today" surface: classifier, profile-driven dispatch,
catalog footprint accounting, per-slot keep_alive lookup.
"""

import json
import os
import time

import pytest

from services.inference_router import (
    DispatchDecision,
    InferenceRouter,
    classify,
    get_router,
    reset_router_for_tests,
)
from services.ollama_service import (
    DEFAULT_KEEP_ALIVE,
    KEEP_ALIVE_BY_SLOT,
    effective_footprint_bytes,
    get_model_by_id,
    get_model_by_litellm,
    keep_alive_for_slot,
)
from services.profile_service import (
    DEFAULT_PROFILE_ID,
    KNOWN_PLACEHOLDER_MODEL_IDS,
    PROFILE_CATALOG,
    ProfilePack,
    SlotLadder,
    SlotSpec,
    get_active_profile,
    get_profile_by_id,
    invalidate_active_profile_cache,
    set_active_profile,
    validate_profile_catalog,
)


# ── Classifier ──────────────────────────────────────────────────────────────


class TestClassify:
    def test_no_messages_no_tools_is_chat(self):
        assert classify(None, None) == "chat"
        assert classify([], None) == "chat"

    def test_tools_yields_tool(self):
        # Tools dominate even when content has code fences — the audit trail
        # should reflect that the request is fundamentally tool-using.
        msgs = [{"role": "user", "content": "```python\nprint(1)\n```"}]
        assert classify(msgs, [{"name": "search_notes"}]) == "tool"

    def test_code_fence_in_user_message_yields_code(self):
        msgs = [{"role": "user", "content": "fix this:\n```py\nprint(2)\n```"}]
        assert classify(msgs, None) == "code"

    def test_code_fence_in_earlier_message_does_not_count(self):
        # Only the *most recent* user turn drives classification — older
        # turns can have code without making this request a code request.
        msgs = [
            {"role": "user", "content": "```py\nprint(1)\n```"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "thanks"},
        ]
        assert classify(msgs, None) == "chat"

    def test_assistant_message_with_code_is_ignored(self):
        msgs = [
            {"role": "user", "content": "tell me about Python"},
            {"role": "assistant", "content": "```py\nprint(1)\n```"},
        ]
        assert classify(msgs, None) == "chat"

    def test_malformed_messages_dont_crash(self):
        # Defensive: messages list could contain non-dicts in pathological inputs.
        assert classify([None, {"role": "user", "content": "hi"}], None) == "chat"
        assert classify([{"role": "user"}], None) == "chat"  # missing content


# ── Router dispatch ─────────────────────────────────────────────────────────


class TestDispatchCloud:
    def test_anthropic_default(self):
        router = InferenceRouter()
        d = router.dispatch("anthropic", None, None, "chat")
        assert d.provider == "anthropic"
        assert d.model == "claude-sonnet-4-20250514"
        assert d.model_id is None
        assert d.slot_class == "conversational"

    def test_anthropic_passes_through_model(self):
        router = InferenceRouter()
        d = router.dispatch("anthropic", "claude-haiku-4-20250514", None, "chat")
        assert d.model == "claude-haiku-4-20250514"

    def test_none_provider_treated_as_anthropic(self):
        router = InferenceRouter()
        d = router.dispatch(None, None, None, "chat")
        assert d.provider == "anthropic"

    def test_openai_passes_through(self):
        router = InferenceRouter()
        d = router.dispatch("openai", "gpt-4o", None, "chat")
        assert d.provider == "openai"
        assert d.model == "gpt-4o"

    def test_google_uses_default_when_no_model(self):
        router = InferenceRouter()
        d = router.dispatch("google", None, None, "chat")
        assert d.provider == "google"
        assert d.model  # some default


class TestDispatchOllamaUserOverride:
    def test_litellm_model_resolves_to_catalog(self):
        router = InferenceRouter()
        d = router.dispatch("ollama", "ollama_chat/qwen3:8b", None, "chat")
        assert d.provider == "ollama"
        assert d.model == "ollama_chat/qwen3:8b"
        assert d.model_id == "qwen3-8b"
        assert d.slot_class == "conversational"
        assert "qwen3-8b" in d.reason

    def test_catalog_id_resolves_to_litellm(self):
        # Operator-style override using the catalog ID directly.
        router = InferenceRouter()
        d = router.dispatch("ollama", "qwen3-8b", None, "chat")
        assert d.model == "ollama_chat/qwen3:8b"
        assert d.model_id == "qwen3-8b"

    def test_unknown_ollama_tag_passes_through(self):
        # Custom Ollama tags the user set up locally — pass through, model_id
        # is None to signal "not in catalog".
        router = InferenceRouter()
        d = router.dispatch("ollama", "ollama_chat/my-custom:latest", None, "chat")
        assert d.provider == "ollama"
        assert d.model == "ollama_chat/my-custom:latest"
        assert d.model_id is None

    def test_base_url_passes_through(self):
        router = InferenceRouter()
        d = router.dispatch("ollama", "ollama_chat/qwen3:8b", "http://192.168.1.10:11434", "chat")
        # Note: base_url normalisation happens at the Ollama-call edge; the
        # router just propagates whatever it was given so the LLM service
        # can apply its own normalisation.
        assert d.base_url == "http://192.168.1.10:11434"


class TestDispatchOllamaProfileFallback:
    def test_no_override_falls_back_to_profile_chat_slot(self):
        # No model override + no override profile → uses default profile's
        # conversational slot.
        router = InferenceRouter()  # default profile
        d = router.dispatch("ollama", None, None, "chat")
        assert d.provider == "ollama"
        assert d.model_id == "qwen3-8b"  # generic-knowledge-worker chat slot
        assert d.slot_class == "conversational"
        assert "generic-knowledge-worker" in d.reason

    def test_developer_profile_routes_code_to_coder_slot(self):
        # When the active profile has a coder slot AND the request is code-class,
        # the router selects the coder slot. This is the slot-class differentiation
        # that justifies the classifier output.
        dev_profile = get_profile_by_id("developer-devops")
        assert dev_profile is not None
        router = InferenceRouter(profile=dev_profile)
        d = router.dispatch("ollama", None, None, "code")
        assert d.model_id == "devstral-small-2-24b"
        assert d.slot_class == "code"

    def test_patent_profile_drops_code_back_to_chat(self):
        # Patent profile has coder=None; a code-class request still gets a
        # conversational model rather than crashing or refusing.
        patent_profile = get_profile_by_id("patent-prosecutor")
        assert patent_profile is not None
        router = InferenceRouter(profile=patent_profile)
        d = router.dispatch("ollama", None, None, "code")
        assert d.slot_class == "conversational"
        assert d.model_id == "qwen3-8b"

    def test_chat_request_with_dev_profile_uses_chat_slot(self):
        dev_profile = get_profile_by_id("developer-devops")
        router = InferenceRouter(profile=dev_profile)
        d = router.dispatch("ollama", None, None, "chat")
        assert d.model_id == "qwen3-8b"  # dev profile chat slot
        assert d.slot_class == "conversational"


class TestRouterSingleton:
    def test_get_router_returns_same_instance(self):
        reset_router_for_tests()
        a = get_router()
        b = get_router()
        assert a is b

    def test_reset_for_tests_creates_fresh(self):
        reset_router_for_tests()
        a = get_router()
        reset_router_for_tests()
        b = get_router()
        assert a is not b


class TestDispatchDecisionAudit:
    def test_audit_dict_contains_routing_fields(self):
        d = DispatchDecision(
            provider="ollama",
            model="ollama_chat/qwen3:8b",
            base_url=None,
            request_class="chat",
            slot_class="conversational",
            model_id="qwen3-8b",
            reason="test",
        )
        audit = d.to_audit_dict()
        assert audit["provider"] == "ollama"
        assert audit["model_id"] == "qwen3-8b"
        assert audit["request_class"] == "chat"
        assert audit["slot_class"] == "conversational"
        assert audit["reason"] == "test"
        # Wire-format hygiene: no model string, no base_url — those don't
        # belong in a UI audit panel.
        assert "model" not in audit
        assert "base_url" not in audit


# ── Profile catalog ──────────────────────────────────────────────────────────


class TestProfileCatalog:
    def test_default_profile_is_in_catalog(self):
        assert DEFAULT_PROFILE_ID in PROFILE_CATALOG

    def test_three_profiles_in_scaffold(self):
        # Scaffold scope per ADR 005 — three profiles enough to validate
        # the schema. Other six deferred until domain validation.
        assert len(PROFILE_CATALOG) == 3
        assert "generic-knowledge-worker" in PROFILE_CATALOG
        assert "developer-devops" in PROFILE_CATALOG
        assert "patent-prosecutor" in PROFILE_CATALOG

    def test_get_active_profile_falls_back_to_default(self, tmp_path, monkeypatch):
        # When no config file exists, get_active_profile() returns the default.
        from unittest.mock import MagicMock, patch

        invalidate_active_profile_cache()
        with patch("config.get_settings") as mock_settings:
            settings = MagicMock()
            settings.workspace_path = tmp_path
            mock_settings.return_value = settings
            active = get_active_profile()
            assert active.id == DEFAULT_PROFILE_ID

    def test_patent_profile_has_no_coder(self):
        p = get_profile_by_id("patent-prosecutor")
        assert p is not None
        assert p.stack.coder is None  # the whole point of profile-driven stacks

    def test_developer_profile_has_coder_ladder(self):
        p = get_profile_by_id("developer-devops")
        assert p is not None
        assert p.stack.coder is not None
        assert p.stack.coder.preferred.model_id == "devstral-small-2-24b"
        # Downgrade ladder must reference real models so the router doesn't
        # crash when walking it.
        for spec in p.stack.coder.downgrade_ladder:
            assert isinstance(spec, SlotSpec)


# ── Active profile resolution from config file ──────────────────────────────


class TestActiveProfileFromConfig:
    """Cover the load-bearing path: get_active_profile reads
    `app/config.json:active_profile_id` and the router picks it up."""

    @pytest.fixture(autouse=True)
    def _drop_cache(self):
        # Cache spans test boundaries because module-level state — tests must
        # invalidate explicitly so prior test config files don't bleed in.
        invalidate_active_profile_cache()
        yield
        invalidate_active_profile_cache()

    def _patch_settings(self, monkeypatch, tmp_path):
        from unittest.mock import MagicMock
        settings = MagicMock()
        settings.workspace_path = tmp_path
        monkeypatch.setattr("config.get_settings", lambda: settings)
        (tmp_path / "app").mkdir(exist_ok=True)
        return tmp_path / "app" / "config.json"

    def test_reads_active_profile_id_from_config(self, tmp_path, monkeypatch):
        config_path = self._patch_settings(monkeypatch, tmp_path)
        config_path.write_text(json.dumps({"active_profile_id": "developer-devops"}))
        assert get_active_profile().id == "developer-devops"

    def test_unknown_profile_id_falls_back_to_default(self, tmp_path, monkeypatch):
        # Stale config from a removed profile shouldn't crash the dispatcher.
        config_path = self._patch_settings(monkeypatch, tmp_path)
        config_path.write_text(json.dumps({"active_profile_id": "removed-profile"}))
        assert get_active_profile().id == DEFAULT_PROFILE_ID

    def test_malformed_json_falls_back_to_default(self, tmp_path, monkeypatch):
        config_path = self._patch_settings(monkeypatch, tmp_path)
        config_path.write_text("{not valid json")
        assert get_active_profile().id == DEFAULT_PROFILE_ID

    def test_mtime_cache_avoids_repeated_reads(self, tmp_path, monkeypatch):
        # The dispatcher hits this path on every WS message. The cache must
        # avoid re-reading the file when nothing has changed.
        from unittest.mock import patch
        config_path = self._patch_settings(monkeypatch, tmp_path)
        config_path.write_text(json.dumps({"active_profile_id": "developer-devops"}))

        # Prime the cache.
        assert get_active_profile().id == "developer-devops"

        # Subsequent calls should not re-read the file. Spy on the parser
        # to count actual reads.
        with patch("services.profile_service._resolve_active_profile_from") as spy:
            for _ in range(5):
                profile = get_active_profile()
            assert profile.id == "developer-devops"
            assert spy.call_count == 0, "cache should suppress repeat reads"

    def test_mtime_change_invalidates_cache(self, tmp_path, monkeypatch):
        config_path = self._patch_settings(monkeypatch, tmp_path)
        config_path.write_text(json.dumps({"active_profile_id": "developer-devops"}))
        assert get_active_profile().id == "developer-devops"

        # Rewrite + bump mtime forward. Some filesystems have second-resolution
        # mtime, so we explicitly set mtime to "old + 1s" to guarantee the
        # cache check sees a different value even if the rewrite happens within
        # the same filesystem-resolution window.
        old_mtime = config_path.stat().st_mtime
        config_path.write_text(json.dumps({"active_profile_id": "patent-prosecutor"}))
        new_mtime = max(config_path.stat().st_mtime, old_mtime + 1)
        os.utime(config_path, (new_mtime, new_mtime))
        assert get_active_profile().id == "patent-prosecutor"


# ── set_active_profile persistence + atomic write ────────────────────────────


class TestSetActiveProfile:
    @pytest.fixture(autouse=True)
    def _drop_cache(self):
        invalidate_active_profile_cache()
        yield
        invalidate_active_profile_cache()

    def _patch_settings(self, monkeypatch, tmp_path):
        from unittest.mock import MagicMock
        settings = MagicMock()
        settings.workspace_path = tmp_path
        monkeypatch.setattr("config.get_settings", lambda: settings)
        return tmp_path / "app" / "config.json"

    def test_persists_active_profile_id(self, tmp_path, monkeypatch):
        config_path = self._patch_settings(monkeypatch, tmp_path)
        set_active_profile("developer-devops")
        config = json.loads(config_path.read_text())
        assert config["active_profile_id"] == "developer-devops"

    def test_round_trips_through_get_active_profile(self, tmp_path, monkeypatch):
        self._patch_settings(monkeypatch, tmp_path)
        set_active_profile("patent-prosecutor")
        assert get_active_profile().id == "patent-prosecutor"

    def test_invalidates_cache(self, tmp_path, monkeypatch):
        self._patch_settings(monkeypatch, tmp_path)
        set_active_profile("developer-devops")
        assert get_active_profile().id == "developer-devops"
        # set_active_profile must invalidate so the next read picks up the
        # new value rather than serving the stale cache.
        set_active_profile("patent-prosecutor")
        assert get_active_profile().id == "patent-prosecutor"

    def test_rejects_unknown_profile_id(self, tmp_path, monkeypatch):
        self._patch_settings(monkeypatch, tmp_path)
        with pytest.raises(ValueError, match="Unknown profile_id"):
            set_active_profile("does-not-exist")

    def test_preserves_unrelated_config_keys(self, tmp_path, monkeypatch):
        # The function does read-modify-write of the whole config; an atomic
        # write must not lose keys it didn't touch.
        config_path = self._patch_settings(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({
            "active_profile_id": "generic-knowledge-worker",
            "local_model": {"active": True, "model_id": "qwen3-8b"},
            "some_other_key": "preserve me",
        }))
        set_active_profile("developer-devops")
        config = json.loads(config_path.read_text())
        assert config["active_profile_id"] == "developer-devops"
        assert config["local_model"]["model_id"] == "qwen3-8b"
        assert config["some_other_key"] == "preserve me"

    def test_atomic_write_no_tmp_left_behind(self, tmp_path, monkeypatch):
        config_path = self._patch_settings(monkeypatch, tmp_path)
        set_active_profile("developer-devops")
        # _atomic_write_json writes config.json.tmp then os.replace's it.
        # After a successful call the tmp file should not exist.
        assert not config_path.with_suffix(".json.tmp").exists()


# ── Profile catalog validation ───────────────────────────────────────────────


class TestValidateProfileCatalog:
    def test_no_unresolved_model_ids_in_scaffold(self):
        """Every profile's stack must reference either a catalog entry or a
        known placeholder. A real typo (`devstal` vs `devstral`) would trip
        this in CI rather than silently degrading to qwen3-8b at runtime.
        """
        unresolved = validate_profile_catalog()
        assert unresolved == {}, f"profiles with unresolved model_ids: {unresolved}"

    def test_known_placeholders_are_listed(self):
        # If we add a new placeholder, this test reminds us to register it.
        # Placeholder set is intentionally small (3 entries) and grows only
        # when a profile field references a not-yet-wired slot consumer.
        assert "qwen3-embedding-0.6b" in KNOWN_PLACEHOLDER_MODEL_IDS
        assert "kokoro-82m" in KNOWN_PLACEHOLDER_MODEL_IDS
        assert "granite-vision-3-2b" in KNOWN_PLACEHOLDER_MODEL_IDS


# ── Schema-vs-walker drift defense ───────────────────────────────────────────


class TestWalkSlotSpecs:
    """Defends against the silent-drift bug where someone adds a new optional
    slot to ProfileStack and forgets to update _walk_slot_specs. Without these
    checks, the new slot's model_id would silently escape `validate_profile_catalog`.
    """

    def _populated_stack(self):
        from services.profile_service import ProfileStack, SlotLadder, SlotSpec
        return ProfileStack(
            embeddings=SlotSpec(model_id="emb"),
            plumbing=SlotSpec(model_id="plumb"),
            conversational=SlotLadder(
                preferred=SlotSpec(model_id="conv-pref"),
                downgrade_ladder=[SlotSpec(model_id="conv-rung1")],
            ),
            reasoning=SlotSpec(model_id="reason"),
            coder=SlotLadder(
                preferred=SlotSpec(model_id="code-pref"),
                downgrade_ladder=[SlotSpec(model_id="code-rung1")],
            ),
            vision=SlotSpec(model_id="vision"),
            long_context=SlotSpec(model_id="long"),
            tts=SlotSpec(model_id="tts"),
        )

    def test_yields_every_direct_slot_and_ladder_rung(self):
        from services.profile_service import _walk_slot_specs
        yielded_ids = {spec.model_id for spec in _walk_slot_specs(self._populated_stack())}
        assert yielded_ids == {
            "emb", "plumb",
            "conv-pref", "conv-rung1",
            "reason",
            "code-pref", "code-rung1",
            "vision", "long", "tts",
        }

    def test_profile_stack_field_set_is_explicit(self):
        # If someone adds a new field to ProfileStack (e.g. `audio`), this
        # assertion fails — pointing the author at _walk_slot_specs and this
        # test as the two places to update so the new slot doesn't silently
        # escape validation.
        from services.profile_service import ProfileStack
        assert set(ProfileStack.model_fields.keys()) == {
            "embeddings", "plumbing", "conversational", "reasoning",
            "coder", "vision", "long_context", "tts",
        }

    def test_optional_slots_unset_are_skipped(self):
        from services.profile_service import _walk_slot_specs, ProfileStack, SlotLadder, SlotSpec
        # Patent-prosecutor-shape: no coder, has vision.
        stack = ProfileStack(
            embeddings=SlotSpec(model_id="emb"),
            plumbing=SlotSpec(model_id="plumb"),
            conversational=SlotLadder(preferred=SlotSpec(model_id="conv")),
            reasoning=SlotSpec(model_id="reason"),
            coder=None,
            vision=SlotSpec(model_id="vision"),
            long_context=None,
            tts=None,
        )
        yielded_ids = {spec.model_id for spec in _walk_slot_specs(stack)}
        assert yielded_ids == {"emb", "plumb", "conv", "reason", "vision"}


# ── Shared config IO (services/_config_io.py) ────────────────────────────────


class TestLockedConfigUpdate:
    """`locked_config_update` is the cross-writer serialization point used by
    `set_active_profile` and `set_active_local_model`. Tests cover the
    skip-on-unchanged optimisation and the read-modify-write atomicity.
    """

    def test_round_trip_writes_changes(self, tmp_path):
        from services._config_io import locked_config_update
        config_path = tmp_path / "config.json"
        with locked_config_update(config_path) as config:
            config["active_profile_id"] = "developer-devops"
            config["other_key"] = 42
        loaded = json.loads(config_path.read_text())
        assert loaded == {"active_profile_id": "developer-devops", "other_key": 42}

    def test_skip_on_unchanged_does_not_bump_mtime(self, tmp_path):
        # mtime stability is load-bearing for the get_active_profile cache —
        # a no-op write that bumps mtime would invalidate every worker's cache
        # and force a re-read. The skip-on-unchanged optimisation prevents that.
        from services._config_io import locked_config_update
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"active_profile_id": "developer-devops"}))
        before_mtime = config_path.stat().st_mtime
        time.sleep(0.05)
        with locked_config_update(config_path) as config:
            # Read the value but don't change it.
            _ = config["active_profile_id"]
        assert config_path.stat().st_mtime == before_mtime

    def test_corrupt_existing_config_replaced_with_valid(self, tmp_path):
        # If config.json is corrupt, locked_config_update treats it as empty
        # and the atomic write replaces it with valid JSON containing the
        # caller's update — the corruption isn't propagated.
        from services._config_io import locked_config_update
        config_path = tmp_path / "config.json"
        config_path.write_text("{not valid json")
        with locked_config_update(config_path) as config:
            config["recovered"] = True
        loaded = json.loads(config_path.read_text())
        assert loaded == {"recovered": True}

    def test_exception_in_block_aborts_write(self, tmp_path):
        # If the caller's block raises, the file is not modified — important
        # for invariant preservation when set_active_profile() validates and
        # raises on bad input.
        from services._config_io import locked_config_update
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"active_profile_id": "developer-devops"}))
        with pytest.raises(RuntimeError, match="boom"):
            with locked_config_update(config_path) as config:
                config["active_profile_id"] = "patent-prosecutor"
                raise RuntimeError("boom")
        # Original content preserved.
        assert json.loads(config_path.read_text()) == {"active_profile_id": "developer-devops"}

    def test_concurrent_writers_do_not_lose_updates(self, tmp_path):
        # Two threads racing on the same config — the lock ensures both
        # updates land. This is the lost-update protection the helper exists
        # to provide.
        import threading
        from services._config_io import locked_config_update
        config_path = tmp_path / "config.json"

        def worker(key, value):
            with locked_config_update(config_path) as config:
                # Sleep inside the lock to widen the race window.
                time.sleep(0.05)
                config[key] = value

        threads = [
            threading.Thread(target=worker, args=("active_profile_id", "developer-devops")),
            threading.Thread(target=worker, args=("local_model", {"active": True, "model_id": "qwen3-8b"})),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        loaded = json.loads(config_path.read_text())
        # Both writes survived — neither was clobbered by the other's
        # read-modify-write cycle.
        assert loaded["active_profile_id"] == "developer-devops"
        assert loaded["local_model"] == {"active": True, "model_id": "qwen3-8b"}


# ── Catalog footprint accounting ─────────────────────────────────────────────


class TestEffectiveFootprint:
    def test_zero_context_returns_weights_only(self):
        entry = get_model_by_id("qwen3-8b")
        fp = effective_footprint_bytes(entry, 0)
        # 5.2 GB weights = 5.2 * 1024^3 bytes
        assert fp == int(5.2 * (1024 ** 3))

    def test_kv_grows_with_context(self):
        entry = get_model_by_id("qwen3-8b")
        fp_short = effective_footprint_bytes(entry, 1000)
        fp_long = effective_footprint_bytes(entry, 10000)
        # 9000 extra tokens × 4096 bytes/token = ~36 MB delta
        assert fp_long - fp_short == 9000 * entry.bytes_per_kv_token

    def test_mamba_kv_is_far_smaller_than_transformer(self):
        # Granite 4 H-Tiny is hybrid mamba — KV per token should be much
        # smaller than a similarly-sized transformer. This is the architectural
        # difference the dispatcher's footprint accounting needs to honour.
        granite = get_model_by_id("granite-4-h-tiny")
        qwen = get_model_by_id("qwen3-8b")
        assert granite is not None and qwen is not None
        assert granite.bytes_per_kv_token < qwen.bytes_per_kv_token / 4

    def test_swa_kv_is_smaller_than_full_transformer(self):
        # Gemma 4 E4B uses sliding-window attention — only the window's worth
        # of KV is stored, so per-token cost is materially lower than a
        # transformer of similar size.
        gemma = get_model_by_id("gemma4-e4b")
        qwen = get_model_by_id("qwen3-8b")
        assert gemma.bytes_per_kv_token < qwen.bytes_per_kv_token

    def test_negative_context_clamped_to_zero(self):
        # Defensive: a negative ctx_len is a programming error but we don't
        # want it to produce a smaller-than-weights footprint.
        entry = get_model_by_id("qwen3-8b")
        fp = effective_footprint_bytes(entry, -1)
        assert fp == int(5.2 * (1024 ** 3))


# ── Catalog field shape ──────────────────────────────────────────────────────


class TestCatalogADR004Fields:
    def test_every_entry_has_attention_arch(self):
        from services.ollama_service import MODEL_CATALOG
        for entry in MODEL_CATALOG:
            assert entry.attention_arch in ("transformer", "mamba", "swa"), entry.id

    def test_every_entry_has_slot_class(self):
        from services.ollama_service import MODEL_CATALOG
        valid = {
            "conversational", "long_context", "reasoning", "code",
            "best_local", "plumbing", "embedding", "vision",
        }
        for entry in MODEL_CATALOG:
            assert entry.slot_class in valid, f"{entry.id} has invalid slot_class {entry.slot_class}"

    def test_granite_entries_are_mamba(self):
        from services.ollama_service import MODEL_CATALOG
        for entry in MODEL_CATALOG:
            if entry.id.startswith("granite-4"):
                assert entry.attention_arch == "mamba"
                assert entry.slot_class == "plumbing"

    def test_gemma_uses_swa(self):
        gemma_e4b = get_model_by_id("gemma4-e4b")
        gemma_26b = get_model_by_id("gemma4-26b-a4b")
        assert gemma_e4b.attention_arch == "swa"
        assert gemma_26b.attention_arch == "swa"


# ── Keep-alive policy ────────────────────────────────────────────────────────


class TestKeepAlivePolicy:
    def test_plumbing_is_forever(self):
        # Always-resident per ADR 004 §"keep_alive policy table".
        assert keep_alive_for_slot("plumbing") == "-1"

    def test_embedding_is_forever(self):
        assert keep_alive_for_slot("embedding") == "-1"

    def test_conversational_is_thirty_minutes(self):
        # Behavior preservation — today's hard-coded value was "30m".
        assert keep_alive_for_slot("conversational") == "30m"

    def test_code_evicts_aggressively(self):
        # On-demand slot — should evict earlier than chat to free unified
        # memory for the next request class.
        assert keep_alive_for_slot("code") == "5m"

    def test_unknown_slot_returns_default(self):
        assert keep_alive_for_slot("nonsense") == DEFAULT_KEEP_ALIVE

    def test_none_slot_returns_default(self):
        assert keep_alive_for_slot(None) == DEFAULT_KEEP_ALIVE

    def test_policy_table_covers_all_catalog_slots(self):
        # Every slot_class used in the catalog must have a keep_alive policy.
        # If someone adds a new slot_class to ModelCatalogEntry without
        # updating this table, this test fails — explicit, not silent.
        from services.ollama_service import MODEL_CATALOG
        for entry in MODEL_CATALOG:
            assert entry.slot_class in KEEP_ALIVE_BY_SLOT, \
                f"slot_class {entry.slot_class!r} missing from KEEP_ALIVE_BY_SLOT"


# ── get_model_by_litellm helper ──────────────────────────────────────────────


class TestGetModelByLitellm:
    def test_resolves_known_model(self):
        entry = get_model_by_litellm("ollama_chat/qwen3:8b")
        assert entry is not None
        assert entry.id == "qwen3-8b"

    def test_unknown_returns_none(self):
        assert get_model_by_litellm("ollama_chat/nonsense:0") is None

    def test_empty_returns_none(self):
        assert get_model_by_litellm("") is None
        assert get_model_by_litellm(None) is None
