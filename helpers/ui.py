"""
UI helpers: keyboards (Blue/Green/Red) + send/edit wrappers.

Button = (text, callback_data, style)   style: "primary"(blue) | "success"(green) | "danger"(red) | None
Messages are sent via Bot API HTTP first (for colors); if that fails, normal buttons are sent via Pyrogram.
"""

from pyrogram import enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from helpers import botapi
from helpers.utils import esc, fmt_delay, fmt_trim
from logger import LOGGER

logger = LOGGER(__name__)

BLUE, GREEN, RED = "primary", "success", "danger"
KEEP = object()  # edit(): keep the markup as it is


# ======================================================
# Keyboards
# ======================================================

def to_markup(rows):
    """Plain (uncolored) Pyrogram markup - for fallback."""
    def _btn(t, d):
        if str(d).startswith(("http://", "https://", "tg://")):
            return InlineKeyboardButton(t, url=d)
        return InlineKeyboardButton(t, callback_data=d)

    return InlineKeyboardMarkup([[_btn(t, d) for t, d, _ in row] for row in rows])


def panel_rows(doc):
    """Main 11-button editor panel (as in the screenshot)."""
    caption_on = doc.get("caption_on", True)
    autodel = doc.get("autodel")
    delinput = doc.get("delinput")

    if autodel:
        ad_btn = (f"⏳ Auto Delete: {fmt_delay(autodel)}", "ed:ad", GREEN)
    else:
        ad_btn = ("⏳ Auto Delete: OFF", "ed:ad", RED)
    if delinput is None:
        di_btn = ("🗑 Delete Input: OFF", "ed:di", RED)
    else:
        di_btn = (f"🗑 Delete Input: {'ON' if delinput == 0 else fmt_delay(delinput)}", "ed:di", GREEN)

    return [
        [("✏️ Edit Artist", "ed:artist", BLUE), ("🗑 Remove Artist", "ed:rm_artist", RED)],
        [("🖼 Set Cover", "ed:cover", BLUE), ("🗑 Remove Cover", "ed:rm_cover", RED)],
        [("🔄 Keep Original Cover", "ed:keep_cover", GREEN)],
        [("📊 Set Title", "ed:title", BLUE), ("🔄 Keep Original Title", "ed:keep_title", GREEN)],
        [("➕ Add Voice Tag", "ed:voice", BLUE), ("➖ Remove Voice Tag", "ed:rm_voice", RED)],
        [("📢 Set Target Channel", "ed:chan", BLUE), ("❌ Remove Target Channel", "ed:rm_chan", RED)],
        [("📁 Batch Process", "ed:batch", BLUE), ("🔗 Merge Audio", "ed:merge", BLUE)],
        [("📝 Caption: ON" if caption_on else "📝 Caption: OFF", "ed:caption", GREEN if caption_on else RED)],
        [("✂️ Start Trim", "ed:st", RED), ("🔄 Normal Start", "ed:st_reset", GREEN)],
        [("✂️ End Trim", "ed:et", RED), ("🔄 Normal End", "ed:et_reset", GREEN)],
        [ad_btn, di_btn],
        [("📊 Status", "ed:status", BLUE), ("💎 My Plan", "ed:plan", BLUE)],
    ]


def cancel_rows(label="❌ Cancel"):
    return [[(label, "in:cancel", RED)]]


def confirm_rows(action):
    return [[("Yes ✅", f"cf:{action}:y", GREEN), ("No ❌", f"cf:{action}:n", RED)]]


def end_mode_rows():
    return [
        [("✂️ Remove from End (remove last N sec)", "et:last", BLUE)],
        [("⏱ End at This Time (end at time)", "et:at", BLUE)],
        [("❌ Cancel", "in:cancel", RED)],
    ]


def title_inc_rows():
    return [
        [("🔢 Yes, increase the number", "ti:y", GREEN)],
        [("📌 No, same title on all", "ti:n", BLUE)],
        [("❌ Cancel", "in:cancel", RED)],
    ]


def _time_rows(prefix, instant=False):
    first = []
    if instant:
        first.append(("⚡ Instantly", f"{prefix}:0", GREEN))
    rows = []
    if first:
        rows.append(first)
    rows += [
        [("⏳ 1 Min", f"{prefix}:60", BLUE), ("⏳ 5 Min", f"{prefix}:300", BLUE), ("⏳ 10 Min", f"{prefix}:600", BLUE)],
        [("⏳ 30 Min", f"{prefix}:1800", BLUE), ("⏳ 1 Hour", f"{prefix}:3600", BLUE), ("⏳ 24 Hours", f"{prefix}:86400", BLUE)],
        [("✍️ Custom Seconds", f"{prefix}:custom", BLUE), ("❌ Turn OFF", f"{prefix}:off", RED)],
    ]
    return rows


def autodel_rows(doc):
    di_on = doc.get("delinput") is not None
    rows = _time_rows("ad")
    rows.append([("🗑 Delete Input File: ENABLED 🟢" if di_on else "🗑 Delete Input File: DISABLED 🔴",
                  "ad:tog", GREEN if di_on else RED)])
    rows.append([("⬅️ Back to Main Menu", "ad:back", BLUE)])
    return rows


def delinput_rows():
    rows = _time_rows("di", instant=True)
    rows.append([("⬅️ Back to Main Menu", "di:back", BLUE)])
    return rows


def autodel_text(doc):
    autodel = doc.get("autodel")
    delinput = doc.get("delinput")
    up = f"ON (deleted after {fmt_delay(autodel)})" if autodel else "OFF (Files not deleted)"
    if delinput is None:
        inp = "DISABLED ❌"
    else:
        inp = "ENABLED ✅" + ("" if delinput == 0 else f" (after {fmt_delay(delinput)})")
    return (
        "⏳ <b>Auto Delete System Settings</b>\n\n"
        f"• <b>Uploaded File Deletion:</b> {up}\n"
        f"• <b>Input Message Deletion:</b> {inp}\n\n"
        "Choose how long after uploading the bot should automatically delete the audio files from Telegram:\n"
        "<i>(Posts in the target channel are not deleted.)</i>"
    )


def delinput_text(doc):
    delinput = doc.get("delinput")
    if delinput is None:
        cur = "DISABLED ❌"
    elif delinput == 0:
        cur = "ENABLED ✅ (immediately after processing)"
    else:
        cur = f"ENABLED ✅ (after {fmt_delay(delinput)})"
    return (
        "🗑 <b>Delete Input Settings</b>\n\n"
        f"• <b>Input Message Deletion:</b> {cur}\n\n"
        "Choose how long after processing your original audio should be deleted:"
    )


# ======================================================
# Send / edit wrappers
# ======================================================

def _reply_params(reply_to):
    if not reply_to:
        return None
    return {"message_id": int(reply_to), "allow_sending_without_reply": True}


async def send(client, chat_id, text, rows=None, photo=None, reply_to=None):
    """Send a message (colored buttons if rows are given). Returns message_id."""
    markup = botapi.serialize_rows(rows) if rows else None
    try:
        if photo:
            res = await botapi.call(
                "sendPhoto", chat_id=chat_id, photo=photo, caption=text, parse_mode="HTML",
                reply_markup=markup, reply_parameters=_reply_params(reply_to),
            )
        else:
            res = await botapi.call(
                "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                reply_markup=markup, reply_parameters=_reply_params(reply_to),
                link_preview_options={"is_disabled": True},
            )
        return res["message_id"]
    except Exception as e:
        logger.warning(f"BotAPI send failed ({e}); falling back to Pyrogram")

    reply_markup = to_markup(rows) if rows else None
    if photo:
        m = await client.send_photo(
            chat_id, photo, caption=text, parse_mode=enums.ParseMode.HTML,
            reply_markup=reply_markup, reply_to_message_id=reply_to,
        )
    else:
        m = await client.send_message(
            chat_id, text, parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True,
            reply_markup=reply_markup, reply_to_message_id=reply_to,
        )
    return m.id


async def edit(client, chat_id, message_id, text=None, rows=KEEP, is_photo=False):
    """
    text=None  -> change only the buttons
    rows=KEEP  -> keep the buttons as they are
    rows=None  -> remove the buttons
    """
    try:
        if text is None:
            if rows is KEEP:
                return
            markup = botapi.serialize_rows(rows) if rows else {"inline_keyboard": []}
            await botapi.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=markup)
            return
        markup = None
        if rows is not KEEP:
            markup = botapi.serialize_rows(rows) if rows else {"inline_keyboard": []}
        if is_photo:
            await botapi.call("editMessageCaption", chat_id=chat_id, message_id=message_id, caption=text,
                              parse_mode="HTML", reply_markup=markup)
        else:
            await botapi.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                              parse_mode="HTML", reply_markup=markup,
                              link_preview_options={"is_disabled": True})
        return
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        logger.warning(f"BotAPI edit failed ({e}); falling back to Pyrogram")

    try:
        reply_markup = KEEP
        if rows is not KEEP:
            reply_markup = to_markup(rows) if rows else None
        if text is None:
            await client.edit_message_reply_markup(chat_id, message_id, reply_markup=reply_markup or InlineKeyboardMarkup([]))
        elif is_photo:
            kw = {} if reply_markup is KEEP else {"reply_markup": reply_markup}
            await client.edit_message_caption(chat_id, message_id, text, parse_mode=enums.ParseMode.HTML, **kw)
        else:
            kw = {} if reply_markup is KEEP else {"reply_markup": reply_markup}
            await client.edit_message_text(chat_id, message_id, text, parse_mode=enums.ParseMode.HTML,
                                           disable_web_page_preview=True, **kw)
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"edit fallback failed: {e}")


async def delete(client, chat_id, ids):
    try:
        await client.delete_messages(chat_id, ids)
    except Exception:
        pass


async def refresh_panel(client, uid, doc, panel=None):
    """Update the buttons of the latest panel (Caption ON/OFF, Auto Delete label...)."""
    from helpers.state import PANEL
    info = panel or PANEL.get(uid)
    if not info:
        return
    chat_id, msg_id, _ = info
    try:
        await edit(client, chat_id, msg_id, rows=panel_rows(doc))
    except Exception as e:
        logger.debug(f"panel refresh failed: {e}")
