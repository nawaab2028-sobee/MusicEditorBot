"""
In-memory state (restart par reset hota hai - user bas button dobara dabata hai).
"""

from pyrogram import filters

# uid -> {"kind": "artist", ...}   (user se abhi kaunsa input maanga ja raha hai)
INPUT = {}

# uid -> {"items": [{"chat_id","msg_id","name","duration","size"}], "panel_id": int, "chat_id": int}
MERGE = {}

# uid -> {"done": int, "total": int}  (/status me Batch Mode ke liye)
BATCH = {}

# uid -> (chat_id, message_id, is_photo)  (latest main panel jisko refresh karna hai)
PANEL = {}


def set_input(uid, kind, **data):
    INPUT[uid] = {"kind": kind, **data}


def get_input(uid):
    return INPUT.get(uid)


def pop_input(uid):
    return INPUT.pop(uid, None)


def clear_user_state(uid):
    """Pending input, merge list aur batch-wait sab saaf."""
    INPUT.pop(uid, None)
    MERGE.pop(uid, None)


async def _has_input(_, __, message):
    u = message.from_user
    if not u or u.id not in INPUT:
        return False
    try:
        from plugins.session import LOGIN_STATE  # login chal raha ho to router ko mat chhedo
        return u.id not in LOGIN_STATE
    except Exception:
        return True


has_input = filters.create(_has_input)
