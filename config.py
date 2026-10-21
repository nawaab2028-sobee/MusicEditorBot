"""
Audio Editor Bot Configuration

Every value is first taken from ENV; if ENV is not set, the hardcoded value below is used
(same system as in the config.py of the Save Restricted bot).
"""

import os


def _get(name: str, default: str) -> str:
    """ENV value (default if empty)."""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _bool(name: str, default: str) -> bool:
    return _get(name, default).lower() in ("1", "true", "yes", "on")


# ==============================
# Telegram Bot Credentials
# ==============================

BOT_TOKEN = _get("BOT_TOKEN", "8371349393:AAGUP1TTH4B4RbDLI7Cjv4bVXAUBl_7mTMg")
API_ID = int(_get("API_ID", "38498066"))
API_HASH = _get("API_HASH", "c9696114751feacdeb1b4487f5839a1a")

# ==============================
# Owner
# ==============================

# Only this user can run owner commands (premium, broadcast, ban...)
OWNER_ID = int(_get("OWNER_ID", "8909902924"))

# ==============================
# Database (MongoDB)
# ==============================

DB_URI = _get(
    "DB_URI",
    "mongodb+srv://mdinabano189_db_user:OExxVWuoNAHuBlFr@cluster0.yzkibwa.mongodb.net/?appName=Cluster0",
)
# NOTE: Separate DB name so that it does not mix with the limits/usage of the Save-Restricted bot.
# To share Premium between both bots, put "SaveRestricted2" in the DB_NAME env.
DB_NAME = _get("DB_NAME", "AudioEditorBot")

# Local JSON mirror (settings are saved here as well as in MongoDB)
DATA_DIR = _get("DATA_DIR", "data")
USERS_JSON = os.path.join(DATA_DIR, "users.json")

# ==============================
# Logging
# ==============================

LOG_CHANNEL = int(_get("LOG_CHANNEL", "-1003781244625"))

# ==============================
# Web server (Flask keep-alive) - Docker/Render port
# ==============================

PORT = int(_get("PORT", "8000"))

# ==============================
# Limits
# ==============================

# How many audios a free user can edit per day (24h)
FREE_DAILY_LIMIT = int(_get("FREE_DAILY_LIMIT", "3"))
# Max audios in a merge
MERGE_MAX_FILES = int(_get("MERGE_MAX_FILES", "50"))
# Max post IDs in one batch (safety cap)
BATCH_MAX_MESSAGES = int(_get("BATCH_MAX_MESSAGES", "1000"))
# Max ffmpeg jobs of users running at the same time (to save Render RAM)
MAX_CONCURRENT_JOBS = int(_get("MAX_CONCURRENT_JOBS", "2"))

# ==============================
# Premium / Support
# ==============================

SUPPORT_USERNAME = _get("SUPPORT_USERNAME", "SmartBoy_ApnaMS")
SUBSCRIPTION = _get("SUBSCRIPTION", "https://graph.org/file/242b7f1b52743938d81f1.jpg")
UPI_ID = _get("UPI_ID", "")
QR_CODE = _get("QR_CODE", "")

# ==============================
# Start image (empty = random waifu API, fallback if it fails)
# ==============================

START_IMAGE = _get("START_IMAGE", "")
START_IMAGE_FALLBACK = "https://graph.org/file/96f7e50b37c6bd4dc5071-5eadeaf54110b8c34a.jpg"

# ==============================
# Bot API (direct HTTP calls for colored buttons)
# ==============================

BOT_API_BASE = _get("BOT_API_BASE", "https://api.telegram.org")

# ==============================
# Paths
# ==============================

DOWNLOAD_DIR = _get("DOWNLOAD_DIR", "downloads")
COVER_DIR = os.path.join(DATA_DIR, "covers")
VOICE_DIR = os.path.join(DATA_DIR, "voice")

# Whether to show error messages to users or not
ERROR_MESSAGE = _bool("ERROR_MESSAGE", "True")
