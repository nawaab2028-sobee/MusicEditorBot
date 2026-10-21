"""
Incoming audio (direct), Merge collection, and plain t.me link (single/range) handling.
group=1: the typed-input router (editor.py, group 0) runs first, and if it consumes the message it does not reach here.
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
        return  # not audio (pdf, zip...) - ignore
    uid = message.from_user.id
    await db.get_user(uid, message.from_user.first_name)
    if uid in MERGE:
        return await actions.add_merge_item(client, message, media)
    await actions.queue_direct_audio(client, message)


@Client.on_message(filters.private & filters.incoming & filters.text & ~filters.regex(r"^/"), group=1)
async def link_handler(client: Client, message: Message):
    """If a t.me link (single or range 100-110) is sent directly, it will be processed like a batch."""
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
