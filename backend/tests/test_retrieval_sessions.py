"""Tests for the session-scoped vault-retrieval entry point (ADR 009).

The entry point — ``services.retrieval.sessions.find_earlier_turn_context``
— is the production substrate for the compaction service. It reaches
into ``memory/conversations/`` via ``memory_service.list_notes`` and
returns up to ``top_k`` matches with frontmatter-stripped snippets.

These tests pin:

- Empty / whitespace-only queries return [] without touching the index.
- Failures in ``list_notes`` are caught (vault retrieval must never
  break a chat turn).
- The current session's own conversation note is excluded so the user
  doesn't see "earlier context" that's actually their current turn.
- Frontmatter is stripped from the returned snippet so the substituted
  block reads as prose, not YAML.
- ``top_k`` is honored.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from services.retrieval import sessions


pytestmark = pytest.mark.anyio(backends=["asyncio"])


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture
def vault_factory(monkeypatch):
    """Patch memory_service.list_notes / get_note with deterministic stubs.

    Returns a callable that accepts a list of (path, title, body,
    session_id) tuples and wires both function patches accordingly.
    """
    def _setup(notes: List[Dict[str, Any]]):
        async def _fake_list_notes(*, folder=None, search=None, limit=10, workspace_path=None):
            assert folder == "conversations"
            assert search  # always fed a query
            return [{"path": n["path"]} for n in notes]

        async def _fake_get_note(path: str, *, workspace_path=None):
            for n in notes:
                if n["path"] == path:
                    fm = {"title": n.get("title", ""), "session_id": n.get("session_id", "")}
                    return {
                        "path": n["path"],
                        "title": n.get("title", ""),
                        "content": n.get("body", ""),
                        "frontmatter": fm,
                    }
            raise FileNotFoundError(path)

        monkeypatch.setattr("services.memory_service.list_notes", _fake_list_notes)
        monkeypatch.setattr("services.memory_service.get_note", _fake_get_note)

    return _setup


# ── Empty inputs ──────────────────────────────────────────────────────


async def test_empty_query_returns_empty_list(vault_factory):
    vault_factory([])
    out = await sessions.find_earlier_turn_context("")
    assert out == []


async def test_whitespace_query_returns_empty_list(vault_factory):
    vault_factory([])
    out = await sessions.find_earlier_turn_context("   \n\t")
    assert out == []


async def test_zero_top_k_returns_empty_list(vault_factory):
    vault_factory([
        {"path": "conversations/a.md", "title": "A", "body": "hello", "session_id": "old1"},
    ])
    out = await sessions.find_earlier_turn_context("hello", top_k=0)
    assert out == []


# ── Listing failures degrade gracefully ───────────────────────────────


async def test_list_notes_failure_returns_empty(monkeypatch):
    async def _explode(**kwargs):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr("services.memory_service.list_notes", _explode)
    out = await sessions.find_earlier_turn_context("query")
    assert out == []


async def test_get_note_failure_skips_candidate(monkeypatch):
    """When one of N candidates can't be loaded, the rest still surface."""
    async def _fake_list_notes(*, folder=None, search=None, limit=10, workspace_path=None):
        return [
            {"path": "conversations/missing.md"},
            {"path": "conversations/loaded.md"},
        ]

    async def _fake_get_note(path: str, *, workspace_path=None):
        if path == "conversations/missing.md":
            raise FileNotFoundError(path)
        return {
            "path": path,
            "title": "ok",
            "content": "real body content",
            "frontmatter": {"title": "ok", "session_id": "old1"},
        }

    monkeypatch.setattr("services.memory_service.list_notes", _fake_list_notes)
    monkeypatch.setattr("services.memory_service.get_note", _fake_get_note)
    out = await sessions.find_earlier_turn_context("query")
    assert len(out) == 1
    assert out[0]["path"] == "conversations/loaded.md"


# ── Same-session exclusion ────────────────────────────────────────────


async def test_current_session_note_excluded(vault_factory):
    vault_factory([
        {"path": "conversations/self.md", "title": "Self", "body": "current text", "session_id": "S1"},
        {"path": "conversations/other.md", "title": "Other", "body": "different text", "session_id": "S2"},
    ])
    out = await sessions.find_earlier_turn_context(
        "text", current_session_id="S1", top_k=5,
    )
    paths = [r["path"] for r in out]
    assert "conversations/self.md" not in paths
    assert "conversations/other.md" in paths


async def test_no_session_id_filter_when_not_provided(vault_factory):
    vault_factory([
        {"path": "conversations/a.md", "title": "A", "body": "txt A", "session_id": "S1"},
        {"path": "conversations/b.md", "title": "B", "body": "txt B", "session_id": "S2"},
    ])
    out = await sessions.find_earlier_turn_context("txt", top_k=5)
    assert {r["path"] for r in out} == {"conversations/a.md", "conversations/b.md"}


# ── Frontmatter stripping + snippet shape ─────────────────────────────


async def test_snippet_strips_frontmatter(vault_factory):
    body_with_fm = (
        "---\n"
        "title: X\n"
        "session_id: ABC\n"
        "---\n"
        "Body text after fm"
    )
    vault_factory([
        {"path": "conversations/a.md", "title": "X", "body": body_with_fm, "session_id": "ABC2"},
    ])
    out = await sessions.find_earlier_turn_context("body")
    assert len(out) == 1
    assert out[0]["snippet"].startswith("Body text after fm")
    assert "session_id" not in out[0]["snippet"]


async def test_snippet_handles_body_without_frontmatter(vault_factory):
    vault_factory([
        {"path": "conversations/a.md", "title": "X", "body": "plain body", "session_id": "ABC"},
    ])
    out = await sessions.find_earlier_turn_context("plain")
    assert out[0]["snippet"] == "plain body"


# ── top_k honored ─────────────────────────────────────────────────────


async def test_top_k_honored(vault_factory):
    vault_factory([
        {"path": f"conversations/{i}.md", "title": f"T{i}", "body": f"body {i}", "session_id": ""}
        for i in range(5)
    ])
    out = await sessions.find_earlier_turn_context("body", top_k=2)
    assert len(out) == 2


async def test_list_notes_limit_capped_at_ceiling(monkeypatch):
    """A pathologically large top_k must not translate into an
    unbounded ``limit`` on the FTS query."""
    captured: dict = {}

    async def _capturing_list_notes(*, folder=None, search=None, limit=10, workspace_path=None):
        captured["limit"] = limit
        return []

    monkeypatch.setattr("services.memory_service.list_notes", _capturing_list_notes)
    out = await sessions.find_earlier_turn_context("query", top_k=10_000)
    assert out == []
    # Hard ceiling kicks in (currently 50 in sessions._LIST_NOTES_CEILING).
    assert captured["limit"] <= 50


async def test_returned_dict_shape(vault_factory):
    """Compaction service depends on the {path,title,snippet,session_id} keys."""
    vault_factory([
        {"path": "conversations/x.md", "title": "Hello", "body": "body x", "session_id": "S99"},
    ])
    out = await sessions.find_earlier_turn_context("body")
    assert len(out) == 1
    item = out[0]
    assert set(item.keys()) == {"path", "title", "snippet", "session_id"}
    assert item["title"] == "Hello"
    assert item["session_id"] == "S99"
