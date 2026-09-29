"""Regression: orchestrator skips photos before any TG fetch when the
enrichment would no-op anyway (vision provider missing / `media_doc_id`
absent). The skip happens *before* the cap counter is incremented so
unskippable photos don't lose their cap slot to silent no-ops.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unread.ai.providers import ProviderUnavailableError
from unread.enrich.base import EnrichOpts
from unread.enrich.pipeline import enrich_messages
from unread.models import Message


def _photo(msg_id: int, *, doc_id: int | None) -> Message:
    return Message(
        chat_id=-100,
        msg_id=msg_id,
        date=datetime.now(UTC),
        text=None,
        media_type="photo",
        media_doc_id=doc_id,
    )


@pytest.mark.asyncio
async def test_vision_unavailable_skips_all_photos_before_fetch():
    msgs = [_photo(i, doc_id=1000 + i) for i in range(1, 4)]

    with (
        patch(
            "unread.ai.vision_provider.make_vision_provider",
            side_effect=ProviderUnavailableError("no api key"),
        ),
        patch("unread.enrich.pipeline.enrich_image") as mock_enrich,
    ):
        repo = AsyncMock()
        client = MagicMock()
        stats = await enrich_messages(
            msgs,
            client=client,
            repo=repo,
            opts=EnrichOpts(image=True, max_images_per_run=10, concurrency=2),
        )

    mock_enrich.assert_not_called()
    client.get_messages.assert_not_called()
    assert stats.skipped.get("image") == len(msgs)
    assert stats.counts.get("image", 0) == 0


@pytest.mark.asyncio
async def test_photo_without_doc_id_skipped_before_fetch():
    msgs = [_photo(1, doc_id=None), _photo(2, doc_id=None)]

    async def _fake_enrich(*args, **kwargs):  # pragma: no cover - assertion guard
        raise AssertionError("enrich_image must not run for doc_id=None photos")

    with (
        patch("unread.ai.vision_provider.make_vision_provider", return_value=MagicMock()),
        patch("unread.enrich.pipeline.enrich_image", side_effect=_fake_enrich) as mock_enrich,
    ):
        repo = AsyncMock()
        client = MagicMock()
        stats = await enrich_messages(
            msgs,
            client=client,
            repo=repo,
            opts=EnrichOpts(image=True, max_images_per_run=10, concurrency=2),
        )

    mock_enrich.assert_not_called()
    client.get_messages.assert_not_called()
    assert stats.skipped.get("image") == len(msgs)


@pytest.mark.asyncio
async def test_doc_id_none_photos_do_not_burn_cap_slots():
    # 3 photos with no doc_id (must be skipped early) followed by 2 with a
    # real doc_id. cap=2 — if the skipped ones burned cap slots, the real
    # ones would all be cap-skipped too. After the fix only the latter two
    # should reach `enrich_image`.
    msgs = [
        _photo(1, doc_id=None),
        _photo(2, doc_id=None),
        _photo(3, doc_id=None),
        _photo(4, doc_id=4000),
        _photo(5, doc_id=5000),
    ]

    enrich_calls: list[int] = []

    async def fake_enrich(msg, **kwargs):
        enrich_calls.append(msg.msg_id)

    with (
        patch("unread.ai.vision_provider.make_vision_provider", return_value=MagicMock()),
        patch("unread.enrich.pipeline.enrich_image", side_effect=fake_enrich),
    ):
        repo = AsyncMock()
        client = MagicMock()
        await enrich_messages(
            msgs,
            client=client,
            repo=repo,
            opts=EnrichOpts(image=True, max_images_per_run=2, concurrency=3),
        )

    assert sorted(enrich_calls) == [4, 5], (
        f"only photos with a doc_id should reach enrich_image; got {enrich_calls}"
    )


@pytest.mark.asyncio
async def test_progress_only_covers_messages_with_enabled_enrichers():
    # Regression: with image enrichment off, the progress bar still walked
    # every photo and labelled it "Image (msg #N)", which read as if the
    # photos were being downloaded. Only enabled kinds should show up.
    voice = Message(
        chat_id=-100,
        msg_id=10,
        date=datetime.now(UTC),
        text=None,
        media_type="voice",
        media_doc_id=9000,
    )
    msgs = [_photo(1, doc_id=1000), _photo(2, doc_id=2000), voice]

    progress = MagicMock()
    progress.__enter__.return_value = progress
    descriptions: list[str] = []
    progress.update.side_effect = lambda _tid, description: descriptions.append(description)

    with (
        patch("rich.progress.Progress", return_value=progress),
        patch("unread.enrich.pipeline.enrich_image") as mock_image,
        patch("unread.enrich.pipeline.enrich_audio", new=AsyncMock(return_value=None)),
    ):
        await enrich_messages(
            msgs,
            client=MagicMock(),
            repo=AsyncMock(),
            opts=EnrichOpts(voice=True, image=False),
        )

    mock_image.assert_not_called()
    assert progress.add_task.call_args.kwargs["total"] == 1
    assert descriptions == ["Voice (msg #10)"]
