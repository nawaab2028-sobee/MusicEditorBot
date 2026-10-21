"""
Incoming audio (direct), Merge collection, aur plain t.me link (single/range) handling.
group=1: typed-input router (editor.py, group 0) pehle chalta hai aur consume kare to yahan nahi aata.
"""

from pyrogram import Client, filters
from pyrogram.types import Message

from database.db import db
from helpers import actions
from helpers.links import parse_telegram_link
from helpers.pipeline import get_audio_media
from helpers.state import MERGE


@Client.on_message(filters.private & filters.incoming & (filters.audio | filters.document), group=1)
async def audio_handler(client: Client, message: Message):
    media = get_audio_media(message)
    if media is None:
        return  # audio nahi hai (pdf, zip...) - ignore
    uid = message.from_user.id
    await db.get_user(uid, message.from_user.first_name)
    if uid in MERGE:
        return await actions.add_merge_item(client, message, media)
    await actions.queue_direct_audio(client, message)


@Client.on_message(filters.private & filters.incoming & filters.text & ~filters.regex(r"^/"), group=1)
async def link_handler(client: Client, message: Message):
    """Seedha t.me link bhejo (single ya range 100-110) to batch ki tarah process hoga."""
    text = message.text or ""
    if "t.me/" not in text:
        return
    try:
        from plugins.session import LOGIN_STATE
        if message.from_user.id in LOGIN_STATE:
            return
    except Exception:
        pass
    kind, target, a, b = parse_telegram_link(text)
    if kind is None or b < a:
        return
    uid = message.from_user.id
    await db.get_user(uid, message.from_user.first_name)
    await actions.start_batch(client, message, uid, kind, target, a, b)
