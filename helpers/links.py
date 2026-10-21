"""
Telegram post link parser (same logic as the Save Restricted bot).

Supported:
  https://t.me/channel/123            -> public, single
  https://t.me/channel/100-110        -> public, range   ("Channel/starting-ending")
  https://t.me/c/1234567890/123       -> private (login required)
  https://t.me/c/1234567890/100-110   -> private range
  https://t.me/c/1234567890/5/123     -> private forum/topic link
"""

import re

TME_PRIVATE_RE = re.compile(r"https?://t\.me/c/(\d+)(?:/\d+)?/(\d+)(?:-(\d+))?")
TME_PUBLIC_RE = re.compile(r"https?://t\.me/([A-Za-z0-9_]+)/(\d+)(?:-(\d+))?")


def parse_telegram_link(text):
    """
    Returns (kind, target, from_id, to_id)
      kind   : "private" | "public" | None
      target : int chat_id (-100...) for private, str username for public
    If no link is found: (None, None, None, None).
    """
    if not text:
        return None, None, None, None
    m = TME_PRIVATE_RE.search(text)
    if m:
        channel_id = int("-100" + m.group(1))
        from_id = int(m.group(2))
        to_id = int(m.group(3)) if m.group(3) else from_id
        return "private", channel_id, from_id, to_id
    m = TME_PUBLIC_RE.search(text)
    if m and m.group(1) not in ("c", "b", "s", "addlist", "joinchat"):
        from_id = int(m.group(2))
        to_id = int(m.group(3)) if m.group(3) else from_id
        return "public", m.group(1), from_id, to_id
    return None, None, None, None
