"""Step 28a — Retrieval trace plumbing.

Verifies that ``build_context`` and ``build_graph_scoped_context`` return a
structured per-note trace alongside their context strings, and that the
chat WebSocket emits a ``trace`` event before ``done`` when context is
non-empty.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.anyio(backends=["asyncio"])


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def isolate_workspace(tmp_path, monkeypatch):
    settings = MagicMock()
    settings.workspace_path = tmp_path
    for mod in [
        "services.session_service", "services.memory_service",
        "services.graph_service", "services.context_builder",
        "services.preference_service", "services.token_tracking",
        "services.workspace_service",
    ]:
        try:
            monkeypatch.setattr(f"{mod}.get_settings", lambda: settings)
        except AttributeError:
            pass
    for d in ["app", "app/sessions", "memory", "memory/inbox",
              "memory/preferences", "graph"]:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    import sqlite3
    from models.database import SCHEMA_SQL, FTS_SQL, TRIGGER_SQL
    with sqlite3.connect(str(tmp_path / "app" / "jarvis.db")) as conn:
        conn.executescript(SCHEMA_SQL + FTS_SQL + TRIGGER_SQL)


@pytest.mark.anyio
async def test_build_context_trace_contains_primary_candidates(monkeypatch):
    """Each retrieval result that ends up in the prompt produces a primary trace entry."""
    from services import context_builder

    fake_results = [
        {
            "path": "inbox/alpha.md",
            "title": "Alpha",
            "_score": 0.81,
            "_signals": {"bm25": 0.42, "cosine": 0.81, "graph": 0.30},
            "_best_chunk": "alpha body",
        },
        {
            "path": "inbox/beta.md",
            "title": "Beta",
            "_score": 0.55,
            "_signals": {"bm25": 0.55, "cosine": 0.40, "graph": 0.10},
            "_best_chunk": "beta body",
        },
    ]

    monkeypatch.setattr(
        "services.context_builder.retrieval.retrieve",
        AsyncMock(return_value=fake_results),
    )
    # Avoid any preference / specialist injection noise.
    monkeypatch.setattr(
        "services.context_builder.preference_service.format_for_prompt",
        lambda _ws=None: None,
    )
    monkeypatch.setattr(
        "services.specialist_service.get_active_specialists",
        lambda: [],
    )
    # Disable expansion — graph isn't loaded in this test.
    monkeypatch.setattr(
        "services.context_builder._build_expansion_context",
        AsyncMock(return_value=([], [])),
    )

    text, tokens, trace = await context_builder.build_context("alpha beta")

    assert text and "<retrieved_note" in text
    assert tokens > 0
    paths = [t["path"] for t in trace]
    assert "inbox/alpha.md" in paths
    assert "inbox/beta.md" in paths
    alpha = next(t for t in trace if t["path"] == "inbox/alpha.md")
    assert alpha["reason"] == "primary"
    assert alpha["via"] == "cosine"  # dominant signal
    assert alpha["score"] == 0.81
    assert alpha["signals"]["bm25"] == 0.42


@pytest.mark.anyio
async def test_build_context_trace_distinguishes_expansion(monkeypatch):
    """Expansion entries carry reason=expansion + edge_type + tier."""
    from services import context_builder

    primary = [{
        "path": "inbox/alpha.md",
        "title": "Alpha",
        "_score": 0.6,
        "_signals": {"bm25": 0.6},
        "_best_chunk": "alpha body",
    }]
    expansion_trace = [{
        "path": "inbox/related.md",
        "title": "Related",
        "score": 0.45,
        "reason": "expansion",
        "via": "graph",
        "edge_type": "related",
        "tier": "strong",
        "signals": {},
    }]

    monkeypatch.setattr(
        "services.context_builder.retrieval.retrieve",
        AsyncMock(return_value=primary),
    )
    monkeypatch.setattr(
        "services.context_builder.preference_service.format_for_prompt",
        lambda _ws=None: None,
    )
    monkeypatch.setattr(
        "services.specialist_service.get_active_specialists",
        lambda: [],
    )
    monkeypatch.setattr(
        "services.context_builder._build_expansion_context",
        AsyncMock(return_value=(["<expansion_note path=\"inbox/related.md\">x</expansion_note>"], expansion_trace)),
    )

    _text, _tokens, trace = await context_builder.build_context("alpha")

    reasons = {t["reason"] for t in trace}
    assert reasons == {"primary", "expansion"}
    related = next(t for t in trace if t["path"] == "inbox/related.md")
    assert related["edge_type"] == "related"
    assert related["tier"] == "strong"
    assert related["via"] == "graph"


@pytest.mark.anyio
async def test_build_context_trace_empty_when_no_results(monkeypatch):
    from services import context_builder

    monkeypatch.setattr(
        "services.context_builder.retrieval.retrieve",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "services.context_builder.preference_service.format_for_prompt",
        lambda _ws=None: None,
    )
    monkeypatch.setattr(
        "services.specialist_service.get_active_specialists",
        lambda: [],
    )
    monkeypatch.setattr(
        "services.context_builder._build_expansion_context",
        AsyncMock(return_value=([], [])),
    )

    text, tokens, trace = await context_builder.build_context("nothing here")

    assert text is None
    assert tokens == 0
    assert trace == []


@pytest.mark.anyio
async def test_import_scoped_context_uses_hybrid_allowlist(monkeypatch):
    """Import-scoped questions use hybrid scoring while staying inside the batch."""
    from services import context_builder

    batch = SimpleNamespace(
        batch_id="import_1",
        source_display_name="Client Demo",
        files=[
            SimpleNamespace(
                status="done",
                note_paths=[
                    "imports/client/first.md",
                    "imports/client/second.md",
                ],
            ),
            SimpleNamespace(status="skipped", note_paths=["imports/client/skipped.md"]),
        ],
    )

    async def _batch_summary(batch_id, workspace_path=None):
        assert batch_id == "import_1"
        return batch

    calls = {}

    async def _retrieve(
        query,
        limit=5,
        workspace_path=None,
        path_allowlist=None,
        deduplicate_folders=True,
    ):
        calls["query"] = query
        calls["limit"] = limit
        calls["path_allowlist"] = path_allowlist
        calls["deduplicate_folders"] = deduplicate_folders
        return [
            {
                "path": "imports/client/second.md",
                "title": "Second",
                "_score": 0.92,
                "_signals": {"bm25": 0.2, "cosine": 0.92, "graph": 0.1},
                "_best_chunk": "semantic match from the second document",
            },
            {
                "path": "inbox/unrelated.md",
                "title": "Unrelated",
                "_score": 1.0,
                "_signals": {"bm25": 1.0},
                "_best_chunk": "must not leak",
            },
            {
                "path": "imports/client/first.md",
                "title": "First",
                "_score": 0.5,
                "_signals": {"bm25": 0.5, "cosine": 0.0, "graph": 0.0},
                "_best_chunk": "keyword match from the first document",
            },
        ]

    async def _get_note(path, workspace_path=None):
        relpath = "second.md" if path.endswith("second.md") else "first.md"
        title = "Second" if path.endswith("second.md") else "First"
        return {
            "title": title,
            "content": (
                "---\n"
                f"title: {title}\n"
                f"source_relpath: {relpath}\n"
                "---\n\n"
                f"{title} body"
            ),
        }

    monkeypatch.setattr(
        "services.source_import.manifest.get_batch_summary",
        _batch_summary,
    )
    monkeypatch.setattr("services.context_builder.retrieval.retrieve", _retrieve)
    monkeypatch.setattr("services.context_builder.memory_service.get_note", _get_note)

    text, trace = await context_builder._build_import_scoped_context(
        "import_1",
        "Which budget item matters?",
    )

    assert calls["path_allowlist"] == {
        "imports/client/first.md",
        "imports/client/second.md",
    }
    assert calls["deduplicate_folders"] is False
    assert text and "must not leak" not in text
    assert text.index('source_relpath="second.md"') < text.index('source_relpath="first.md"')
    assert trace[0]["path"] == "imports/client/second.md"
    assert trace[0]["via"] == "import_batch"
    assert trace[0]["signals"]["cosine"] == 0.92
    assert trace[0]["signals"]["import_scope"] == 1.0


@pytest.mark.anyio
async def test_chat_ws_emits_trace_event_before_done(monkeypatch):
    """When build_system_prompt_with_stats returns a trace, the WS surfaces it
    as a `trace` event positioned right before `done`."""
    from starlette.testclient import TestClient

    from main import app
    from services.system_prompt import StreamEvent

    fake_trace = [{
        "path": "inbox/alpha.md",
        "title": "Alpha",
        "score": 0.81,
        "reason": "primary",
        "via": "cosine",
        "edge_type": None,
        "tier": None,
        "signals": {"bm25": 0.42, "cosine": 0.81, "graph": 0.30},
    }]

    async def _fake_stats(*args, **kwargs):
        return "SYSTEM PROMPT", {
            "base_tokens": 10, "context_tokens": 5,
            "lang_tokens": 2, "total_tokens": 17,
            "trace": fake_trace,
        }
    monkeypatch.setattr(
        "routers.chat.build_system_prompt_with_stats", _fake_stats,
    )

    with patch("routers.chat.get_api_key", return_value="sk-ant-test"), \
         patch("routers.chat.OllamaDispatcher") as mock_cls:
        async def _gen(**kwargs):
            yield StreamEvent(type="text_delta", content="ok")
        mock_cls.return_value.stream_response = _gen

        with TestClient(app) as client:
            with client.websocket_connect("/api/chat/ws") as ws:
                ws.receive_json()  # session_start
                ws.send_json({"content": "hi"})

                events = []
                while True:
                    msg = ws.receive_json()
                    events.append(msg)
                    if msg["type"] == "done":
                        break

    types = [e["type"] for e in events]
    assert "trace" in types
    trace_idx = types.index("trace")
    done_idx = types.index("done")
    assert trace_idx < done_idx, "trace event must arrive before done"

    trace_event = events[trace_idx]
    assert trace_event["items"] == fake_trace
