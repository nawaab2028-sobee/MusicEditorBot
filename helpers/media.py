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
        "sample_rate": int(_f(audio.get("sample_rate"), 44100)) or 44100,
        "channels": int(_f(audio.get("channels"), 2)) or 2,
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


FADE = 0.012      # seconds - tiny fade on every join, removes clicks/pops at the cut points
MIN_SEG = 0.02    # shorter pieces of the original are dropped
NORM = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"


def build_items(inserts, ts, trim_end, main_len):
    """
    Split the (trimmed) original at every insert point.
    inserts : [(t, tag_path, tag_duration)] sorted by t (t = seconds counted from the trimmed start)
    Returns [("main", start, end|None) | ("tag", path, duration)] in play order.
    """
    items, prev = [], 0.0
    for t, path, dur in inserts:
        t = min(max(0.0, t), main_len)
        if t - prev >= MIN_SEG:
            items.append(("main", ts + prev, ts + t))
            prev = t
        items.append(("tag", path, dur))
    if main_len - prev >= MIN_SEG:
        items.append(("main", ts + prev, trim_end))
    return items


def _chain(src, pre, length, idx, n, tail):
    parts = [pre] if pre else []
    parts.append(NORM)
    if idx > 0:
        parts.append(f"afade=t=in:st=0:d={FADE}")
    if idx < n - 1 and length is not None and length > 4 * FADE:
        parts.append(f"afade=t=out:st={length - FADE:.4f}:d={FADE}")
    return f"[{src}:a]" + ",".join(parts) + f"[{tail}]"


async def _run_ff(cmd, what):
    try:
        rc, _, err = await run_cmd(cmd)
    except FileNotFoundError:
        raise MediaError("ffmpeg is not installed.")
    return rc, err


async def _tag_for(tag_path, sr, ch, br, dur):
    """Voice tag encoded ONCE to the same mp3 format as the song (cached), so it can be joined without re-encoding the song."""
    out = f"{tag_path}.{sr}_{ch}_{br}.mp3"
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    tmp = out + ".part"
    af = f"afade=t=in:st=0:d={FADE},afade=t=out:st={max(0.0, dur - FADE):.4f}:d={FADE}" if dur > 4 * FADE else "anull"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", tag_path, "-vn", "-map", "0:a:0",
           "-map_metadata", "-1", "-af", af, "-ar", str(sr), "-ac", str(ch), "-c:a", "libmp3lame",
           "-b:a", f"{br}k", "-write_xing", "0", "-id3v2_version", "0", "-write_id3v1", "0", "-f", "mp3", tmp]
    rc, err = await _run_ff(cmd, "tag")
    if rc != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        raise MediaError(f"Could not prepare the voice tag: {_tail(err, 200)}")
    os.replace(tmp, out)
    return out


async def insert_tags_fast(src, out_path, inserts, *, ts, trim_end, main_len, sample_rate, channels, bitrate_k,
                           workdir, cover=None, title=None, artist=None, meta_src=None):
    """
    Place voice tags inside an MP3 WITHOUT decoding / re-encoding the song: the song is cut with stream copy
    (frame accurate, ~26 ms) and joined with the pre-encoded tags. Seconds instead of minutes, no quality loss.
    inserts: [(t, tag_path, tag_duration)] sorted, t counted from the trimmed start.
    """
    br = bitrate_k if sample_rate >= 32000 else 64
    tag_files = {}
    for _, path, dur in inserts:
        if path not in tag_files:
            tag_files[path] = asyncio.ensure_future(_tag_for(path, sample_rate, channels, br, dur))
    try:
        await asyncio.gather(*tag_files.values())
    except BaseException:
        for f in tag_files.values():
            f.cancel()
        raise

    items = build_items(inserts, float(ts or 0.0), trim_end, float(main_len))
    segs, order = [], []
    for k, it in enumerate(items):
        if it[0] == "tag":
            order.append(tag_files[it[1]].result())
            continue
        _, a, b = it
        seg = os.path.join(workdir, f"seg{k:03d}.mp3")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{a:.3f}", "-i", src]
        if b is not None:
            cmd += ["-t", f"{max(0.0, b - a):.3f}"]
        cmd += ["-map", "0:a:0", "-c:a", "copy", "-map_metadata", "-1", "-write_xing", "0", "-id3v2_version", "0",
                "-write_id3v1", "0", "-f", "mp3", seg]
        segs.append((seg, cmd))
        order.append(seg)
    results = await asyncio.gather(*[_run_ff(c, "cut") for _, c in segs])
    for (seg, _), (rc, err) in zip(segs, results):
        if rc != 0 or not os.path.exists(seg) or os.path.getsize(seg) == 0:
            raise MediaError(f"Could not cut the audio: {_tail(err, 200)}")

    lst = os.path.join(workdir, "join.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in order:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst]
    idx = 1
    if cover:
        cmd += ["-i", cover]
        idx += 1
    meta_idx = 0
    if meta_src:
        cmd += ["-i", meta_src]
        meta_idx = idx
    cmd += ["-map", "0:a:0"]
    if cover:
        cmd += ["-map", "1:v:0", "-c:v", "copy"]
    cmd += ["-c:a", "copy", "-map_metadata", str(meta_idx), "-id3v2_version", "3", "-write_id3v1", "1"]
    if cover:
        cmd += ["-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)",
                "-disposition:v", "attached_pic"]
    if title is not None:
        cmd += ["-metadata", f"title={title}"]
    if artist is not None:
        cmd += ["-metadata", f"artist={artist}"]
    cmd += ["-f", "mp3", out_path]
    rc, err = await _run_ff(cmd, "join")
    for seg, _ in segs:
        try:
            os.remove(seg)
        except OSError:
            pass
    if rc != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise MediaError(f"Could not join the audio: {_tail(err, 200)}")
    return out_path


async def concat_lossless(inputs, out_path):
    """Join several audios into one lossless (flac) temp file. Used before inserting voice tags into a merge."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for p in inputs:
        cmd += ["-i", p]
    chains = [f"[{i}:a]{NORM}[a{i}]" for i in range(len(inputs))]
    chains.append("".join(f"[a{i}]" for i in range(len(inputs))) + f"concat=n={len(inputs)}:v=0:a=1[out]")
    cmd += ["-filter_complex", ";".join(chains), "-map", "[out]", "-c:a", "flac", "-compression_level", "0",
            "-f", "flac", out_path]
    try:
        rc, _, err = await run_cmd(cmd)
    except FileNotFoundError:
        raise MediaError("ffmpeg is not installed.")
    if rc != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        logger.error(f"ffmpeg concat failed: {_tail(err, 1500)}")
        raise MediaError(f"Could not merge the audios: {_tail(err, 200)}")
    return out_path


async def render_audio(
    inputs,
    out_path,
    *,
    trim_start=None,
    trim_end=None,
    inserts=None,
    main_len=None,
    meta_src=None,
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
    inserts     : [(t, voice_tag_path, tag_duration)] sorted by t - voice tags placed inside the audio
                  (single input only). t is counted from the start of the trimmed audio.
    main_len    : length of the trimmed original in seconds (required with inserts)
    meta_src    : file the original tags (album, year...) are copied from (default = first input)
    cover       : jpg path or None (None = no cover)
    title/artist: None = keep the original tag
    copy_ok     : True = stream copy (mp3, no inserts, single input)
    """
    if not inputs:
        raise MediaError("No input file.")
    single = len(inputs) == 1
    if not single:
        trim_start = trim_end = None
        copy_ok = False
        inserts = None
    if inserts:
        copy_ok = False
        if not main_len or main_len <= 0:
            raise MediaError("Internal error: audio length is unknown.")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    n_audio = len(inputs)
    meta_idx = 0

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
    elif inserts:
        items = build_items(inserts, float(trim_start or 0.0), trim_end, float(main_len))
        n = len(items)
        cmd += ["-i", inputs[0]]          # input 0 = original (also the metadata source)
        idx, used0 = 1, False
        chains, labels = [], []
        for k, it in enumerate(items):
            if it[0] == "main":
                _, a, b = it
                if not used0:
                    src, used0 = 0, True
                else:
                    cmd += ["-i", inputs[0]]
                    src, idx = idx, idx + 1
                pre = f"atrim=start={a:.3f}" + (f":end={b:.3f}" if b is not None else "") + ",asetpts=PTS-STARTPTS"
                length = (b - a) if b is not None else None
            else:
                _, path, dur = it
                cmd += ["-i", path]
                src, idx = idx, idx + 1
                pre, length = "", dur
            chains.append(_chain(src, pre, length, k, n, f"a{k}"))
            labels.append(f"[a{k}]")
        chains.append("".join(labels) + f"concat=n={n}:v=0:a=1[out]")
        cover_idx = idx
        if cover:
            cmd += ["-i", cover]
        if meta_src:
            meta_idx = cover_idx + (1 if cover else 0)
            cmd += ["-i", meta_src]
        cmd += ["-filter_complex", ";".join(chains), "-map", "[out]"]
        if cover:
            cmd += ["-map", f"{cover_idx}:v:0", "-c:v", "copy"]
        cmd += ["-c:a", "libmp3lame", "-compression_level", "7", "-b:a", f"{bitrate_k}k"]
    else:
        for p in inputs:
            cmd += ["-i", p]
        cover_idx = n_audio
        if cover:
            cmd += ["-i", cover]
        if meta_src:
            meta_idx = cover_idx + (1 if cover else 0)
            cmd += ["-i", meta_src]

        chains, labels = [], []
        for k in range(n_audio):
            pre = ""
            if single and (trim_start or trim_end is not None):
                parts = []
                if trim_start:
                    parts.append(f"start={trim_start:.3f}")
                if trim_end is not None:
                    parts.append(f"end={trim_end:.3f}")
                pre = "atrim=" + ":".join(parts) + ",asetpts=PTS-STARTPTS,"
            chains.append(f"[{k}:a]{pre}{NORM}[a{k}]")
            labels.append(f"[a{k}]")
        chains.append("".join(labels) + f"concat=n={n_audio}:v=0:a=1[out]")
        cmd += ["-filter_complex", ";".join(chains), "-map", "[out]"]
        if cover:
            cmd += ["-map", f"{cover_idx}:v:0", "-c:v", "copy"]
        cmd += ["-c:a", "libmp3lame", "-compression_level", "7", "-b:a", f"{bitrate_k}k"]

    cmd += ["-map_metadata", str(meta_idx), "-id3v2_version", "3", "-write_id3v1", "1"]
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
