"""Chhote common helpers."""

import html
import re
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".oga", ".opus", ".wma", ".amr", ".mka", ".ac3", ".aiff")


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def humanbytes(size) -> str:
    if not size:
        return "0 B"
    size = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    n = 0
    while size >= 1024 and n < len(units) - 1:
        size /= 1024
        n += 1
    return f"{round(size, 2)} {units[n]}"


def fmt_duration(seconds) -> str:
    """185 -> 3:05 , 3700 -> 1:01:40"""
    try:
        seconds = int(round(float(seconds)))
    except Exception:
        return "0:00"
    h, rem = divmod(max(0, seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_trim(seconds) -> str:
    """Trim time ko dikhane ke liye: 90 -> 1:30 , 90.5 -> 1:30.50"""
    sec = float(seconds)
    shown = fmt_duration(int(sec))
    frac = sec - int(sec)
    if frac > 0.004:
        shown += f"{frac:.2f}"[1:]
    return shown


def fmt_delay(seconds: int) -> str:
    """60 -> 1 min, 3600 -> 1 hour, 90 -> 90 sec"""
    seconds = int(seconds)
    if seconds == 0:
        return "instantly"
    if seconds % 86400 == 0:
        d = seconds // 86400
        return f"{d} day" + ("s" if d > 1 else "")
    if seconds % 3600 == 0:
        h = seconds // 3600
        return f"{h} hour" + ("s" if h > 1 else "")
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} sec"


def parse_time(text):
    """
    xVettBot wala format: SS | MM:SS | HH:MM:SS  (seconds me decimal bhi chalega).
    Valid -> float seconds, galat -> None.
    """
    if text is None:
        return None
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3:
        return None
    try:
        nums = []
        for i, p in enumerate(parts):
            p = p.strip()
            if not p:
                return None
            if i == len(parts) - 1:
                if not re.fullmatch(r"\d+(\.\d+)?", p):
                    return None
                nums.append(float(p))
            else:
                if not p.isdigit():
                    return None
                nums.append(int(p))
    except ValueError:
        return None
    # Higher unit ke saath aane par minutes/seconds < 60 hone chahiye
    if len(nums) >= 2 and nums[-1] >= 60:
        return None
    if len(nums) == 3 and nums[1] >= 60:
        return None
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return float(total)


def sanitize_filename(name: str, fallback: str = "audio") -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:100].strip(" .")) or fallback


_TITLE_NUM_RE = re.compile(r"^(.*?)(\d+)\s*$", re.S)


def parse_title_counter(text: str):
    """'Song 1' -> ('Song ', 1, 1) | 'Part 007' -> ('Part ', 7, 3) | number nahi -> None"""
    m = _TITLE_NUM_RE.match(text.strip())
    if not m:
        return None
    prefix, digits = m.group(1), m.group(2)
    if not prefix.strip():
        return None  # sirf number = title nahi maana jaata
    return prefix, int(digits), len(digits)


def make_counter_title(prefix: str, n: int, width: int) -> str:
    return f"{prefix}{str(n).zfill(width)}"


def current_title(doc):
    """User doc se abhi ka title (ya None = original)."""
    base = doc.get("title")
    if not base:
        return None
    if doc.get("title_inc") and doc.get("title_n") is not None:
        return make_counter_title(doc.get("title_prefix", ""), int(doc["title_n"]), int(doc.get("title_w", 1)))
    return base


def parse_chat_ref(text: str):
    """
    Channel input -> '@username' ya int chat_id. Galat ho to None.
      @name | name | https://t.me/name | -1001234567890 | 1234567890 (=> -100 add)
    """
    t = (text or "").strip()
    if not t:
        return None
    m = re.fullmatch(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,})/?", t)
    if m:
        return "@" + m.group(1)
    if re.fullmatch(r"-?\d{5,}", t):
        n = int(t)
        if n > 0:
            return int("-100" + t)
        return n
    m = re.fullmatch(r"@?([A-Za-z][A-Za-z0-9_]{3,})", t)
    if m:
        return "@" + m.group(1)
    return None
