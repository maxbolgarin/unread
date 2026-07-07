"""B12: `unread ask --semantic` cosine scan dimension-mismatch guard.

`semantic_search` used to `zip(qvec, v, strict=False)` unconditionally,
silently truncating to the shorter vector whenever a stored row's
dimensionality didn't match the query's (a stale index built under the
same model *name* but a different embedding size, or a corrupt BLOB) —
producing a meaningless-but-plausible-looking cosine score instead of an
error. The fix skips mismatched rows, counts them, and emits exactly one
`ask.embeddings.dim_mismatch` warning pointing at the `--build-index`
rebuild command.
"""

from __future__ import annotations

import array
from datetime import UTC, datetime

import pytest

from unread.ask.embeddings import semantic_search
from unread.models import Message


def _vec_bytes(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


def _msg(chat_id: int, msg_id: int, text: str) -> Message:
    return Message(chat_id=chat_id, msg_id=msg_id, date=datetime.now(UTC), text=text)


class _FakeRepo:
    """Minimal stand-in for `Repo` covering just what `semantic_search` calls."""

    def __init__(self, rows: list[tuple[int, int, bytes]], messages: list[Message]) -> None:
        self._rows = rows
        self._messages = messages

    async def get_embeddings(self, chat_ids: list[int], model: str) -> list[tuple[int, int, bytes]]:
        return self._rows

    async def iter_messages(self, chat_id: int, min_msg_id: int, max_msg_id: int):
        for m in self._messages:
            if m.chat_id == chat_id:
                yield m


class _FakeLog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def warning(self, event: str, **kw) -> None:
        self.calls.append((event, kw))

    def error(self, event: str, **kw) -> None:  # pragma: no cover
        self.calls.append((event, kw))

    def info(self, event: str, **kw) -> None:  # pragma: no cover
        self.calls.append((event, kw))


@pytest.fixture(autouse=True)
def _fake_embed_question(monkeypatch):
    """Stub `_embed_batch` so `semantic_search` doesn't need a real OpenAI
    client; the question always embeds to a fixed 3-dim unit vector."""

    async def _fake(oai, model, inputs):
        return [[1.0, 0.0, 0.0]]

    monkeypatch.setattr("unread.ask.embeddings._embed_batch", _fake)


async def test_mismatched_row_excluded_and_warning_emitted_once(monkeypatch):
    fake_log = _FakeLog()
    monkeypatch.setattr("unread.ask.embeddings.log", fake_log)

    good1 = _vec_bytes([1.0, 0.0, 0.0])  # identical direction -> score 1.0
    good2 = _vec_bytes([0.0, 1.0, 0.0])  # orthogonal -> score 0.0
    bad = _vec_bytes([1.0, 0.0, 0.0, 0.0, 0.0])  # wrong dim: 5 vs expected 3

    rows = [
        (1, 10, good1),
        (1, 11, bad),
        (1, 12, good2),
    ]
    messages = [
        _msg(1, 10, "alpha"),
        _msg(1, 12, "beta"),
    ]
    repo = _FakeRepo(rows, messages)

    results = await semantic_search(
        repo=repo,
        oai=object(),
        question="q",
        chat_ids=[1],
        model="text-embedding-3-small",
        limit=10,
    )

    # The mismatched row (msg_id=11) never made it into the ranked results.
    result_ids = {m.msg_id for m, _ in results}
    assert result_ids == {10, 12}

    # Exactly one structured warning, with the right fields.
    mismatch_calls = [c for c in fake_log.calls if c[0] == "ask.embeddings.dim_mismatch"]
    assert len(mismatch_calls) == 1
    _, kw = mismatch_calls[0]
    assert kw["count"] == 1
    assert kw["expected_dim"] == 3
    assert kw["model"] == "text-embedding-3-small"
    assert "--build-index" in kw["hint"]


async def test_all_rows_matching_dim_are_ranked_and_no_warning_fires(monkeypatch):
    fake_log = _FakeLog()
    monkeypatch.setattr("unread.ask.embeddings.log", fake_log)

    v_high = _vec_bytes([1.0, 0.0, 0.0])  # identical direction -> score 1.0
    v_low = _vec_bytes([0.0, 1.0, 0.0])  # orthogonal -> score 0.0
    rows = [(1, 20, v_high), (1, 21, v_low)]
    messages = [_msg(1, 20, "a"), _msg(1, 21, "b")]
    repo = _FakeRepo(rows, messages)

    results = await semantic_search(
        repo=repo,
        oai=object(),
        question="q",
        chat_ids=[1],
        model="text-embedding-3-small",
        limit=10,
    )

    assert [m.msg_id for m, _ in results] == [20, 21]  # ranked by cosine, best first
    assert not any(c[0] == "ask.embeddings.dim_mismatch" for c in fake_log.calls)
