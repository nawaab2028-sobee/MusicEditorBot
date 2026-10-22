"""
Editor panel: all buttons (Artist, Cover, Title, Voice Tag, Target Channel, Batch, Merge,
Caption, Start/End Trim, Auto Delete, Delete Input) + their input prompts + Yes/No confirmations.
"""

import os
import uuid

from pyrogram import Client, enums, filters
from pyrogram.types import CallbackQuery, Message

from config import COVER_DIR, FREE_DAILY_LIMIT, MERGE_MAX_FILES
from database.db import db
from helpers import actions, media, ui
from helpers.links import parse_telegram_link
from helpers.pipeline import UserError, drop_cached, drop_tag_file, validate_first_link
from helpers.planinfo import build_plan_text, plan_rows
from helpers.state import INPUT, MERGE, PANEL, get_input, has_input, pop_input, set_input
from helpers.utils import (esc, fmt_delay, fmt_duration, fmt_trim, make_counter_title, parse_chat_ref,
                           parse_time, parse_title_counter)
from logger import LOGGER
from plugins import voicetags

logger = LOGGER(__name__)

HTML = enums.ParseMode.HTML
MAX_DELAY = 172800  # 48 hours (within Telegram's delete limit)


# ======================================================
# helpers
# ======================================================

async def _prompt(client, uid, chat_id, kind, text, rows=None, **data):
    """Ask for input: remove the old prompt, send a new one, and set the state."""
    old = get_input(uid)
    if old and old.get("prompt_id"):
        await ui.delete(client, old.get("chat_id", chat_id), [old["prompt_id"]])
    await voicetags.on_replaced(client, old)
    pid = await ui.send(client, chat_id, text, rows=rows if rows is not None else ui.cancel_rows())
    set_input(uid, kind, prompt_id=pid, chat_id=chat_id, **data)


async def _finish_input(client, uid):
    """Input received: clear the state + delete the prompt (the one with the Cancel button)."""
    st = pop_input(uid)
    if st and st.get("prompt_id"):
        await ui.delete(client, st.get("chat_id"), [st["prompt_id"]])


async def vtags_count(uid):
    from helpers import vtags
    return len(await vtags.get_tags(uid))


async def _refresh(client, uid):
    await ui.refresh_panel(client, uid, await db.get_user(uid))


CONFIRM_TEXT = {
    "rm_artist": "🗑 <b>Remove Artist</b>\n\nAre you sure you want to <b>remove the Artist name</b>?\n<i>Every audio will then keep its original artist.</i>",
    "rm_cover": "🗑 <b>Remove Cover</b>\n\nAre you sure you want to <b>remove the Cover</b>?\n<i>The audio will come without a cover. If you want the original cover, press \"Keep Original Cover\".</i>",
    "keep_title": "🔄 <b>Keep Original Title</b>\n\nAre you sure you want to <b>reset the Title</b>?\n<i>Every audio will then keep its original title.</i>",
    "rm_chan": "❌ <b>Remove Target Channel</b>\n\nAre you sure you want to <b>remove the Target Channel</b>?\n<i>The audio will be sent only in this chat.</i>",
    "caption_on": "📝 <b>Caption ON</b>\n\nAre you sure you want to turn <b>Caption ON</b>?\n<i>The audio will come with a caption.</i>",
    "caption_off": "📝 <b>Caption OFF</b>\n\nAre you sure you want to turn <b>Caption OFF</b>?\n<i>The audio will come without a caption.</i>",
}


# ======================================================
# main panel buttons
# ======================================================

@Client.on_callback_query(filters.regex(r"^ed:"))
async def panel_callback(client: Client, cq: CallbackQuery):
    uid = cq.from_user.id
    action = cq.data[3:]
    msg = cq.message
    if msg is None:
        return await cq.answer()
    chat_id = msg.chat.id
    doc = await db.get_user(uid, cq.from_user.first_name)
    PANEL[uid] = (chat_id, msg.id, bool(msg.photo))

    # ---------- Artist ----------
    if action == "artist":
        await _prompt(client, uid, chat_id, "artist", "Send me the Artist Name:")
    elif action == "rm_artist":
        if not doc.get("artist"):
            return await cq.answer("ℹ️ Artist is already Original.", show_alert=True)
        await ui.send(client, chat_id, CONFIRM_TEXT["rm_artist"], rows=ui.confirm_rows("rm_artist"))

    # ---------- Cover ----------
    elif action == "cover":
        await _prompt(client, uid, chat_id, "cover",
                      "🖼 <b>Send me the Cover image</b>\n\nSend a photo or an image file (jpg/png/webp - any size). Not a URL, send the image directly.")
    elif action == "rm_cover":
        if doc.get("cover_mode", "original") == "none":
            return await cq.answer("ℹ️ Cover is already removed.", show_alert=True)
        await ui.send(client, chat_id, CONFIRM_TEXT["rm_cover"], rows=ui.confirm_rows("rm_cover"))
    elif action == "keep_cover":
        if doc.get("cover_mode", "original") == "original":
            return await cq.answer("ℹ️ The Original cover is already being used.", show_alert=True)
        await db.update_user(uid, cover_mode="original")
        await db.unset_user(uid, "cover_file_id", "cover_unique")
        drop_cached(uid, "cover")
        await _refresh(client, uid)
        await cq.answer("✅ Every audio will now keep its Original cover.", show_alert=True)
        return

    # ---------- Title ----------
    elif action == "title":
        await _prompt(
            client, uid, chat_id, "title",
            "📊 <b>Send me the Title:</b>\n\n<i>Tip: send it with a number at the end (like</i> <code>Song 1</code><i>) and "
            "the number can increase on every audio - I will ask you.</i>",
        )
    elif action == "keep_title":
        if not doc.get("title"):
            return await cq.answer("ℹ️ Title is already Original.", show_alert=True)
        await ui.send(client, chat_id, CONFIRM_TEXT["keep_title"], rows=ui.confirm_rows("keep_title"))

    # ---------- Voice tag ----------
    elif action == "voice":
        await voicetags.start_add(client, uid, chat_id)
    elif action in ("rm_voice", "vlist"):
        if action == "rm_voice" and not await vtags_count(uid):
            return await cq.answer("ℹ️ No Voice Tag is saved.", show_alert=True)
        await voicetags.show_manager(client, uid, chat_id)

    # ---------- Target channel ----------
    elif action == "chan":
        await _prompt(
            client, uid, chat_id, "channel",
            "📢 <b>Set Target Channel</b>\n\n"
            "• <b>Public channel:</b> send the username (<code>@channel</code>) or the channel ID\n"
            "• <b>Private channel:</b> send the Chat ID (<code>-100xxxxxxxxxx</code>)\n\n"
            "⚠️ <b>The bot must be made an Admin in that channel</b> (with the Post Messages permission).",
        )
    elif action == "rm_chan":
        if not doc.get("target_chat"):
            return await cq.answer("ℹ️ Target Channel is not set.", show_alert=True)
        await ui.send(client, chat_id, CONFIRM_TEXT["rm_chan"], rows=ui.confirm_rows("rm_chan"))

    # ---------- Batch / Merge ----------
    elif action == "batch":
        left = await actions.allowance_left(uid)
        if left is not None and left <= 0:
            await actions.send_limit(client, uid, chat_id)
        else:
            await _prompt(
                client, uid, chat_id, "batch_first",
                "📁 <b>Batch Mode</b>\n\n"
                "Send me the <b>first</b> audio post link (e.g., <code>https://t.me/channel/123</code>).\n\n"
                "<i>Or directly send the whole range:</i> <code>https://t.me/channel/100-110</code>\n"
                "<i>For a private channel, use /login first.</i>",
                rows=[[("❌ Cancel Batch", "in:cancel", "danger")]],
            )
    elif action == "merge":
        left = await actions.allowance_left(uid)
        if left is not None and left <= 0:
            await actions.send_limit(client, uid, chat_id)
        else:
            old = MERGE.get(uid)
            if old and old.get("panel_id"):
                await ui.delete(client, chat_id, [old["panel_id"]])
            MERGE[uid] = {"items": [], "chat_id": chat_id, "panel_id": None}
            MERGE[uid]["panel_id"] = await ui.send(client, chat_id, actions.merge_text(uid), rows=actions.merge_rows())

    # ---------- Caption ----------
    elif action == "caption":
        act = "caption_off" if doc.get("caption_on", True) else "caption_on"
        await ui.send(client, chat_id, CONFIRM_TEXT[act], rows=ui.confirm_rows(act))

    # ---------- Trim ----------
    elif action == "st":
        await _prompt(
            client, uid, chat_id, "start_trim",
            "✂️ <b>Start Trim</b>\n\nWhere should the audio start from? Send the time:\n"
            "<code>SS</code> | <code>MM:SS</code> | <code>HH:MM:SS</code>\n\n"
            "<i>Example: <code>15</code> or <code>00:15</code> = the first 15 sec will be removed.</i>",
        )
    elif action == "st_reset":
        if not doc.get("start_trim"):
            return await cq.answer("ℹ️ Start Trim is already Normal (off).", show_alert=True)
        await db.unset_user(uid, "start_trim")
        await cq.answer("✅ Normal Start - the audio will now play from the beginning.", show_alert=True)
        return await _refresh(client, uid)
    elif action == "et":
        old = get_input(uid)
        if old and old.get("prompt_id"):
            await ui.delete(client, old.get("chat_id", chat_id), [old["prompt_id"]])
            pop_input(uid)
        await ui.send(
            client, chat_id,
            "✂️ <b>End Trim</b>\n\nWhich method do you want?\n\n"
            "• <b>Remove from End</b> - cut the last N sec of every audio\n"
            "• <b>End at This Time</b> - the audio ends at this time (xVett style)",
            rows=ui.end_mode_rows(),
        )
    elif action == "et_reset":
        if not doc.get("end_trim"):
            return await cq.answer("ℹ️ End Trim is already Normal (off).", show_alert=True)
        await db.unset_user(uid, "end_trim")
        await cq.answer("✅ Normal End - the audio will now play fully.", show_alert=True)
        return await _refresh(client, uid)

    # ---------- Auto delete / delete input ----------
    elif action == "ad":
        await ui.send(client, chat_id, ui.autodel_text(doc), rows=ui.autodel_rows(doc))
    elif action == "di":
        await ui.send(client, chat_id, ui.delinput_text(doc), rows=ui.delinput_rows())

    # ---------- Extra ----------
    elif action == "status":
        from plugins.start import build_status
        await client.send_message(chat_id, await build_status(uid, cq.from_user.first_name), parse_mode=HTML)
    elif action == "plan":
        await ui.send(client, chat_id, await build_plan_text(uid, cq.from_user.first_name), rows=plan_rows())

    await cq.answer()


# ======================================================
# confirmations (Yes / No)
# ======================================================

CONFIRM_DONE = {
    "rm_artist": "✅ <b>Artist removed.</b>\nEvery audio will now keep its original artist.",
    "rm_cover": "✅ <b>Cover removed.</b>\nThe audio will now come without a cover.",
    "keep_title": "✅ <b>Title reset.</b>\nEvery audio will now keep its original title.",
    "rm_chan": "✅ <b>Target Channel removed.</b>",
    "caption_on": "✅ <b>Caption ON.</b>\nThe audio will now come with a caption.",
    "caption_off": "✅ <b>Caption OFF.</b>\nThe audio will now come without a caption.",
}


@Client.on_callback_query(filters.regex(r"^cf:"))
async def confirm_callback(client: Client, cq: CallbackQuery):
    uid = cq.from_user.id
    try:
        _, action, ans = cq.data.split(":")
    except ValueError:
        return await cq.answer()
    msg = cq.message
    if msg is None or action not in CONFIRM_DONE:
        return await cq.answer()

    if ans != "y":
        await ui.edit(client, msg.chat.id, msg.id, text="❌ <b>Cancelled.</b> No changes were made.", rows=None)
        return await cq.answer("Cancelled")

    if action == "rm_artist":
        await db.unset_user(uid, "artist")
    elif action == "rm_cover":
        await db.update_user(uid, cover_mode="none")
        await db.unset_user(uid, "cover_file_id", "cover_unique")
        drop_cached(uid, "cover")
    elif action == "keep_title":
        await db.unset_user(uid, "title", "title_inc", "title_n", "title_prefix", "title_w")
    elif action == "rm_chan":
        await db.unset_user(uid, "target_chat", "target_title")
    elif action == "caption_on":
        await db.update_user(uid, caption_on=True)
    elif action == "caption_off":
        await db.update_user(uid, caption_on=False)

    await ui.edit(client, msg.chat.id, msg.id, text=CONFIRM_DONE[action], rows=None)
    await _refresh(client, uid)
    await cq.answer("Done ✅")


# ======================================================
# cancel / small callbacks
# ======================================================

@Client.on_callback_query(filters.regex(r"^in:cancel$"))
async def cancel_input_cb(client: Client, cq: CallbackQuery):
    uid = cq.from_user.id
    st = pop_input(uid)
    if st and st.get("pending"):
        drop_tag_file(uid, st["pending"])  # half-added voice tag
    await voicetags.on_replaced(client, st)
    try:
        await cq.message.delete()
    except Exception:
        pass
    await cq.answer("❌ Cancelled")


@Client.on_callback_query(filters.regex(r"^ti:"))
async def title_choice_cb(client: Client, cq: CallbackQuery):
    uid = cq.from_user.id
    st = get_input(uid)
    if not st or st.get("kind") != "title_choice":
        return await cq.answer("This option has expired. Press Set Title again.", show_alert=True)
    inc = cq.data == "ti:y"
    if inc:
        await db.update_user(uid, title=st["text"], title_inc=True, title_prefix=st["prefix"],
                             title_n=st["n"], title_w=st["w"])
    else:
        await db.update_user(uid, title=st["text"], title_inc=False)
        await db.unset_user(uid, "title_n", "title_prefix", "title_w")
    pop_input(uid)
    if inc:
        nxt = make_counter_title(st["prefix"], st["n"] + 1, st["w"])
        res = (f"✅ <b>Title set:</b> {esc(st['text'])}\n"
               f"🔢 Auto-number ON: first audio = <b>{esc(st['text'])}</b>, next = <b>{esc(nxt)}</b> ...")
    else:
        res = f"✅ <b>Title set:</b> {esc(st['text'])}\n<i>This same title will be applied to every audio.</i>"
    await ui.edit(client, cq.message.chat.id, cq.message.id, text=res, rows=None)
    await _refresh(client, uid)
    await cq.answer("Saved ✅")


@Client.on_callback_query(filters.regex(r"^et:"))
async def end_mode_cb(client: Client, cq: CallbackQuery):
    uid = cq.from_user.id
    mode = cq.data[3:]
    chat_id = cq.message.chat.id
    try:
        await cq.message.delete()
    except Exception:
        pass
    if mode == "last":
        text = ("✂️ <b>End Trim - Remove from End</b>\n\nHow many seconds at the end of the audio should be removed?\n"
                "<code>SS</code> | <code>MM:SS</code> | <code>HH:MM:SS</code>\n<i>Example: <code>10</code> = the last 10 sec will be cut.</i>")
    else:
        text = ("✂️ <b>End Trim - End at This Time</b>\n\nAt what time should the audio end?\n"
                "<code>SS</code> | <code>MM:SS</code> | <code>HH:MM:SS</code>\n<i>Example: <code>02:00</code> = the audio ends at 2:00.</i>")
    await _prompt(client, uid, chat_id, "end_trim", text, mode=mode)
    await cq.answer()


# ======================================================
# Auto delete / Delete input menus
# ======================================================

@Client.on_callback_query(filters.regex(r"^(ad|di):"))
async def timer_callback(client: Client, cq: CallbackQuery):
    uid = cq.from_user.id
    which, val = cq.data.split(":", 1)
    field = "autodel" if which == "ad" else "delinput"
    msg = cq.message
    chat_id = msg.chat.id
    doc = await db.get_user(uid)

    if val == "back":
        PANEL[uid] = (chat_id, msg.id, False)
        await ui.edit(client, chat_id, msg.id, text="🎛 <b>Main Menu</b>", rows=ui.panel_rows(doc))
        return await cq.answer()

    toast = None
    if val == "custom":
        await _prompt(
            client, uid, chat_id, f"{field}_custom",
            "✍️ <b>Custom time</b>\n\nSend a number in seconds (number only).\n"
            f"<i>Example: <code>120</code> = 2 min. Max {MAX_DELAY} (48 hours).</i>",
            menu_id=msg.id, menu_kind=which,
        )
        return await cq.answer()
    if val == "off":
        await db.unset_user(uid, field)
        toast = "✅ Turned OFF"
    elif val == "tog":  # only in the Auto Delete menu: quick toggle for Delete Input
        if doc.get("delinput") is None:
            await db.update_user(uid, delinput=0)
            toast = "✅ Delete Input ENABLED"
        else:
            await db.unset_user(uid, "delinput")
            toast = "✅ Delete Input DISABLED"
    else:
        try:
            secs = int(val)
        except ValueError:
            return await cq.answer()
        await db.update_user(uid, **{field: secs})
        toast = f"✅ Set: {fmt_delay(secs)}"

    doc = await db.get_user(uid)
    if which == "ad":
        await ui.edit(client, chat_id, msg.id, text=ui.autodel_text(doc), rows=ui.autodel_rows(doc))
    else:
        await ui.edit(client, chat_id, msg.id, text=ui.delinput_text(doc), rows=ui.delinput_rows())
    await _refresh(client, uid)
    await cq.answer(toast)


# ======================================================
# Merge buttons
# ======================================================

@Client.on_callback_query(filters.regex(r"^mg:"))
async def merge_callback(client: Client, cq: CallbackQuery):
    uid = cq.from_user.id
    action = cq.data[3:]
    msg = cq.message
    st = MERGE.get(uid)
    if not st:
        try:
            await msg.delete()
        except Exception:
            pass
        return await cq.answer("Merge session has expired. Press Merge Audio again.", show_alert=True)

    if action == "cancel":
        MERGE.pop(uid, None)
        try:
            await msg.delete()
        except Exception:
            pass
        return await cq.answer("❌ Merge cancelled")
    if action == "clear":
        st["items"].clear()
        await ui.edit(client, msg.chat.id, msg.id, text=actions.merge_text(uid), rows=actions.merge_rows())
        return await cq.answer("💥 Cleared")
    if action == "go":
        n = len(st["items"])
        if n < 2:
            return await cq.answer("Send at least 2 audios.", show_alert=True)
        left = await actions.allowance_left(uid)
        if left is not None and n > left:
            return await cq.answer(
                f"Free limit: only {left} more audio(s) can be edited today, but you sent {n}. "
                "Remove some (Clear Files) or get Premium.", show_alert=True)
        await actions.start_merge(client, uid, msg.chat.id, msg.id)
        return await cq.answer("🔗 Merge started!")
    await cq.answer()


# ======================================================
# typed inputs (artist, title, cover, voice, channel, trim, custom time, batch links)
# ======================================================

@Client.on_message(filters.private & filters.incoming & has_input & ~filters.regex(r"^/"), group=0)
async def input_router(client: Client, message: Message):
    uid = message.from_user.id
    st = get_input(uid)
    if not st:
        return
    kind = st["kind"]
    consumed = await _dispatch_input(client, message, uid, st, kind)
    if consumed:
        message.stop_propagation()


async def _dispatch_input(client, message, uid, st, kind):
    text = (message.text or "").strip() if message.text else None
    chat_id = message.chat.id

    # inline-button states (ignore text)
    if kind in ("title_choice",):
        if text is not None:
            await message.reply_text("⬆️ Choose using the buttons above, or press Cancel.", quote=True)
            return True
        pop_input(uid)
        return False

    # media-expecting states
    if kind == "cover":
        return await _in_cover(client, message, uid, st)
    if kind in ("voice", "voice_title", "voice_rename", "vplan_tags", "vplan_pos"):
        return await voicetags.handle_input(client, message, uid, st, kind)

    # text-expecting states: if audio/media arrives, clear the state and let the audio be processed
    if text is None:
        await _finish_input(client, uid)
        return False

    handler = {
        "artist": _in_artist,
        "title": _in_title,
        "channel": _in_channel,
        "start_trim": _in_start_trim,
        "end_trim": _in_end_trim,
        "autodel_custom": _in_custom_delay,
        "delinput_custom": _in_custom_delay,
        "batch_first": _in_batch_first,
        "batch_last": _in_batch_last,
    }.get(kind)
    if not handler:
        pop_input(uid)
        return False
    return await handler(client, message, uid, st, text)


async def _in_artist(client, message, uid, st, text):
    if len(text) > 128:
        await message.reply_text("❌ Keep the Artist name under 128 characters.", quote=True)
        return True
    await db.update_user(uid, artist=text)
    await _finish_input(client, uid)
    await message.reply_text(f"✅ Artist set to: <b>{esc(text)}</b>", parse_mode=HTML, quote=True)
    await _refresh(client, uid)
    return True


async def _in_title(client, message, uid, st, text):
    if len(text) > 150:
        await message.reply_text("❌ Keep the Title under 150 characters.", quote=True)
        return True
    parsed = parse_title_counter(text)
    if parsed:
        prefix, n, w = parsed
        await _finish_input(client, uid)
        nxt = make_counter_title(prefix, n + 1, w)
        pid = await ui.send(
            client, message.chat.id,
            f"🔢 A number was found at the end of the Title: <b>{esc(text)}</b>\n\n"
            f"Should I auto-number? First audio = <b>{esc(text)}</b>, next = <b>{esc(nxt)}</b>, and so on...",
            rows=ui.title_inc_rows(),
        )
        set_input(uid, "title_choice", prompt_id=None, chat_id=message.chat.id, text=text, prefix=prefix, n=n, w=w)
        return True
    await db.update_user(uid, title=text, title_inc=False)
    await db.unset_user(uid, "title_n", "title_prefix", "title_w")
    await _finish_input(client, uid)
    await message.reply_text(f"✅ Title set to: <b>{esc(text)}</b>\n<i>This same title will be applied to every audio.</i>",
                             parse_mode=HTML, quote=True)
    await _refresh(client, uid)
    return True


async def _in_cover(client, message, uid, st):
    f = message.photo or message.document or message.sticker
    if not f:
        if message.text:
            await message.reply_text("❌ Please send an <b>image</b> for the Cover (photo or image file).", parse_mode=HTML, quote=True)
            return True
        await _finish_input(client, uid)  # audio arrived - clear the state and let the audio be processed
        return False
    note = await message.reply_text("⏳ Processing the cover...", quote=True)
    os.makedirs(COVER_DIR, exist_ok=True)
    src = os.path.join(COVER_DIR, f"{uid}_{uuid.uuid4().hex[:6]}.src")
    uniq = f.file_unique_id
    cover = os.path.join(COVER_DIR, f"{uid}_{uniq}.jpg")
    thumb = os.path.join(COVER_DIR, f"{uid}_{uniq}_t.jpg")
    try:
        got = await client.download_media(message, file_name=src)
        await media.make_custom_cover(got, cover, thumb)
    except media.MediaError as e:
        await note.edit_text(f"❌ {esc(e)}", parse_mode=HTML)
        return True
    except Exception as e:
        logger.warning(f"cover download failed: {e}")
        await note.edit_text("❌ Image could not be downloaded. Please send it again.")
        return True
    finally:
        try:
            os.remove(src)
        except Exception:
            pass
    drop_cached(uid, "cover", keep=(os.path.basename(cover), os.path.basename(thumb)))
    await db.update_user(uid, cover_mode="custom", cover_file_id=f.file_id, cover_unique=uniq)
    await _finish_input(client, uid)
    try:
        await note.delete()
    except Exception:
        pass
    await message.reply_photo(cover, caption="✅ <b>Cover set!</b>\nThis same cover will now be applied to every audio.",
                              parse_mode=HTML, quote=True)
    await _refresh(client, uid)
    return True


async def _validate_channel(client, ref):
    """Returns (chat, None) or (None, error_text)."""
    from pyrogram.enums import ChatMemberStatus, ChatType
    try:
        chat = await client.get_chat(ref)
    except Exception as e:
        return None, f"Channel not found: <code>{esc(e)}</code>"
    try:
        me = await client.get_chat_member(chat.id, "me")
    except Exception:
        return None, "The bot is not a member/admin of this channel. First make the bot an <b>Admin</b> in the channel."
    status = me.status
    if chat.type == ChatType.CHANNEL:
        if status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return None, "The bot is not an <b>Admin</b> in the channel. Make it an Admin (with the Post Messages permission)."
        priv = getattr(me, "privileges", None)
        if priv is not None and getattr(priv, "can_post_messages", True) is False:
            return None, "The bot does not have the <b>Post Messages</b> permission. Please grant the permission."
    elif status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        return None, "The bot is not a member of this chat."
    return chat, None


async def _in_channel(client, message, uid, st, text):
    ref = parse_chat_ref(text)
    if ref is None:
        await message.reply_text(
            "❌ Invalid format. Send: <code>@channelusername</code> or <code>-100xxxxxxxxxx</code>",
            parse_mode=HTML, quote=True)
        return True
    chat, err = await _validate_channel(client, ref)
    if err:
        await message.reply_text(f"❌ {err}\n\n<i>Correct it and send again, or press Cancel.</i>", parse_mode=HTML, quote=True)
        return True
    await db.update_user(uid, target_chat=chat.id, target_title=chat.title or str(chat.id))
    await _finish_input(client, uid)
    await message.reply_text(
        f"✅ <b>Target Channel set!</b>\n📢 {esc(chat.title or '')} (<code>{chat.id}</code>)\n"
        "<i>Every edited audio will now be auto-forwarded here as well.</i>", parse_mode=HTML, quote=True)
    await _refresh(client, uid)
    return True


async def _in_start_trim(client, message, uid, st, text):
    sec = parse_time(text)
    if sec is None or sec <= 0:
        await message.reply_text("❌ Send a valid time (greater than 0): <code>SS</code>, <code>MM:SS</code> or <code>HH:MM:SS</code>",
                                 parse_mode=HTML, quote=True)
        return True
    doc = await db.get_user(uid)
    et = doc.get("end_trim")
    if et and et.get("mode") == "at" and sec >= et["sec"]:
        await message.reply_text(
            f"❌ Start time must be smaller than the end time (<b>{fmt_trim(et['sec'])}</b>).", parse_mode=HTML, quote=True)
        return True
    await db.update_user(uid, start_trim=sec)
    await _finish_input(client, uid)
    await message.reply_text(f"✅ <b>Start Trim set:</b> the audio will now start from <b>{fmt_trim(sec)}</b>.",
                             parse_mode=HTML, quote=True)
    await _refresh(client, uid)
    return True


async def _in_end_trim(client, message, uid, st, text):
    sec = parse_time(text)
    if sec is None or sec <= 0:
        await message.reply_text("❌ Send a valid time (greater than 0): <code>SS</code>, <code>MM:SS</code> or <code>HH:MM:SS</code>",
                                 parse_mode=HTML, quote=True)
        return True
    mode = st.get("mode", "last")
    doc = await db.get_user(uid)
    stt = doc.get("start_trim") or 0
    if mode == "at" and sec <= stt:
        await message.reply_text(
            f"❌ End time must be greater than the start time (<b>{fmt_trim(stt)}</b>).", parse_mode=HTML, quote=True)
        return True
    await db.update_user(uid, end_trim={"mode": mode, "sec": sec})
    await _finish_input(client, uid)
    if mode == "last":
        res = f"✅ <b>End Trim set:</b> the last <b>{fmt_trim(sec)}</b> of every audio will be removed."
    else:
        res = f"✅ <b>End Trim set:</b> the audio will end at <b>{fmt_trim(sec)}</b>."
    await message.reply_text(res, parse_mode=HTML, quote=True)
    await _refresh(client, uid)
    return True


async def _in_custom_delay(client, message, uid, st, text):
    if not text.isdigit() or not (1 <= int(text) <= MAX_DELAY):
        await message.reply_text(f"❌ Send only a number (in seconds), between 1 and {MAX_DELAY}.", quote=True)
        return True
    secs = int(text)
    field = "autodel" if st["kind"] == "autodel_custom" else "delinput"
    await db.update_user(uid, **{field: secs})
    menu_id, menu_kind = st.get("menu_id"), st.get("menu_kind")
    await _finish_input(client, uid)
    doc = await db.get_user(uid)
    label = "Auto Delete" if field == "autodel" else "Delete Input"
    await message.reply_text(f"✅ <b>{label}</b> set: <b>{secs}</b> sec ({fmt_delay(secs)})", parse_mode=HTML, quote=True)
    if menu_id:
        if menu_kind == "ad":
            await ui.edit(client, message.chat.id, menu_id, text=ui.autodel_text(doc), rows=ui.autodel_rows(doc))
        else:
            await ui.edit(client, message.chat.id, menu_id, text=ui.delinput_text(doc), rows=ui.delinput_rows())
    await _refresh(client, uid)
    return True


async def _in_batch_first(client, message, uid, st, text):
    kind, target, a, b = parse_telegram_link(text)
    if kind is None:
        await message.reply_text(
            "❌ Send a valid Telegram post link, e.g. <code>https://t.me/channel/123</code>", parse_mode=HTML, quote=True)
        return True
    if b != a:  # range link: https://t.me/channel/100-110
        if b < a:
            await message.reply_text("❌ Invalid range: the end number must be greater than the start.", quote=True)
            return True
        await _finish_input(client, uid)
        await actions.start_batch(client, message, uid, kind, target, a, b)
        return True
    wait = await message.reply_text("🔎 Checking the link...", quote=True)
    try:
        ok = await validate_first_link(client, uid, kind, target, a)
    except UserError as e:
        await wait.edit_text(str(e), parse_mode=HTML)
        await _finish_input(client, uid)
        return True
    except Exception as e:
        logger.warning(f"batch first link check failed: {e}")
        await wait.edit_text("❌ Could not check the link. Is the link correct? Please try again.")
        return True
    if not ok:
        await wait.edit_text("❌ The linked message does not contain an audio file.")
        return True
    try:
        await wait.delete()
    except Exception:
        pass
    await _finish_input(client, uid)
    await _prompt(
        client, uid, message.chat.id, "batch_last",
        "✅ First link accepted. Now send me the <b>last</b> audio post link from the same channel.",
        rows=[[("❌ Cancel Batch", "in:cancel", "danger")]],
        bkind=kind, btarget=target, first=a,
    )
    return True


async def _in_batch_last(client, message, uid, st, text):
    kind, target, a, b = parse_telegram_link(text)
    if kind is None:
        await message.reply_text("❌ Send a valid Telegram post link.", quote=True)
        return True
    if kind != st["bkind"] or str(target).lower() != str(st["btarget"]).lower():
        await message.reply_text("❌ The last link must be from the <b>same channel</b>.", parse_mode=HTML, quote=True)
        return True
    first = st["first"]
    last = b
    if last < first:
        await message.reply_text("❌ The post number of the last link must be greater than that of the first link.", quote=True)
        return True
    await _finish_input(client, uid)
    await actions.start_batch(client, message, uid, kind, target, first, last)
    return True
