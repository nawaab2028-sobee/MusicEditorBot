"""
Media helpers: ffprobe / ffmpeg (trim, voice tag, merge, tags, cover) + Pillow (cover resize).

All tagging is done with ffmpeg (no extra library needed):
  * mp3 input + no voice tag  -> stream copy (zero quality loss, fast)
  * everything else (non-mp3, voice tag, merge) -> mp3 re-encode (libmp3lame)
"""

import asyncio
import json
import os

from PIL import Image, ImageOps

from logger import LOGGER

logger = LOGGER(__name__)

COVER_SIZE = 600   # embedded cover (square)
THUMB_SIZE = 320   # Telegram thumb max side
THUMB_MAX_BYTES = 190 * 1024


class MediaError(Exception):
    """ffmpeg / file problem (message suitable to show to the user)."""


# ======================================================
# subprocess
# ======================================================

async def run_cmd(cmd, timeout=3600):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise MediaError("Processing timed out.")
    except asyncio.CancelledError:
        # /stop: kill the ffmpeg process as well
        try:
            proc.kill()
        except Exception:
            pass
        raise
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


# ======================================================
# probe
# ======================================================

async def probe(path):
    """
    Returns dict: duration, title, artist, album, codec, bitrate (kbps), has_cover, is_mp3
    Raises MediaError if it is not audio / is corrupt.
    """
    try:
        rc, out, err = await run_cmd(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            timeout=120,
        )
    except FileNotFoundError:
        raise MediaError("ffmpeg is not installed (ffprobe not found).")
    if rc != 0:
        raise MediaError("This is not a valid audio file.")
    try:
        data = json.loads(out)
    except Exception:
        raise MediaError("This is not a valid audio file.")

    streams = data.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise MediaError("No audio stream found in this file.")
    fmt = data.get("format", {}) or {}

    tags = {}
    for src in ((audio.get("tags") or {}), (fmt.get("tags") or {})):
        for k, v in src.items():
            tags.setdefault(k.lower(), v)

    def _f(v, default=0.0):
        try:
            return float(v)
        except Exception:
            return default

    duration = _f(fmt.get("duration")) or _f(audio.get("duration"))
    bitrate = int(_f(audio.get("bit_rate")) or _f(fmt.get("bit_rate"))) // 1000
    has_cover = any(
        s.get("codec_type") == "video" and (s.get("disposition", {}) or {}).get("attached_pic") == 1
        for s in streams
    ) or any(s.get("codec_type") == "video" for s in streams)

    return {
        "duration": duration,
        "title": (tags.get("title") or "").strip() or None,
        "artist": (tags.get("artist") or "").strip() or None,
        "album": (tags.get("album") or "").strip() or None,
        "codec": audio.get("codec_name"),
        "bitrate": bitrate,
        "has_cover": has_cover,
        "is_mp3": audio.get("codec_name") == "mp3" and "mp3" in (fmt.get("format_name") or ""),
    }


def pick_bitrate(kbps):
    """Output mp3 bitrate based on the source bitrate (128..320k)."""
    if not kbps or kbps <= 0:
        return 192
    for step in (128, 160, 192, 224, 256, 320):
        if kbps <= step:
            return step
    return 320


# ======================================================
# images
# ======================================================

async def extract_cover(path, out_jpg):
    """Extract the original cover from inside the audio. True = found."""
    try:
        rc, _, _ = await run_cmd(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path,
             "-an", "-map", "0:v:0", "-frames:v", "1", "-q:v", "2", out_jpg],
            timeout=120,
        )
    except MediaError:
        return False
    return rc == 0 and os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 0


def _flatten(img):
    """RGBA/P -> RGB (white background), EXIF rotation fix."""
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def _save_jpeg_limited(img, out_path, max_bytes=None):
    quality = 90
    while True:
        img.save(out_path, "JPEG", quality=quality, optimize=True)
        if not max_bytes or os.path.getsize(out_path) <= max_bytes or quality <= 40:
            return
        quality -= 10


def _make_cover_sync(src, cover_out, thumb_out):
    with Image.open(src) as im:
        try:
            im.seek(0)  # gif/webp animated -> first frame
        except Exception:
            pass
        img = _flatten(im)
    # "Auto fit": image of any size/format -> square cover (center crop)
    square = ImageOps.fit(img, (COVER_SIZE, COVER_SIZE), Image.LANCZOS)
    _save_jpeg_limited(square, cover_out)
    thumb = ImageOps.fit(img, (THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
    _save_jpeg_limited(thumb, thumb_out, THUMB_MAX_BYTES)


def _make_thumb_sync(src, thumb_out):
    with Image.open(src) as im:
        img = _flatten(im)
    img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
    _save_jpeg_limited(img, thumb_out, THUMB_MAX_BYTES)


async def make_custom_cover(src, cover_out, thumb_out):
    """Image sent by the user -> square cover + Telegram thumb. Raises MediaError if the image is invalid."""
    try:
        await asyncio.to_thread(_make_cover_sync, src, cover_out, thumb_out)
    except Exception as e:
        logger.warning(f"cover process failed: {e}")
        raise MediaError("This is not a valid image. Please send a photo or an image file.")


async def make_thumb(src, thumb_out):
    """Original cover (same aspect ratio) -> Telegram thumb. False = failed."""
    try:
        await asyncio.to_thread(_make_thumb_sync, src, thumb_out)
        return True
    except Exception as e:
        logger.warning(f"thumb process failed: {e}")
        return False


# ======================================================
# main render (ffmpeg)
# ======================================================

def _tail(text, n=400):
    text = (text or "").strip()
    return text[-n:]


async def render_audio(
    inputs,
    out_path,
    *,
    trim_start=None,
    trim_end=None,
    voice_tag=None,
    cover=None,
    title=None,
    artist=None,
    bitrate_k=192,
    copy_ok=False,
):
    """
    inputs      : [path, ...]  (1 file = edit, 2+ = merge)
    trim_start  : seconds (single input only)
    trim_end    : seconds, absolute end time (single input only)
    voice_tag   : path or None -> added at the START of the song
    cover       : jpg path or None (None = no cover)
    title/artist: None = keep the original tag
    copy_ok     : True = stream copy (mp3, no voice tag, single input)
    """
    if not inputs:
        raise MediaError("No input file.")
    single = len(inputs) == 1
    if not single:
        trim_start = trim_end = None
        copy_ok = False
    if voice_tag:
        copy_ok = False

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    n_audio = len(inputs)

    if copy_ok:
        if trim_start:
            cmd += ["-ss", f"{trim_start:.3f}"]
        cmd += ["-i", inputs[0]]
        cover_idx = 1
        if cover:
            cmd += ["-i", cover]
        if trim_start is not None or trim_end is not None:
            end = trim_end
            if end is not None:
                cmd += ["-t", f"{max(0.0, end - (trim_start or 0.0)):.3f}"]
        cmd += ["-map", "0:a:0"]
        if cover:
            cmd += ["-map", f"{cover_idx}:v:0", "-c:v", "copy"]
        cmd += ["-c:a", "copy"]
    else:
        for p in inputs:
            cmd += ["-i", p]
        idx = n_audio
        voice_idx = None
        if voice_tag:
            cmd += ["-i", voice_tag]
            voice_idx = idx
            idx += 1
        cover_idx = idx
        if cover:
            cmd += ["-i", cover]

        norm = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
        chains, labels = [], []
        order = ([voice_idx] if voice_idx is not None else []) + list(range(n_audio))
        for k, i in enumerate(order):
            pre = ""
            if single and i == 0 and (trim_start or trim_end is not None):
                parts = []
                if trim_start:
                    parts.append(f"start={trim_start:.3f}")
                if trim_end is not None:
                    parts.append(f"end={trim_end:.3f}")
                pre = "atrim=" + ":".join(parts) + ",asetpts=PTS-STARTPTS,"
            chains.append(f"[{i}:a]{pre}{norm}[a{k}]")
            labels.append(f"[a{k}]")
        chains.append("".join(labels) + f"concat=n={len(order)}:v=0:a=1[out]")
        cmd += ["-filter_complex", ";".join(chains), "-map", "[out]"]
        if cover:
            cmd += ["-map", f"{cover_idx}:v:0", "-c:v", "copy"]
        cmd += ["-c:a", "libmp3lame", "-b:a", f"{bitrate_k}k"]

    cmd += ["-map_metadata", "0", "-id3v2_version", "3", "-write_id3v1", "1"]
    if cover:
        cmd += ["-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)",
                "-disposition:v", "attached_pic"]
    if title is not None:
        cmd += ["-metadata", f"title={title}"]
    if artist is not None:
        cmd += ["-metadata", f"artist={artist}"]
    cmd += ["-f", "mp3", out_path]

    try:
        rc, _, err = await run_cmd(cmd)
    except FileNotFoundError:
        raise MediaError("ffmpeg is not installed.")
    if rc != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        logger.error(f"ffmpeg failed: {_tail(err, 1500)}")
        raise MediaError(f"Could not process the audio: {_tail(err, 200)}")
    return out_path
