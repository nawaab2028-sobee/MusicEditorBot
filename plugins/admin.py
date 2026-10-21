"""
Owner tools: /stats, /broadcast, /ban, /unban  (+ blocks banned users)
"""

import asyncio
import time

import psutil
from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait, InputUserDeactivated, PeerIdInvalid, UserIsBlocked
from pyrogram.types import CallbackQuery, Message

from config import OWNER_ID
from database.db import db
from logger import LOGGER

logger = LOGGER(__name__)
HTML = enums.ParseMode.HTML
START_TIME = time.time()


# --- block banned users from everything (group -2 = runs first) ---
@Client.on_message(filters.private & filters.incoming, group=-2)
async def ban_gate_msg(client: Client, message: Message):
    u = message.from_user
    if not u or u.id == OWNER_ID:
        return
    if await db.is_banned(u.id):
        await message.reply_text("🚫 You are banned from this bot.")
        message.stop_propagation()


@Client.on_callback_query(group=-2)
async def ban_gate_cb(client: Client, cq: CallbackQuery):
    if cq.from_user.id != OWNER_ID and await db.is_banned(cq.from_user.id):
        await cq.answer("🚫 You are banned.", show_alert=True)
        cq.stop_propagation()


@Client.on_message(filters.command("ban") & filters.user(OWNER_ID))
async def ban(client: Client, message: Message):
    if len(message.command) < 2 or not message.command[1].lstrip("-").isdigit():
        return await message.reply_text("<b>Usage:</b> <code>/ban user_id</code>", parse_mode=HTML)
    uid = int(message.command[1])
    await db.get_user(uid, str(uid))
    await db.ban_user(uid)
    await message.reply_text(f"🚫 User <code>{uid}</code> banned.", parse_mode=HTML)


@Client.on_message(filters.command("unban") & filters.user(OWNER_ID))
async def unban(client: Client, message: Message):
    if len(message.command) < 2 or not message.command[1].lstrip("-").isdigit():
        return await message.reply_text("<b>Usage:</b> <code>/unban user_id</code>", parse_mode=HTML)
    uid = int(message.command[1])
    await db.unban_user(uid)
    await message.reply_text(f"✅ User <code>{uid}</code> unbanned.", parse_mode=HTML)


@Client.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def stats(client: Client, message: Message):
    up = int(time.time() - START_TIME)
    d, rem = divmod(up, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    await message.reply_text(
        "<b>📊 Bot Stats</b>\n\n"
        f"👥 Users: <code>{await db.total_users_count()}</code>\n"
        f"⏱ Uptime: <code>{d}d {h}h {m}m</code>\n"
        f"🖥 CPU: <code>{psutil.cpu_percent(interval=0.3)}%</code>\n"
        f"⚙️ RAM: <code>{psutil.virtual_memory().percent}%</code>\n"
        f"💿 Disk: <code>{psutil.disk_usage('/').percent}%</code>\n"
        f"🗄 MongoDB: <code>{'OK' if db.mongo_ok else 'DOWN (JSON mode)'}</code>",
        parse_mode=HTML)


@Client.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast(client: Client, message: Message):
    b_msg = message.reply_to_message
    if not b_msg:
        return await message.reply_text("<b>Reply to a message with /broadcast.</b>", parse_mode=HTML, quote=True)
    sts = await message.reply_text("<b>Broadcasting...</b>", parse_mode=HTML, quote=True)
    ids = await db.get_all_user_ids()
    ok = fail = 0
    for uid in ids:
        try:
            await b_msg.copy(chat_id=uid)
            ok += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await b_msg.copy(chat_id=uid)
                ok += 1
            except Exception:
                fail += 1
        except (InputUserDeactivated, UserIsBlocked, PeerIdInvalid):
            fail += 1
        except Exception as e:
            logger.debug(f"broadcast err {uid}: {e}")
            fail += 1
        await asyncio.sleep(0.1)
    await sts.edit_text(f"<b>📢 Broadcast Done</b>\n\n✅ Sent: <code>{ok}</code>\n❌ Failed: <code>{fail}</code>", parse_mode=HTML)
