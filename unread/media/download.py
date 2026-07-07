"""Download + ffmpeg preprocessing for voice / videonote / video messages."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from unread.config import get_settings
from unread.util.logging import get_logger

if TYPE_CHECKING:
    from telethon import TelegramClient

log = get_logger(__name__)

MAX_OPENAI_MB = 24  # OpenAI audio API 25 MB limit; leave a 1 MB safety margin.


class FfmpegMissing(RuntimeError):
    """Raised when the configured ffmpeg binary isn't on PATH."""


class NoAudioStream(RuntimeError):
    """Raised when transcoding a video whose container has no audio track.

    Distinct from generic transcode failures so the enrichment pipeline can
    treat these as "skipped, nothing to do" rather than errors worth logging
    at ERROR level. A silent screen-recording or a GIF-uploaded-as-video is
    not a fault of the user or of the tool.
    """


async def _run(cmd: list[str]) -> tuple[int, bytes, bytes]:
    # env=clean_subprocess_env() so ffmpeg doesn't carry our API keys
    # in its environment block — visible via /proc/<pid>/environ to
    # other local users on shared hosts.
    from unread.util.subprocess_env import clean_subprocess_env

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=clean_subprocess_env(),
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


# ffmpeg emits this wording when `-vn` strips the only stream (so the output
# ends up with zero streams). Match is case-insensitive and substring-based
# because ffmpeg's phrasing varies slightly across builds.
_NO_STREAM_NEEDLES = (
    "does not contain any stream",
    "output file does not contain any stream",
)


def _is_no_audio_stream(stderr: bytes) -> bool:
    blob = stderr.decode(errors="ignore").lower()
    return any(n in blob for n in _NO_STREAM_NEEDLES)


def _ffmpeg_fail(cmd: list[str], stderr: bytes, stage: str) -> RuntimeError:
    """Build a RuntimeError that keeps the *tail* of stderr (where the real
    error is) rather than the banner, plus the failing command for repro.

    Videos with no audio track are surfaced as `NoAudioStream` so callers
    can skip them cleanly instead of treating a silent video as a bug.
    """
    if _is_no_audio_stream(stderr):
        return NoAudioStream("video has no audio track")
    tail = stderr.decode(errors="ignore").strip().splitlines()[-4:]
    tail_str = " | ".join(line.strip() for line in tail) or "<no stderr>"
    return RuntimeError(f"ffmpeg {stage} failed. cmd={' '.join(cmd)} tail={tail_str}")


async def _ffmpeg_present(path: str) -> bool:
    try:
        rc, _, _ = await _run([path, "-version"])
        return rc == 0
    except FileNotFoundError:
        return False


def media_size_bytes(msg_obj) -> int:
    """Best-effort byte-size estimate for a Telethon message's media.

    Returns 0 when the size isn't readable off the object — caller
    treats 0 as "unknown, don't enforce the cap" so we never refuse a
    legitimate small file just because Telethon's payload shape
    changed between SDK versions.
    """
    media = getattr(msg_obj, "media", None)
    if media is None:
        return 0
    # Documents (videos, audio, generic files) carry an explicit `size`.
    doc = getattr(msg_obj, "document", None) or getattr(media, "document", None)
    if doc is not None:
        size = getattr(doc, "size", None)
        if isinstance(size, int) and size > 0:
            return size
    # Photos: pick the largest size variant Telegram offers.
    photo = getattr(msg_obj, "photo", None) or getattr(media, "photo", None)
    if photo is not None:
        biggest = 0
        for sz in getattr(photo, "sizes", None) or []:
            for attr in ("size", "sizes"):
                val = getattr(sz, attr, None)
                if isinstance(val, int):
                    biggest = max(biggest, val)
                elif isinstance(val, list) and val:
                    ints = [int(x) for x in val if isinstance(x, int)]
                    if ints:
                        biggest = max(biggest, *ints)
        if biggest > 0:
            return biggest
    return 0


class MediaTooLarge(RuntimeError):
    """Raised when a download is refused for exceeding the configured cap."""


async def download_message(
    client: TelegramClient,
    msg_obj,
    out_path: Path,
    *,
    max_bytes: int | None = None,
) -> Path:
    """Download a Telethon message's media to `out_path`.

    Writes to a sibling `.part` file first and atomic-renames on success.
    A Ctrl-C / network drop mid-download then leaves only the `.part`,
    which the caller cleans up — *not* a truncated `out_path` that
    `_existing_for_msg` would later mistake for a finished download and
    skip on the next run.

    Pre-prod blocker: enforces a size cap before invoking
    ``client.download_media`` so a 4 GB video can't silently fill the
    user's disk. ``max_bytes`` defaults to
    ``settings.media.max_download_mb * 1024 * 1024`` (0 disables);
    callers can override per-call. Raises :class:`MediaTooLarge` when
    the source exceeds the cap so the orchestrator can record the skip
    rather than treating it as a generic download failure.
    """
    if max_bytes is None:
        try:
            cap_mb = int(getattr(get_settings().media, "max_download_mb", 0) or 0)
        except Exception:  # pragma: no cover - settings unreadable
            cap_mb = 0
        max_bytes = cap_mb * 1024 * 1024
    if max_bytes and max_bytes > 0:
        size_bytes = media_size_bytes(msg_obj)
        if size_bytes > max_bytes:
            raise MediaTooLarge(
                f"media_too_large: {size_bytes} bytes exceeds cap {max_bytes} bytes "
                f"(msg_id={getattr(msg_obj, 'id', '?')})"
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")
    # Clear any leftover .part from a previous interrupted run so we
    # don't append to / read back stale bytes.
    with contextlib.suppress(FileNotFoundError):
        tmp_path.unlink()
    try:
        result = await client.download_media(msg_obj, file=str(tmp_path))
    except BaseException:
        # Includes CancelledError / KeyboardInterrupt — clean up before
        # propagating so `_existing_for_msg` doesn't lock us out.
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise
    if result is None:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise RuntimeError(f"download_media returned None for msg={msg_obj.id}")
    Path(result).replace(out_path)
    return out_path


async def transcode_for_openai(
    src: Path, media_type: str, tmp_dir: Path, *, prefer_mp3: bool = False
) -> list[Path]:
    """Prepare audio for OpenAI:
      - voice (.ogg/opus): pass through, unless ``prefer_mp3=True``
        (default Whisper model ``gpt-4o-mini-transcribe`` rejects opus).
      - videonote/video: extract mono 16 kHz mp3 at 64k.
      - split into ≤600 s segments if file > 25 MB.

    Returns a list of 1+ files; the caller transcribes each segment in order.

    Contract (callers rely on this): every returned path is EITHER ``src``
    itself (the voice pass-through branch — nothing needed producing) OR a
    fresh file inside ``tmp_dir``. This function never renames, moves, or
    otherwise mutates ``src`` in place, so callers that don't own ``src``
    (e.g. a user's own file on the local-file analysis path) can safely
    unlink every returned path where ``p != src`` once they're done with
    it, with no risk of touching the caller's original file. Any
    intermediate re-encode this function creates for its own purposes (a
    voice opus→mp3 conversion, the pre-chunk mp3 fed to the segmenter, …)
    is unlinked internally as soon as it's superseded — callers never see
    those paths and don't need to guess at internal naming.
    """
    settings = get_settings()
    ffmpeg = settings.media.ffmpeg_path
    from unread.util.fsmode import ensure_private_dir

    ensure_private_dir(tmp_dir)

    if media_type == "voice":
        # Telegram voice is always Opus in OGG. The file may arrive with
        # `.oga` (Telethon's default for opus) OR with no extension at all
        # — `download_message` writes via a `.part` atomic-rename, and
        # Telethon's `_get_proper_filename` sees `.part` as the existing
        # suffix and skips adding `.oga`. After the rename to the final
        # path (which has no extension) the file looks suffix-less to us.
        # Normalize to `.ogg` unconditionally: OpenAI's Whisper detects
        # format from filename and rejects extensionless uploads with
        # a 400 `Unsupported file format`.
        #
        # Copy (never rename/move) into `tmp_dir` — `src` may be a user's
        # own file on the local-file analysis path (`unread ./note.oga`).
        # Renaming it in place would (a) mutate a file the user never
        # asked us to touch, and (b) silently overwrite a preexisting
        # `<name>.ogg` sitting right next to it (POSIX rename semantics).
        suffix = src.suffix.lower()
        if suffix in {"", ".oga"}:
            prepared = tmp_dir / f"{src.stem or 'voice'}.ogg"
            shutil.copy2(src, prepared)
        else:
            prepared = src
        # `gpt-4o-mini-transcribe` (the default since the model swap)
        # and `gpt-4o-transcribe` reject opus payloads with a 4xx,
        # even though `whisper-1` accepted them. Force a re-encode to
        # mp3 when the caller flagged the active model as opus-hostile.
        if prefer_mp3 and prepared.suffix.lower() in {".ogg", ".oga", ".opus"}:
            if not await _ffmpeg_present(ffmpeg):
                raise FfmpegMissing(
                    f"ffmpeg not found at '{ffmpeg}'. Required for opus→mp3 "
                    "with gpt-4o-mini-transcribe / gpt-4o-transcribe; "
                    "install ffmpeg or set `[openai] audio_model_default = "
                    '"whisper-1"` in `~/.unread/config.toml`.'
                )
            mp3_path = tmp_dir / f"{src.stem}_voice.mp3"
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(prepared),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "64k",
                str(mp3_path),
            ]
            rc, _, err = await _run(cmd)
            if rc != 0:
                raise _ffmpeg_fail(cmd, err, "voice opus→mp3")
            # `prepared` (the tmp .ogg copy made above, if any) is
            # superseded by `mp3_path` — drop it now instead of leaking
            # it. Skipped when `prepared is src` (opus pass-through with
            # no copy made — nothing of ours to clean up).
            if prepared != src:
                with contextlib.suppress(FileNotFoundError):
                    prepared.unlink()
            prepared = mp3_path
    else:
        if not await _ffmpeg_present(ffmpeg):
            raise FfmpegMissing(
                f"ffmpeg not found at '{ffmpeg}'. Install ffmpeg or update config.media.ffmpeg_path."
            )
        prepared = tmp_dir / f"{src.stem}_prep.mp3"
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            str(prepared),
        ]
        rc, _, err = await _run(cmd)
        if rc != 0:
            raise _ffmpeg_fail(cmd, err, "transcode")

    size_mb = prepared.stat().st_size / (1024 * 1024)
    if size_mb <= MAX_OPENAI_MB:
        return [prepared]

    # Need to chunk. Re-encode voice to mp3 first if we didn't already.
    if media_type == "voice":
        if not await _ffmpeg_present(ffmpeg):
            raise FfmpegMissing(f"ffmpeg required for chunking voice >{MAX_OPENAI_MB} MB.")
        normalized = tmp_dir / f"{src.stem}_voice.mp3"
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(prepared),
            "-ac",
            "1",
            "-b:a",
            "64k",
            str(normalized),
        ]
        rc, _, err = await _run(cmd)
        if rc != 0:
            raise _ffmpeg_fail(cmd, err, "voice→mp3")
        # `prepared` (the tmp .ogg copy or the prefer_mp3 mp3, whichever we
        # entered this branch with) is superseded by `normalized` — unless
        # it *is* `src` (opus pass-through, nothing of ours to clean up).
        if prepared not in (src, normalized):
            with contextlib.suppress(FileNotFoundError):
                prepared.unlink()
        prepared = normalized

    # `prepared` is now the single file fed to the segmenter below — for
    # voice that's `normalized` from just above; for video/videonote it's
    # the `_prep.mp3` produced earlier. Either way it's superseded by the
    # chunk files once they exist, so clean it up once segmenting succeeds
    # (this is the one internal intermediate the old code never unlinked —
    # `_prep.mp3` used to leak forever whenever a video needed chunking).
    pre_segment = prepared

    seg_pattern = tmp_dir / f"{src.stem}_chunk_%03d.mp3"
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(prepared),
        "-f",
        "segment",
        "-segment_time",
        "600",
        "-c",
        "copy",
        str(seg_pattern),
    ]
    rc, _, err = await _run(cmd)
    if rc != 0:
        raise _ffmpeg_fail(cmd, err, "segment")
    chunks = sorted(tmp_dir.glob(f"{src.stem}_chunk_*.mp3"))
    if not chunks:
        raise RuntimeError("ffmpeg produced no chunks")
    # `pre_segment` served only as input to the segmenter above — no
    # longer needed now that the chunk files exist. Never `src` itself
    # (video's `_prep.mp3` and voice's `normalized` both live in `tmp_dir`,
    # but the guard is kept as defense-in-depth against future refactors).
    if pre_segment != src and pre_segment not in chunks:
        with contextlib.suppress(FileNotFoundError):
            pre_segment.unlink()
    return chunks


def sha1_of_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(1 << 16), b""):
            h.update(buf)
    return h.hexdigest()
