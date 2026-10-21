"""
In-memory state (resets on restart - the user simply presses the button again).
"""

from pyrogram import filters

# uid -> {"kind": "artist", ...}   (which input is currently being asked from the user)
INPUT = {}

# uid -> {"items": [{"chat_id","msg_id","name","duration","size"}], "panel_id": int, "chat_id": int}
MERGE = {}

# uid -> {"done": int, "total": int}  (for Batch Mode in /status)
BATCH = {}

# uid -> (chat_id, message_id, is_photo)  (latest main panel that needs to be refreshed)
PANEL = {}


def set_input(uid, kind, **data):
    INPUT[uid] = {"kind": kind, **data}


def get_input(uid):
    return INPUT.get(uid)


def pop_input(uid):
    return INPUT.pop(uid, None)


def clear_user_state(uid):
    """Clear pending input, merge list and batch-wait."""
    INPUT.pop(uid, None)
    MERGE.pop(uid, None)


async def _has_input(_, __, message):
    u = message.from_user
    if not u or u.id not in INPUT:
        return False
    try:
        from plugins.session import LOGIN_STATE  # if login is in progress, do not disturb the router
        return u.id not in LOGIN_STATE
    except Exception:
        return True


has_input = filters.create(_has_input)
