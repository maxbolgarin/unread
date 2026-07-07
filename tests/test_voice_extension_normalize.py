"""Voice files must reach OpenAI with a `.ogg` filename suffix.

Real-world failure (forum analysis, 67/67 voice messages failing):
`download_message` uses an atomic `.part` rename. Telethon's
`_get_proper_filename` (see `.venv/.../telethon/client/downloads.py`)
sees `<path>.part` as having extension `.part` and refuses to add the
auto-detected `.oga`. After the rename, the file lives at `<path>` with
no extension. OpenAI's Whisper endpoint detects format from the
upload's filename — extensionless → 400 `Unsupported file format`.

`transcode_for_openai` must normalize voice files to `.ogg` regardless
of the input suffix so the bytes ride into Whisper with a recognized
filename.

IMPORTANT: normalization must NOT mutate `src` in place. `src` can be
a user's own file on the local-file analysis path (`unread ./note.oga`)
— an in-place `src.rename(...)` both mutates a file the user never
asked us to touch and (per POSIX rename semantics) silently overwrites
a preexisting `note.ogg` sitting right next to it. The fix copies into
`tmp_dir` instead, so `src` always survives untouched and every
produced path lives under `tmp_dir`.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_voice_no_extension_normalized_to_ogg_copy(tmp_path: Path):
    """The buggy real-world path: voice file arrives with no suffix at all.
    Without normalization OpenAI rejects with 400 `Unsupported file format`.

    The normalized `.ogg` file must be a COPY living in `tmp_dir` — `src`
    (the original download / user file) must survive untouched at its
    original path. A separate `tmp_dir` (distinct from `src`'s parent)
    proves the file didn't just get renamed alongside `src`.
    """
    from unread.media.download import transcode_for_openai

    src_dir = tmp_path / "src_dir"
    src_dir.mkdir()
    tmp_dir = tmp_path / "tmp_dir"

    src = src_dir / "1234_5678"  # No extension — matches audio.py's src construction.
    original_bytes = b"OggS\x00\x02"  # Minimal OggS header bytes.
    src.write_bytes(original_bytes)

    parts = await transcode_for_openai(src, "voice", tmp_dir, prefer_mp3=False)

    assert len(parts) == 1
    assert parts[0].suffix.lower() == ".ogg", (
        f"voice files must reach OpenAI with a .ogg suffix; got {parts[0].name!r}"
    )
    assert parts[0].exists()
    assert parts[0] != src, "must not return the original path — a copy must be produced"
    assert parts[0].parent == tmp_dir, "the normalized copy must live in tmp_dir, not next to src"

    # `src` itself must survive completely untouched — no rename, no mutation.
    assert src.exists(), "src must not be renamed/moved away"
    assert src.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_voice_oga_normalized_to_ogg_copy(tmp_path: Path):
    """Telethon's historical naming (`.oga`) is also normalized — OpenAI's
    filename whitelist accepts `.oga` and `.ogg` both, but the prefer_mp3
    branch keys off `.ogg`, so we collapse here for consistency. Same
    src-survives / copy-in-tmp_dir contract as the no-extension case."""
    from unread.media.download import transcode_for_openai

    src_dir = tmp_path / "src_dir"
    src_dir.mkdir()
    tmp_dir = tmp_path / "tmp_dir"

    src = src_dir / "1234_5678.oga"
    original_bytes = b"OggS\x00\x02"
    src.write_bytes(original_bytes)

    parts = await transcode_for_openai(src, "voice", tmp_dir, prefer_mp3=False)

    assert len(parts) == 1
    assert parts[0].suffix.lower() == ".ogg"
    assert parts[0].exists()
    assert parts[0] != src
    assert parts[0].parent == tmp_dir

    assert src.exists()
    assert src.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_voice_no_extension_does_not_overwrite_existing_ogg(tmp_path: Path):
    """Regression for the silent-overwrite bug: POSIX `rename` would clobber
    a preexisting `<stem>.ogg` sitting next to `src` with no warning. The
    copy-into-tmp_dir fix can't overwrite anything in `src`'s directory
    because it never writes there."""
    from unread.media.download import transcode_for_openai

    src_dir = tmp_path / "src_dir"
    src_dir.mkdir()
    tmp_dir = tmp_path / "tmp_dir"

    src = src_dir / "note"
    src.write_bytes(b"OggS\x00\x02new-voice-bytes")

    preexisting_ogg = src_dir / "note.ogg"
    preexisting_bytes = b"totally unrelated preexisting file"
    preexisting_ogg.write_bytes(preexisting_bytes)

    parts = await transcode_for_openai(src, "voice", tmp_dir, prefer_mp3=False)

    assert len(parts) == 1
    assert parts[0].parent == tmp_dir
    # The preexisting file next to src must be untouched.
    assert preexisting_ogg.read_bytes() == preexisting_bytes
    assert src.exists()


@pytest.mark.asyncio
async def test_voice_ogg_passes_through(tmp_path: Path):
    """A file already named `.ogg` stays put — no needless rename."""
    from unread.media.download import transcode_for_openai

    src = tmp_path / "1234_5678.ogg"
    src.write_bytes(b"OggS\x00\x02")

    parts = await transcode_for_openai(src, "voice", tmp_path, prefer_mp3=False)

    assert len(parts) == 1
    assert parts[0] == src
    assert parts[0].exists()


def test_download_message_part_trick_strips_extension():
    """Lock the upstream behavior so the transcoder-side workaround stays
    justified. Telethon's `_get_proper_filename` sees `.part` as the
    existing extension and does NOT add the auto-detected one — meaning
    `download_message`'s atomic rename leaves the file extensionless.
    """
    import os

    from telethon.client.downloads import DownloadMethods

    # Telethon's helper with a `.part` path: should keep `.part` and NOT
    # add the proposed `.oga` extension.
    result = DownloadMethods._get_proper_filename(
        os.path.join(os.sep + "tmp", "1234_5678.part"),
        "document",
        ".oga",
    )
    # Path is unchanged — Telethon respects the existing (`.part`) suffix.
    assert result.endswith(".part"), (
        f"Telethon helper unexpectedly rewrote the path: {result!r}. "
        "If this assertion flips, revisit transcode_for_openai's voice branch "
        "— the no-extension workaround may no longer be necessary."
    )


# Avoid Telethon import-time noise if the SDK isn't available in some envs.
def _telethon_available() -> bool:
    try:
        import telethon  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark_telethon = pytest.mark.skipif(
    not _telethon_available(),
    reason="telethon not installed",
)
