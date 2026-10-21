"""/myplan text builder (Save Restricted bot ke premium.py wala system, same UI)."""

from datetime import datetime

from config import FREE_DAILY_LIMIT, OWNER_ID, SUPPORT_USERNAME
from database.db import IST, Database, db, to_ist


def fmt_ist(dt):
    return dt.strftime("%d %b %Y, %I:%M %p") + " IST"


def fmt_left(delta):
    secs = max(0, int(delta.total_seconds()))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    return f"{days}d {hours}h {rem // 60}m"


def active_grants(user_id, doc):
    """(grants, expired_premium_dt). grant = {label, start, end}; end None = permanent."""
    now = datetime.now(IST)
    grants, expired = [], None
    if user_id == OWNER_ID:
        grants.append({"label": "Owner", "start": None, "end": None})
    if doc.get("is_auth"):
        end = to_ist(doc.get("auth_expiry"))
        if doc.get("auth_expiry") in (None, "") or (end and end > now):
            grants.append({"label": "Premium", "start": to_ist(doc.get("auth_start")), "end": end})
    if doc.get("is_premium"):
        end = to_ist(doc.get("premium_expiry"))
        if doc.get("premium_expiry") in (None, "") or (end and end > now):
            grants.append({"label": "Premium", "start": to_ist(doc.get("premium_start")), "end": end})
        elif end:
            expired = end
    return grants, expired


async def build_plan_text(user_id, first_name=""):
    doc = await db.get_user(user_id, first_name)
    total_saves = doc.get("total_saves", 0)
    grants, expired = active_grants(user_id, doc)

    if grants:
        grant = max(grants, key=lambda g: (g["end"] is None, g["end"] or datetime.min.replace(tzinfo=IST)))
        now_ist = datetime.now(IST)
        if grant["label"] == "Owner":
            period = "<b>📅 Validity:</b> <code>Lifetime (Owner)</code>\n\n"
        else:
            start_text = fmt_ist(grant["start"]) if grant["start"] else "Not recorded"
            if grant["end"] is None:
                end_text, left = "Lifetime (Permanent)", ""
            else:
                end_text = fmt_ist(grant["end"])
                left = f"<b>⌛ Time Left:</b> <code>{fmt_left(grant['end'] - now_ist)}</code>\n"
            period = (
                f"<b>🟢 Activated From:</b> <code>{start_text}</code>\n"
                f"<b>📅 Valid Till:</b> <code>{end_text}</code>\n"
                f"{left}\n"
            )
        return (
            "<b>👑 Premium Status: Active</b>\n\n"
            f"{period}"
            "<b>♾️ Daily Audio Limit:</b> Unlimited\n"
            "<b>♾️ Batch / Merge Limit:</b> Unlimited\n"
            f"<b>📊 Total Audios Edited:</b> <code>{total_saves}</code>\n\n"
            "<i>Thank you for supporting the bot! 🎉</i>"
        )

    used, reset = await db.get_usage(user_id)
    left = max(0, FREE_DAILY_LIMIT - used)
    expired_text = f"<b>⚠️ Your premium expired on:</b> <code>{fmt_ist(expired)}</code>\n\n" if expired else ""
    reset_text = ""
    if reset is not None and used > 0:
        reset_ist = to_ist(reset)
        if reset_ist:
            reset_text = f"<b>🔄 Resets At:</b> <code>{fmt_ist(reset_ist)}</code>\n"
    return (
        "<b>👤 Plan: Free Tier</b>\n\n"
        f"{expired_text}"
        f"<b>🎫 Daily Audio Tokens:</b> <code>{left} / {FREE_DAILY_LIMIT}</code>\n"
        f"{reset_text}"
        f"<b>📊 Total Audios Edited:</b> <code>{total_saves}</code>\n\n"
        "<i>Upgrade to Premium for unlimited access! 🚀</i>"
    )


def plan_rows():
    return [
        [("💎 View Premium Plans", "premium_plans_btn", "primary")],
        [("📞 Contact Admin", f"https://t.me/{SUPPORT_USERNAME}", None)],
    ]
