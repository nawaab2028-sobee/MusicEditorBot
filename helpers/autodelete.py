"""
Auto-delete scheduler. Entries are also saved in MongoDB, so deletion continues even after a bot restart.
"""

import asyncio
import time

from pyrogram.errors import FloodWait

from database.db import db
from logger import LOGGER

logger = LOGGER(__name__)


async def schedule_delete(client, chat_id, ids, delay):
    """delay = seconds. 0 = delete immediately."""
    ids = [int(i) for i in ids if i]
    if not ids:
        return
    if delay <= 0:
        try:
            await client.delete_messages(chat_id, ids)
        except Exception as e:
            logger.debug(f"instant delete failed: {e}")
        return
    await db.add_delete(chat_id, ids, time.time() + delay)


async def autodelete_loop(client):
    """Deletes due messages every 10 seconds."""
    await db.load_pending_deletes()
    while True:
        try:
            for entry in db.due_deletes():
                try:
                    await client.delete_messages(entry["chat_id"], entry["ids"])
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                    continue  # retry in the next loop
                except Exception as e:
                    logger.debug(f"auto delete failed {entry['chat_id']}: {e}")
                await db.finish_delete(entry["_id"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"autodelete loop error: {e}")
        await asyncio.sleep(10)
