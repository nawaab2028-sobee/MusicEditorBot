"""
Core processing pipeline:
  download -> probe -> (trim / voice tag / convert / tags / cover via ffmpeg) -> resend
  -> target channel copy -> auto delete -> counters

Direct audio, Batch (public + private/login) aur Merge teeno yahi use karte hain.
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
from helpers import media
from helpers.autodelete import schedule_delete
from helpers.media import MediaError
from helpers.state import BATCH
from helpers.utils import (AUDIO_EXTS, current_title, esc, fmt_duration, fmt_trim, humanbytes,
                           sanitize_filename)
from logger import LOGGER

logger = LOGGER(__name__)


class UserError(Exception):
    """User ko seedha dikhane wala error message."""


# ======================================================
# small helpers
# ======================================================

def get_audio_media(m):
    """Message me audio (audio ya audio-type document) ho to uska media object, warna None."""
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
    Returns (trim_start|None, trim_end|None). Invalid combo par UserError.
      start_trim : seconds (audio yaha se shuru)
      end_trim   : {"mode": "last"|"at", "sec": N}
        last -> aakhri N sec hata do   |   at -> N sec par audio khatam
    """
    ts = float(start_trim or 0)
    te = float(duration)
    if end_trim:
        sec = float(end_trim.get("sec", 0))
        te = duration - sec if end_trim.get("mode") == "last" else sec
    te = min(te, duration)
    if te - ts < 0.5:
        raise UserError(
            f"Trim settings is audio ({fmt_duration(duration)}) par fit nahi hoti "
            f"(start: {fmt_trim(ts)}, end: {fmt_trim(max(te, 0))}). /status me trim check karke change karo."
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
    """Returns (cover_path, thumb_path) ya (None, None). Cache missing ho to Telegram se dobara download."""
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


async def ensure_voice(client, uid, doc):
    """Voice tag ki local file (cache). Returns path ya None."""
    fid, uniq = doc.get("voice_file_id"), doc.get("voice_unique") or "x"
    if not fid:
        return None
    os.makedirs(VOICE_DIR, exist_ok=True)
    path = os.path.join(VOICE_DIR, f"{uid}_{uniq}.bin")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        got = await client.download_media(fid, file_name=path)
        if got and got != path and os.path.exists(got):
            shutil.move(got, path)
        return path if os.path.exists(path) else None
    except Exception as e:
        logger.warning(f"voice tag restore failed for {uid}: {e}")
        return None


def drop_cached(uid, kind, keep=()):
    """Purani cache files hata do (kind: cover | voice). keep = in file names ko mat hatao."""
    folder = COVER_DIR if kind == "cover" else VOICE_DIR
    try:
        for f in os.listdir(folder):
            if f.startswith(f"{uid}_") and f not in keep:
                os.remove(os.path.join(folder, f))
    except Exception:
        pass


# ======================================================
# prepare cover (shared)
# ======================================================

async def prepare_cover(client, downloader, uid, doc, src_path, info, mediaobj, workdir):
    """
    cover_mode ke hisaab se (cover_path_to_embed, thumb_path).
      original -> audio ka apna cover (hamesha re-attach)
      custom   -> user ka cover
      none     -> koi cover nahi
    """
    mode = doc.get("cover_mode", "original")
    cover = thumb = None
    if mode == "none":
        return None, None
    if mode == "custom":
        cover, thumb = await ensure_custom_cover(client, uid, doc)
        if cover:
            return cover, thumb
        # custom cover restore nahi hua -> original par fallback
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
    """Final audio user ko bhejo + target channel copy + auto delete. Returns (sent_msg, target_error|None)."""
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


# ======================================================
# single audio
# ======================================================

async def process_audio(client, uid, *, src_msg, downloader, chat_id, status_id, input_msg_id=None, prefix=""):
    """
    Ek audio message ko poora process karke user ko bhejo.
    Returns dict(name, target_error). Error par UserError / MediaError.
    """
    mediaobj = get_audio_media(src_msg)
    if mediaobj is None:
        raise UserError("Is message me audio nahi hai.")

    doc = dict(await db.get_user(uid))  # settings snapshot
    orig_name = getattr(mediaobj, "file_name", None) or f"audio_{src_msg.id}.mp3"
    short = esc(orig_name[:60])
    workdir = os.path.join(DOWNLOAD_DIR, str(uid), uuid.uuid4().hex[:8])
    os.makedirs(os.path.join(workdir, "in"), exist_ok=True)

    try:
        # 1. download
        await set_status(client, chat_id, status_id, f"{prefix}⬇️ Downloading... <b>{short}</b>")
        path = await downloader.download_media(
            src_msg, file_name=os.path.join(workdir, "in") + os.sep,
            progress=make_progress(client, chat_id, status_id, f"{prefix}⬇️ Downloading... {short}"),
        )
        if not path or not os.path.exists(path):
            raise UserError("Download fail ho gaya.")

        # 2. probe
        await set_status(client, chat_id, status_id, f"{prefix}⚙️ Processing... <b>{short}</b>")
        info = await media.probe(path)
        duration = info["duration"]

        # 3. settings
        artist_custom = doc.get("artist") or None
        title_custom = current_title(doc)
        trim_start, trim_end = compute_trim(duration, doc.get("start_trim"), doc.get("end_trim"))
        voice = await ensure_voice(client, uid, doc) if doc.get("voice_file_id") else None
        cover, thumb = await prepare_cover(client, downloader, uid, doc, path, info, mediaobj, workdir)

        stem = sanitize_filename(title_custom) if title_custom else sanitize_filename(os.path.splitext(orig_name)[0])
        out = os.path.join(workdir, stem + ".mp3")

        # 4. ffmpeg
        await media.render_audio(
            [path], out,
            trim_start=trim_start, trim_end=trim_end, voice_tag=voice, cover=cover,
            title=title_custom, artist=artist_custom,
            bitrate_k=media.pick_bitrate(info["bitrate"]),
            copy_ok=info["is_mp3"] and not voice,
        )
        out_info = await media.probe(out)

        # 5. send
        final_title = title_custom or info["title"] or getattr(mediaobj, "title", None)
        final_artist = artist_custom or info["artist"] or getattr(mediaobj, "performer", None)
        sent, target_error = await deliver(
            client, doc, chat_id, out,
            title=final_title, artist=final_artist, duration=out_info["duration"], thumb=thumb,
            file_name=os.path.basename(out), status_id=status_id, label=short, fallback_name=stem,
        )

        # 6. input delete + counters
        if input_msg_id and doc.get("delinput") is not None:
            await schedule_delete(client, chat_id, [input_msg_id], doc["delinput"])
        await db.add_traffic(uid)
        await db.advance_title(uid)
        return {"name": os.path.basename(out), "target_error": target_error}
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
            "Is content ke liye login chahiye. /login se apna account connect karo."
        )
    acc = Client(
        f"audioed_{uid}", session_string=sess, api_id=API_ID, api_hash=API_HASH,
        in_memory=True, max_concurrent_transmissions=5,
    )
    try:
        await acc.connect()
    except Exception as e:
        raise UserError(f"❌ <b>Authentication Failed</b>\n\nSession expire ho sakta hai. /logout karke /login karo.\n<code>{esc(e)}</code>")
    return acc


class Fetcher:
    """Channel posts laata hai: pehle bot se (public), warna user ke login session se."""

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
                    return bot_msgs, self.client  # sab messages deleted/empty hi honge
                raise UserError(
                    "🔒 <b>Authentication Required</b>\n\n"
                    "Yeh channel bot se access nahi ho raha. /login se apna account connect karo."
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
                    "Check karo ki logged-in account us channel/group ka member hai aur link sahi hai.\n"
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
    """Batch ke pehle link me audio hai? Returns True/False (error par UserError)."""
    f = Fetcher(client, uid)
    try:
        msgs, _ = await f.get(kind, target, [msg_id])
        return get_audio_media(msgs[0] if msgs else None) is not None
    finally:
        await f.close()


async def run_batch(client, uid, chat_id, status_id, kind, target, from_id, to_id):
    ids = list(range(from_id, to_id + 1))[:BATCH_MAX_MESSAGES]
    total = len(ids)
    done = skipped = failed = 0
    errors, reason = [], None
    fetcher = Fetcher(client, uid)
    BATCH[uid] = {"done": 0, "total": total}
    try:
        seen = 0
        for i in range(0, total, 50):
            msgs, downloader = await fetcher.get(kind, target, ids[i:i + 50])
            for m in msgs:
                seen += 1
                BATCH[uid]["done"] = seen
                if get_audio_media(m) is None:
                    skipped += 1
                    continue
                rem = await db.remaining_allowance(uid)
                if rem is not None and rem <= 0:
                    reason = "limit"
                    break
                try:
                    await process_audio(
                        client, uid, src_msg=m, downloader=downloader, chat_id=chat_id,
                        status_id=status_id, prefix=f"📁 Batch {seen}/{total}\n",
                    )
                    done += 1
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
        BATCH.pop(uid, None)
        await fetcher.close()
        head = {
            "stopped": "🛑 <b>Batch Stopped</b>",
            "limit": "❌ <b>Daily limit reached - batch roka gaya</b>",
            "error": "❌ <b>Batch Failed</b>",
        }.get(reason, "✅ <b>Batch Complete</b>")
        lines = [head, "", f"✅ Done: <b>{done}</b>", f"⏭ Skipped (no audio): <b>{skipped}</b>", f"❌ Failed: <b>{failed}</b>"]
        if errors:
            lines += ["", "<b>Errors:</b>"] + [f"• {e if e.startswith('🔒') or e.startswith('❌') else esc(e)}" for e in errors[:5]]
        if reason == "limit":
            lines += ["", "<i>Premium lo - unlimited batch ke liye /myplan</i>"]
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

async def run_merge(client, uid, chat_id, status_id, items, panel_id=None):
    """items: [{"msg_id", "name"}] user ke chat me bheji hui audios (send order me)."""
    doc = dict(await db.get_user(uid))
    n = len(items)
    workdir = os.path.join(DOWNLOAD_DIR, str(uid), "merge_" + uuid.uuid4().hex[:8])
    os.makedirs(os.path.join(workdir, "in"), exist_ok=True)
    try:
        msgs = await client.get_messages(chat_id, [i["msg_id"] for i in items])
        msgs = msgs if isinstance(msgs, list) else [msgs]
        paths, infos, first_media = [], [], None
        for k, m in enumerate(msgs, 1):
            mo = get_audio_media(m)
            if mo is None:
                continue  # user ne delete kar diya hoga
            first_media = first_media or mo
            label = f"🔗 Merge: {k}/{n}\n"
            nm = esc((getattr(mo, "file_name", None) or "audio")[:50])
            p = await client.download_media(
                m, file_name=os.path.join(workdir, "in", f"{k:03d}") + os.sep,
                progress=make_progress(client, chat_id, status_id, f"{label}⬇️ Downloading... {nm}"),
            )
            if not p:
                raise UserError(f"{nm} download nahi hua.")
            paths.append(p)
            infos.append(await media.probe(p))
        if len(paths) < 2:
            raise UserError("Merge ke liye kam se kam 2 valid audio chahiye.")

        await set_status(client, chat_id, status_id, f"🔗 Merging {len(paths)} audios... ⚙️")
        first = infos[0]
        artist_custom = doc.get("artist") or None
        title_custom = current_title(doc)
        voice = await ensure_voice(client, uid, doc) if doc.get("voice_file_id") else None
        cover, thumb = await prepare_cover(client, client, uid, doc, paths[0], first, first_media, workdir)

        title_final = title_custom or "Merged Audio"
        out = os.path.join(workdir, sanitize_filename(title_final, "Merged_Audio") + ".mp3")
        br = max(media.pick_bitrate(i["bitrate"]) for i in infos)
        await media.render_audio(
            paths, out, voice_tag=voice, cover=cover, title=title_final, artist=artist_custom, bitrate_k=br,
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
        return {"name": os.path.basename(out), "count": len(paths), "target_error": target_error}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
