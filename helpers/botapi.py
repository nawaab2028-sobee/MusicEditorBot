"""
Direct Telegram Bot API (HTTP) calls.

Kyun: Blue / Green / Red colored inline buttons Bot API ke `style` field se aate hain
(primary / success / danger). Pyrogram ka version isse support kare ya na kare, yeh
HTTP route hamesha chalta hai. Callback queries normal tareeke se Pyrogram handlers me hi aate hain.
"""

import asyncio

import requests

from config import BOT_API_BASE, BOT_TOKEN
from logger import LOGGER

logger = LOGGER(__name__)


class BotApiError(Exception):
    pass


def _post(method, params):
    url = f"{BOT_API_BASE}/bot{BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=params, timeout=30)
        data = r.json()
    except Exception as e:
        raise BotApiError(f"request failed: {e}")
    if not data.get("ok"):
        raise BotApiError(data.get("description", "unknown error"))
    return data.get("result")


async def call(method, **params):
    params = {k: v for k, v in params.items() if v is not None}
    return await asyncio.to_thread(_post, method, params)


def serialize_rows(rows):
    """rows: [[(text, callback_data, style|None), ...], ...]  ->  Bot API inline_keyboard"""
    out = []
    for row in rows:
        r = []
        for text, data, style in row:
            if str(data).startswith(("http://", "https://", "tg://")):
                btn = {"text": text, "url": data}
            else:
                btn = {"text": text, "callback_data": data}
            if style:
                btn["style"] = style
            r.append(btn)
        out.append(r)
    return {"inline_keyboard": out}
