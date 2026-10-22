"""
Voice tag storage + plan logic (no Telegram handlers here).

User doc fields:
  voice_tags : [{"id", "name", "file_id", "unique", "duration"}]   saved tags (order = number shown to the user)
  voice_seq  : last used tag id
  voice_mode : "ask" (default) | "auto" | "off"
  voice_plan : {"items": [{"t": seconds | "end", "tag": tag_id}]}   last used plan
"""

import re

from config import VOICE_MAX_POSITIONS, VOICE_TAG_FREE_LIMIT, VOICE_TAG_PREMIUM_LIMIT
from database.db import db
from helpers.utils import esc, fmt_duration, fmt_trim, parse_time

MODES = ("ask", "auto", "off")
MODE_LABEL = {
    "ask": "Ask every time",
    "auto": "Auto (use last plan)",
    "off": "Off",
}
_SPLIT = re.compile(r"[,;\s]+")


# ======================================================
# storage
# ======================================================

async def migrate_legacy(uid):
    """Old single voice tag (voice_file_id) -> tag #1, same behaviour as before (added at the start, automatically)."""
    doc = await db.get_user(uid)
    if not doc.get("voice_file_id") or doc.get("voice_tags"):
        return
    tag = {
        "id": 1,
        "name": str(doc.get("voice_name") or "1")[:40],
        "file_id": doc["voice_file_id"],
        "unique": doc.get("voice_unique") or "legacy",
        "duration": int(doc.get("voice_duration") or 0),
    }
    await db.update_user(uid, voice_tags=[tag], voice_seq=1, voice_mode="auto",
                         voice_plan={"items": [{"t": 0.0, "tag": 1}]})
    await db.unset_user(uid, "voice_file_id", "voice_unique", "voice_name", "voice_duration")


async def get_tags(uid):
    await migrate_legacy(uid)
    doc = await db.get_user(uid)
    return list(doc.get("voice_tags") or [])


async def tag_limit(uid):
    return VOICE_TAG_PREMIUM_LIMIT if await db.has_unlimited_access(uid) else VOICE_TAG_FREE_LIMIT


def default_name(tags):
    used = {str(t.get("name")) for t in tags}
    n = 1
    while str(n) in used:
        n += 1
    return str(n)


async def add_tag(uid, name, file_id, unique, duration):
    doc = await db.get_user(uid)
    tags = list(doc.get("voice_tags") or [])
    seq = int(doc.get("voice_seq") or 0) + 1
    tag = {"id": seq, "name": name, "file_id": file_id, "unique": unique, "duration": int(duration or 0)}
    tags.append(tag)
    await db.update_user(uid, voice_tags=tags, voice_seq=seq)
    return tag


async def remove_tags(uid, ids):
    """Remove tags (and drop them from the saved plan). Returns the removed tag dicts."""
    doc = await db.get_user(uid)
    tags = list(doc.get("voice_tags") or [])
    gone = [t for t in tags if t["id"] in ids]
    keep = [t for t in tags if t["id"] not in ids]
    fields = {"voice_tags": keep}
    plan = doc.get("voice_plan")
    if plan:
        items = [i for i in plan.get("items", []) if i.get("tag") not in ids]
        if items:
            fields["voice_plan"] = {"items": items}
        else:
            await db.unset_user(uid, "voice_plan")
    await db.update_user(uid, **fields)
    return gone


async def rename_tag(uid, tag_id, name):
    doc = await db.get_user(uid)
    tags = [dict(t) for t in (doc.get("voice_tags") or [])]
    for t in tags:
        if t["id"] == tag_id:
            t["name"] = name
    await db.update_user(uid, voice_tags=tags)


def find(tags, tag_id):
    return next((t for t in tags if t["id"] == tag_id), None)


async def get_mode(uid):
    doc = await db.get_user(uid)
    m = doc.get("voice_mode", "ask")
    return m if m in MODES else "ask"


async def cycle_mode(uid):
    m = await get_mode(uid)
    new = MODES[(MODES.index(m) + 1) % len(MODES)]
    await db.update_user(uid, voice_mode=new)
    return new


async def save_plan(uid, plan):
    await db.update_user(uid, voice_plan=plan)


def shared_unique(tags, unique, except_id=None):
    return any(t.get("unique") == unique and t["id"] != except_id for t in tags)


# ======================================================
# parsing
# ======================================================

def parse_picks(text, n):
    """'2' | '1,2,1' -> ([indexes 1..n], None) or (None, error)."""
    parts = [p for p in _SPLIT.split(text.strip()) if p]
    if not parts:
        return None, "Send the tag number, e.g. <code>1</code> or <code>1,2,1</code>."
    if len(parts) > VOICE_MAX_POSITIONS:
        return None, f"Too many tags. Maximum {VOICE_MAX_POSITIONS} at a time."
    picks = []
    for p in parts:
        if not p.isdigit():
            return None, f"<code>{esc(p)}</code> is not a valid tag number. Send only numbers, e.g. <code>1</code> or <code>1,2</code>."
        i = int(p)
        if not 1 <= i <= n:
            return None, f"Tag number <b>{i}</b> does not exist. You have tags <b>1</b> to <b>{n}</b>."
        picks.append(i)
    return picks, None


def parse_positions(text, n_tags, picks, max_dur=None):
    """
    '15' | '15,45,58' | '0, 1:30, end' | '15@2,45@1'  ->  (items, None) or (None, error)
    items = [{"t": float|"end", "tag_idx": 1..n}]  sorted by time (stable).
    picks = tag numbers chosen in step 1 (cycled over the positions without an explicit @tag).
    """
    parts = [p for p in _SPLIT.split(text.strip()) if p]
    if not parts:
        return None, "Send the time in seconds, e.g. <code>15</code> or <code>15,45,58</code>."
    if len(parts) > VOICE_MAX_POSITIONS:
        return None, f"Too many positions. Maximum {VOICE_MAX_POSITIONS} per audio."

    raw = []
    for p in parts:
        tag_idx = None
        if "@" in p:
            p, _, tg = p.partition("@")
            if not tg.isdigit() or not 1 <= int(tg) <= n_tags:
                return None, f"Tag number after <code>@</code> must be between 1 and {n_tags}."
            tag_idx = int(tg)
        low = p.lower()
        if low in ("end", "last", "e"):
            t = "end"
        elif low in ("start", "begin", "s"):
            t = 0.0
        else:
            t = parse_time(p)
            if t is None:
                return None, (f"<code>{esc(p)}</code> is not a valid time. Use seconds (<code>15</code>), "
                              "<code>MM:SS</code>, <code>start</code> or <code>end</code>.")
            if max_dur and t > max_dur + 0.5:
                return None, (f"<b>{fmt_trim(t)}</b> is beyond the end of the audio "
                              f"(<b>{fmt_duration(max_dur)}</b>). Send a smaller time.")
        raw.append((t, tag_idx))

    explicit = sum(1 for _, ti in raw if ti is not None)
    free = len(raw) - explicit
    if free and len(picks) > free:
        return None, (f"You chose <b>{len(picks)}</b> tags but gave only <b>{free}</b> position(s) for them. "
                      "Send one time for each tag, or use fewer tags.")
    items, k = [], 0
    for t, ti in raw:
        if ti is None:
            ti = picks[k % len(picks)]
            k += 1
        items.append({"t": t, "tag_idx": ti})
    items.sort(key=lambda i: float("inf") if i["t"] == "end" else i["t"])
    return items, None


def make_plan(items, tags):
    """items (tag_idx) -> storable plan (tag ids)."""
    return {"items": [{"t": i["t"], "tag": tags[i["tag_idx"] - 1]["id"]} for i in items]}


# ======================================================
# text
# ======================================================

def fmt_tag_line(i, tag):
    return f"<b>{i}.</b> {esc(str(tag.get('name'))[:40])} <i>({fmt_duration(tag.get('duration') or 0)})</i>"


def fmt_plan(plan, tags):
    """'🔊 name @ 0:15 • 🔊 name @ end'"""
    if not plan or not plan.get("items"):
        return "None"
    out = []
    for it in plan["items"]:
        tag = find(tags, it.get("tag"))
        if not tag:
            continue
        at = "end" if it["t"] == "end" else ("start" if float(it["t"]) <= 0.01 else fmt_trim(it["t"]))
        out.append(f"{esc(str(tag['name'])[:25])} @ {at}")
    return " • ".join(out) if out else "None"


# ======================================================
# resolve at render time
# ======================================================

def resolve(plan, tags, eff_dur):
    """
    plan + current tags + length of the (trimmed) audio -> ([(t, tag)], [skipped notes])
    Positions beyond the audio are skipped; tags that were deleted are skipped.
    """
    ok, skipped = [], []
    for it in (plan or {}).get("items", []):
        tag = find(tags, it.get("tag"))
        if not tag:
            skipped.append("a deleted voice tag")
            continue
        t = eff_dur if it["t"] == "end" else float(it["t"])
        if t > eff_dur + 0.05:
            skipped.append(f"{fmt_trim(t)} (audio is only {fmt_duration(eff_dur)})")
            continue
        ok.append((min(t, eff_dur), tag))
    ok.sort(key=lambda x: x[0])
    return ok, skipped
