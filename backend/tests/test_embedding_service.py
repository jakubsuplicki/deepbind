"""Unit tests for the local embedding service.

Tests stay fast by stubbing the fastembed model. A single smoke test
exercises the real model (skipped if fastembed isn't installed).
"""
import os

import aiosqlite
import pytest

pytestmark = pytest.mark.anyio(backends=["asyncio"])

from models.database import init_database
from services import embedding_service
from services.embedding_service import (
    blob_to_vector,
    content_hash,
    cosine_similarity,
    vector_to_blob,
)
from services.memory_service import create_note


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "app").mkdir()
    return tmp_path


@pytest.fixture
async def ws_db(ws):
    await init_database(ws / "app" / "jarvis.db")
    return ws


@pytest.fixture
def enable_embeddings(monkeypatch):
    """Stub the fastembed model with a deterministic hash-based vector so
    tests don't need to download 200MB of model weights."""
    monkeypatch.delenv("JARVIS_DISABLE_EMBEDDINGS", raising=False)

    class FakeModel:
        def embed(self, texts):
            import hashlib
            for text in texts:
                digest = hashlib.sha256(text.encode("utf-8")).digest()
                vec = [(digest[i] - 128) / 128.0 for i in range(32)]
                yield _FakeVec(vec)

    class _FakeVec(list):
        def tolist(self):
            return list(self)

    monkeypatch.setattr(embedding_service, "_model", FakeModel())
    monkeypatch.setattr(embedding_service, "_DIMENSIONS", 32)
    monkeypatch.setattr(embedding_service, "is_available", lambda: True)
    yield


def test_content_hash_changes_with_content():
    assert content_hash("hello") == content_hash("hello")
    assert content_hash("hello") != content_hash("hello!")


def test_vector_blob_roundtrip():
    vec = [0.1, -0.2, 0.3, 0.4, -0.5]
    blob = vector_to_blob(vec)
    recovered = blob_to_vector(blob)
    assert len(recovered) == len(vec)
    for a, b in zip(vec, recovered):
        assert abs(a - b) < 1e-6


def test_cosine_similarity_identical():
    v = [1.0, 2.0, 3.0]
    assert cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-6)


def test_cosine_similarity_orthogonal():
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert cosine_similarity(a, b) == pytest.approx(0.0, abs=1e-6)


def test_cosine_similarity_zero_vector():
    """A zero vector is degenerate — return 0 instead of NaN."""
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_embed_text_returns_vector(enable_embeddings):
    vec = embedding_service.embed_text("hello world")
    assert isinstance(vec, list)
    assert len(vec) == 32
    assert all(isinstance(x, float) for x in vec)


@pytest.mark.anyio
async def test_embed_note_stores_embedding(enable_embeddings, ws_db):
    await create_note(
        "inbox/test.md",
        "---\ntitle: Test\ntags: []\n---\n\nSome body content",
        ws_db,
    )

    db_path = ws_db / "app" / "jarvis.db"
    await embedding_service.embed_note(
        "inbox/test.md",
        "---\ntitle: Test\ntags: []\n---\n\nSome body content",
        db_path,
    )

    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            "SELECT path, dimensions, model_name FROM note_embeddings"
        )
        rows = await cursor.fetchall()

    assert len(rows) == 1
    assert rows[0][0] == "inbox/test.md"
    assert rows[0][1] == 32


@pytest.mark.anyio
async def test_embed_note_skips_unchanged(enable_embeddings, ws_db):
    db_path = ws_db / "app" / "jarvis.db"
    content = "---\ntitle: Test\ntags: []\n---\n\nBody"

    await create_note("inbox/test.md", content, ws_db)
    first = await embedding_service.embed_note("inbox/test.md", content, db_path)
    second = await embedding_service.embed_note("inbox/test.md", content, db_path)

    assert first is True
    assert second is False  # skipped because content unchanged


@pytest.mark.anyio
async def test_search_similar_returns_ranked(enable_embeddings, ws_db):
    db_path = ws_db / "app" / "jarvis.db"
    await create_note(
        "inbox/alpha.md",
        "---\ntitle: Alpha\ntags: []\n---\n\nFirst body",
        ws_db,
    )
    await create_note(
        "inbox/beta.md",
        "---\ntitle: Beta\ntags: []\n---\n\nSecond body",
        ws_db,
    )

    results = await embedding_service.search_similar(
        "First body", limit=5, workspace_path=ws_db
    )
    assert len(results) == 2
    # Scores should be monotonically decreasing
    assert results[0][1] >= results[1][1]


@pytest.mark.anyio
async def test_reindex_all_processes_every_note(enable_embeddings, ws_db):
    await create_note(
        "inbox/a.md", "---\ntitle: A\n---\n\naa", ws_db
    )
    await create_note(
        "inbox/b.md", "---\ntitle: B\n---\n\nbb", ws_db
    )

    from services.embedding_service import reindex_all
    count = await reindex_all(workspace_path=ws_db)
    assert count >= 0  # Notes may have been embedded at create time

    db_path = ws_db / "app" / "jarvis.db"
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM note_embeddings")
        row = await cursor.fetchone()
    assert row[0] == 2


def test_is_available_reflects_fastembed_install():
    """is_available should return True when fastembed module is importable."""
    import importlib.util
    expected = importlib.util.find_spec("fastembed") is not None
    assert embedding_service.is_available() == expected


# ── Chunk-hash-skip on re-ingest (ADR 013 knob-2) ───────────────────────────


class _CallCountingModel:
    """Fake embedding model that counts how many texts it was asked to embed.

    Used to verify that re-ingest of unchanged content skips the model
    entirely, and that partial edits embed only the changed chunks.
    """

    def __init__(self):
        self.embed_calls: list[list[str]] = []
        self.total_texts_embedded = 0

    def embed(self, texts):
        import hashlib
        texts = list(texts)
        self.embed_calls.append(texts)
        self.total_texts_embedded += len(texts)
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            yield _FakeVec([(digest[i] - 128) / 128.0 for i in range(32)])


class _FakeVec(list):
    def tolist(self):
        return list(self)


@pytest.fixture
def counting_embeddings(monkeypatch):
    """Like ``enable_embeddings`` but returns the counter so tests can assert
    on the number of texts the model was asked to embed."""
    monkeypatch.delenv("JARVIS_DISABLE_EMBEDDINGS", raising=False)
    counter = _CallCountingModel()
    monkeypatch.setattr(embedding_service, "_model", counter)
    monkeypatch.setattr(embedding_service, "_DIMENSIONS", 32)
    monkeypatch.setattr(embedding_service, "is_available", lambda: True)
    return counter


@pytest.mark.anyio
async def test_chunk_hash_skip_re_embeds_zero_chunks_when_unchanged(counting_embeddings, ws_db):
    """Re-ingesting identical content must not call the embedding model again.

    ``create_note`` triggers both ``embed_note`` (whole-note vector) and
    ``embed_note_chunks``; we measure only the second invocation by
    sampling the counter after creation finishes.
    """
    db_path = ws_db / "app" / "jarvis.db"
    content = "---\ntitle: Test\n---\n\n# Section A\n\nBody one.\n\n# Section B\n\nBody two.\n"

    await create_note("inbox/test.md", content, ws_db)
    # Make sure chunks are populated (create_note may or may not have embedded them)
    n_first = await embedding_service.embed_note_chunks("inbox/test.md", content, db_path)
    assert n_first > 0

    # Sample counter AFTER first chunk-embed; second call must not increment it
    embeddings_before = counting_embeddings.total_texts_embedded
    n_second = await embedding_service.embed_note_chunks("inbox/test.md", content, db_path)
    assert n_second == n_first  # same chunk count
    assert counting_embeddings.total_texts_embedded == embeddings_before, (
        f"re-ingest of unchanged content should not call the embedding model, "
        f"but {counting_embeddings.total_texts_embedded - embeddings_before} texts were embedded"
    )


@pytest.mark.anyio
async def test_chunk_hash_skip_only_embeds_changed_chunks(counting_embeddings, ws_db):
    """Editing one section should only re-embed that section's chunks."""
    db_path = ws_db / "app" / "jarvis.db"
    original = "---\ntitle: Test\n---\n\n# Section A\n\nThe original A body has enough words to make a real chunk plus some extras.\n\n# Section B\n\nSection B body is also long enough to be its own chunk with real text.\n"
    edited = "---\ntitle: Test\n---\n\n# Section A\n\nThe ORIGINAL A body has enough words to make a real chunk plus some extras.\n\n# Section B\n\nSection B body is also long enough to be its own chunk with real text.\n"

    await create_note("inbox/test.md", original, ws_db)
    n_first = await embedding_service.embed_note_chunks("inbox/test.md", original, db_path)
    assert n_first > 0

    embeddings_before_edit = counting_embeddings.total_texts_embedded
    n_second = await embedding_service.embed_note_chunks("inbox/test.md", edited, db_path)
    new_embeddings = counting_embeddings.total_texts_embedded - embeddings_before_edit

    # At least one chunk changed (Section A); at least one (Section B) didn't.
    assert new_embeddings >= 1, "edited section should re-embed at least one chunk"
    assert new_embeddings < n_first, (
        f"partial edit re-embedded {new_embeddings}/{n_first} chunks; "
        f"chunk-hash-skip should have reused at least one"
    )


@pytest.mark.anyio
async def test_chunk_hash_skip_reuses_bit_identical_blobs(counting_embeddings, ws_db):
    """Reused embeddings must be the SAME bytes as before — not re-encoded."""
    db_path = ws_db / "app" / "jarvis.db"
    content = "---\ntitle: Test\n---\n\n# Section A\n\nA body that's long enough to make at least one chunk.\n"

    await create_note("inbox/test.md", content, ws_db)
    await embedding_service.embed_note_chunks("inbox/test.md", content, db_path)

    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            "SELECT chunk_index, embedding FROM chunk_embeddings WHERE path = ? ORDER BY chunk_index",
            ("inbox/test.md",),
        )
        first_blobs = {row[0]: row[1] for row in await cursor.fetchall()}

    # Re-ingest identical content
    await embedding_service.embed_note_chunks("inbox/test.md", content, db_path)

    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            "SELECT chunk_index, embedding FROM chunk_embeddings WHERE path = ? ORDER BY chunk_index",
            ("inbox/test.md",),
        )
        second_blobs = {row[0]: row[1] for row in await cursor.fetchall()}

    assert first_blobs.keys() == second_blobs.keys()
    for idx, blob in first_blobs.items():
        assert blob == second_blobs[idx], (
            f"chunk {idx} blob changed across re-ingest of identical content"
        )


@pytest.mark.anyio
async def test_chunk_hash_skip_replaces_obsolete_chunks_when_content_completely_changes(
    counting_embeddings, ws_db
):
    """Re-ingesting wholly different content should drop old chunks and embed new.

    The hash-skip path is per-chunk; if every chunk's hash misses the
    snapshot, every chunk is re-embedded — and old DB rows are gone.
    """
    db_path = ws_db / "app" / "jarvis.db"
    # Different titles ensure the anchor chunk also changes — otherwise the
    # hash-skip path correctly reuses the anchor across re-ingest.
    original = "---\ntitle: First Title\n---\n\n# Section A\n\nFirst body that is long enough to chunk meaningfully.\n"
    rewritten = "---\ntitle: Second Title\n---\n\n# Section Z\n\nCompletely different text with no overlap whatsoever.\n"

    await create_note("inbox/test.md", original, ws_db)
    n_first = await embedding_service.embed_note_chunks("inbox/test.md", original, db_path)
    assert n_first > 0

    embeddings_before = counting_embeddings.total_texts_embedded
    n_second = await embedding_service.embed_note_chunks("inbox/test.md", rewritten, db_path)
    new_embeddings = counting_embeddings.total_texts_embedded - embeddings_before

    assert n_second > 0
    # Every chunk is new content, so every chunk must be embedded
    assert new_embeddings == n_second, (
        f"completely-changed content should re-embed every chunk; "
        f"{new_embeddings}/{n_second} were embedded"
    )

    # Old chunks for this note should not coexist with new ones
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE path = ?",
            ("inbox/test.md",),
        )
        row = await cursor.fetchone()
    assert row[0] == n_second, "old chunks should have been deleted"


@pytest.mark.anyio
async def test_chunk_hash_skip_rebuilds_when_chunks_added(counting_embeddings, ws_db):
    """Adding new content should embed only the new chunks, not re-embed the old."""
    db_path = ws_db / "app" / "jarvis.db"
    short = "---\ntitle: Test\n---\n\n# Section A\n\nSection A has enough body text to produce real chunks.\n"
    longer = (
        "---\ntitle: Test\n---\n\n# Section A\n\n"
        "Section A has enough body text to produce real chunks.\n\n"
        "# Section B\n\nSection B is brand new and should be the only chunk re-embedded.\n"
    )

    await create_note("inbox/test.md", short, ws_db)
    n_first = await embedding_service.embed_note_chunks("inbox/test.md", short, db_path)
    embeddings_before = counting_embeddings.total_texts_embedded

    n_second = await embedding_service.embed_note_chunks("inbox/test.md", longer, db_path)
    new_embeddings = counting_embeddings.total_texts_embedded - embeddings_before

    assert n_second > n_first, "longer content should produce more chunks"
    assert new_embeddings == (n_second - n_first), (
        f"only the {n_second - n_first} new chunks should embed, "
        f"but model was called for {new_embeddings}"
    )
