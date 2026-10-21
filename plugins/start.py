"""
/start (image + caption + editor panel), /help, /status, /stop
"""

import asyncio
import random

import requests
from pyrogram import Client, enums, filters
from pyrogram.types import CallbackQuery, Message

from config import FREE_DAILY_LIMIT, START_IMAGE, START_IMAGE_FALLBACK
from database.db import db
from helpers import ui
from helpers.jobs import stop_all
from helpers.state import BATCH, INPUT, MERGE, PANEL, clear_user_state
from helpers.utils import current_title, esc, fmt_delay, fmt_trim
from logger import LOGGER

logger = LOGGER(__name__)

REACTIONS = ["👍", "❤️", "🔥", "🥰", "👏", "😁", "🤩", "🎉", "🙏", "👌", "💯", "⚡", "🏆", "😎", "🎧"]

START_TXT = """<b>👋 Hello {mention},</b>
<b>🎵 I am <a href="https://t.me/{username}">{name}</a></b>
<i>Your Advanced Bulk Audio Editor.</i>
<blockquote><b>✏️ Edit Artist, Title & Cover
🔊 Voice Tag  •  ✂️ Start/End Trim
📁 Batch Process  •  🔗 Merge Audio
📢 Auto-send to your Channel</b></blockquote>
<b>🎧 Send me any audio to get started!</b>
<i>Set your settings using the buttons below.</i>

Check /status to see your current config.
Use /stop to clear your queue and stop processing."""

HELP_TXT = """<b>📚 Audio Editor - Help</b>

<blockquote><b>1️⃣ Set your settings</b>
Open /start and set Artist, Cover, Title, Voice Tag, Trim, Caption and Auto Delete using the buttons. Settings stay saved.</blockquote>
<blockquote><b>2️⃣ Send audio</b>
Send any audio - the bot will download it, edit it and send it back.</blockquote>
<blockquote><b>3️⃣ Batch Process</b>
Process channel audios together: give the first and last post links, or directly send <code>https://t.me/channel/100-110</code>. For a private channel, use /login first.</blockquote>
<blockquote><b>4️⃣ Merge Audio</b>
Press the Merge button, send the audios, then press <b>Merge Now</b>.</blockquote>
<blockquote><b>5️⃣ Trim</b>
Time format: <code>SS</code>, <code>MM:SS</code> or <code>HH:MM:SS</code></blockquote>

<b>📱 Commands</b>
/start - Open the panel
/status - All your settings
/stop - Clear queue + stop processing
/myplan - Plan & daily limit
/login /logout - Private channel access
/cancel - Cancel the current input

<b>🛑 Free users:</b> {limit} audio / 24 hours  •  <b>💎 Premium:</b> Unlimited"""


def _random_image_url():
    if START_IMAGE:
        return START_IMAGE
    apis = ["https://api.waifu.pics/sfw/waifu", "https://nekos.life/api/v2/img/waifu"]
    try:
        r = requests.get(random.choice(apis), timeout=5)
        r.raise_for_status()
        return r.json()["url"]
    except Exception as e:
        logger.warning(f"start image api failed: {e}")
        return START_IMAGE_FALLBACK


async def build_status(uid, first_name=""):
    doc = await db.get_user(uid, first_name)

    cover = {"original": "Keeping Original", "custom": "Custom Set ✅", "none": "Removed (No Cover)"}.get(
        doc.get("cover_mode", "original"), "Keeping Original")
    artist = esc(doc.get("artist")) if doc.get("artist") else "Keeping Original"

    if doc.get("voice_file_id"):
        voice = "Set ✅" + (f" ({esc(doc.get('voice_name'))})" if doc.get("voice_name") else "")
    else:
        voice = "Not set"

    if doc.get("target_chat"):
        target = f"{esc(doc.get('target_title') or '')} (<code>{doc['target_chat']}</code>)".strip()
    else:
        target = "Not set"

    from helpers.state import INPUT as _INPUT
    if uid in BATCH:
        b = BATCH[uid]
        batch = f"Active ({b['done']}/{b['total']})"
    elif _INPUT.get(uid, {}).get("kind") in ("batch_first", "batch_last"):
        batch = "Waiting for link"
    else:
        batch = "Inactive"

    title = current_title(doc)
    next_title = esc(title) if title else "Keeping Original"

    st = doc.get("start_trim")
    start_trim = f"From {fmt_trim(st)}" if st else "Off"
    et = doc.get("end_trim")
    if et:
        end_trim = (f"Remove last {fmt_trim(et['sec'])}" if et.get("mode") == "last"
                    else f"Ends at {fmt_trim(et['sec'])}")
    else:
        end_trim = "Off"

    ad = doc.get("autodel")
    autodel = f"{fmt_delay(ad)} ✅" if ad else "Disabled ❌"
    di = doc.get("delinput")
    delinput = "Disabled ❌" if di is None else ("Enabled ✅" if di == 0 else f"{fmt_delay(di)} ✅")

    rem = await db.remaining_allowance(uid)
    plan = "💎 Plan: Premium (Unlimited)" if rem is None else f"👤 Plan: Free ({rem}/{FREE_DAILY_LIMIT} left today)"

    return (
        "📊 <b>Your Configuration:</b>\n\n"
        f"🎤 Artist: {artist}\n"
        f"🖼️ Cover: {cover}\n"
        f"🔊 Voice Tag: {voice}\n"
        f"📢 Target Channel: {target}\n"
        f"📦 Batch Mode: {batch}\n"
        f"🔢 Next Title: {next_title}\n"
        f"📝 Caption: {'Enabled ✅' if doc.get('caption_on', True) else 'Disabled ❌'}\n"
        f"🗑️ Start Trim: {start_trim}\n"
        f"✂️ End Trim: {end_trim}\n"
        f"⏱️ Auto Delete Uploaded: {autodel}\n"
        f"🗑️ Auto Delete Input File: {delinput}\n\n"
        f"{plan}"
    )


@Client.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    user = message.from_user
    uid, chat_id = user.id, message.chat.id
    doc = await db.get_user(uid, user.first_name)
    INPUT.pop(uid, None)

    try:
        await message.react(emoji=random.choice(REACTIONS), big=True)
    except Exception:
        pass

    me = client.me or await client.get_me()
    caption = START_TXT.format(
        mention=f'<a href="tg://user?id={uid}">{esc(user.first_name)}</a>',
        username=me.username, name=esc(me.first_name),
    )
    rows = ui.panel_rows(doc)

    photo = await asyncio.to_thread(_random_image_url)
    try:
        msg_id = await ui.send(client, chat_id, caption, rows=rows, photo=photo, reply_to=message.id)
        PANEL[uid] = (chat_id, msg_id, True)
    except Exception as e:
        logger.warning(f"start photo failed ({e}); sending text panel")
        msg_id = await ui.send(client, chat_id, caption, rows=rows, reply_to=message.id)
        PANEL[uid] = (chat_id, msg_id, False)


@Client.on_message(filters.command("help") & filters.private)
async def help_cmd(client: Client, message: Message):
    await ui.send(client, message.chat.id, HELP_TXT.format(limit=FREE_DAILY_LIMIT),
                  rows=[[("❌ Close", "close_btn", "danger")]])


@Client.on_message(filters.command("status") & filters.private)
async def status_cmd(client: Client, message: Message):
    text = await build_status(message.from_user.id, message.from_user.first_name)
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML, quote=True)


@Client.on_message(filters.command("stop") & filters.private)
async def stop_cmd(client: Client, message: Message):
    uid = message.from_user.id
    cleared, was_running = await stop_all(uid)
    had_state = uid in INPUT or uid in MERGE
    clear_user_state(uid)
    BATCH.pop(uid, None)

    if cleared or was_running or had_state:
        parts = []
        if was_running:
            parts.append("stopped the running process")
        if cleared:
            parts.append(f"cleared {cleared} queued file(s)")
        if had_state:
            parts.append("cleared pending input/merge list")
        await message.reply_text("🛑 <b>Stopped!</b>\n\n" + "\n".join(f"• {p}" for p in parts),
                                 parse_mode=enums.ParseMode.HTML, quote=True)
    else:
        await message.reply_text("ℹ️ No process or queue is running right now.", quote=True)


@Client.on_callback_query(filters.regex(r"^close_btn$"))
async def close_cb(client: Client, cq: CallbackQuery):
    try:
        await cq.message.delete()
    except Exception:
        pass
    await cq.answer()
