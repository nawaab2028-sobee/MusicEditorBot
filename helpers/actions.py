"""
Shared actions used by several plugins:
  limit message, direct audio queue, batch start, merge panel + merge job.
"""

import asyncio

from pyrogram import enums

from config import BATCH_MAX_MESSAGES, FREE_DAILY_LIMIT, MERGE_MAX_FILES
from database.db import db, to_ist
from helpers import pipeline, ui
from helpers.jobs import Job, enqueue, pending_weight, queue_position
from helpers.media import MediaError
from helpers.pipeline import UserError, set_status
from helpers.state import MERGE, clear_user_state
from helpers.utils import esc, fmt_duration
from logger import LOGGER

logger = LOGGER(__name__)


# ======================================================
# limit
# ======================================================

async def limit_text(uid):
    used, reset = await db.get_usage(uid)
    pend = pending_weight(uid)
    shown = min(FREE_DAILY_LIMIT, used + pend)
    text = (
        "❌ <b>Daily Limit Exceeded!</b>\n\n"
        f"As a free user, your daily limit is <b>{FREE_DAILY_LIMIT}</b> files.\n"
        f"You have used <b>{shown}</b> files today (including files currently processing/queued).\n"
        "Remaining allowance: <b>0</b> files.\n"
    )
    if reset is not None:
        r = to_ist(reset)
        if r:
            text += f"\n🔄 Limit resets at: <code>{r.strftime('%d %b, %I:%M %p')} IST</code>\n"
    text += "\n💎 Get <b>Premium</b> and edit unlimited audios — /myplan"
    return text


LIMIT_ROWS = [[("💎 Upgrade to Premium", "premium_plans_btn", "success")]]


async def allowance_left(uid):
    """None = unlimited, otherwise the remaining allowance after subtracting queued/processing audios."""
    rem = await db.remaining_allowance(uid)
    if rem is None:
        return None
    return max(0, rem - pending_weight(uid))


async def send_limit(client, uid, chat_id, reply_to=None):
    await ui.send(client, chat_id, await limit_text(uid), rows=LIMIT_ROWS, reply_to=reply_to)


# ======================================================
# direct audio
# ======================================================

async def queue_direct_audio(client, message):
    uid, chat_id = message.from_user.id, message.chat.id
    await db.get_user(uid, message.from_user.first_name)

    left = await allowance_left(uid)
    if left is not None and left <= 0:
        return await send_limit(client, uid, chat_id, reply_to=message.id)

    ahead = queue_position(uid)
    status = await message.reply_text(
        f"⏳ <b>Queued</b> (#{ahead + 1}) — will start after the previous audio is complete." if ahead
        else "📥 <b>Starting...</b>",
        quote=True, parse_mode=enums.ParseMode.HTML,
    )

    async def run():
        try:
            res = await pipeline.process_audio(
                client, uid, src_msg=message, downloader=client, chat_id=chat_id,
                status_id=status.id, input_msg_id=message.id,
            )
            try:
                await status.delete()
            except Exception:
                pass
            if res.get("target_error"):
                await client.send_message(
                    chat_id,
                    "⚠️ <b>Could not send the audio to the target channel.</b>\n"
                    f"<code>{esc(res['target_error'])}</code>\n"
                    "Is the bot an admin in the channel? Check the Target Channel in /status.",
                    parse_mode=enums.ParseMode.HTML,
                )
        except asyncio.CancelledError:
            await set_status(client, chat_id, status.id, "🛑 <b>Stopped.</b>")
            raise
        except (UserError, MediaError) as e:
            await set_status(client, chat_id, status.id, f"❌ {e if str(e).startswith(('🔒', '❌')) else esc(e)}")
        except Exception as e:
            logger.error(f"audio job failed: {e}", exc_info=True)
            await set_status(client, chat_id, status.id, f"❌ <b>Failed:</b> <code>{esc(str(e)[:300])}</code>")

    async def dropped():
        await set_status(client, chat_id, status.id, "🛑 <b>Removed from the queue.</b>")

    await enqueue(uid, Job(run, weight=1, kind="audio", on_dropped=dropped))


# ======================================================
# batch
# ======================================================

async def start_batch(client, message, uid, kind, target, a, b):
    """Add a batch job to the queue. a..b = post id range."""
    chat_id = message.chat.id
    left = await allowance_left(uid)
    if left is not None and left <= 0:
        return await send_limit(client, uid, chat_id, reply_to=message.id)

    count = min(b - a + 1, BATCH_MAX_MESSAGES)
    note = f"\n<i>(Max {BATCH_MAX_MESSAGES} posts per batch)</i>" if (b - a + 1) > BATCH_MAX_MESSAGES else ""
    ahead = queue_position(uid)
    status = await message.reply_text(
        f"📁 <b>Batch {'queued' if ahead else 'starting'}</b>\nPosts: <b>{count}</b> ({a}-{a + count - 1}){note}",
        quote=True, parse_mode=enums.ParseMode.HTML,
    )
    weight = count if left is None else min(count, left)
    chat_id = message.chat.id

    async def run():
        await pipeline.run_batch(client, uid, chat_id, status.id, kind, target, a, a + count - 1)

    async def dropped():
        await set_status(client, chat_id, status.id, "🛑 <b>Batch removed from the queue.</b>")

    await enqueue(uid, Job(run, weight=weight if left is not None else 0, kind="batch", on_dropped=dropped))


# ======================================================
# merge
# ======================================================

def merge_text(uid):
    items = MERGE[uid]["items"]
    lines = [
        "🔗 <b>Merge Mode</b>",
        "",
        f"Send the audios you want to merge one after another (min 2, max {MERGE_MAX_FILES}).",
        "Merge order = the order in which you send them.",
        "",
        f"📋 <b>Queue ({len(items)}):</b>",
    ]
    if not items:
        lines.append("<i>No audio yet.</i>")
    shown = items[-15:]
    start = len(items) - len(shown)
    if start:
        lines.append(f"<i>... {start} earlier</i>")
    for k, it in enumerate(shown, start + 1):
        lines.append(f"{k}. {esc(it['name'][:45])} ({fmt_duration(it['duration'])})")
    lines += ["", "When ready, press <b>Merge Now</b>."]
    return "\n".join(lines)


def merge_rows():
    return [
        [("🔗 Merge Now", "mg:go", "success")],
        [("💥 Clear Files", "mg:clear", "danger"), ("❌ Cancel", "mg:cancel", "danger")],
    ]


_merge_tasks = {}


async def _merge_refresh(client, uid):
    await asyncio.sleep(1.2)  # combine quick-fire uploads into a single edit
    st = MERGE.get(uid)
    if not st or not st.get("panel_id"):
        return
    await ui.edit(client, st["chat_id"], st["panel_id"], text=merge_text(uid), rows=merge_rows())


def schedule_merge_refresh(client, uid):
    t = _merge_tasks.get(uid)
    if t and not t.done():
        return
    _merge_tasks[uid] = asyncio.create_task(_merge_refresh(client, uid))


async def add_merge_item(client, message, mediaobj):
    uid = message.from_user.id
    st = MERGE[uid]
    if len(st["items"]) >= MERGE_MAX_FILES:
        return await message.reply_text(f"⚠️ Only {MERGE_MAX_FILES} audios can be merged at most. Press <b>Merge Now</b>.",
                                        parse_mode=enums.ParseMode.HTML)
    st["items"].append({
        "msg_id": message.id,
        "name": getattr(mediaobj, "file_name", None) or getattr(mediaobj, "title", None) or f"audio_{message.id}",
        "duration": getattr(mediaobj, "duration", 0) or 0,
    })
    schedule_merge_refresh(client, uid)


async def start_merge(client, uid, chat_id, panel_msg_id):
    """Add the merge job to the queue (copy the items and then clear the MERGE state)."""
    st = MERGE.pop(uid)
    items = list(st["items"])
    n = len(items)
    ahead = queue_position(uid)
    await ui.edit(
        client, chat_id, panel_msg_id,
        text=f"🔗 <b>Merge {'queued' if ahead else 'starting'}</b> — {n} audios", rows=None,
    )

    async def run():
        try:
            res = await pipeline.run_merge(client, uid, chat_id, panel_msg_id, items)
            try:
                await client.delete_messages(chat_id, [panel_msg_id])
            except Exception:
                pass
            if res.get("target_error"):
                await client.send_message(
                    chat_id, f"⚠️ <b>Could not send to the target channel.</b>\n<code>{esc(res['target_error'])}</code>",
                    parse_mode=enums.ParseMode.HTML,
                )
        except asyncio.CancelledError:
            await set_status(client, chat_id, panel_msg_id, "🛑 <b>Merge stopped.</b>")
            raise
        except (UserError, MediaError) as e:
            await set_status(client, chat_id, panel_msg_id, f"❌ {e if str(e).startswith(('🔒', '❌')) else esc(e)}")
        except Exception as e:
            logger.error(f"merge job failed: {e}", exc_info=True)
            await set_status(client, chat_id, panel_msg_id, f"❌ <b>Merge failed:</b> <code>{esc(str(e)[:300])}</code>")

    async def dropped():
        await set_status(client, chat_id, panel_msg_id, "🛑 <b>Merge removed from the queue.</b>")

    await enqueue(uid, Job(run, weight=n, kind="merge", on_dropped=dropped))
