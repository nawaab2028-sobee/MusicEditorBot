"""
Core processing pipeline:
  download -> probe -> (trim / voice tag / convert / tags / cover via ffmpeg) -> resend
  -> target channel copy -> auto delete -> counters

Direct audio, Batch (public + private/login) and Merge all use this.
"""

import asyncio
import os
import shutil
import time
import uuid

from pyrogram import Client, enums
from pyrogram.errors import FloodWait

from config import (API_HASH, API_ID, BATCH_MAX_MESSAGES, COVER_DIR, DOWNLOAD_DIR, VOICE_DIR)
from database.db import db
from helpers import media, vtags
from helpers.fastio import FastClient
from helpers.autodelete import schedule_delete
from helpers.media import MediaError
from helpers.state import BATCH
from helpers.utils import (AUDIO_EXTS, current_title, esc, fmt_duration, fmt_trim, humanbytes,
                           sanitize_filename)
from logger import LOGGER

logger = LOGGER(__name__)


class UserError(Exception):
    """Error message shown directly to the user."""


# ======================================================
# small helpers
# ======================================================

def get_audio_media(m):
    """If the message has audio (audio or an audio-type document), returns its media object, otherwise None."""
    if m is None or getattr(m, "empty", False):
        return None
    if getattr(m, "audio", None):
        return m.audio
    d = getattr(m, "document", None)
    if d and ((d.mime_type or "").lower().startswith("audio/") or (d.file_name or "").lower().endswith(AUDIO_EXTS)):
        return d
    return None


def compute_trim(duration, start_trim, end_trim):
    """
    Returns (trim_start|None, trim_end|None). Raises UserError for an invalid combination.
      start_trim : seconds (audio starts from here)
      end_trim   : {"mode": "last"|"at", "sec": N}
        last -> remove the last N sec   |   at -> audio ends at N sec
    """
    ts = float(start_trim or 0)
    te = float(duration)
    if end_trim:
        sec = float(end_trim.get("sec", 0))
        te = duration - sec if end_trim.get("mode") == "last" else sec
    te = min(te, duration)
    if te - ts < 0.5:
        raise UserError(
            f"Trim settings do not fit this audio ({fmt_duration(duration)}) "
            f"(start: {fmt_trim(ts)}, end: {fmt_trim(max(te, 0))}). Check the trim in /status and change it."
        )
    return (ts if ts > 0.01 else None), (te if te < duration - 0.05 else None)


def build_caption(title, artist, duration, size, fallback):
    lines = [f"🎵 <b>{esc((title or fallback)[:200])}</b>"]
    if artist:
        lines.append(f"🎤 {esc(artist[:200])}")
    lines.append(f"⏱ {fmt_duration(duration)}  •  💾 {humanbytes(size)}")
    return "\n".join(lines)


def _bar(pct):
    filled = int(pct / 5)
    return "█" * filled + "░" * (20 - filled)


async def set_status(client, chat_id, status_id, text):
    if not status_id:
        return
    try:
        await client.edit_message_text(chat_id, status_id, text, parse_mode=enums.ParseMode.HTML)
    except FloodWait:
        pass
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"status edit failed: {e}")


def make_progress(client, chat_id, status_id, label):
    last = [0.0]

    async def cb(current, total):
        now = time.time()
        if now - last[0] < 4 and current != total:
            return
        last[0] = now
        pct = (current * 100 / total) if total else 0
        await set_status(
            client, chat_id, status_id,
            f"{label}\n<code>[{_bar(pct)}] {pct:.1f}%</code>\n💾 {humanbytes(current)} / {humanbytes(total)}",
        )

    return cb


# ======================================================
# cached custom cover / voice tag
# ======================================================

async def ensure_custom_cover(client, uid, doc):
    """Returns (cover_path, thumb_path) or (None, None). If the cache is missing, it is downloaded again from Telegram."""
    fid, uniq = doc.get("cover_file_id"), doc.get("cover_unique") or "x"
    if not fid:
        return None, None
    os.makedirs(COVER_DIR, exist_ok=True)
    cover = os.path.join(COVER_DIR, f"{uid}_{uniq}.jpg")
    thumb = os.path.join(COVER_DIR, f"{uid}_{uniq}_t.jpg")
    if os.path.exists(cover) and os.path.exists(thumb):
        return cover, thumb
    tmp = os.path.join(COVER_DIR, f"{uid}_{uuid.uuid4().hex[:6]}.src")
    try:
        src = await client.download_media(fid, file_name=tmp)
        await media.make_custom_cover(src, cover, thumb)
        try:
            os.remove(src)
        except Exception:
            pass
        return cover, thumb
    except Exception as e:
        logger.warning(f"custom cover restore failed for {uid}: {e}")
        return None, None


def tag_path(uid, tag):
    return os.path.join(VOICE_DIR, f"{uid}_{tag.get('unique') or 'x'}_{tag['id']}.bin")


async def ensure_tag_file(client, uid, tag):
    """Local file of one saved voice tag (cache). Downloads again from Telegram if missing. Returns path or None."""
    if not tag or not tag.get("file_id"):
        return None
    os.makedirs(VOICE_DIR, exist_ok=True)
    path = tag_path(uid, tag)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        got = await client.download_media(tag["file_id"], file_name=path)
        if got and got != path and os.path.exists(got):
            shutil.move(got, path)
        return path if os.path.exists(path) and os.path.getsize(path) > 0 else None
    except Exception as e:
        logger.warning(f"voice tag restore failed for {uid}: {e}")
        return None


def drop_tag_file(uid, tag):
    try:
        os.remove(tag_path(uid, tag))
    except Exception:
        pass


_TAG_DUR = {}


async def tag_duration(path):
    """Exact duration of a cached tag file (ffprobe once, then memory)."""
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return 0.0
    if key not in _TAG_DUR:
        _TAG_DUR[key] = (await media.probe(path))["duration"]
    return _TAG_DUR[key]


def drop_cached(uid, kind, keep=()):
    """Remove old cache files (kind: cover | voice). keep = do not remove these file names."""
    folder = COVER_DIR if kind == "cover" else VOICE_DIR
    try:
        for f in os.listdir(folder):
            if f.startswith(f"{uid}_") and f not in keep:
                os.remove(os.path.join(folder, f))
    except Exception:
        pass


async def prepare_inserts(client, uid, doc, plan, eff_dur):
    """
    plan -> ([(t, local_path, tag_duration)], [skipped notes]).
    Positions are counted from the start of the (trimmed) audio.
    """
    if not plan:
        return [], []
    resolved, skipped = vtags.resolve(plan, list(doc.get("voice_tags") or []), eff_dur)
    if not resolved:
        return [], skipped
    uniq = {}
    for _, tag in resolved:
        uniq[tag["id"]] = tag
    paths = dict(zip(uniq, await asyncio.gather(*[ensure_tag_file(client, uid, t) for t in uniq.values()])))
    inserts = []
    for t, tag in resolved:
        path = paths.get(tag["id"])
        if not path:
            skipped.append(f"tag {esc(str(tag['name']))} (file not available)")
            continue
        try:
            inserts.append((t, path, await tag_duration(path)))
        except MediaError:
            skipped.append(f"tag {esc(str(tag['name']))} (not a valid audio)")
    return inserts, skipped


# ======================================================
# download (with check + one retry) and look-ahead
# ======================================================

async def download_audio(downloader, src_msg, mediaobj, folder, progress=None):
    """Download one audio. Verifies the size and retries once, so a half-downloaded file is never processed."""
    expected = int(getattr(mediaobj, "file_size", 0) or 0)
    os.makedirs(folder, exist_ok=True)
    for attempt in (1, 2):
        path = await downloader.download_media(src_msg, file_name=folder + os.sep, progress=progress)
        if path and os.path.exists(path) and (not expected or os.path.getsize(path) == expected):
            return path
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        if attempt == 1:
            await asyncio.sleep(1)
    raise UserError("Download failed. Please try again.")


class Prefetch:
    """Downloads the next audio of a batch while the current one is being processed / uploaded."""

    def __init__(self, downloader, msg, uid):
        self.workdir = os.path.join(DOWNLOAD_DIR, str(uid), uuid.uuid4().hex[:8])
        self.task = asyncio.create_task(
            download_audio(downloader, msg, get_audio_media(msg), os.path.join(self.workdir, "in")))

    async def discard(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        shutil.rmtree(self.workdir, ignore_errors=True)


# ======================================================
# prepare cover (shared)
# ======================================================

async def prepare_cover(client, downloader, uid, doc, src_path, info, mediaobj, workdir):
    """
    Based on cover_mode: (cover_path_to_embed, thumb_path).
      original -> the audio's own cover (always re-attached)
      custom   -> the user's cover
      none     -> no cover
    """
    mode = doc.get("cover_mode", "original")
    cover = thumb = None
    if mode == "none":
        return None, None
    if mode == "custom":
        cover, thumb = await ensure_custom_cover(client, uid, doc)
        if cover:
            return cover, thumb
        # custom cover could not be restored -> fall back to original
    if info and info.get("has_cover"):
        ex = os.path.join(workdir, "orig_cover.jpg")
        if await media.extract_cover(src_path, ex):
            cover = ex
            th = os.path.join(workdir, "thumb.jpg")
            thumb = th if await media.make_thumb(ex, th) else None
    if not thumb and mediaobj is not None:
        try:
            thumbs = getattr(mediaobj, "thumbs", None)
            if thumbs:
                got = await downloader.download_media(thumbs[0].file_id, file_name=os.path.join(workdir, "tg_thumb.jpg"))
                if got:
                    th = os.path.join(workdir, "thumb2.jpg")
                    thumb = th if await media.make_thumb(got, th) else None
        except Exception:
            pass
    return cover, thumb


# ======================================================
# deliver (send + target + autodelete)
# ======================================================

async def _flood_retry(factory, tries=3):
    for i in range(tries):
        try:
            return await factory()
        except FloodWait as e:
            if i == tries - 1:
                raise
            await asyncio.sleep(e.value + 1)


async def deliver(client, doc, chat_id, out_path, *, title, artist, duration, thumb, file_name, status_id, label, fallback_name):
    """Send the final audio to the user + copy to target channel + auto delete. Returns (sent_msg, target_error|None)."""
    size = os.path.getsize(out_path)
    caption = build_caption(title, artist, duration, size, fallback_name) if doc.get("caption_on", True) else ""
    prog = make_progress(client, chat_id, status_id, f"⬆️ Uploading... {label}")

    sent = await _flood_retry(lambda: client.send_audio(
        chat_id, out_path, caption=caption, parse_mode=enums.ParseMode.HTML,
        duration=int(duration), performer=artist, title=title, thumb=thumb,
        file_name=file_name, progress=prog,
    ))

    target_error = None
    target = doc.get("target_chat")
    if target:
        try:
            await _flood_retry(lambda: client.copy_message(target, chat_id, sent.id))
        except Exception as e:
            logger.warning(f"target copy failed: {e}")
            target_error = str(e)

    autodel = doc.get("autodel")
    if autodel:
        await schedule_delete(client, chat_id, [sent.id], autodel)
    return sent, target_error


async def render_with_tags(src, out, *, info, trim_start, trim_end, eff_dur, inserts, cover, title, artist,
                           bitrate_k, workdir, meta_src=None):
    """
    Voice tags inside the audio, as fast as possible:
      mp3 source      -> cut with stream copy + join with pre-encoded tags (no decode / re-encode: seconds)
      other formats   -> ONE mp3 encode, then the same copy-join
    If anything goes wrong, falls back to the slower full re-encode so the user still gets the file.
    """
    meta_src = meta_src or src
    try:
        mp3, ts, te, minfo = src, trim_start, trim_end, info
        if not info["is_mp3"]:
            mp3 = os.path.join(workdir, "mid.mp3")
            await media.render_audio([src], mp3, trim_start=trim_start, trim_end=trim_end, cover=None,
                                     bitrate_k=bitrate_k)
            ts = te = None
            minfo = await media.probe(mp3)
        return await media.insert_tags_fast(
            mp3, out, inserts, ts=ts or 0.0, trim_end=te, main_len=eff_dur,
            sample_rate=minfo["sample_rate"], channels=minfo["channels"], bitrate_k=bitrate_k,
            workdir=workdir, cover=cover, title=title, artist=artist, meta_src=meta_src)
    except media.MediaError as e:
        logger.warning(f"fast voice tag join failed ({e}); using full re-encode")
        return await media.render_audio(
            [src], out, trim_start=trim_start, trim_end=trim_end, inserts=inserts, main_len=eff_dur,
            cover=cover, title=title, artist=artist, bitrate_k=bitrate_k)


# ======================================================
# single audio
# ======================================================

async def process_audio(client, uid, *, src_msg, downloader, chat_id, status_id, input_msg_id=None, prefix="",
                        plan=None, pre=None, on_downloaded=None):
    """
    Fully process one audio message and send it to the user.
    plan = voice tag plan (None = no voice tag), pre = Prefetch of this audio (batch look-ahead).
    Returns dict(name, target_error, voice_skipped, voice_used). Raises UserError / MediaError on error.
    """
    mediaobj = get_audio_media(src_msg)
    if mediaobj is None:
        if pre:
            await pre.discard()
        raise UserError("There is no audio in this message.")

    doc = dict(await db.get_user(uid))  # settings snapshot
    orig_name = getattr(mediaobj, "file_name", None) or f"audio_{src_msg.id}.mp3"
    short = esc(orig_name[:60])
    if pre:
        workdir = pre.workdir
    else:
        workdir = os.path.join(DOWNLOAD_DIR, str(uid), uuid.uuid4().hex[:8])
    os.makedirs(os.path.join(workdir, "in"), exist_ok=True)

    try:
        # 1. download
        t_dl = time.time()
        await set_status(client, chat_id, status_id, f"{prefix}⬇️ Downloading... <b>{short}</b>")
        if pre:
            path = await pre.task
        else:
            path = await download_audio(
                downloader, src_msg, mediaobj, os.path.join(workdir, "in"),
                progress=make_progress(client, chat_id, status_id, f"{prefix}⬇️ Downloading... {short}"),
            )
        t_dl = time.time() - t_dl
        if on_downloaded:
            on_downloaded()

        # 2. probe
        await set_status(client, chat_id, status_id, f"{prefix}⚙️ Processing... <b>{short}</b>")
        info = await media.probe(path)
        duration = info["duration"]

        # 3. settings
        artist_custom = doc.get("artist") or None
        title_custom = current_title(doc)
        trim_start, trim_end = compute_trim(duration, doc.get("start_trim"), doc.get("end_trim"))
        eff_dur = (trim_end if trim_end is not None else duration) - (trim_start or 0.0)
        (inserts, voice_skipped), (cover, thumb) = await asyncio.gather(
            prepare_inserts(client, uid, doc, plan, eff_dur),
            prepare_cover(client, downloader, uid, doc, path, info, mediaobj, workdir),
        )

        stem = sanitize_filename(title_custom) if title_custom else sanitize_filename(os.path.splitext(orig_name)[0])
        out = os.path.join(workdir, stem + ".mp3")

        # 4. ffmpeg
        t_edit = time.time()
        if inserts:
            await render_with_tags(
                path, out, info=info, trim_start=trim_start, trim_end=trim_end, eff_dur=eff_dur, inserts=inserts,
                cover=cover, title=title_custom, artist=artist_custom,
                bitrate_k=media.pick_bitrate(info["bitrate"]), workdir=workdir)
        else:
            await media.render_audio(
                [path], out,
                trim_start=trim_start, trim_end=trim_end, cover=cover,
                title=title_custom, artist=artist_custom,
                bitrate_k=media.pick_bitrate(info["bitrate"]),
                copy_ok=info["is_mp3"],
            )
        t_edit = time.time() - t_edit
        out_info = await media.probe(out)

        # 5. send
        final_title = title_custom or info["title"] or getattr(mediaobj, "title", None)
        final_artist = artist_custom or info["artist"] or getattr(mediaobj, "performer", None)
        t_up = time.time()
        sent, target_error = await deliver(
            client, doc, chat_id, out,
            title=final_title, artist=final_artist, duration=out_info["duration"], thumb=thumb,
            file_name=os.path.basename(out), status_id=status_id, label=short, fallback_name=stem,
        )
        t_up = time.time() - t_up
        mb = os.path.getsize(out) / 1048576
        logger.info(f"⏱ {orig_name[:40]} | download {t_dl:.1f}s | edit {t_edit:.1f}s | upload {t_up:.1f}s "
                    f"({mb / max(t_up, 0.01):.1f} MB/s, {mb:.1f} MB)")

        # 6. input delete + counters
        if input_msg_id and doc.get("delinput") is not None:
            await schedule_delete(client, chat_id, [input_msg_id], doc["delinput"])
        await db.add_traffic(uid)
        await db.advance_title(uid)
        return {"name": os.path.basename(out), "target_error": target_error,
                "voice_skipped": voice_skipped, "voice_used": len(inserts)}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ======================================================
# batch (public + private via login session)
# ======================================================

async def open_session(uid):
    sess = await db.get_session(uid)
    if not sess:
        raise UserError(
            "🔒 <b>Authentication Required</b>\n\n"
            "Login is required for this content. Connect your account using /login."
        )
    acc = FastClient(
        f"audioed_{uid}", session_string=sess, api_id=API_ID, api_hash=API_HASH,
        in_memory=True, max_concurrent_transmissions=5,
    )
    try:
        await acc.connect()
    except Exception as e:
        raise UserError(f"❌ <b>Authentication Failed</b>\n\nThe session may have expired. Use /logout and then /login again.\n<code>{esc(e)}</code>")
    return acc


class Fetcher:
    """Fetches channel posts: first via the bot (public), otherwise via the user's login session."""

    def __init__(self, client, uid):
        self.client, self.uid, self.acc = client, uid, None

    async def _session(self):
        if self.acc is None:
            self.acc = await open_session(self.uid)
        return self.acc

    async def get(self, kind, target, ids):
        """Returns (messages_list, downloader_client)."""
        bot_msgs = None
        if kind == "public":
            try:
                res = await self.client.get_messages(target, ids)
                bot_msgs = res if isinstance(res, list) else [res]
                if any(m is not None and not getattr(m, "empty", False) for m in bot_msgs):
                    return bot_msgs, self.client
            except Exception as e:
                logger.debug(f"bot fetch failed for {target}: {e}")
            if not await db.get_session(self.uid):
                if bot_msgs is not None:
                    return bot_msgs, self.client  # all messages must be deleted/empty
                raise UserError(
                    "🔒 <b>Authentication Required</b>\n\n"
                    "This channel cannot be accessed by the bot. Connect your account using /login."
                )
        acc = await self._session()
        try:
            res = await acc.get_messages(target, ids)
        except Exception:
            try:
                await acc.get_chat(target)
                res = await acc.get_messages(target, ids)
            except Exception as e2:
                raise UserError(
                    "❌ <b>Unable to Fetch Messages</b>\n\n"
                    "Check that the logged-in account is a member of that channel/group and that the link is correct.\n"
                    f"<code>{esc(e2)}</code>"
                )
        return (res if isinstance(res, list) else [res]), acc

    async def close(self):
        if self.acc is not None:
            try:
                await self.acc.disconnect()
            except Exception:
                pass
            self.acc = None


async def validate_first_link(client, uid, kind, target, msg_id):
    """Does the first link of the batch contain audio? Returns True/False (raises UserError on error)."""
    f = Fetcher(client, uid)
    try:
        msgs, _ = await f.get(kind, target, [msg_id])
        return get_audio_media(msgs[0] if msgs else None) is not None
    finally:
        await f.close()


async def run_batch(client, uid, chat_id, status_id, kind, target, from_id, to_id, plan=None):
    ids = list(range(from_id, to_id + 1))[:BATCH_MAX_MESSAGES]
    total = len(ids)
    done = skipped = failed = 0
    tag_skipped = 0
    errors, reason = [], None
    fetcher = Fetcher(client, uid)
    BATCH[uid] = {"done": 0, "total": total}
    pre_map = {}   # index in msgs -> Prefetch of the next audio
    try:
        seen = 0
        for i in range(0, total, 50):
            msgs, downloader = await fetcher.get(kind, target, ids[i:i + 50])
            audio_idx = [k for k, m in enumerate(msgs) if get_audio_media(m) is not None]
            for k, m in enumerate(msgs):
                seen += 1
                BATCH[uid]["done"] = seen
                if get_audio_media(m) is None:
                    skipped += 1
                    continue
                pre = pre_map.pop(k, None)
                rem = await db.remaining_allowance(uid)
                if rem is not None and rem <= 0:
                    reason = "limit"
                    if pre:
                        await pre.discard()
                    break

                def start_next(k=k):
                    """Current audio is downloaded -> start downloading the next one in the background."""
                    nxt = next((x for x in audio_idx if x > k), None)
                    if nxt is not None and nxt not in pre_map:
                        pre_map[nxt] = Prefetch(downloader, msgs[nxt], uid)

                try:
                    res = await process_audio(
                        client, uid, src_msg=m, downloader=downloader, chat_id=chat_id,
                        status_id=status_id, prefix=f"📁 Batch {seen}/{total}\n",
                        plan=plan, pre=pre, on_downloaded=start_next,
                    )
                    done += 1
                    tag_skipped += 1 if res.get("voice_skipped") else 0
                except (UserError, MediaError) as e:
                    failed += 1
                    errors.append(f"#{m.id}: {e}")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    failed += 1
                    errors.append(f"#{m.id}: {e}")
                    logger.error(f"batch item failed: {e}", exc_info=True)
            if reason:
                break
    except asyncio.CancelledError:
        reason = "stopped"
        raise
    except UserError as e:
        reason = "error"
        errors.append(str(e))
    finally:
        for pf in list(pre_map.values()):
            await pf.discard()
        pre_map.clear()
        BATCH.pop(uid, None)
        await fetcher.close()
        head = {
            "stopped": "🛑 <b>Batch Stopped</b>",
            "limit": "❌ <b>Daily limit reached - batch stopped</b>",
            "error": "❌ <b>Batch Failed</b>",
        }.get(reason, "✅ <b>Batch Complete</b>")
        lines = [head, "", f"✅ Done: <b>{done}</b>", f"⏭ Skipped (no audio): <b>{skipped}</b>", f"❌ Failed: <b>{failed}</b>"]
        if tag_skipped:
            lines.append(f"🔊 Voice tag skipped (position beyond audio length) in: <b>{tag_skipped}</b>")
        if errors:
            lines += ["", "<b>Errors:</b>"] + [f"• {e if e.startswith('🔒') or e.startswith('❌') else esc(e)}" for e in errors[:5]]
        if reason == "limit":
            lines += ["", "<i>Get Premium for unlimited batch - /myplan</i>"]
        try:
            await client.send_message(chat_id, "\n".join(lines), parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass
        try:
            await client.delete_messages(chat_id, [status_id])
        except Exception:
            pass


# ======================================================
# merge
# ======================================================

async def run_merge(client, uid, chat_id, status_id, items, panel_id=None, plan=None):
    """items: [{"msg_id", "name"}] audios sent in the user's chat (in the order they were sent)."""
    doc = dict(await db.get_user(uid))
    n = len(items)
    workdir = os.path.join(DOWNLOAD_DIR, str(uid), "merge_" + uuid.uuid4().hex[:8])
    os.makedirs(os.path.join(workdir, "in"), exist_ok=True)
    try:
        msgs = await client.get_messages(chat_id, [i["msg_id"] for i in items])
        msgs = msgs if isinstance(msgs, list) else [msgs]
        valid = [(m, get_audio_media(m)) for m in msgs]
        valid = [(m, mo) for m, mo in valid if mo is not None]  # the user may have deleted some
        first_media = valid[0][1] if valid else None

        # download up to 3 files at the same time (order is kept)
        sem = asyncio.Semaphore(3)
        finished = [0]

        async def grab(k, m, mo):
            async with sem:
                p = await download_audio(client, m, mo, os.path.join(workdir, "in", f"{k:03d}"))
            finished[0] += 1
            await set_status(client, chat_id, status_id, f"🔗 Merge: ⬇️ Downloaded {finished[0]}/{len(valid)}")
            return p

        await set_status(client, chat_id, status_id, f"🔗 Merge: ⬇️ Downloading {len(valid)} audios...")
        grabs = [asyncio.create_task(grab(k, m, mo)) for k, (m, mo) in enumerate(valid, 1)]
        try:
            paths = list(await asyncio.gather(*grabs))
        except BaseException:
            for g in grabs:
                g.cancel()
            await asyncio.gather(*grabs, return_exceptions=True)
            raise
        infos = [await media.probe(p) for p in paths]
        if len(paths) < 2:
            raise UserError("At least 2 valid audios are required for merge.")

        await set_status(client, chat_id, status_id, f"🔗 Merging {len(paths)} audios... ⚙️")
        first = infos[0]
        artist_custom = doc.get("artist") or None
        title_custom = current_title(doc)
        cover, thumb = await prepare_cover(client, client, uid, doc, paths[0], first, first_media, workdir)

        title_final = title_custom or "Merged Audio"
        out = os.path.join(workdir, sanitize_filename(title_final, "Merged_Audio") + ".mp3")
        br = max(media.pick_bitrate(i["bitrate"]) for i in infos)

        inserts, voice_skipped = [], []
        total_len = sum(i["duration"] for i in infos)
        if plan:
            inserts, voice_skipped = await prepare_inserts(client, uid, doc, plan, total_len)
        if inserts:
            # merge (one encode), then place the voice tags by stream copy
            joined = os.path.join(workdir, "joined.mp3")
            await media.render_audio(paths, joined, bitrate_k=br)
            jinfo = await media.probe(joined)
            await render_with_tags(
                joined, out, info=jinfo, trim_start=None, trim_end=None, eff_dur=jinfo["duration"],
                inserts=[(min(t, jinfo["duration"]), p, d) for t, p, d in inserts],
                cover=cover, title=title_final, artist=artist_custom, bitrate_k=br, workdir=workdir,
                meta_src=paths[0])
        else:
            await media.render_audio(
                paths, out, cover=cover, title=title_final, artist=artist_custom, bitrate_k=br,
            )
        out_info = await media.probe(out)
        final_artist = artist_custom or first["artist"]

        sent, target_error = await deliver(
            client, doc, chat_id, out,
            title=title_final, artist=final_artist, duration=out_info["duration"], thumb=thumb,
            file_name=os.path.basename(out), status_id=status_id, label="merged audio", fallback_name="Merged Audio",
        )

        ids = [i["msg_id"] for i in items]
        if doc.get("delinput") is not None:
            await schedule_delete(client, chat_id, ids, doc["delinput"])
        await db.add_traffic(uid, len(paths))
        await db.advance_title(uid)
        return {"name": os.path.basename(out), "count": len(paths), "target_error": target_error,
                "voice_skipped": voice_skipped, "voice_used": len(inserts)}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
