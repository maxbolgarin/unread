"""`extract_audio` / `extract_video` must clean up their ffmpeg temp files.

Real-world leak: `unread ./note.oga` (local-file analysis) routes through
`extract_audio`, which calls `media/download.py:transcode_for_openai` to
produce copies / re-encodes / chunk files under `settings.media.tmp_dir`.
Neither extractor ever deleted those outputs — every local audio/video
analysis leaked files into `tmp_dir` forever.

Fix: both extractors wrap the transcribe loop in try/finally and unlink
every path `transcode_for_openai` returned, EXCEPT when that path *is*
the original input (`p == path`) — the voice-passthrough branch can
legitimately return `[path]`, and deleting the caller's own file would
just be the B8 bug (renaming/mutating the user's file) in a different
shape.

These tests fake `transcode_for_openai` and `_transcribe_file` so they
never touch ffmpeg / the network — offline per repo convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _stub_provider_resolution(monkeypatch) -> None:
    """Make the audio-provider resolution succeed without a real API key."""
    monkeypatch.setattr("unread.ai.providers.resolve_audio", lambda _s: ("openai", "whisper-1"))
    monkeypatch.setattr("unread.ai.providers.make_audio_client", lambda _p, _s: object())


def _use_tmp_dir(monkeypatch, tmp_dir: Path) -> None:
    from unread.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings.media, "tmp_dir", tmp_dir)


@pytest.mark.asyncio
@pytest.mark.parametrize("extractor_name", ["extract_audio", "extract_video"])
async def test_extract_cleans_up_tmp_copies_after_success(tmp_path, monkeypatch, extractor_name):
    """Tmp files transcode_for_openai produced (a copy + a chunk, standing
    in for the real `.ogg` copy / `_prep.mp3` / `_chunk_*.mp3` outputs) are
    gone once extraction succeeds; the user's source file is untouched."""
    _stub_provider_resolution(monkeypatch)
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    _use_tmp_dir(monkeypatch, tmp_dir)

    src = tmp_path / "note.oga"
    original_bytes = b"original audio bytes"
    src.write_bytes(original_bytes)

    produced_copy = tmp_dir / "note_copy.ogg"
    produced_copy.write_bytes(b"converted copy")
    chunk = tmp_dir / "note_chunk_000.mp3"
    chunk.write_bytes(b"chunk bytes")

    async def fake_transcode(path, _media_type, _tmp_dir, *, prefer_mp3=False):
        assert path == src
        return [produced_copy, chunk]

    async def fake_transcribe(_oai, part, _model, _lang):
        return f"transcript for {part.name}"

    monkeypatch.setattr("unread.media.download.transcode_for_openai", fake_transcode)
    monkeypatch.setattr("unread.enrich.audio._transcribe_file", fake_transcribe)

    from unread.files import extractors

    extractor = getattr(extractors, extractor_name)
    result = await extractor(src)

    assert result.text == "transcript for note_copy.ogg\ntranscript for note_chunk_000.mp3"
    assert not produced_copy.exists(), "tmp copy must be cleaned up after success"
    assert not chunk.exists(), "tmp chunk must be cleaned up after success"
    assert src.exists(), "the user's original file must survive"
    assert src.read_bytes() == original_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("extractor_name", ["extract_audio", "extract_video"])
async def test_extract_voice_passthrough_never_deletes_source(tmp_path, monkeypatch, extractor_name):
    """When transcode_for_openai returns `[path]` unchanged (voice
    pass-through, nothing produced), cleanup must NOT delete the source —
    it's the exact `p == path` exemption the B7/B8 fix depends on."""
    _stub_provider_resolution(monkeypatch)
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    _use_tmp_dir(monkeypatch, tmp_dir)

    src = tmp_path / "note.ogg"
    original_bytes = b"original audio bytes"
    src.write_bytes(original_bytes)

    async def fake_transcode(path, _media_type, _tmp_dir, *, prefer_mp3=False):
        return [path]  # pass-through: nothing produced in tmp_dir

    async def fake_transcribe(_oai, _path, _model, _lang):
        return "some transcript text"

    monkeypatch.setattr("unread.media.download.transcode_for_openai", fake_transcode)
    monkeypatch.setattr("unread.enrich.audio._transcribe_file", fake_transcribe)

    from unread.files import extractors

    extractor = getattr(extractors, extractor_name)
    result = await extractor(src)

    assert result.text == "some transcript text"
    assert src.exists(), "the user's original file must survive the pass-through branch"
    assert src.read_bytes() == original_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("extractor_name", ["extract_audio", "extract_video"])
async def test_extract_cleans_up_tmp_files_when_transcribe_raises(tmp_path, monkeypatch, extractor_name):
    """Cleanup must run even when transcription fails midway — a try/finally,
    not a happy-path-only unlink."""
    _stub_provider_resolution(monkeypatch)
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    _use_tmp_dir(monkeypatch, tmp_dir)

    src = tmp_path / "note.oga"
    original_bytes = b"original audio bytes"
    src.write_bytes(original_bytes)

    produced_copy = tmp_dir / "note_copy.ogg"
    produced_copy.write_bytes(b"converted copy")
    chunk = tmp_dir / "note_chunk_000.mp3"
    chunk.write_bytes(b"chunk bytes")

    async def fake_transcode(path, _media_type, _tmp_dir, *, prefer_mp3=False):
        return [produced_copy, chunk]

    async def fake_transcribe_raises(_oai, _path, _model, _lang):
        raise RuntimeError("transcription boom")

    monkeypatch.setattr("unread.media.download.transcode_for_openai", fake_transcode)
    monkeypatch.setattr("unread.enrich.audio._transcribe_file", fake_transcribe_raises)

    from unread.files import extractors

    extractor = getattr(extractors, extractor_name)
    with pytest.raises(RuntimeError, match="transcription boom"):
        await extractor(src)

    assert not produced_copy.exists(), "tmp copy must be cleaned up even when transcribe raises"
    assert not chunk.exists(), "tmp chunk must be cleaned up even when transcribe raises"
    assert src.exists(), "the user's original file must survive an exception mid-transcribe"
    assert src.read_bytes() == original_bytes
