"""
Database layer.

* MongoDB (motor) = main storage  (same system as the Save Restricted bot, collection: users)
* Local JSON mirror (data/users.json) = backup; if Mongo is down, settings are loaded from here
* In-memory cache = no DB roundtrip on every button press
"""

import asyncio
import datetime
import json
import os
import time
import uuid
from datetime import date, timedelta, timezone, time as dtime

import motor.motor_asyncio

from config import DB_NAME, DB_URI, OWNER_ID, USERS_JSON, FREE_DAILY_LIMIT
from logger import LOGGER

logger = LOGGER(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Default values of settings (used when the key is not present in the user doc)
DEFAULTS = {
    "caption_on": True,
    "cover_mode": "original",  # original | custom | none
}

# These keys are not written to the JSON mirror (sensitive)
_JSON_SKIP = {"session", "_id"}


# ======================================================
# Time helpers (same logic as premium.py)
# ======================================================

def to_dt(value):
    """str / date / datetime -> naive datetime (server clock). None for an invalid value."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, datetime.datetime):
            return value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone().replace(tzinfo=None)
        if isinstance(value, date):
            return datetime.datetime.combine(value, dtime(23, 59))
        if isinstance(value, str):
            value = value.strip()
            if len(value) == 10:
                return datetime.datetime.combine(date.fromisoformat(value), dtime(23, 59))
            return to_dt(datetime.datetime.fromisoformat(value))
    except Exception:
        return None
    return None


def to_ist(value):
    """Stored expiry/start -> aware datetime (IST). date / 'YYYY-MM-DD' = valid until the end of that day."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, str):
            value = value.strip()
            if len(value) == 10:
                value = date.fromisoformat(value)
            else:
                value = datetime.datetime.fromisoformat(value)
        if isinstance(value, date) and not isinstance(value, datetime.datetime):
            return datetime.datetime.combine(value, dtime(23, 59)).replace(tzinfo=IST)
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
        return value.astimezone(IST)
    except Exception:
        return None


def _json_default(obj):
    if isinstance(obj, (datetime.datetime, date)):
        return obj.isoformat()
    return str(obj)


class Database:
    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
        self.db = self._client[database_name]
        self.col = self.db.users
        self.dcol = self.db.scheduled_deletes

        self.cache = {}          # uid -> user doc
        self._json = {}          # uid(str) -> doc (from file)
        self._mem_deletes = {}   # _id -> {"_id","chat_id","ids","due"}
        self._flush_task = None
        self.mongo_ok = True
        self._load_json()

    # --------------------------------------------------
    # JSON mirror
    # --------------------------------------------------
    def _load_json(self):
        try:
            if os.path.exists(USERS_JSON):
                with open(USERS_JSON, "r", encoding="utf-8") as f:
                    self._json = json.load(f) or {}
                logger.info(f"JSON mirror loaded: {len(self._json)} users")
        except Exception as e:
            logger.warning(f"JSON mirror load failed: {e}")
            self._json = {}

    def _write_json_sync(self):
        try:
            os.makedirs(os.path.dirname(USERS_JSON) or ".", exist_ok=True)
            data = {}
            for uid, doc in self.cache.items():
                data[str(uid)] = {k: v for k, v in doc.items() if k not in _JSON_SKIP}
            tmp = USERS_JSON + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, default=_json_default, ensure_ascii=False)
            os.replace(tmp, USERS_JSON)
        except Exception as e:
            logger.warning(f"JSON mirror write failed: {e}")

    def _mark_dirty(self):
        """Debounced JSON write (2s)."""
        if self._flush_task and not self._flush_task.done():
            return
        try:
            self._flush_task = asyncio.get_running_loop().create_task(self._flush_later())
        except RuntimeError:
            self._write_json_sync()

    async def _flush_later(self):
        await asyncio.sleep(2)
        await asyncio.to_thread(self._write_json_sync)

    async def flush(self):
        await asyncio.to_thread(self._write_json_sync)

    # --------------------------------------------------
    # Mongo safe wrapper
    # --------------------------------------------------
    async def _safe(self, coro, default=None):
        try:
            res = await coro
            self.mongo_ok = True
            return res
        except Exception as e:
            self.mongo_ok = False
            logger.error(f"MongoDB error: {e}")
            return default

    async def ping(self):
        try:
            await self._client.admin.command("ping")
            self.mongo_ok = True
            return True
        except Exception as e:
            self.mongo_ok = False
            logger.error(f"MongoDB ping failed: {e}")
            return False

    # --------------------------------------------------
    # Users
    # --------------------------------------------------
    @staticmethod
    def _new_user(uid, name):
        return dict(
            id=int(uid),
            name=name,
            session=None,
            daily_usage=0,
            limit_reset_time=None,
            total_saves=0,
        )

    async def ensure_user(self, uid, name=""):
        """Load/create the user. Returns (doc, created)."""
        uid = int(uid)
        if uid in self.cache:
            return self.cache[uid], False
        doc = await self._safe(self.col.find_one({"id": uid}))
        if doc:
            doc.pop("_id", None)
            self.cache[uid] = doc
            return doc, False
        # Not found in Mongo -> check the JSON mirror (useful when Mongo is down)
        if not self.mongo_ok and str(uid) in self._json:
            doc = dict(self._json[str(uid)])
            doc.setdefault("session", None)
            self.cache[uid] = doc
            return doc, False
        doc = self._new_user(uid, name)
        await self._safe(self.col.insert_one(dict(doc)))
        self.cache[uid] = doc
        self._mark_dirty()
        logger.info(f"New user added to DB: {uid} - {name}")
        return doc, True

    async def get_user(self, uid, name=""):
        doc, _ = await self.ensure_user(uid, name)
        return doc

    async def is_user_exist(self, uid):
        uid = int(uid)
        if uid in self.cache:
            return True
        doc = await self._safe(self.col.find_one({"id": uid}))
        return bool(doc)

    async def update_user(self, uid, **fields):
        doc = await self.get_user(uid)
        doc.update(fields)
        await self._safe(self.col.update_one({"id": int(uid)}, {"$set": fields}, upsert=True))
        self._mark_dirty()

    async def unset_user(self, uid, *keys):
        doc = await self.get_user(uid)
        for k in keys:
            doc.pop(k, None)
        await self._safe(self.col.update_one({"id": int(uid)}, {"$unset": {k: "" for k in keys}}))
        self._mark_dirty()

    async def total_users_count(self):
        n = await self._safe(self.col.count_documents({}), None)
        return n if n is not None else len(self.cache)

    async def get_all_user_ids(self):
        ids = []
        try:
            cursor = self.col.find({}, {"id": 1})
            async for d in cursor:
                if "id" in d:
                    ids.append(int(d["id"]))
        except Exception as e:
            logger.error(f"get_all_user_ids failed: {e}")
            ids = list(self.cache.keys())
        return ids

    async def delete_user(self, uid):
        self.cache.pop(int(uid), None)
        await self._safe(self.col.delete_many({"id": int(uid)}))
        self._mark_dirty()

    # --------------------------------------------------
    # Session (login)
    # --------------------------------------------------
    async def set_session(self, uid, session):
        await self.update_user(uid, session=session)

    async def get_session(self, uid):
        doc = await self.get_user(uid)
        return doc.get("session")

    # --------------------------------------------------
    # Ban
    # --------------------------------------------------
    async def ban_user(self, uid):
        await self.update_user(uid, is_banned=True)
        logger.warning(f"User banned: {uid}")

    async def unban_user(self, uid):
        await self.update_user(uid, is_banned=False)
        logger.info(f"User unbanned: {uid}")

    async def is_banned(self, uid):
        doc = await self.get_user(uid)
        return bool(doc.get("is_banned", False))

    # --------------------------------------------------
    # Premium / Auth  (Save Restricted bot system)
    # --------------------------------------------------
    async def add_premium(self, uid, expiry_date):
        """expiry_date: 'YYYY-MM-DD' string or None (= permanent)."""
        await self.update_user(
            uid,
            is_premium=True,
            premium_expiry=expiry_date,
            premium_start=datetime.datetime.now(),
            daily_usage=0,
            limit_reset_time=None,
        )
        logger.info(f"User {uid} granted premium until {expiry_date}")

    async def remove_premium(self, uid):
        await self.update_user(uid, is_premium=False, premium_expiry=None)
        logger.info(f"User {uid} removed from premium")

    async def add_auth(self, uid, expiry_datetime):
        await self.update_user(
            uid,
            is_auth=True,
            auth_expiry=expiry_datetime,
            auth_start=datetime.datetime.now(),
            daily_usage=0,
            limit_reset_time=None,
        )
        logger.info(f"User {uid} granted auth until {expiry_datetime}")

    async def remove_auth(self, uid):
        await self.update_user(uid, is_auth=False, auth_expiry=None)
        logger.info(f"User {uid} auth access removed")

    @staticmethod
    def premium_active(doc):
        """Is paid premium currently valid? (with expiry check)"""
        if not doc.get("is_premium"):
            return False
        raw = doc.get("premium_expiry")
        if raw in (None, ""):
            return True  # permanent
        end = to_ist(raw)
        return bool(end and end > datetime.datetime.now(IST))

    @staticmethod
    def auth_active(doc):
        if not doc.get("is_auth"):
            return False
        raw = doc.get("auth_expiry")
        if raw in (None, ""):
            return True
        end = to_ist(raw)
        return bool(end and end > datetime.datetime.now(IST))

    async def has_unlimited_access(self, uid):
        """Owner / active premium / active auth = no limit."""
        if int(uid) == OWNER_ID:
            return True
        doc = await self.get_user(uid)
        return self.premium_active(doc) or self.auth_active(doc)

    # --------------------------------------------------
    # Daily limit (free user = FREE_DAILY_LIMIT audio / 24h)
    # --------------------------------------------------
    async def get_usage(self, uid):
        """Returns (used, reset_dt). Resets usage to 0 if the window has ended."""
        doc = await self.get_user(uid)
        reset = to_dt(doc.get("limit_reset_time"))
        if reset is not None and datetime.datetime.now() >= reset:
            await self.update_user(uid, daily_usage=0, limit_reset_time=None)
            return 0, None
        return int(doc.get("daily_usage", 0) or 0), reset

    async def remaining_allowance(self, uid):
        """None = unlimited, otherwise how many more audios are allowed today."""
        if await self.has_unlimited_access(uid):
            return None
        used, _ = await self.get_usage(uid)
        return max(0, FREE_DAILY_LIMIT - used)

    async def add_traffic(self, uid, n=1):
        """n audios processed -> increase usage. The 24h timer starts on the first usage."""
        doc = await self.get_user(uid)
        total = int(doc.get("total_saves", 0) or 0) + n
        if await self.has_unlimited_access(uid):
            await self.update_user(uid, total_saves=total)
            return
        used, reset = await self.get_usage(uid)
        if reset is None:
            reset = datetime.datetime.now() + timedelta(hours=24)
        await self.update_user(uid, daily_usage=used + n, limit_reset_time=reset, total_saves=total)

    # --------------------------------------------------
    # Title counter (Set Title with auto-number)
    # --------------------------------------------------
    async def advance_title(self, uid):
        doc = await self.get_user(uid)
        if doc.get("title_inc") and doc.get("title_n") is not None:
            await self.update_user(uid, title_n=int(doc["title_n"]) + 1)

    # --------------------------------------------------
    # Scheduled deletes (auto delete) - continue to work even after a restart
    # --------------------------------------------------
    async def add_delete(self, chat_id, ids, due_ts):
        entry = {"_id": uuid.uuid4().hex, "chat_id": int(chat_id), "ids": [int(i) for i in ids], "due": float(due_ts)}
        self._mem_deletes[entry["_id"]] = entry
        await self._safe(self.dcol.insert_one(dict(entry)))

    async def load_pending_deletes(self):
        try:
            async for d in self.dcol.find({}):
                self._mem_deletes[d["_id"]] = d
            logger.info(f"Pending auto-deletes loaded: {len(self._mem_deletes)}")
        except Exception as e:
            logger.warning(f"load_pending_deletes failed: {e}")

    def due_deletes(self, now=None):
        now = now or time.time()
        return [e for e in list(self._mem_deletes.values()) if e["due"] <= now]

    async def finish_delete(self, entry_id):
        self._mem_deletes.pop(entry_id, None)
        await self._safe(self.dcol.delete_one({"_id": entry_id}))


db = Database(DB_URI, DB_NAME)
