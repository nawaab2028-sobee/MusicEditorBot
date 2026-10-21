"""
Premium system (Save Restricted bot jaisa): /myplan, /premium + owner commands
/add_premium, /remove_premium, /addauth, /rmauth
"""

from datetime import date, datetime, timedelta

from pyrogram import Client, enums, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import FREE_DAILY_LIMIT, OWNER_ID, SUBSCRIPTION, SUPPORT_USERNAME, UPI_ID, QR_CODE
from database.db import db, to_ist
from helpers import ui
from helpers.planinfo import build_plan_text, fmt_ist, plan_rows
from logger import LOGGER

logger = LOGGER(__name__)
HTML = enums.ParseMode.HTML

PLANS_TEXT = (
    "<b>💎 Premium Plans</b>\n\n"
    "<blockquote>\n"
    "<b>Why Go Premium?</b>\n"
    "• ♾️ <b>Unlimited</b> daily audio edits\n"
    "• 📁 <b>Unlimited</b> batch & 🔗 merge\n"
    "• ⚡ <b>Zero</b> daily-limit waiting\n"
    "• 👑 <b>Premium</b> badge\n"
    "</blockquote>\n\n"
    f"<i>Free plan: {FREE_DAILY_LIMIT} audio / 24 hours</i>\n\n"
    "<b>💲 Pricing:</b>\n"
    "• <b>1 Week:</b> ₹50 / $0.5\n"
    "• <b>1 Month:</b> ₹150 / $1.5\n"
    "• <b>3 Month:</b> ₹300 / $3\n\n"
    "<i>Tap the button below to buy instantly.</i>"
)


def _plans_rows():
    return [
        [("💳 Buy Premium Now", f"https://t.me/{SUPPORT_USERNAME}", "success")],
        [("⬅️ Back to My Plan", "myplan_back_btn", "primary")],
    ]


# ======================================================
# user commands
# ======================================================

@Client.on_message(filters.command("myplan") & filters.private)
async def my_plan(client: Client, message: Message):
    user = message.from_user
    text = await build_plan_text(user.id, user.first_name)
    await ui.send(client, message.chat.id, text, rows=plan_rows(), reply_to=message.id)


@Client.on_message(filters.command(["premium", "plan"]) & filters.private)
async def premium_info(client: Client, message: Message):
    await ui.send(client, message.chat.id, PLANS_TEXT, rows=_plans_rows(), reply_to=message.id)


@Client.on_callback_query(filters.regex(r"^premium_plans_btn$"))
async def premium_plans_callback(client: Client, cq: CallbackQuery):
    msg = cq.message
    if msg.photo:
        await ui.send(client, msg.chat.id, PLANS_TEXT, rows=_plans_rows())
    else:
        await ui.edit(client, msg.chat.id, msg.id, text=PLANS_TEXT, rows=_plans_rows())
    await cq.answer()


@Client.on_callback_query(filters.regex(r"^myplan_back_btn$"))
async def myplan_back_callback(client: Client, cq: CallbackQuery):
    text = await build_plan_text(cq.from_user.id, cq.from_user.first_name)
    await ui.edit(client, cq.message.chat.id, cq.message.id, text=text, rows=plan_rows())
    await cq.answer()


# ======================================================
# owner commands
# ======================================================

@Client.on_message(filters.command("add_premium") & filters.user(OWNER_ID) & filters.private)
async def add_premium_admin(client: Client, message: Message):
    if len(message.command) < 3:
        return await message.reply_text(
            "<b>⚠️ Admin Usage:</b>\n<code>/add_premium &lt;user_id&gt; &lt;days&gt;</code>\n\n"
            "<i>Use 0 for permanent premium.</i>", parse_mode=HTML)
    try:
        user_id = int(message.command[1])
        days = int(message.command[2])
        if days == 0:
            expiry, duration = None, "Permanent"
        else:
            expiry = (date.today() + timedelta(days=days)).isoformat()
            duration = f"{days} days (until {fmt_ist(to_ist(expiry))})"
        await db.get_user(user_id, str(user_id))
        await db.add_premium(user_id, expiry)
        await message.reply_text(
            f"<b>✅ Premium Added Successfully</b>\n\n<b>User ID:</b> <code>{user_id}</code>\n<b>Duration:</b> {duration}",
            parse_mode=HTML)
        try:
            await client.send_message(user_id, "🎉 <b>Premium activate ho gaya!</b>\nAb aapke liye koi daily limit nahi. /myplan dekho.",
                                      parse_mode=HTML)
        except Exception:
            pass
    except ValueError:
        await message.reply_text("❌ <b>Error:</b> User ID and Days must be numbers.", parse_mode=HTML)
    except Exception as e:
        await message.reply_text(f"❌ <b>Error:</b> {e}", parse_mode=HTML)


@Client.on_message(filters.command("remove_premium") & filters.user(OWNER_ID) & filters.private)
async def remove_premium_admin(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("<b>⚠️ Usage:</b> <code>/remove_premium &lt;user_id&gt;</code>", parse_mode=HTML)
    try:
        user_id = int(message.command[1])
        await db.remove_premium(user_id)
        await message.reply_text(f"✅ Premium removed from <code>{user_id}</code>.", parse_mode=HTML)
    except Exception as e:
        await message.reply_text(f"Error: {e}")


@Client.on_message(filters.command("addauth") & filters.user(OWNER_ID) & filters.private)
async def add_auth_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "<b>⚠️ Owner Usage:</b>\n<code>/addauth &lt;user_id&gt; [days]</code>\n\n"
            "<i>Days default 7. User ko unlimited access milta hai expiry tak.</i>", parse_mode=HTML)
    try:
        user_id = int(message.command[1])
        days = int(message.command[2]) if len(message.command) > 2 else 7
        await db.get_user(user_id, str(user_id))
        expiry = datetime.now() + timedelta(days=days)
        await db.add_auth(user_id, expiry)
        await message.reply_text(
            f"<b>✅ Auth Access Granted</b>\n\n<b>User ID:</b> <code>{user_id}</code>\n"
            f"<b>Duration:</b> {days} day(s)\n<b>Expires:</b> <code>{fmt_ist(to_ist(expiry))}</code>", parse_mode=HTML)
    except ValueError:
        await message.reply_text("❌ <b>Error:</b> User ID and days must be numbers.", parse_mode=HTML)
    except Exception as e:
        await message.reply_text(f"❌ <b>Error:</b> {e}", parse_mode=HTML)


@Client.on_message(filters.command("rmauth") & filters.user(OWNER_ID) & filters.private)
async def remove_auth_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("<b>⚠️ Usage:</b> <code>/rmauth &lt;user_id&gt;</code>", parse_mode=HTML)
    try:
        user_id = int(message.command[1])
        await db.remove_auth(user_id)
        await message.reply_text(f"<b>✅ Auth Access Removed</b>\n\n<b>User ID:</b> <code>{user_id}</code>", parse_mode=HTML)
    except ValueError:
        await message.reply_text("❌ <b>Error:</b> User ID must be a number.", parse_mode=HTML)
    except Exception as e:
        await message.reply_text(f"❌ <b>Error:</b> {e}", parse_mode=HTML)
