"""
Voice Tag system:
  * manager      : save several voice tags (each with a title), listen, rename, remove, mode (Ask / Auto / Off)
  * plan flow    : when an audio (or batch / merge) is sent, ask which tag(s) and at which second(s) to insert them
Entry points used by other modules: start_add, show_manager, handle_input, plan_gate, append_audio, on_replaced
"""

import os
import uuid

from pyrogram import Client, enums, filters
from pyrogram.types import CallbackQuery, Message

from config import VOICE_DIR
from database.db import db
from helpers import media, ui, vtags
from helpers.pipeline import (UserError, compute_trim, drop_tag_file, ensure_tag_file, get_audio_media,
                              tag_path)
from helpers.state import get_input, pop_input, set_input
from helpers.utils import esc, fmt_duration
from logger import LOGGER

logger = LOGGER(__name__)
HTML = enums.ParseMode.HTML

BLUE, GREEN, RED = ui.BLUE, ui.GREEN, ui.RED
MAX_TAG_SECONDS = 600
NAME_MAX = 40
PLAN_KINDS = ("vplan_tags", "vplan_pos")


# ======================================================
# small helpers
# ======================================================

async def _prompt(client, uid, chat_id, kind, text, rows=None, **data):
    old = get_input(uid)
    if old and old.get("prompt_id"):
        await ui.delete(client, old.get("chat_id", chat_id), [old["prompt_id"]])
    pid = await ui.send(client, chat_id, text, rows=rows if rows is not None else ui.cancel_rows())
    set_input(uid, kind, prompt_id=pid, chat_id=chat_id, **data)
    return pid


async def _refresh_panel(client, uid):
    await ui.refresh_panel(client, uid, await db.get_user(uid))


def _grid(buttons, per_row=5):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


async def _limit_info(uid):
    tags = await vtags.get_tags(uid)
    return tags, await vtags.tag_limit(uid)


# ======================================================
# manager
# ======================================================

async def _manager_view(uid):
    tags, limit = await _limit_info(uid)
    mode = await vtags.get_mode(uid)
    lines = [f"🔊 <b>My Voice Tags</b>  ({len(tags)}/{limit})", f"⚙️ Mode: <b>{vtags.MODE_LABEL[mode]}</b>", ""]
    if tags:
        lines += [vtags.fmt_tag_line(i, t) for i, t in enumerate(tags, 1)]
        lines += ["", "<i>Tap a number to listen, rename or remove that tag.</i>"]
    else:
        lines.append("<i>No voice tags saved yet. Press Add Voice Tag.</i>")
    lines += ["",
              "<blockquote><b>Modes</b>\n"
              "• <b>Ask every time</b> - the bot asks which tag and where, for each audio\n"
              "• <b>Auto</b> - the last plan is applied to every audio without asking\n"
              "• <b>Off</b> - voice tags are not used</blockquote>"]
    rows = _grid([(str(i), f"vt:sel:{t['id']}", BLUE) for i, t in enumerate(tags, 1)])
    if len(tags) < limit:
        rows.append([("➕ Add Voice Tag", "vt:add", GREEN)])
    elif not await db.has_unlimited_access(uid):
        rows.append([("💎 Get Premium for more tags", "premium_plans_btn", GREEN)])
    rows.append([(f"⚙️ Mode: {vtags.MODE_LABEL[mode]}", "vt:mode", BLUE)])
    last = [("❌ Close", "close_btn", RED)]
    if tags:
        last.insert(0, ("🗑 Remove All", "vt:clr", RED))
    rows.append(last)
    return "\n".join(lines), rows


async def show_manager(client, uid, chat_id, edit_id=None):
    text, rows = await _manager_view(uid)
    if edit_id:
        await ui.edit(client, chat_id, edit_id, text=text, rows=rows)
    else:
        await ui.send(client, chat_id, text, rows=rows)


async def start_add(client, uid, chat_id):
    tags, limit = await _limit_info(uid)
    if len(tags) >= limit:
        prem = await db.has_unlimited_access(uid)
        text = (f"❌ <b>Voice Tag limit reached</b> ({len(tags)}/{limit})\n\n"
                + ("Remove one of your tags to add a new one."
                   if prem else
                   f"Free users can save up to <b>{limit}</b> voice tags. "
                   "💎 <b>Premium</b> users can save up to <b>50</b>.\n\nRemove a tag, or get Premium."))
        rows = [[("🔊 My Voice Tags", "vt:ls", BLUE)]]
        if not prem:
            rows.insert(0, [("💎 Upgrade to Premium", "premium_plans_btn", GREEN)])
        return await ui.send(client, chat_id, text, rows=rows)
    await _prompt(
        client, uid, chat_id, "voice",
        f"🔊 <b>Send me the Voice Tag</b>  ({len(tags) + 1}/{limit})\n\n"
        "Send a short audio / voice note. You will choose later where to place it in each song.")


@Client.on_callback_query(filters.regex(r"^vt:"))
async def vt_callback(client: Client, cq: CallbackQuery):
    uid = cq.from_user.id
    parts = cq.data.split(":")
    action = parts[1]
    arg = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else None
    msg = cq.message
    if msg is None:
        return await cq.answer()
    chat_id = msg.chat.id
    tags = await vtags.get_tags(uid)

    if action == "ls":
        await show_manager(client, uid, chat_id, edit_id=msg.id)
        return await cq.answer()

    if action == "add":
        await start_add(client, uid, chat_id)
        return await cq.answer()

    if action == "mode":
        new = await vtags.cycle_mode(uid)
        await show_manager(client, uid, chat_id, edit_id=msg.id)
        return await cq.answer(f"Mode: {vtags.MODE_LABEL[new]}", show_alert=False)

    if action in ("sel", "play", "ren", "del", "dely", "deln"):
        tag = vtags.find(tags, arg)
        if not tag:
            await cq.answer("This voice tag no longer exists.", show_alert=True)
            if action != "play":
                await show_manager(client, uid, chat_id, edit_id=msg.id)
            return
        idx = tags.index(tag) + 1

        if action == "sel":
            await ui.edit(
                client, chat_id, msg.id,
                text=(f"🔊 <b>Voice Tag {idx}</b>\n\n<b>Title:</b> {esc(tag['name'])}\n"
                      f"<b>Length:</b> {fmt_duration(tag.get('duration') or 0)}"),
                rows=[[("▶️ Listen", f"vt:play:{tag['id']}", GREEN), ("✏️ Rename", f"vt:ren:{tag['id']}", BLUE)],
                      [("🗑 Remove", f"vt:del:{tag['id']}", RED), ("⬅️ Back", "vt:ls", BLUE)]])
            return await cq.answer()

        if action == "play":
            await cq.answer("▶️ Sending the voice tag...")
            await _send_preview(client, uid, chat_id, tag, idx)
            return

        if action == "ren":
            await _prompt(client, uid, chat_id, "voice_rename",
                          f"✏️ <b>Rename Voice Tag {idx}</b>\n\nSend the new title (max {NAME_MAX} characters).\n"
                          f"Current: <b>{esc(tag['name'])}</b>", tag_id=tag["id"])
            return await cq.answer()

        if action == "del":
            await ui.edit(client, chat_id, msg.id,
                          text=f"🗑 <b>Remove Voice Tag {idx}</b>\n\nAre you sure you want to remove "
                               f"<b>{esc(tag['name'])}</b>?",
                          rows=[[("Yes ✅", f"vt:dely:{tag['id']}", GREEN), ("No ❌", f"vt:sel:{tag['id']}", RED)]])
            return await cq.answer()

        if action == "dely":
            gone = await vtags.remove_tags(uid, {tag["id"]})
            left = await vtags.get_tags(uid)
            for g in gone:
                if not vtags.shared_unique(left, g.get("unique")):
                    drop_tag_file(uid, g)
            await show_manager(client, uid, chat_id, edit_id=msg.id)
            await _refresh_panel(client, uid)
            return await cq.answer("✅ Voice tag removed")

        if action == "deln":
            return await cq.answer()

    if action == "clr":
        if not tags:
            return await cq.answer("No voice tags to remove.", show_alert=True)
        await ui.edit(client, chat_id, msg.id,
                      text=f"🗑 <b>Remove All Voice Tags</b>\n\nAre you sure you want to remove all <b>{len(tags)}</b> voice tags?",
                      rows=[[("Yes ✅", "vt:clry", GREEN), ("No ❌", "vt:ls", RED)]])
        return await cq.answer()

    if action == "clry":
        gone = await vtags.remove_tags(uid, {t["id"] for t in tags})
        for g in gone:
            drop_tag_file(uid, g)
        await db.unset_user(uid, "voice_plan")
        await show_manager(client, uid, chat_id, edit_id=msg.id)
        await _refresh_panel(client, uid)
        return await cq.answer("✅ All voice tags removed")

    if action == "deft":  # use the default title while adding
        st = get_input(uid)
        if not st or st.get("kind") != "voice_title":
            return await cq.answer("This option has expired. Press Add Voice Tag again.", show_alert=True)
        await _save_pending(client, uid, st, st["default"])
        return await cq.answer()

    await cq.answer()


async def _send_preview(client, uid, chat_id, tag, idx):
    src = await ensure_tag_file(client, uid, tag)
    if not src:
        return await client.send_message(chat_id, "❌ Could not load this voice tag. Please add it again.")
    prev = os.path.join(VOICE_DIR, f"prev_{uid}_{uuid.uuid4().hex[:6]}.mp3")
    try:
        await media.render_audio([src], prev, bitrate_k=128, cover=None, copy_ok=False)
        await client.send_audio(
            chat_id, prev, caption=f"🔊 <b>Voice Tag {idx}</b> - {esc(tag['name'])}", parse_mode=HTML,
            title=str(tag["name"]), performer="Voice Tag", file_name=f"{vtags_safe(tag['name'])}.mp3",
            reply_markup=ui.to_markup([[("❌ Close", "close_btn", RED)]]))
    except Exception as e:
        logger.warning(f"voice tag preview failed: {e}")
        await client.send_message(chat_id, "❌ Could not send the preview.")
    finally:
        try:
            os.remove(prev)
        except OSError:
            pass


def vtags_safe(name):
    from helpers.utils import sanitize_filename
    return sanitize_filename(str(name), "Voice Tag")


# ======================================================
# add a tag: audio -> title
# ======================================================

async def _in_voice(client, message, uid, st):
    f = message.audio or message.voice
    if not f and message.document and (message.document.mime_type or "").startswith("audio/"):
        f = message.document
    if not f:
        if message.text:
            await message.reply_text("❌ Please send an <b>audio / voice note</b> for the Voice Tag.",
                                     parse_mode=HTML, quote=True)
            return True
        _clear(uid)
        return False

    tags, limit = await _limit_info(uid)
    if len(tags) >= limit:
        _clear(uid)
        await start_add(client, uid, message.chat.id)
        return True
    same = next((t for t in tags if t.get("unique") == f.file_unique_id), None)
    if same:
        await message.reply_text(
            f"ℹ️ This audio is already saved as Voice Tag <b>{tags.index(same) + 1}</b> ({esc(same['name'])}). "
            "Send a different one.", parse_mode=HTML, quote=True)
        return True

    note = await message.reply_text("⏳ Checking the voice tag...", quote=True)
    doc = await db.get_user(uid)
    seq = int(doc.get("voice_seq") or 0) + 1
    pending = {"id": seq, "name": "", "file_id": f.file_id, "unique": f.file_unique_id,
               "duration": int(getattr(f, "duration", 0) or 0)}
    path = await ensure_tag_file(client, uid, pending)
    ok, exact = False, 0.0
    if path:
        try:
            exact = (await media.probe(path))["duration"]
            ok = True
        except media.MediaError:
            ok = False
    if not ok:
        drop_tag_file(uid, pending)
        await note.edit_text("❌ This does not look like a valid audio. Please send a different audio / voice note.")
        return True
    if exact > MAX_TAG_SECONDS:
        drop_tag_file(uid, pending)
        await note.edit_text(f"❌ A voice tag can be at most {fmt_duration(MAX_TAG_SECONDS)} long. Please send a shorter one.")
        return True
    pending["duration"] = int(round(exact))
    try:
        await note.delete()
    except Exception:
        pass

    default = vtags.default_name(tags)
    old = pop_input(uid)
    if old and old.get("prompt_id"):
        await ui.delete(client, old.get("chat_id"), [old["prompt_id"]])
    await _prompt(
        client, uid, message.chat.id, "voice_title",
        f"✅ Voice tag received ({fmt_duration(exact)}).\n\n"
        "📝 <b>Send a Title for this voice tag</b> (max "
        f"{NAME_MAX} characters)\nor press the button to use the default number.",
        rows=[[(f"✅ Use \"{default}\"", "vt:deft", GREEN)], [("❌ Cancel", "in:cancel", RED)]],
        pending=pending, default=default)
    return True


def _clear(uid):
    st = pop_input(uid)
    if st and st.get("pending"):
        drop_tag_file(uid, st["pending"])
    return st


async def _save_pending(client, uid, st, name):
    pending = st["pending"]
    tags, limit = await _limit_info(uid)
    chat_id = st.get("chat_id")
    pop_input(uid)
    if st.get("prompt_id"):
        await ui.delete(client, chat_id, [st["prompt_id"]])
    if len(tags) >= limit:
        drop_tag_file(uid, pending)
        return await start_add(client, uid, chat_id)
    old_path = tag_path(uid, pending)
    tag = await vtags.add_tag(uid, name, pending["file_id"], pending["unique"], pending["duration"])
    new_path = tag_path(uid, tag)
    if new_path != old_path and os.path.exists(old_path):
        os.replace(old_path, new_path)
    tags = await vtags.get_tags(uid)
    await ui.send(
        client, chat_id,
        f"✅ <b>Voice Tag saved!</b>\n\n🔊 Number <b>{len(tags)}</b> - {esc(name)} ({fmt_duration(tag['duration'])})\n"
        f"Saved: <b>{len(tags)}/{limit}</b>",
        rows=[[("🔊 My Voice Tags", "vt:ls", BLUE), ("➕ Add Another", "vt:add", GREEN)]])
    await _refresh_panel(client, uid)


async def _in_voice_title(client, message, uid, st):
    text = (message.text or "").strip() if message.text else None
    if text is None:
        _clear(uid)
        return False
    if not text:
        return True
    if len(text) > NAME_MAX:
        await message.reply_text(f"❌ Keep the title under {NAME_MAX} characters.", quote=True)
        return True
    await _save_pending(client, uid, st, text)
    return True


async def _in_voice_rename(client, message, uid, st):
    text = (message.text or "").strip() if message.text else None
    if text is None:
        pop_input(uid)
        return False
    if not text:
        return True
    if len(text) > NAME_MAX:
        await message.reply_text(f"❌ Keep the title under {NAME_MAX} characters.", quote=True)
        return True
    tags = await vtags.get_tags(uid)
    tag = vtags.find(tags, st.get("tag_id"))
    old = pop_input(uid)
    if old and old.get("prompt_id"):
        await ui.delete(client, old.get("chat_id"), [old["prompt_id"]])
    if not tag:
        await message.reply_text("❌ This voice tag no longer exists.", quote=True)
        return True
    await vtags.rename_tag(uid, tag["id"], text)
    await message.reply_text(f"✅ Renamed to: <b>{esc(text)}</b>", parse_mode=HTML, quote=True)
    return True


# ======================================================
# per-audio plan: which tag(s) + where
# ======================================================

def _scope_text(ctx):
    sc = ctx["scope"]
    if sc == "direct":
        n = len(ctx["msgs"])
        return f"for <b>{n}</b> audio{'s' if n != 1 else ''} (send more audios to add them here)"
    if sc == "batch":
        return "for <b>every audio of this batch</b>"
    return "for the <b>merged audio</b>"


def _trim_note(doc):
    if doc.get("start_trim") or doc.get("end_trim"):
        return "\n<i>ℹ️ Time is counted after your Start/End Trim.</i>"
    return ""


async def _max_duration(uid, ctx):
    """Known length of the (trimmed) audio, only when it is a single known audio - else None (no early check)."""
    dur = None
    if ctx["scope"] == "direct" and len(ctx["msgs"]) == 1:
        mo = get_audio_media(ctx["msgs"][0])
        dur = float(getattr(mo, "duration", 0) or 0) if mo else 0
    elif ctx["scope"] == "merge":
        dur = float(ctx.get("duration") or 0)
    if not dur or dur <= 0:
        return None
    doc = await db.get_user(uid)
    try:
        ts, te = compute_trim(dur, doc.get("start_trim"), doc.get("end_trim"))
    except UserError:
        return None
    return (te if te is not None else dur) - (ts or 0.0)


def _tags_view(ctx, tags, doc):
    lines = ["🔊 <b>Voice Tag</b>", f"<i>{_scope_text(ctx)}</i>", "", "Your saved voice tags:"]
    lines += [vtags.fmt_tag_line(i, t) for i, t in enumerate(tags, 1)]
    lines += ["",
              "👉 Send the <b>number</b> of the tag to add, e.g. <code>2</code>\n"
              "To use different tags at different places send numbers with commas, e.g. <code>1,2,1</code>\n\n"
              "▶️ Tap a number below to listen first."]
    rows = _grid([(f"▶️ {i}", f"vt:play:{t['id']}", BLUE) for i, t in enumerate(tags, 1)])
    plan = doc.get("voice_plan")
    if plan and any(vtags.find(tags, i.get("tag")) for i in plan.get("items", [])):
        rows.append([("♻️ Use last plan", "vp:last", GREEN)])
    rows.append([("🚫 No Voice Tag", "vp:skip", RED), ("❌ Cancel", "vp:cancel", RED)])
    return "\n".join(lines), rows


def _pos_view(ctx, tags, picks, doc, max_dur):
    chosen = ", ".join(f"<b>{p}</b> ({esc(str(tags[p - 1]['name'])[:20])})" for p in picks)
    lines = ["📍 <b>Where to add the voice tag?</b>", f"<i>{_scope_text(ctx)}</i>", "",
             f"Selected tag{'s' if len(picks) > 1 else ''}: {chosen}"]
    if max_dur:
        lines.append(f"Audio length: <b>{fmt_duration(max_dur)}</b>")
    lines += ["",
              "Send the second(s) after which the tag should play, separated by commas:\n"
              "• <code>15</code> → audio plays 15 sec, then the tag, then the rest of the audio\n"
              "• <code>15,45,58</code> → tag after 15, 45 and 58 sec\n"
              "• <code>0</code> = at the very start,  <code>end</code> = at the very end\n"
              "• Time can also be <code>MM:SS</code> (e.g. <code>1:30</code>)\n"
              "• Different tag per position: <code>15@1,45@2</code> (@ + tag number)"
              + _trim_note(doc)]
    rows = []
    if len(tags) > 1:
        rows.append([("⬅️ Change Tag", "vp:back", BLUE)])
    rows.append([("🚫 No Voice Tag", "vp:skip", RED), ("❌ Cancel", "vp:cancel", RED)])
    return "\n".join(lines), rows


async def plan_gate(client, uid, chat_id, cont, *, scope, msgs=None, duration=None):
    """
    Decide how the voice tag plan is obtained, then call  await cont(plan, ctx).
    No tags / mode Off -> cont(None) | mode Auto with a saved plan -> cont(saved plan) | else ask the user.
    """
    tags = await vtags.get_tags(uid)
    doc = await db.get_user(uid)
    mode = await vtags.get_mode(uid)
    ctx = {"scope": scope, "msgs": list(msgs or []), "duration": duration}
    if not tags or mode == "off":
        return await cont(None, ctx)
    plan = doc.get("voice_plan")
    if mode == "auto" and plan and plan.get("items"):
        return await cont(plan, ctx)
    await _open_tags_step(client, uid, chat_id, cont, ctx, tags, doc)


async def _open_tags_step(client, uid, chat_id, cont, ctx, tags, doc):
    if len(tags) == 1:
        return await _open_pos_step(client, uid, chat_id, cont, ctx, tags, doc, picks=[1])
    text, rows = _tags_view(ctx, tags, doc)
    await _prompt(client, uid, chat_id, "vplan_tags", text, rows=rows, cont=cont, ctx=ctx, scope=ctx["scope"], picks=None)


async def _open_pos_step(client, uid, chat_id, cont, ctx, tags, doc, picks):
    text, rows = _pos_view(ctx, tags, picks, doc, await _max_duration(uid, ctx))
    await _prompt(client, uid, chat_id, "vplan_pos", text, rows=rows, cont=cont, ctx=ctx, scope=ctx["scope"], picks=picks)


async def append_audio(client, uid, message):
    """Another audio arrives while the plan question is open -> it joins the same plan."""
    st = get_input(uid)
    if not st or st.get("kind") not in PLAN_KINDS:
        return False
    if st.get("scope") != "direct":
        await message.reply_text("⬆️ Please finish (or cancel) the voice tag question above first.", quote=True)
        return True
    st["ctx"]["msgs"].append(message)
    tags = await vtags.get_tags(uid)
    doc = await db.get_user(uid)
    if st["kind"] == "vplan_tags":
        text, rows = _tags_view(st["ctx"], tags, doc)
    else:
        text, rows = _pos_view(st["ctx"], tags, st["picks"], doc, await _max_duration(uid, st["ctx"]))
    await ui.edit(client, st["chat_id"], st["prompt_id"], text=text, rows=rows)
    return True


async def on_replaced(client, old):
    """A plan question was replaced by another prompt - tell the user that the waiting audios were dropped."""
    if old and old.get("kind") in PLAN_KINDS and old.get("scope") == "direct":
        n = len(old["ctx"]["msgs"])
        try:
            await client.send_message(
                old["chat_id"], f"⚠️ Voice tag selection was cancelled. {n} audio(s) were not processed - please send again.")
        except Exception:
            pass


async def _finish_plan(client, uid, st, plan, summary):
    pop_input(uid)
    await ui.edit(client, st["chat_id"], st["prompt_id"], text=summary, rows=None)
    await st["cont"](plan, st["ctx"])


async def _in_plan_tags(client, message, uid, st, text):
    tags = await vtags.get_tags(uid)
    picks, err = vtags.parse_picks(text, len(tags))
    if err:
        await message.reply_text(f"❌ {err}", parse_mode=HTML, quote=True)
        return True
    doc = await db.get_user(uid)
    pos_text, rows = _pos_view(st["ctx"], tags, picks, doc, await _max_duration(uid, st["ctx"]))
    st["kind"], st["picks"] = "vplan_pos", picks
    await ui.edit(client, st["chat_id"], st["prompt_id"], text=pos_text, rows=rows)
    return True


async def _in_plan_pos(client, message, uid, st, text):
    tags = await vtags.get_tags(uid)
    items, err = vtags.parse_positions(text, len(tags), st["picks"], await _max_duration(uid, st["ctx"]))
    if err:
        await message.reply_text(f"❌ {err}", parse_mode=HTML, quote=True)
        return True
    plan = vtags.make_plan(items, tags)
    await vtags.save_plan(uid, plan)
    summary = f"✅ <b>Voice tag plan set</b>\n🔊 {vtags.fmt_plan(plan, tags)}"
    if st["scope"] == "direct":
        n = len(st["ctx"]["msgs"])
        summary += f"\n<i>Processing {n} audio{'s' if n != 1 else ''}...</i>"
    await _finish_plan(client, uid, st, plan, summary)
    return True


@Client.on_callback_query(filters.regex(r"^vp:"))
async def vp_callback(client: Client, cq: CallbackQuery):
    uid = cq.from_user.id
    action = cq.data[3:]
    st = get_input(uid)
    if not st or st.get("kind") not in PLAN_KINDS or st.get("prompt_id") != cq.message.id:
        try:
            await cq.message.delete()
        except Exception:
            pass
        return await cq.answer("This question has expired. Send the audio again.", show_alert=True)
    tags = await vtags.get_tags(uid)
    doc = await db.get_user(uid)

    if action == "skip":
        await cq.answer()
        return await _finish_plan(client, uid, st, None, "🚫 <b>No voice tag</b> for this one.")

    if action == "cancel":
        pop_input(uid)
        n = len(st["ctx"]["msgs"])
        extra = f" {n} audio(s) were not processed." if st["scope"] == "direct" else ""
        await ui.edit(client, st["chat_id"], st["prompt_id"], text="❌ <b>Cancelled.</b>" + extra, rows=None)
        return await cq.answer("Cancelled")

    if action == "last":
        plan = doc.get("voice_plan") or {}
        items = [i for i in plan.get("items", []) if vtags.find(tags, i.get("tag"))]
        if not items:
            return await cq.answer("No valid saved plan. Choose a tag.", show_alert=True)
        plan = {"items": items}
        await cq.answer()
        return await _finish_plan(client, uid, st, plan, f"✅ <b>Using the last plan</b>\n🔊 {vtags.fmt_plan(plan, tags)}")

    if action == "back":
        if len(tags) < 2:
            return await cq.answer()
        text, rows = _tags_view(st["ctx"], tags, doc)
        st["kind"], st["picks"] = "vplan_tags", None
        await ui.edit(client, st["chat_id"], st["prompt_id"], text=text, rows=rows)
        return await cq.answer()

    await cq.answer()


# ======================================================
# typed input dispatcher (called from editor.input_router)
# ======================================================

async def handle_input(client, message, uid, st, kind):
    """Returns True when the message was consumed."""
    if kind == "voice":
        return await _in_voice(client, message, uid, st)
    if kind == "voice_title":
        return await _in_voice_title(client, message, uid, st)
    if kind == "voice_rename":
        return await _in_voice_rename(client, message, uid, st)
    if kind in PLAN_KINDS:
        text = (message.text or "").strip() if message.text else None
        if text is None:
            return False  # an audio: audio_handler adds it to the plan (do NOT clear the state)
        if not text:
            return True
        if kind == "vplan_tags":
            return await _in_plan_tags(client, message, uid, st, text)
        return await _in_plan_pos(client, message, uid, st, text)
    return False
