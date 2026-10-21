import asyncio
import datetime
import os
from datetime import timezone, timedelta

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import BotCommand, Message

from config import API_HASH, API_ID, BOT_TOKEN, COVER_DIR, DATA_DIR, DOWNLOAD_DIR, LOG_CHANNEL, OWNER_ID, VOICE_DIR
from database.db import db
from helpers.autodelete import autodelete_loop
from logger import LOGGER

# Keep-alive Flask server (Render / Docker, port 8000)
try:
    from keep_alive import keep_alive
except ImportError:
    keep_alive = None

logger = LOGGER(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

USER_CACHE = set()


class Bot(Client):
    def __init__(self):
        super().__init__(
            name="AudioEditorBot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            plugins=dict(root="plugins"),
            workers=16,
            sleep_threshold=15,
            max_concurrent_transmissions=5,
            ipv6=False,
            in_memory=False,
        )
        self._keep_alive_started = False
        self._bg_tasks = []

    async def start(self):
        for d in (DATA_DIR, COVER_DIR, VOICE_DIR, DOWNLOAD_DIR):
            os.makedirs(d, exist_ok=True)

        # 1. Keep-alive pehle (Render port detect kare)
        if keep_alive and not self._keep_alive_started:
            try:
                keep_alive()
                self._keep_alive_started = True
                logger.info("Keep-alive server started.")
            except Exception as e:
                logger.warning(f"Keep-alive failed: {e}")

        # 2. FloodWait-safe login loop
        while True:
            try:
                await super().start()
                break
            except FloodWait as e:
                wait_time = int(e.value) + 10
                logger.warning(f"FLOOD_WAIT during login. Sleeping {wait_time}s...")
                await asyncio.sleep(wait_time)
            except Exception as e:
                logger.error(f"Critical Startup Error: {e}")
                await asyncio.sleep(15)

        me = await self.get_me()

        # 3. DB
        mongo = await db.ping()
        user_count = await db.total_users_count() if mongo else len(db.cache)
        logger.info(f"MongoDB {'connected' if mongo else 'DOWN (JSON mirror mode)'}: {user_count} users.")

        # 4. Auto-delete scheduler
        self._bg_tasks.append(asyncio.create_task(autodelete_loop(self)))

        # 5. Startup log
        now = datetime.datetime.now(IST)
        text = (
            "<b><i>🤖 Audio Editor Bot Started ♻️</i></b>\n\n"
            f"<b>Bot:</b> @{me.username}\n"
            f"<b>Users:</b> <code>{user_count}</code>\n"
            f"<b>MongoDB:</b> <code>{'OK' if mongo else 'DOWN'}</code>\n"
            f"<b>Time:</b> <code>{now.strftime('%I:%M %p')} IST</code>"
        )
        try:
            await self.send_message(LOG_CHANNEL, text)
        except Exception as e:
            logger.warning(f"Failed to send startup log: {e}")

        await self.set_bot_commands_list()

    async def stop(self, *args):
        try:
            await self.send_message(LOG_CHANNEL, "<b><i>❌ Audio Editor Bot going Offline</i></b>")
        except Exception:
            pass
        for t in self._bg_tasks:
            t.cancel()
        try:
            await db.flush()
        except Exception:
            pass
        await asyncio.shield(super().stop())
        logger.info("Bot stopped cleanly")

    async def set_bot_commands_list(self):
        commands = [
            BotCommand("start", "Start the bot / open panel"),
            BotCommand("status", "Show your current config"),
            BotCommand("stop", "Clear queue & stop processing"),
            BotCommand("myplan", "Check your plan"),
            BotCommand("help", "How to use"),
            BotCommand("login", "Login (private channel batch)"),
            BotCommand("logout", "Logout"),
            BotCommand("cancel", "Cancel current action"),
            BotCommand("premium", "Premium info"),
        ]
        await self.set_bot_commands(commands)


BotInstance = Bot()


@BotInstance.on_message(filters.private & filters.incoming, group=-1)
async def new_user_log(bot: Client, message: Message):
    user = message.from_user
    if not user or user.id in USER_CACHE:
        return
    _, created = await db.ensure_user(user.id, user.first_name)
    if created:
        now = datetime.datetime.now(IST)
        log_text = (
            "<b>#NewUser 👤</b>\n"
            f"<b>User:</b> {user.mention}\n"
            f"<b>ID:</b> <code>{user.id}</code>\n"
            f"<b>Time:</b> {now.strftime('%I:%M %p')} IST"
        )
        try:
            await bot.send_message(LOG_CHANNEL, log_text)
        except Exception:
            pass
    USER_CACHE.add(user.id)


@BotInstance.on_message(filters.command("cmd") & filters.user(OWNER_ID))
async def update_commands(bot: Client, message: Message):
    try:
        await bot.set_bot_commands_list()
        await message.reply_text("✅ Commands menu updated!")
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")


if __name__ == "__main__":
    BotInstance.run()
