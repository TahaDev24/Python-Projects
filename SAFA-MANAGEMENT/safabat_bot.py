#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧠 صفابات — عضو دیجیتال سرگرم‌کننده، رقابتی و کاربردی گروه‌های بله
تک‌فایلی | Python 3.10+ | SQLite | فقط کتابخانه‌ی requests (بدون هوش مصنوعی)

راه‌اندازی:
  1) pip install requests
  2) متغیر محیطی BALE_BOT_TOKEN را با توکن ربات تنظیم کن
  3) در صورت نیاز، BALE_ADMIN_ID را با آیدی عددی ادمین تنظیم کن
  4) python safabat_bot.py   (دیتابیس safabat.db خودش ساخته می‌شود)
  5) ربات را به گروه اضافه کن؛ برای مدیریت (حذف پیام/بن) ادمینش کن
  6) /selftest ← اجرای تست‌های داخلی روی دیتابیس موقت

بخش‌ها: منو و تنظیمات • موتور پاسخ (۱۰۰ Trigger) • جوک و جوک‌متر • فال • ۱۳ بازی •
XP/Level/مدال/مأموریت/لیگ • حافظه و DNA و آمار • مدیریت گروه • آزمایشگاه و رویدادها
"""

# ╔══════════════════════════════════════════════════════════════╗
# ║  تنظیمات محرمانه از متغیرهای محیطی خوانده می‌شوند             ║
# ╚══════════════════════════════════════════════════════════════╝
BOT_TOKEN = ""
ADMIN_ID = 0

import os
import re
import sys
import json
import time
import random
import hashlib
import signal
import sqlite3
import logging
import threading
import datetime
import types
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

import requests

# Credentials are deliberately not stored in this public source file.
BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN", "").strip()
try:
    ADMIN_ID = int(os.environ.get("BALE_ADMIN_ID", "0") or 0)
except ValueError:
    ADMIN_ID = 0

# ══════════════════════════════════════════════════════════════════
# ███ PHASE 1 — ARCHITECTURE / BALE API / SQLITE / CORE
# ══════════════════════════════════════════════════════════════════

BOT_NAME = "صفابات"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "safabat.db")
LOG_FILE = os.path.join(BASE_DIR, "safabat.log")
LOCK_FILE = os.path.join(BASE_DIR, "safabat.lock")

API_BASE = "https://tapi.bale.ai"
POLL_TIMEOUT = 10            # ثانیه (Long Polling)
POLL_LIMIT = 50
STALE_MESSAGE_SECONDS = 600  # پیام‌های قدیمی‌تر از این بعد از خاموشی نادیده گرفته می‌شوند
IRAN_UTC_OFFSET = 12600      # +03:30
CALLBACK_DATA_MAX = 64
MAX_TEXT = 4000

# محدودیت‌های امنیتی
CB_RATE_LIMIT = (14, 10)     # حداکثر ۱۴ کلیک در ۱۰ ثانیه برای هر کاربر
CMD_RATE_LIMIT = (7, 10)     # حداکثر ۷ دستور در ۱۰ ثانیه
DOUBLE_CLICK_WINDOW = 1.0    # ثانیه
CB_SEEN_TTL = 600
ADMIN_CACHE_TTL = 60
ADMIN_CACHE_FAIL_TTL = 15
ERROR_REPORT_INTERVAL = 600
LOG_RETENTION_DAYS = 30
LEDGER_RETENTION_DAYS = 45
SESSION_GRACE_SECONDS = 120
WARNING_RETENTION_DAYS = 90
REPORT_RETENTION_DAYS = 90
GAME_RECORD_RETENTION_DAYS = 180
LEAGUE_RETENTION_WEEKS = 26
MAX_LOGS_PER_CHAT = 2000
STATS_DAY_KEY_RETENTION_DAYS = 60

# فیلدهای مجاز (whitelist) — نام ستون هرگز از ورودی کاربر نمی‌آید
SETTING_TYPES = {
    "auto_reply": "bool", "games": "bool", "fortune": "bool", "jokes": "bool",
    "missions": "bool", "lab": "bool", "anti_spam": "bool", "anti_link": "bool",
    "anti_flood": "bool", "filter_enabled": "bool", "auto_mod": "bool",
    "welcome_on": "bool", "goodbye_on": "bool",
    "mod_level": "int", "warn_limit": "int", "reply_chance": "int",
    "welcome_text": "text", "goodbye_text": "text", "rules_text": "text",
}
SETTING_RANGES = {"mod_level": (1, 3), "warn_limit": (1, 10), "reply_chance": (0, 100)}

USER_STAT_FIELDS = {
    "xp", "level", "games_played", "games_won", "games_lost", "draws",
    "streak", "best_streak", "joke_votes", "jokes_sent", "missions_done",
    "msgs_count", "duels_sent", "duels_won", "weekly_points",
}

# ──────────────────────────────────────────────────────────────────
# State مشترک برنامه (تا در تست‌ها بشود جایگزین کرد)
# ──────────────────────────────────────────────────────────────────
APP = types.SimpleNamespace(
    db=None, api=None, bot_id=0, bot_username="", running=True,
    lock_file=None, started_at=0,
)

LOG = logging.getLogger("safabat")


# ──────────────────────────────────────────────────────────────────
# لاگ و پنهان‌سازی توکن
# ──────────────────────────────────────────────────────────────────
def redact(text):
    s = str(text)
    if BOT_TOKEN and len(BOT_TOKEN) > 8:
        s = s.replace(BOT_TOKEN, "<token>")
    return re.sub(r"/bot[^/\s'\"]+/", "/bot<token>/", s)


class RedactFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def setup_logging():
    LOG.setLevel(logging.INFO)
    if LOG.handlers:
        return
    fmt = RedactFormatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    LOG.addHandler(fh)
    LOG.addHandler(sh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ──────────────────────────────────────────────────────────────────
# زمان (ایران، بدون DST)
# ──────────────────────────────────────────────────────────────────
def now():
    return int(time.time())


def iran_dt(ts=None):
    ts = now() if ts is None else ts
    return datetime.datetime.fromtimestamp(ts + IRAN_UTC_OFFSET, tz=datetime.timezone.utc)


def day_key(ts=None):
    return iran_dt(ts).strftime("%Y-%m-%d")


def hour_of(ts=None):
    return iran_dt(ts).hour


def week_key(ts=None):
    """هفته ایرانی از شنبه شروع می‌شود؛ کلید = تاریخ میلادی همان شنبه."""
    d = iran_dt(ts).date()
    back = (d.weekday() - 5) % 7  # Monday=0 ... Saturday=5
    return (d - datetime.timedelta(days=back)).strftime("%Y-%m-%d")


# ──────────────────────────────────────────────────────────────────
# ابزارهای متن فارسی
# ──────────────────────────────────────────────────────────────────
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_CHAR_MAP = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "أ": "ا", "إ": "ا", "ؤ": "و"})
_STRIP_RE = re.compile("[\u064b-\u065f\u0670\u0640\u200c\u200d\u200e\u200f]")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def norm_text(text):
    """نرمال‌سازی برای مقایسه: یکسان‌سازی ی/ک، حذف اعراب/نیم‌فاصله/علائم، ارقام لاتین."""
    s = str(text or "").translate(_DIGITS).translate(_CHAR_MAP)
    s = _STRIP_RE.sub("", s).lower()
    s = _PUNCT_RE.sub(" ", s)
    return _SPACE_RE.sub(" ", s).strip()


def text_hash(text):
    return hashlib.sha1(norm_text(text).encode("utf-8")).hexdigest()


def safe_text(text, limit=MAX_TEXT):
    t = str(text)
    return t if len(t) <= limit else t[: limit - 1] + "…"


def display_name(u):
    u = u or {}
    name = ((u.get("first_name") or "") + " " + (u.get("last_name") or "")).strip()
    return name or (u.get("username") or "") or "کاربر"


def mention(u):
    u = u or {}
    return "@" + u["username"] if u.get("username") else display_name(u)


def chance(percent):
    return random.random() * 100 < percent


def pick(seq):
    return random.choice(list(seq))


# ──────────────────────────────────────────────────────────────────
# Cooldown / Rate limit / TTL (فقط در RAM، سبک)
# ──────────────────────────────────────────────────────────────────
class TTLStore:
    def __init__(self, max_items=20000):
        self._d = {}
        self.max_items = max_items

    def add(self, key, ttl):
        """True اگر کلید جدید بود (و ثبت شد)، False اگر هنوز فعال است."""
        t = time.monotonic()
        exp = self._d.get(key)
        if exp is not None and exp > t:
            return False
        if len(self._d) >= self.max_items:
            self.purge()
        self._d[key] = t + ttl
        return True

    def left(self, key):
        exp = self._d.get(key)
        if exp is None:
            return 0.0
        return max(0.0, exp - time.monotonic())

    def discard(self, key):
        self._d.pop(key, None)

    def purge(self):
        t = time.monotonic()
        for k in [k for k, e in self._d.items() if e <= t]:
            del self._d[k]

    def clear(self):
        self._d.clear()

    def __len__(self):
        return len(self._d)


class RateLimiter:
    def __init__(self, max_keys=20000):
        self._h = {}
        self.max_keys = max_keys

    def allow(self, key, limit, window):
        t = time.monotonic()
        q = self._h.get(key)
        if q is None:
            if len(self._h) >= self.max_keys:
                self.purge()
            q = self._h[key] = []
        cutoff = t - window
        while q and q[0] <= cutoff:
            q.pop(0)
        if len(q) >= limit:
            return False
        q.append(t)
        return True

    def purge(self, max_age=120):
        cutoff = time.monotonic() - max_age
        for k in [k for k, q in self._h.items() if not q or q[-1] <= cutoff]:
            del self._h[k]

    def clear(self):
        self._h.clear()


COOLDOWNS = TTLStore()
CB_SEEN = TTLStore()
CLICK_LOCK = TTLStore()
RATE = RateLimiter()
GROUP_SEEN = TTLStore(5000)
USER_SEEN = TTLStore(20000)
ADMIN_CACHE = {}       # (chat_id, user_id) -> (bool, expires_monotonic)
SETTINGS_CACHE = {}    # chat_id -> dict


def cooldown_ok(key, seconds):
    """اگر Cooldown تمام شده باشد True می‌دهد و Cooldown جدید شروع می‌شود."""
    return COOLDOWNS.add(key, seconds)


def cooldown_left(key):
    return COOLDOWNS.left(key)


def purge_memory_stores():
    for s in (COOLDOWNS, CB_SEEN, CLICK_LOCK, GROUP_SEEN, USER_SEEN):
        s.purge()
    RATE.purge()
    t = time.monotonic()
    for k in [k for k, (_, e) in ADMIN_CACHE.items() if e <= t]:
        del ADMIN_CACHE[k]


# ──────────────────────────────────────────────────────────────────
# SQLite
# ──────────────────────────────────────────────────────────────────
class Database:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self._depth = 0
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=15)
        self.conn.row_factory = sqlite3.Row
        for p in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL", "PRAGMA busy_timeout=10000",
                  "PRAGMA cache_size=-2000", "PRAGMA temp_store=MEMORY"):
            self.conn.execute(p)

    def run(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params)

    def many(self, sql, seq):
        with self.lock:
            return self.conn.executemany(sql, seq)

    def all(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def val(self, sql, params=(), default=None):
        r = self.one(sql, params)
        return default if r is None or r[0] is None else r[0]

    @contextmanager
    def tx(self):
        with self.lock:
            if self._depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("COMMIT")

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except sqlite3.Error as e:
                LOG.debug("db close: %s", e)


SCHEMA_VERSION = 1

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)",

    """CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
        first_seen INTEGER DEFAULT 0, last_seen INTEGER DEFAULT 0)""",

    """CREATE TABLE IF NOT EXISTS groups(
        chat_id INTEGER PRIMARY KEY, title TEXT, chat_type TEXT, added_at INTEGER DEFAULT 0,
        last_active INTEGER DEFAULT 0, active INTEGER DEFAULT 1, member_count INTEGER DEFAULT 0)""",

    """CREATE TABLE IF NOT EXISTS group_settings(
        chat_id INTEGER PRIMARY KEY,
        auto_reply INTEGER DEFAULT 1, games INTEGER DEFAULT 1, fortune INTEGER DEFAULT 1,
        jokes INTEGER DEFAULT 1, missions INTEGER DEFAULT 1, lab INTEGER DEFAULT 1,
        anti_spam INTEGER DEFAULT 1, anti_link INTEGER DEFAULT 1, anti_flood INTEGER DEFAULT 1,
        filter_enabled INTEGER DEFAULT 1, auto_mod INTEGER DEFAULT 1,
        welcome_on INTEGER DEFAULT 1, goodbye_on INTEGER DEFAULT 0,
        mod_level INTEGER DEFAULT 2, warn_limit INTEGER DEFAULT 3, reply_chance INTEGER DEFAULT 35,
        welcome_text TEXT, goodbye_text TEXT, rules_text TEXT)""",

    """CREATE TABLE IF NOT EXISTS user_stats(
        chat_id INTEGER, user_id INTEGER,
        xp INTEGER DEFAULT 0, level INTEGER DEFAULT 1,
        games_played INTEGER DEFAULT 0, games_won INTEGER DEFAULT 0, games_lost INTEGER DEFAULT 0,
        draws INTEGER DEFAULT 0, streak INTEGER DEFAULT 0, best_streak INTEGER DEFAULT 0,
        joke_votes INTEGER DEFAULT 0, jokes_sent INTEGER DEFAULT 0, missions_done INTEGER DEFAULT 0,
        msgs_count INTEGER DEFAULT 0, duels_sent INTEGER DEFAULT 0, duels_won INTEGER DEFAULT 0,
        weekly_points INTEGER DEFAULT 0, first_active INTEGER DEFAULT 0, last_active INTEGER DEFAULT 0,
        PRIMARY KEY(chat_id, user_id))""",
    "CREATE INDEX IF NOT EXISTS ix_user_stats_xp ON user_stats(chat_id, xp DESC)",
    "CREATE INDEX IF NOT EXISTS ix_user_stats_active ON user_stats(chat_id, last_active)",

    """CREATE TABLE IF NOT EXISTS group_stats(
        chat_id INTEGER, skey TEXT, svalue INTEGER DEFAULT 0, PRIMARY KEY(chat_id, skey))""",

    """CREATE TABLE IF NOT EXISTS games(
        game_key TEXT PRIMARY KEY, title TEXT, emoji TEXT, enabled INTEGER DEFAULT 1, plays INTEGER DEFAULT 0)""",

    """CREATE TABLE IF NOT EXISTS game_records(
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER, game_key TEXT,
        result TEXT, score INTEGER DEFAULT 0, xp INTEGER DEFAULT 0, created_at INTEGER)""",
    "CREATE INDEX IF NOT EXISTS ix_gr_user ON game_records(chat_id, user_id)",
    "CREATE INDEX IF NOT EXISTS ix_gr_time ON game_records(chat_id, created_at)",

    """CREATE TABLE IF NOT EXISTS jokes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, text_hash TEXT UNIQUE, category TEXT,
        shown INTEGER DEFAULT 0, laughs INTEGER DEFAULT 0, mehs INTEGER DEFAULT 0, score REAL DEFAULT 50,
        last_shown INTEGER DEFAULT 0, status TEXT DEFAULT 'approved',
        submitted_by INTEGER DEFAULT 0, submit_chat INTEGER DEFAULT 0, created_at INTEGER DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS ix_jokes_cat ON jokes(status, category)",

    """CREATE TABLE IF NOT EXISTS joke_votes(
        joke_id INTEGER, user_id INTEGER, vote INTEGER, ts INTEGER, PRIMARY KEY(joke_id, user_id))""",

    """CREATE TABLE IF NOT EXISTS fortunes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, text TEXT NOT NULL, text_hash TEXT UNIQUE,
        shown INTEGER DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS ix_fortunes_cat ON fortunes(category)",

    """CREATE TABLE IF NOT EXISTS missions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, mkey TEXT UNIQUE, title TEXT, kind TEXT, metric TEXT,
        target INTEGER DEFAULT 1, reward_xp INTEGER DEFAULT 0, reward_medal TEXT,
        secret INTEGER DEFAULT 0, active INTEGER DEFAULT 1)""",

    """CREATE TABLE IF NOT EXISTS user_missions(
        chat_id INTEGER, user_id INTEGER, mission_id INTEGER, period TEXT,
        progress INTEGER DEFAULT 0, done INTEGER DEFAULT 0, claimed INTEGER DEFAULT 0,
        PRIMARY KEY(chat_id, user_id, mission_id, period))""",

    """CREATE TABLE IF NOT EXISTS achievements(
        akey TEXT PRIMARY KEY, title TEXT, emoji TEXT, description TEXT,
        secret INTEGER DEFAULT 0, reward_xp INTEGER DEFAULT 0)""",

    """CREATE TABLE IF NOT EXISTS user_achievements(
        chat_id INTEGER, user_id INTEGER, akey TEXT, earned_at INTEGER,
        PRIMARY KEY(chat_id, user_id, akey))""",

    """CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, ekey TEXT, title TEXT,
        multiplier REAL DEFAULT 1, starts_at INTEGER, ends_at INTEGER, active INTEGER DEFAULT 1,
        created_at INTEGER)""",
    "CREATE INDEX IF NOT EXISTS ix_events_chat ON events(chat_id, active)",

    """CREATE TABLE IF NOT EXISTS reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, reporter_id INTEGER, target_id INTEGER,
        target_message_id INTEGER, reason TEXT, status TEXT DEFAULT 'open', created_at INTEGER,
        handled_by INTEGER DEFAULT 0, handled_at INTEGER DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS ix_reports_chat ON reports(chat_id, status)",

    """CREATE TABLE IF NOT EXISTS warnings(
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER, reason TEXT,
        by_id INTEGER, created_at INTEGER)""",
    "CREATE INDEX IF NOT EXISTS ix_warn_user ON warnings(chat_id, user_id)",

    """CREATE TABLE IF NOT EXISTS group_memory(
        chat_id INTEGER, mkey TEXT, mvalue TEXT, updated_at INTEGER, PRIMARY KEY(chat_id, mkey))""",

    # ─── جداول اضافه که واقعاً لازم‌اند ───
    """CREATE TABLE IF NOT EXISTS restrictions(
        chat_id INTEGER, user_id INTEGER, kind TEXT, until INTEGER DEFAULT 0, reason TEXT,
        by_id INTEGER DEFAULT 0, created_at INTEGER, PRIMARY KEY(chat_id, user_id, kind))""",

    """CREATE TABLE IF NOT EXISTS filter_words(
        chat_id INTEGER, word TEXT, PRIMARY KEY(chat_id, word))""",

    """CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, kind TEXT, actor_id INTEGER,
        target_id INTEGER, detail TEXT, ts INTEGER)""",
    "CREATE INDEX IF NOT EXISTS ix_logs_chat ON logs(chat_id, ts)",

    """CREATE TABLE IF NOT EXISTS sessions(
        sid TEXT PRIMARY KEY, chat_id INTEGER, user_id INTEGER, kind TEXT, data TEXT,
        created_at INTEGER, expires_at INTEGER)""",
    "CREATE INDEX IF NOT EXISTS ix_sessions_exp ON sessions(expires_at)",
    "CREATE INDEX IF NOT EXISTS ix_sessions_find ON sessions(chat_id, kind)",

    "CREATE TABLE IF NOT EXISTS reward_ledger(token TEXT PRIMARY KEY, ts INTEGER)",

    """CREATE TABLE IF NOT EXISTS league_scores(
        chat_id INTEGER, week_key TEXT, user_id INTEGER, points INTEGER DEFAULT 0,
        PRIMARY KEY(chat_id, week_key, user_id))""",

    """CREATE TABLE IF NOT EXISTS records(
        chat_id INTEGER, rkey TEXT, user_id INTEGER, value INTEGER, ts INTEGER,
        PRIMARY KEY(chat_id, rkey))""",
]

REQUIRED_TABLES = [
    "users", "groups", "group_settings", "user_stats", "group_stats", "games", "game_records",
    "jokes", "joke_votes", "fortunes", "missions", "achievements", "user_achievements", "events",
    "reports", "warnings", "group_memory", "user_missions", "restrictions", "filter_words",
    "logs", "sessions", "reward_ledger", "league_scores", "records", "meta",
]


def init_schema(db):
    with db.tx():
        for stmt in SCHEMA:
            db.run(stmt)
        db.run("INSERT OR IGNORE INTO meta(k, v) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))


# ──────────────────────────────────────────────────────────────────
# توابع داده‌ای هسته
# ──────────────────────────────────────────────────────────────────
def meta_get(key, default=None):
    return APP.db.val("SELECT v FROM meta WHERE k=?", (key,), default)


def meta_set(key, value):
    APP.db.run("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (key, str(value)))


def upsert_user(u):
    if not u or not u.get("id"):
        return
    t = now()
    APP.db.run(
        """INSERT INTO users(user_id, username, first_name, last_name, first_seen, last_seen)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name,
           last_name=excluded.last_name, last_seen=excluded.last_seen""",
        (int(u["id"]), u.get("username"), u.get("first_name"), u.get("last_name"), t, t))


def get_user(user_id):
    r = APP.db.one("SELECT * FROM users WHERE user_id=?", (int(user_id),))
    if r is None:
        return {"id": int(user_id)}
    return {"id": r["user_id"], "username": r["username"], "first_name": r["first_name"],
            "last_name": r["last_name"]}


def upsert_group(chat):
    if not chat or chat.get("id") is None:
        return
    t = now()
    cid = int(chat["id"])
    with APP.db.tx():
        APP.db.run(
            """INSERT INTO groups(chat_id, title, chat_type, added_at, last_active, active)
               VALUES(?,?,?,?,?,1)
               ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, chat_type=excluded.chat_type,
               last_active=excluded.last_active, active=1""",
            (cid, chat.get("title"), chat.get("type"), t, t))
        APP.db.run("INSERT OR IGNORE INTO group_settings(chat_id) VALUES(?)", (cid,))


def mark_group_inactive(chat_id):
    APP.db.run("UPDATE groups SET active=0 WHERE chat_id=?", (int(chat_id),))
    SETTINGS_CACHE.pop(int(chat_id), None)


def get_settings(chat_id):
    cid = int(chat_id)
    cached = SETTINGS_CACHE.get(cid)
    if cached is not None:
        return cached
    APP.db.run("INSERT OR IGNORE INTO group_settings(chat_id) VALUES(?)", (cid,))
    row = APP.db.one("SELECT * FROM group_settings WHERE chat_id=?", (cid,))
    data = {k: row[k] for k in row.keys()}
    if len(SETTINGS_CACHE) > 500:
        SETTINGS_CACHE.clear()
    SETTINGS_CACHE[cid] = data
    return data


def set_setting(chat_id, key, value):
    """True اگر ذخیره شد. کلید و مقدار اعتبارسنجی می‌شوند."""
    typ = SETTING_TYPES.get(key)
    if typ is None:
        return False
    if typ == "bool":
        value = 1 if value in (1, True, "1", "on", "ON") else 0
    elif typ == "int":
        try:
            value = int(value)
        except (TypeError, ValueError):
            return False
        lo, hi = SETTING_RANGES.get(key, (-10**9, 10**9))
        if not lo <= value <= hi:
            return False
    else:
        value = None if value is None else safe_text(value, 1500)
    cid = int(chat_id)
    APP.db.run("INSERT OR IGNORE INTO group_settings(chat_id) VALUES(?)", (cid,))
    APP.db.run(f"UPDATE group_settings SET {key}=? WHERE chat_id=?", (value, cid))  # key از whitelist
    SETTINGS_CACHE.pop(cid, None)
    return True


def toggle_setting(chat_id, key):
    if SETTING_TYPES.get(key) != "bool":
        return None
    new = 0 if get_settings(chat_id).get(key) else 1
    set_setting(chat_id, key, new)
    return bool(new)


class StatBuffer:
    """شمارنده‌ها در RAM جمع می‌شوند و دسته‌ای روی SQLite نوشته می‌شوند."""

    def __init__(self):
        self.g = {}
        self.u = {}
        self.touch = {}

    def group_add(self, chat_id, skey, n=1):
        k = (int(chat_id), skey)
        self.g[k] = self.g.get(k, 0) + n

    def user_add(self, chat_id, user_id, field, n=1):
        if field not in USER_STAT_FIELDS:
            raise ValueError("unknown user stat field: %s" % field)
        k = (int(chat_id), int(user_id), field)
        self.u[k] = self.u.get(k, 0) + n

    def user_touch(self, chat_id, user_id, ts=None):
        self.touch[(int(chat_id), int(user_id))] = now() if ts is None else ts

    def pending(self):
        return len(self.g) + len(self.u) + len(self.touch)

    def flush(self):
        if not self.pending() or APP.db is None:
            return 0
        g, u, touch = self.g, self.u, self.touch
        self.g, self.u, self.touch = {}, {}, {}
        try:
            with APP.db.tx():
                APP.db.many(
                    """INSERT INTO group_stats(chat_id, skey, svalue) VALUES(?,?,?)
                       ON CONFLICT(chat_id, skey) DO UPDATE SET svalue=svalue+excluded.svalue""",
                    [(c, k, v) for (c, k), v in g.items()])
                for (c, uid, f), v in u.items():
                    APP.db.run(  # f از whitelist
                        f"""INSERT INTO user_stats(chat_id, user_id, {f}) VALUES(?,?,?)
                            ON CONFLICT(chat_id, user_id) DO UPDATE SET {f}={f}+excluded.{f}""",
                        (c, uid, v))
                APP.db.many(
                    """INSERT INTO user_stats(chat_id, user_id, first_active, last_active) VALUES(?,?,?,?)
                       ON CONFLICT(chat_id, user_id) DO UPDATE SET last_active=excluded.last_active,
                       first_active=CASE WHEN first_active=0 THEN excluded.first_active ELSE first_active END""",
                    [(c, uid, ts, ts) for (c, uid), ts in touch.items()])
        except sqlite3.Error:
            LOG.exception("stat flush failed")
            return 0
        return len(g) + len(u) + len(touch)


STATS = StatBuffer()


def user_stat_get(chat_id, user_id, field):
    if field not in USER_STAT_FIELDS:
        raise ValueError("unknown user stat field: %s" % field)
    return APP.db.val(f"SELECT {field} FROM user_stats WHERE chat_id=? AND user_id=?",
                      (int(chat_id), int(user_id)), 0)


def mem_get(chat_id, key, default=None):
    return APP.db.val("SELECT mvalue FROM group_memory WHERE chat_id=? AND mkey=?", (int(chat_id), key), default)


def mem_set(chat_id, key, value):
    APP.db.run(
        """INSERT INTO group_memory(chat_id, mkey, mvalue, updated_at) VALUES(?,?,?,?)
           ON CONFLICT(chat_id, mkey) DO UPDATE SET mvalue=excluded.mvalue, updated_at=excluded.updated_at""",
        (int(chat_id), key, str(value), now()))


def mem_del(chat_id, key):
    APP.db.run("DELETE FROM group_memory WHERE chat_id=? AND mkey=?", (int(chat_id), key))


def log_action(chat_id, kind, actor_id=0, target_id=0, detail=""):
    APP.db.run("INSERT INTO logs(chat_id, kind, actor_id, target_id, detail, ts) VALUES(?,?,?,?,?,?)",
               (int(chat_id), kind, int(actor_id or 0), int(target_id or 0), safe_text(detail, 500), now()))


# ─── Session ها (موقت؛ با Cleanup پاک می‌شوند) ───
def session_put(sid, chat_id, user_id, kind, data, ttl):
    t = now()
    APP.db.run(
        """INSERT INTO sessions(sid, chat_id, user_id, kind, data, created_at, expires_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(sid) DO UPDATE SET chat_id=excluded.chat_id, user_id=excluded.user_id,
           kind=excluded.kind, data=excluded.data, expires_at=excluded.expires_at""",
        (sid, int(chat_id), int(user_id), kind, json.dumps(data, ensure_ascii=False), t, t + int(ttl)))


def session_get(sid):
    r = APP.db.one("SELECT * FROM sessions WHERE sid=? AND expires_at>?", (sid, now()))
    if r is None:
        return None
    try:
        data = json.loads(r["data"])
    except (TypeError, ValueError):
        data = {}
    return {"sid": r["sid"], "chat_id": r["chat_id"], "user_id": r["user_id"], "kind": r["kind"],
            "data": data, "created_at": r["created_at"], "expires_at": r["expires_at"]}


def session_del(sid):
    return APP.db.run("DELETE FROM sessions WHERE sid=?", (sid,)).rowcount > 0


def session_find(chat_id, kind, user_id=None):
    if user_id is None:
        rows = APP.db.all("SELECT sid FROM sessions WHERE chat_id=? AND kind=? AND expires_at>?",
                          (int(chat_id), kind, now()))
    else:
        rows = APP.db.all("SELECT sid FROM sessions WHERE chat_id=? AND kind=? AND user_id=? AND expires_at>?",
                          (int(chat_id), kind, int(user_id), now()))
    return [s for s in (session_get(r["sid"]) for r in rows) if s]


# ─── ورودی متنی در انتظار (فقط جایی که واقعاً متن لازم است) ───
def input_set(chat_id, user_id, kind, data=None, ttl=300):
    session_put(f"in:{int(chat_id)}:{int(user_id)}", chat_id, user_id, "input:" + kind, data or {}, ttl)


def input_get(chat_id, user_id):
    return session_get(f"in:{int(chat_id)}:{int(user_id)}")


def input_clear(chat_id, user_id):
    return session_del(f"in:{int(chat_id)}:{int(user_id)}")


def reward_once(token):
    """True فقط بار اول برای هر token → جلوگیری از پاداش تکراری."""
    return APP.db.run("INSERT OR IGNORE INTO reward_ledger(token, ts) VALUES(?,?)", (token, now())).rowcount == 1


# ──────────────────────────────────────────────────────────────────
# Cleanup — موقت‌ها پاک می‌شوند؛ XP/مدال/رکورد/آمار می‌مانند
# ──────────────────────────────────────────────────────────────────
def run_cleanup():
    t = now()
    out = {}
    with APP.db.tx():
        out["sessions"] = APP.db.run("DELETE FROM sessions WHERE expires_at<=?", (t - SESSION_GRACE_SECONDS,)).rowcount
        out["events_ended"] = APP.db.run("UPDATE events SET active=0 WHERE active=1 AND ends_at<=?", (t,)).rowcount
        out["events_deleted"] = APP.db.run("DELETE FROM events WHERE ends_at<=?", (t - 7 * 86400,)).rowcount
        out["logs"] = APP.db.run("DELETE FROM logs WHERE ts<?", (t - LOG_RETENTION_DAYS * 86400,)).rowcount
        out["ledger"] = APP.db.run("DELETE FROM reward_ledger WHERE ts<?", (t - LEDGER_RETENTION_DAYS * 86400,)).rowcount
        cutoff = day_key(t - STATS_DAY_KEY_RETENTION_DAYS * 86400)
        out["day_stats"] = APP.db.run(
            "DELETE FROM group_stats WHERE skey LIKE 'day:%' AND substr(skey, 5, 10) < ?", (cutoff,)).rowcount
        out["expired_restrictions"] = APP.db.run(
            "DELETE FROM restrictions WHERE until>0 AND until<=?", (t - 86400,)).rowcount
        out["warnings"] = APP.db.run("DELETE FROM warnings WHERE created_at<?", (t - WARNING_RETENTION_DAYS * 86400,)).rowcount
        out["reports"] = APP.db.run("DELETE FROM reports WHERE status!='open' AND created_at<?", (t - REPORT_RETENTION_DAYS * 86400,)).rowcount
        out["game_records"] = APP.db.run("DELETE FROM game_records WHERE created_at<?", (t - GAME_RECORD_RETENTION_DAYS * 86400,)).rowcount
        out["league"] = APP.db.run("DELETE FROM league_scores WHERE week_key<?", (week_key(t - LEAGUE_RETENTION_WEEKS * 7 * 86400),)).rowcount
        out["logs_capped"] = 0
        for r in APP.db.all("SELECT chat_id FROM logs GROUP BY chat_id HAVING COUNT(*)>?", (MAX_LOGS_PER_CHAT,)):
            out["logs_capped"] += APP.db.run(
                "DELETE FROM logs WHERE chat_id=? AND id<=(SELECT id FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (r["chat_id"], r["chat_id"], MAX_LOGS_PER_CHAT)).rowcount
    purge_memory_stores()
    return out


# ──────────────────────────────────────────────────────────────────
# Bale API  (https://tapi.bale.ai/bot<TOKEN>/<method> — JSON POST)
# ──────────────────────────────────────────────────────────────────
class BaleAPIError(Exception):
    def __init__(self, method, code, desc, retry_after=0):
        super().__init__("%s failed [%s]: %s" % (method, code, desc))
        self.method, self.code, self.desc, self.retry_after = method, code, str(desc or ""), retry_after


class BaleAPI:
    def __init__(self, token):
        self.base = "%s/bot%s/" % (API_BASE, token)
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "safabat/1.0"})

    def call(self, method, payload=None, timeout=20, retries=3):
        url = self.base + method
        last = None
        for attempt in range(max(1, retries)):
            try:
                r = self.s.post(url, json=payload or {}, timeout=timeout)
            except requests.RequestException as e:
                last = BaleAPIError(method, 0, redact(e))
                if attempt + 1 < retries:
                    time.sleep(min(1.5 * (attempt + 1), 6))
                continue
            try:
                data = r.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("ok"):
                return data.get("result")
            data = data if isinstance(data, dict) else {}
            code = data.get("error_code", r.status_code)
            desc = data.get("description") or (r.text or "")[:200]
            params = data.get("parameters")
            ra = params.get("retry_after", 0) if isinstance(params, dict) else 0
            last = BaleAPIError(method, code, desc, ra)
            if code == 429 or r.status_code == 429:
                if attempt + 1 < retries:
                    time.sleep(min(ra or 2, 30))
                continue
            if r.status_code >= 500:
                if attempt + 1 < retries:
                    time.sleep(min(1.5 * (attempt + 1), 6))
                continue
            raise last
        raise last or BaleAPIError(method, 0, "unknown error")

    def try_call(self, method, payload=None, **kw):
        """مثل call ولی خطا را فقط لاگ می‌کند و None می‌دهد (برای کارهای غیرحیاتی)."""
        try:
            return self.call(method, payload, **kw)
        except BaleAPIError as e:
            LOG.warning("API %s: %s", method, redact(e))
            return None

    # ─── متدهای مورد استفاده ───
    def get_me(self):
        return self.call("getMe", retries=2)

    def delete_webhook(self):
        return self.try_call("deleteWebhook")

    def get_updates(self, offset, timeout=POLL_TIMEOUT, limit=POLL_LIMIT):
        return self.call("getUpdates", {"offset": offset, "timeout": timeout, "limit": limit},
                         timeout=timeout + 15, retries=1) or []

    def send_message(self, chat_id, text, reply_markup=None, reply_to=None):
        p = {"chat_id": chat_id, "text": safe_text(text)}
        if reply_markup:
            p["reply_markup"] = reply_markup
        if reply_to:
            p["reply_to_message_id"] = reply_to
        return self.call("sendMessage", p)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        p = {"chat_id": chat_id, "message_id": message_id, "text": safe_text(text)}
        if reply_markup:
            p["reply_markup"] = reply_markup
        return self.call("editMessageText", p)

    def delete_message(self, chat_id, message_id):
        return self.try_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, retries=1)

    def answer_callback_query(self, cb_id, text="", alert=False):
        p = {"callback_query_id": cb_id}
        if text:
            p["text"] = text[:190]
        if alert:
            p["show_alert"] = True
        return self.try_call("answerCallbackQuery", p, retries=1)

    def get_chat_member(self, chat_id, user_id):
        return self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id}, retries=2)

    def ban_chat_member(self, chat_id, user_id):
        return self.call("banChatMember", {"chat_id": chat_id, "user_id": user_id}, retries=2)

    def unban_chat_member(self, chat_id, user_id):
        return self.call("unbanChatMember", {"chat_id": chat_id, "user_id": user_id}, retries=2)


# ─── ساخت کیبورد Inline ───
def btn(text, data=None, url=None):
    b = {"text": text}
    if url:
        b["url"] = url
        return b
    if data is None or len(data.encode("utf-8")) > CALLBACK_DATA_MAX:
        raise ValueError("callback_data invalid or longer than %d bytes: %r" % (CALLBACK_DATA_MAX, data))
    b["callback_data"] = data
    return b


def kb(*rows):
    return {"inline_keyboard": [list(r) for r in rows if r]}


# ──────────────────────────────────────────────────────────────────
# ادمین‌ها
# ──────────────────────────────────────────────────────────────────
def is_bot_admin(user_id):
    return bool(ADMIN_ID) and int(user_id or 0) == int(ADMIN_ID)


def is_group_admin(chat_id, user_id):
    if is_bot_admin(user_id):
        return True
    key = (int(chat_id), int(user_id))
    c = ADMIN_CACHE.get(key)
    if c and c[1] > time.monotonic():
        return c[0]
    try:
        m = APP.api.get_chat_member(chat_id, user_id) or {}
        ok = str(m.get("status", "")).lower() in ("creator", "owner", "administrator", "admin")
        ADMIN_CACHE[key] = (ok, time.monotonic() + ADMIN_CACHE_TTL)
        return ok
    except BaleAPIError as e:
        LOG.warning("admin check failed: %s", redact(e))
        ADMIN_CACHE[key] = (False, time.monotonic() + ADMIN_CACHE_FAIL_TTL)
        return False


def clear_admin_cache(chat_id=None):
    for k in [k for k in ADMIN_CACHE if chat_id is None or k[0] == int(chat_id)]:
        del ADMIN_CACHE[k]


# ──────────────────────────────────────────────────────────────────
# Context و Router
# ──────────────────────────────────────────────────────────────────
class Ctx:
    def __init__(self, kind, chat, user, message, text="", cb_id=None, data=""):
        self.kind = kind                      # "message" | "callback"
        self.chat = chat or {}
        self.chat_id = self.chat.get("id")
        self.chat_type = self.chat.get("type", "private")
        self.user = user or {}
        self.user_id = self.user.get("id")
        self.msg = message or {}
        self.message_id = self.msg.get("message_id")
        self.text = text or ""
        self.cb_id = cb_id
        self.data = data or ""
        self.parts = self.data.split(":") if self.data else []
        self.args = ""
        self.reply_to = self.msg.get("reply_to_message") if kind == "message" else None
        self.answered = False

    @property
    def is_private(self):
        return self.chat_type == "private"

    @property
    def is_group(self):
        return self.chat_type in ("group", "supergroup")

    @property
    def is_bot_admin(self):
        return is_bot_admin(self.user_id)

    def is_admin(self):
        if self.is_bot_admin:
            return True
        return False if self.is_private else is_group_admin(self.chat_id, self.user_id)

    def send(self, text, markup=None, reply_to=None):
        try:
            return APP.api.send_message(self.chat_id, text, markup, reply_to)
        except BaleAPIError as e:
            LOG.warning("send failed: %s", redact(e))
            return None

    def reply(self, text, markup=None):
        return self.send(text, markup, reply_to=self.message_id if self.kind == "message" else None)

    def edit(self, text, markup=None):
        if self.message_id is None:
            return self.send(text, markup)
        try:
            return APP.api.edit_message_text(self.chat_id, self.message_id, text, markup)
        except BaleAPIError as e:
            if "not modified" in e.desc.lower():
                return None
            LOG.warning("edit failed, sending new message: %s", redact(e))
            return self.send(text, markup)

    def answer(self, text="", alert=False):
        if self.cb_id and not self.answered:
            self.answered = True
            APP.api.answer_callback_query(self.cb_id, text, alert)

    def delete(self):
        if self.message_id is not None:
            APP.api.delete_message(self.chat_id, self.message_id)


COMMANDS = {}
CALLBACKS = {}
MESSAGE_HOOKS = []
INPUT_HANDLERS = {}
JOIN_HOOKS = []
BOT_ADDED_HOOKS = []
PRE_HOOKS = []  # قبل از دستورها/ورودی‌ها/بقیه‌ی هوک‌ها اجرا می‌شوند (مدیریت گروه)
LEAVE_HOOKS = []
STARTUP_HOOKS = []
PERIODIC = []
SELFTESTS = []


def command(*names, admin=False, private=True, group=True):
    """admin: False | "group" (ادمین گروه یا ادمین ربات) | "bot" (فقط ادمین ربات)."""
    def deco(fn):
        for n in names:
            if n in COMMANDS:
                raise RuntimeError("duplicate command: " + n)
            COMMANDS[n] = (fn, admin, private, group)
        return fn
    return deco


def callback(prefix, admin=False):
    def deco(fn):
        if prefix in CALLBACKS:
            raise RuntimeError("duplicate callback prefix: " + prefix)
        CALLBACKS[prefix] = (fn, admin)
        return fn
    return deco


def on_message(priority=50):
    def deco(fn):
        MESSAGE_HOOKS.append((priority, fn))
        MESSAGE_HOOKS.sort(key=lambda x: x[0])
        return fn
    return deco


def on_input(kind):
    def deco(fn):
        if kind in INPUT_HANDLERS:
            raise RuntimeError("duplicate input handler: " + kind)
        INPUT_HANDLERS[kind] = fn
        return fn
    return deco


def on_join(fn):
    JOIN_HOOKS.append(fn)
    return fn


def on_leave(fn):
    LEAVE_HOOKS.append(fn)
    return fn


def on_startup(fn):
    STARTUP_HOOKS.append(fn)
    return fn


def periodic(seconds, name=None):
    def deco(fn):
        PERIODIC.append({"fn": fn, "every": seconds, "next": time.monotonic() + seconds, "name": name or fn.__name__})
        return fn
    return deco


def selftest(name):
    def deco(fn):
        SELFTESTS.append((name, fn))
        return fn
    return deco


_CMD_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(?:[\s]+(.*))?$", re.S)


def parse_command(text):
    """(cmd, args) یا None. دستورِ خطاب‌شده به ربات دیگر نادیده گرفته می‌شود."""
    m = _CMD_RE.match((text or "").strip())
    if not m:
        return None
    cmd, target, args = m.group(1).lower(), m.group(2), (m.group(3) or "").strip()
    if target and APP.bot_username and target.lower() != APP.bot_username.lower():
        return None
    return cmd, args


def _deny_admin(ctx, level):
    if level == "bot":
        return not ctx.is_bot_admin
    if level == "group":
        return not ctx.is_admin()
    return False


def dispatch_command(ctx):
    parsed = parse_command(ctx.text)
    if not parsed:
        return False
    cmd, ctx.args = parsed
    entry = COMMANDS.get(cmd)
    if entry is None:
        return False
    fn, admin, private_ok, group_ok = entry
    if not RATE.allow(("cmd", ctx.user_id), *CMD_RATE_LIMIT):
        return True
    if ctx.is_private and not private_ok:
        ctx.reply("این دستور فقط داخل گروه کار می‌کنه 👥")
        return True
    if ctx.is_group and not group_ok:
        ctx.reply("این دستور فقط توی پیوی ربات کار می‌کنه 💬")
        return True
    if _deny_admin(ctx, admin):
        ctx.reply("⛔ این بخش فقط مخصوص ادمینه")
        return True
    fn(ctx)
    return True


def guard_callback(ctx):
    """چند لایه محافظت: Callback تکراری، Double Click، Rate limit. True یعنی اجازه اجرا."""
    if ctx.cb_id and not CB_SEEN.add(ctx.cb_id, CB_SEEN_TTL):
        return False
    if not RATE.allow(("cb", ctx.user_id), *CB_RATE_LIMIT):
        ctx.answer("⏳ یکم آروم‌تر", False)
        return False
    if not CLICK_LOCK.add((ctx.user_id, ctx.chat_id, ctx.message_id, ctx.data), DOUBLE_CLICK_WINDOW):
        ctx.answer()
        return False
    return True


def dispatch_callback(ctx):
    entry = CALLBACKS.get(ctx.parts[0]) if ctx.parts else None
    if entry is None:
        ctx.answer("این دکمه دیگه فعال نیست", False)
        return
    fn, admin = entry
    if not guard_callback(ctx):
        return
    if _deny_admin(ctx, admin):
        ctx.answer("⛔ فقط ادمین‌ها می‌تونن", True)
        return
    try:
        fn(ctx)
    finally:
        ctx.answer()


def callback_owner_ok(ctx, owner_id):
    """برای دکمه‌هایی که مال یک کاربر مشخص‌اند (مثلاً بازی شخصی)."""
    if int(owner_id) == int(ctx.user_id):
        return True
    ctx.answer("این دکمه مال تو نیست 🙅", True)
    return False


def track_activity(ctx):
    """شمارنده‌های سبک آماری (حافظه آماری، نه ذخیره پیام)."""
    if not ctx.is_group or ctx.user_id is None:
        return
    t = now()
    STATS.group_add(ctx.chat_id, "total:msgs")
    STATS.group_add(ctx.chat_id, "hour:%02d" % hour_of(t))
    STATS.group_add(ctx.chat_id, "day:%s:msgs" % day_key(t))
    STATS.user_add(ctx.chat_id, ctx.user_id, "msgs_count")
    STATS.user_touch(ctx.chat_id, ctx.user_id, t)


def _remember_entities(ctx):
    if ctx.user_id is not None and USER_SEEN.add(ctx.user_id, 300):
        upsert_user(ctx.user)
    if ctx.chat_id is not None and not ctx.is_private and GROUP_SEEN.add(ctx.chat_id, 600):
        upsert_group(ctx.chat)


def _handle_service_message(msg, chat):
    handled = False
    joined = msg.get("new_chat_members") or []
    if joined:
        handled = True
        for u in joined:
            if u.get("id") == APP.bot_id:
                upsert_group(chat)
                LOG.info("added to group %s", chat.get("id"))
                bot_ctx = Ctx("message", chat, msg.get("from") or {}, msg)
                for fn in BOT_ADDED_HOOKS:
                    _safe_run(fn, bot_ctx)
                continue
            if u.get("is_bot"):
                continue
            upsert_user(u)
            ctx = Ctx("message", chat, u, msg)
            STATS.group_add(chat["id"], "total:joins")
            for fn in JOIN_HOOKS:
                _safe_run(fn, ctx, u)
    left = msg.get("left_chat_member")
    if left:
        handled = True
        if left.get("id") == APP.bot_id:
            mark_group_inactive(chat["id"])
            LOG.info("removed from group %s", chat.get("id"))
        elif not left.get("is_bot"):
            ctx = Ctx("message", chat, left, msg)
            for fn in LEAVE_HOOKS:
                _safe_run(fn, ctx, left)
    return handled


def _safe_run(fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 — خطای یک Hook نباید کل ربات را بخواباند
        LOG.exception("hook %s failed", getattr(fn, "__name__", fn))
        report_error(e)
        return None


def handle_message(msg):
    chat = msg.get("chat") or {}
    if chat.get("id") is None:
        return
    if chat.get("type") == "channel":
        return
    if msg.get("date") and now() - int(msg["date"]) > STALE_MESSAGE_SECONDS:
        return
    if _handle_service_message(msg, chat):
        return
    user = msg.get("from") or {}
    if not user.get("id") or user.get("is_bot"):
        return
    ctx = Ctx("message", chat, user, msg, text=msg.get("text") or msg.get("caption") or "")
    _remember_entities(ctx)
    for pre in list(PRE_HOOKS):
        if _safe_run(pre, ctx):
            return
    if ctx.text.startswith("/"):
        track_activity(ctx)
        if dispatch_command(ctx):
            return
    else:
        track_activity(ctx)
        st = input_get(ctx.chat_id, ctx.user_id) if ctx.text else None
        if st and st["kind"].startswith("input:"):
            fn = INPUT_HANDLERS.get(st["kind"][6:])
            if fn is not None and fn(ctx, st["data"]) is not False:
                return
    for _, fn in list(MESSAGE_HOOKS):
        if fn(ctx):
            return


def handle_callback(cq):
    user = cq.get("from") or {}
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    if not user.get("id") or chat.get("id") is None:
        APP.api.answer_callback_query(cq.get("id"), "")
        return
    ctx = Ctx("callback", chat, user, msg, cb_id=cq.get("id"), data=cq.get("data") or "")
    _remember_entities(ctx)
    dispatch_callback(ctx)


def handle_update(update):
    if update.get("callback_query"):
        handle_callback(update["callback_query"])
    elif update.get("message"):
        handle_message(update["message"])


# ──────────────────────────────────────────────────────────────────
# گزارش خطا به ادمین (محدود و بدون توکن)
# ──────────────────────────────────────────────────────────────────
def report_error(exc):
    if not ADMIN_ID or APP.api is None:
        return
    if not cooldown_ok("error_report", ERROR_REPORT_INTERVAL):
        return
    try:
        APP.api.call("sendMessage", {"chat_id": ADMIN_ID, "text": safe_text(
            "⚠️ %s خطا گرفت:\n%s: %s" % (BOT_NAME, type(exc).__name__, redact(exc)), 600)}, retries=1)
    except BaleAPIError as e:
        LOG.debug("error report not delivered: %s", redact(e))


# ──────────────────────────────────────────────────────────────────
# کارهای دوره‌ای هسته
# ──────────────────────────────────────────────────────────────────
@periodic(20, "stats_flush")
def _task_flush_stats():
    STATS.flush()


@periodic(600, "cleanup")
def _task_cleanup():
    res = run_cleanup()
    if any(res.values()):
        LOG.info("cleanup: %s", res)


@periodic(120, "memory_purge")
def _task_purge():
    purge_memory_stores()


@periodic(3600, "wal_checkpoint")
def _task_checkpoint():
    APP.db.run("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()


def run_periodic():
    t = time.monotonic()
    for task in PERIODIC:
        if t >= task["next"]:
            task["next"] = t + task["every"]
            _safe_run(task["fn"])


# ──────────────────────────────────────────────────────────────────
# SELFTEST (فقط ادمین ربات) — روی دیتابیس موقت و API جعلی اجرا می‌شود
# ──────────────────────────────────────────────────────────────────
class FakeAPI:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def rec(*a, **k):
            self.calls.append((name, a, k))
            return {"message_id": 1, "status": "member"}
        return rec


@contextmanager
def isolated_env():
    STATS.flush()
    old_db, old_api = APP.db, APP.api
    APP.db = Database(":memory:")
    init_schema(APP.db)
    APP.api = FakeAPI()
    SETTINGS_CACHE.clear()
    ADMIN_CACHE.clear()
    try:
        yield APP.api
    finally:
        STATS.flush()
        APP.db.close()
        APP.db, APP.api = old_db, old_api
        SETTINGS_CACHE.clear()
        ADMIN_CACHE.clear()


def check(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


def run_selftests():
    results = []
    for name, fn in SELFTESTS:
        try:
            with isolated_env():
                detail = fn()
            results.append((name, True, str(detail or "ok")))
        except Exception as e:  # noqa: BLE001
            results.append((name, False, redact("%s: %s" % (type(e).__name__, e))))
    return results


_PROBE = {"hits": 0, "admin_hits": 0}


@callback("selftest_probe")
def _cb_probe(ctx):
    _PROBE["hits"] += 1
    ctx.answer("ok")


@callback("selftest_admin", admin="bot")
def _cb_probe_admin(ctx):
    _PROBE["admin_hits"] += 1


def _fake_cb_update(cb_id, data, user_id, chat_id=-1001, message_id=7):
    return {"update_id": 1, "callback_query": {
        "id": cb_id, "from": {"id": user_id, "first_name": "T"}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "group", "title": "g"}}}}


def _non_admin_id():
    return 4242424242 if ADMIN_ID != 4242424242 else 4242424243


@selftest("SQLite: نسخه و ساخت همه جدول‌ها")
def _t_schema():
    check(sqlite3.sqlite_version_info >= (3, 24, 0), "SQLite too old for UPSERT: " + sqlite3.sqlite_version)
    names = {r["name"] for r in APP.db.all("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in REQUIRED_TABLES if t not in names]
    check(not missing, "missing tables: %s" % missing)
    init_schema(APP.db)  # idempotent
    return "%d tables, sqlite %s" % (len(REQUIRED_TABLES), sqlite3.sqlite_version)


@selftest("User/Group upsert و تنظیمات پیش‌فرض")
def _t_entities():
    upsert_user({"id": 11, "first_name": "Ali", "username": "ali"})
    upsert_user({"id": 11, "first_name": "Ali2", "username": "ali"})
    check(get_user(11)["first_name"] == "Ali2")
    upsert_group({"id": -5, "title": "G", "type": "group"})
    s = get_settings(-5)
    check(s["anti_link"] == 1 and s["goodbye_on"] == 0 and s["mod_level"] == 2, "bad defaults")
    check(toggle_setting(-5, "games") is False and get_settings(-5)["games"] == 0, "toggle failed")
    check(toggle_setting(-5, "nonsense") is None, "toggle accepted unknown key")
    return "ok"


@selftest("Settings: whitelist و SQL injection")
def _t_settings_security():
    upsert_group({"id": -6, "title": "G", "type": "group"})
    check(not set_setting(-6, "games=0; DROP TABLE users; --", 1), "injection key accepted")
    check(not set_setting(-6, "mod_level", 99), "out of range accepted")
    check(set_setting(-6, "rules_text", "x'); DROP TABLE users; --"), "text rejected")
    check(APP.db.val("SELECT COUNT(*) FROM users") == 0, "users table damaged")
    check(get_settings(-6)["rules_text"].startswith("x'"), "text not stored literally")
    try:
        STATS.user_add(1, 1, "xp; DROP TABLE users", 1)
        raise AssertionError("bad stat field accepted")
    except ValueError as e:
        check("unknown user stat field" in str(e), "wrong error: %s" % e)
    return "ok"


@selftest("StatBuffer: شمارنده‌های آماری")
def _t_stats():
    STATS.group_add(-9, "total:msgs", 3)
    STATS.group_add(-9, "total:msgs", 2)
    STATS.user_add(-9, 5, "msgs_count", 4)
    STATS.user_touch(-9, 5, 1000)
    STATS.flush()
    check(APP.db.val("SELECT svalue FROM group_stats WHERE chat_id=-9 AND skey='total:msgs'") == 5)
    check(user_stat_get(-9, 5, "msgs_count") == 4)
    STATS.user_touch(-9, 5, 2000)
    STATS.flush()
    r = APP.db.one("SELECT first_active, last_active FROM user_stats WHERE chat_id=-9 AND user_id=5")
    check(r["first_active"] == 1000 and r["last_active"] == 2000, "first/last active wrong")
    return "ok"


@selftest("Cooldown / Rate limit / TTL")
def _t_limits():
    check(cooldown_ok("t:a", 5) and not cooldown_ok("t:a", 5), "cooldown")
    rl = RateLimiter()
    check(all(rl.allow("k", 3, 10) for _ in range(3)) and not rl.allow("k", 3, 10), "rate limit")
    st = TTLStore()
    check(st.add("x", 0.05) and not st.add("x", 0.05), "ttl block")
    time.sleep(0.07)
    check(st.add("x", 0.05), "ttl expiry")
    return "ok"


@selftest("زمان ایران: روز/ساعت/هفته (شنبه)")
def _t_time():
    ts = int(datetime.datetime(2026, 10, 4, 12, 0, tzinfo=datetime.timezone.utc).timestamp())
    check(day_key(ts) == "2026-10-04", day_key(ts))
    check(hour_of(ts) == 15, str(hour_of(ts)))
    check(week_key(ts) == "2026-10-03", week_key(ts))  # شنبه ۳ اکتبر ۲۰۲۶
    sat = int(datetime.datetime(2026, 10, 3, 1, 0, tzinfo=datetime.timezone.utc).timestamp())
    check(week_key(sat) == "2026-10-03", "saturday")
    fri = int(datetime.datetime(2026, 10, 2, 12, 0, tzinfo=datetime.timezone.utc).timestamp())
    check(week_key(fri) == "2026-09-26", "friday belongs to previous week")
    return "ok"


@selftest("متن فارسی: نرمال‌سازی و Hash تکراری")
def _t_text():
    check(norm_text("كي   بيداره؟") == norm_text("کی بیداره"), "ك/ي")
    check(norm_text("بی‌پولم") == norm_text("بیپولم"), "ZWNJ")
    check(norm_text("۱۲۳") == "123", "digits")
    check(text_hash("سلام  دنیا!") == text_hash("سلام دنیا"), "hash")
    return "ok"


@selftest("Inline Button: محدودیت callback_data")
def _t_buttons():
    check(btn("a", "menu:home")["callback_data"] == "menu:home")
    try:
        btn("a", "x" * 65)
        raise AssertionError("long callback accepted")
    except ValueError as e:
        check("callback_data" in str(e), "wrong error: %s" % e)
    m = kb([btn("1", "a:b"), btn("2", "a:c")], [btn("3", "a:d")])
    check(len(m["inline_keyboard"]) == 2 and len(m["inline_keyboard"][0]) == 2, "kb shape")
    return "ok"


@selftest("Bale API: URL، موفق، خطا (بدون شبکه)")
def _t_api():
    class Resp:
        def __init__(self, code, body):
            self.status_code, self._b, self.text = code, body, json.dumps(body)

        def json(self):
            return self._b

    class Sess:
        def __init__(self, resp):
            self.resp, self.urls = resp, []
            self.headers = {}

        def post(self, url, json=None, timeout=None):
            self.urls.append(url)
            return self.resp

    api = BaleAPI("TOKEN123456789")
    api.s = Sess(Resp(200, {"ok": True, "result": {"id": 1}}))
    check(api.call("getMe") == {"id": 1}, "ok path")
    check(api.s.urls[0] == "%s/botTOKEN123456789/getMe" % API_BASE, api.s.urls[0])
    check(API_BASE == "https://tapi.bale.ai" or API_BASE.startswith("http://127.0.0.1"), "unexpected API base: " + API_BASE)
    api.s = Sess(Resp(400, {"ok": False, "error_code": 400, "description": "Bad Request"}))
    try:
        api.call("sendMessage", {})
        raise AssertionError("no error raised")
    except BaleAPIError as e:
        check(e.code == 400 and len(api.s.urls) == 1, "must not retry 4xx")
    check(api.try_call("sendMessage", {}) is None, "try_call")
    return "ok"


@selftest("امنیت: پنهان‌سازی توکن در لاگ")
def _t_redact():
    s = redact("error at https://tapi.bale.ai/bot12345:SECRETSECRET/getUpdates")
    check("SECRET" not in s and "<token>" in s, s)
    return "ok"


@selftest("Router: Callback تکراری، Double Click، Admin")
def _t_router():
    api = APP.api
    uid = _non_admin_id()
    _PROBE["hits"] = _PROBE["admin_hits"] = 0
    handle_update(_fake_cb_update("cb-A", "selftest_probe:x", uid))
    check(_PROBE["hits"] == 1, "first click must run")
    handle_update(_fake_cb_update("cb-A", "selftest_probe:x", uid))
    check(_PROBE["hits"] == 1, "same callback id ran twice")
    handle_update(_fake_cb_update("cb-B", "selftest_probe:x", uid))
    check(_PROBE["hits"] == 1, "double click not blocked")
    handle_update(_fake_cb_update("cb-C", "selftest_probe:y", uid))
    check(_PROBE["hits"] == 2, "different button must run")
    handle_update(_fake_cb_update("cb-D", "selftest_admin:x", uid))
    check(_PROBE["admin_hits"] == 0, "non-admin ran admin callback")
    check(any(c[0] == "answer_callback_query" for c in api.calls), "no answer sent")
    handle_update(_fake_cb_update("cb-E", "unknown_prefix:x", uid))
    return "ok"


@selftest("دستورها: parse، خطاب به ربات دیگر")
def _t_commands():
    old = APP.bot_username
    APP.bot_username = "safabat_bot"
    try:
        check(parse_command("/start") == ("start", ""))
        check(parse_command("/Start@safabat_bot  hello world") == ("start", "hello world"))
        check(parse_command("/start@other_bot") is None, "other bot's command accepted")
        check(parse_command("سلام") is None)
    finally:
        APP.bot_username = old
    check("selftest" in COMMANDS and COMMANDS["selftest"][1] == "bot", "selftest must be bot-admin only")
    return "ok"


@selftest("Session و ورودی متنی موقت")
def _t_sessions():
    input_set(-1, 2, "rules", {"a": 1}, ttl=60)
    check(input_get(-1, 2)["data"] == {"a": 1} and input_get(-1, 2)["kind"] == "input:rules")
    check(input_clear(-1, 2) and input_get(-1, 2) is None, "clear")
    session_put("g:1", -1, 2, "game", {"n": 5}, ttl=-1)
    check(session_get("g:1") is None, "expired session visible")
    return "ok"


@selftest("Reward: جلوگیری از پاداش تکراری")
def _t_reward():
    check(reward_once("game:abc:1") is True and reward_once("game:abc:1") is False, "duplicate reward")
    return "ok"


@selftest("Cleanup: موقت‌ها پاک، XP/مدال/رکورد می‌مانند")
def _t_cleanup():
    t = now()
    session_put("old", -1, 1, "game", {}, ttl=-(SESSION_GRACE_SECONDS + 30))
    session_put("alive", -1, 1, "game", {}, ttl=1000)
    APP.db.run("INSERT INTO events(chat_id, ekey, title, multiplier, starts_at, ends_at, active, created_at) "
               "VALUES(-1,'xp2','x',2,?,?,1,?)", (t - 100, t - 50, t - 100))
    APP.db.run("INSERT INTO user_stats(chat_id, user_id, xp) VALUES(-1, 1, 999)")
    APP.db.run("INSERT INTO user_achievements(chat_id, user_id, akey, earned_at) VALUES(-1,1,'first_win',?)", (t,))
    APP.db.run("INSERT INTO records(chat_id, rkey, user_id, value, ts) VALUES(-1,'best',1,10,?)", (t,))
    APP.db.run("INSERT INTO group_stats(chat_id, skey, svalue) VALUES(-1,'day:2000-01-01:msgs',5)")
    out = run_cleanup()
    check(out["sessions"] == 1 and session_get("alive") is not None, "session cleanup wrong")
    check(APP.db.val("SELECT active FROM events WHERE chat_id=-1") == 0, "ended event still active")
    check(APP.db.val("SELECT COUNT(*) FROM group_stats WHERE skey LIKE 'day:2000%'") == 0, "old day stats stay")
    check(user_stat_get(-1, 1, "xp") == 999, "XP lost!")
    check(APP.db.val("SELECT COUNT(*) FROM user_achievements") == 1, "achievement lost!")
    check(APP.db.val("SELECT COUNT(*) FROM records") == 1, "record lost!")
    return "ok"


@selftest("پیام گروهی: ثبت کاربر/گروه و آمار")
def _t_message_flow():
    msg = {"message_id": 5, "date": now(), "text": "سلام", "from": {"id": 77, "first_name": "Sara"},
           "chat": {"id": -777, "type": "group", "title": "GG"}}
    USER_SEEN.discard(77)
    GROUP_SEEN.discard(-777)
    handle_update({"update_id": 2, "message": msg})
    STATS.flush()
    check(get_user(77)["first_name"] == "Sara", "user not stored")
    check(APP.db.val("SELECT title FROM groups WHERE chat_id=-777") == "GG", "group not stored")
    check(user_stat_get(-777, 77, "msgs_count") == 1, "msg count")
    old = dict(msg, date=now() - STALE_MESSAGE_SECONDS - 5, message_id=6)
    handle_update({"update_id": 3, "message": old})
    STATS.flush()
    check(user_stat_get(-777, 77, "msgs_count") == 1, "stale message counted")
    return "ok"


@selftest("RAM: مصرف حافظه فرایند")
def _t_ram():
    try:
        import resource
        kb_used = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mb = kb_used / 1024.0 if sys.platform != "darwin" else kb_used / (1024.0 * 1024.0)
        check(mb < 200, "high memory: %.1fMB" % mb)
        return "peak RSS %.1f MB" % mb
    except ImportError:
        return "resource module unavailable (skipped)"


@command("selftest", admin="bot")
def cmd_selftest(ctx):
    results = run_selftests()
    ok = sum(1 for _, good, _ in results if good)
    lines = ["🧪 خودآزمایی %s: %d/%d موفق" % (BOT_NAME, ok, len(results)), ""]
    for name, good, detail in results:
        lines.append(("✅ " if good else "❌ ") + name + ("" if good and detail == "ok" else "\n    ↳ " + detail))
    ctx.reply("\n".join(lines))


# ══════════════════════════════════════════════════════════════════
# ███ END OF PHASE 1
# ══════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════
# ███ PHASE 2 — MAIN UI / INLINE NAVIGATION / USER-GROUP / SETTINGS
# ══════════════════════════════════════════════════════════════════

# ─── کاتالوگ‌های ثابت (فازهای بعدی از همین‌ها استفاده می‌کنند) ───
GAME_CATALOG = [
    ("number_guess", "🎯", "حدس عدد"),
    ("word_guess", "🔤", "حدس کلمه"),
    ("riddle", "🧩", "معما"),
    ("trivia", "🧠", "اطلاعات عمومی"),
    ("speed", "⚡", "سرعتی"),
    ("math", "🔢", "ریاضی"),
    ("emoji_guess", "😀", "حدس ایموجی"),
    ("country_guess", "🌍", "حدس کشور"),
    ("rps", "✊", "سنگ کاغذ قیچی"),
    ("dice", "🎲", "تاس"),
    ("duel", "⚔️", "دوئل"),
    ("group_game", "👥", "بازی گروهی"),
    ("league", "🏆", "لیگ"),
]
GAME_TITLES = {k: t for k, _, t in GAME_CATALOG}
GAME_EMOJIS = {k: e for k, e, _ in GAME_CATALOG}

JOKE_CATEGORIES = [
    ("daily", "😂", "روزمره"),
    ("school", "🏫", "مدرسه"),
    ("family", "👨‍👩‍👦", "خانواده"),
    ("money", "💸", "پول"),
    ("phone", "📱", "گوشی"),
    ("internet", "🌐", "اینترنت"),
    ("gaming", "🎮", "گیم"),
    ("coding", "🧑‍💻", "برنامه‌نویسی"),
    ("friends", "👥", "رفیق‌ها"),
    ("oops", "🤦", "سوتی"),
    ("unlucky", "😭", "بدشانسی"),
    ("logic", "🧠", "منطقی"),
    ("dialog", "🗣️", "دیالوگی"),
    ("oneliner", "⚡", "یک‌خطی"),
]
JOKE_CAT_LABELS = {k: t for k, _, t in JOKE_CATEGORIES}

FORTUNE_CATEGORIES = [
    ("love", "❤️", "عشق"),
    ("money", "💰", "پول"),
    ("school", "📚", "مدرسه"),
    ("gaming", "🎮", "گیم"),
    ("funny", "😂", "فال خاکی"),
    ("night", "🌙", "شب"),
    ("today", "☀️", "امروز"),
    ("lucky", "🎲", "شانسی"),
    ("event", "👀", "اتفاق امروز"),
    ("character", "🔥", "شخصیت"),
]
FORTUNE_CAT_LABELS = {k: t for k, _, t in FORTUNE_CATEGORIES}

# کلیدهایی که ادمین از پنل «تنظیمات» روشن/خاموش می‌کند
FEATURE_LABELS = [
    ("auto_reply", "😂", "پاسخ خودکار"),
    ("games", "🎮", "بازی‌ها"),
    ("fortune", "🔮", "فال"),
    ("jokes", "😂", "جوک"),
    ("missions", "🎯", "مأموریت"),
    ("lab", "🧪", "آزمایشگاه"),
    ("anti_spam", "🛡️", "ضد اسپم"),
    ("anti_link", "🔗", "ضد لینک"),
]
FEATURE_KEYS = {k for k, _, _ in FEATURE_LABELS}
FEATURE_NAMES = {k: n for k, _, n in FEATURE_LABELS}
CHANCE_STEP = 5

MENU_LAYOUT = [
    [("🎮 بازی‌ها", "games"), ("😂 سرگرمی", "fun")],
    [("🧠 حافظه", "memory"), ("🧬 DNA گروه", "dna")],
    [("🏆 رتبه‌ها", "ranks"), ("🎯 مأموریت‌ها", "missions")],
    [("🛡️ مدیریت", "manage"), ("📊 آمار", "stats")],
    [("⚙️ تنظیمات", "settings"), ("ℹ️ راهنما", "help")],
]

SECTIONS = {}         # کلید بخش -> تابع (ctx)
SECTION_FEATURE = {}  # کلید بخش -> ستون تنظیماتی که باید روشن باشد
SECTION_ADMIN = set() # بخش‌هایی که داخل گروه فقط ادمین می‌بیند
MEMBERS_CHECK = TTLStore(2000)


def seed_catalog():
    """فهرست بازی‌ها در جدول games ثبت می‌شود (تکراری نمی‌شود، آمار plays حفظ می‌شود)."""
    with APP.db.tx():
        for key, emoji, title in GAME_CATALOG:
            APP.db.run("INSERT INTO games(game_key, title, emoji) VALUES(?,?,?) "
                       "ON CONFLICT(game_key) DO UPDATE SET title=excluded.title, emoji=excluded.emoji",
                       (key, title, emoji))


on_startup(seed_catalog)


def section(key, feature=None, admin=False):
    """ثبت یک بخش از منوی اصلی. دکمه‌ی منو فقط وقتی دیده می‌شود که بخش واقعاً پیاده‌سازی شده باشد."""
    def deco(fn):
        if key in SECTIONS:
            raise RuntimeError("duplicate section: " + key)
        SECTIONS[key] = fn
        if feature:
            SECTION_FEATURE[key] = feature
        if admin:
            SECTION_ADMIN.add(key)
        return fn
    return deco


def on_bot_added(fn):
    BOT_ADDED_HOOKS.append(fn)
    return fn


def day_start(ts=None):
    """شروع روز (نیمه‌شب ایران) به‌صورت timestamp."""
    d = iran_dt(ts)
    base = datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)
    return int(base.timestamp()) - IRAN_UTC_OFFSET


def daily_pick(chat_id, items, salt=""):
    """انتخاب ثابت در طول یک روز برای هر گروه."""
    h = int(hashlib.sha1(("%s:%s:%s" % (day_key(), chat_id, salt)).encode("utf-8")).hexdigest()[:8], 16)
    return items[h % len(items)]


def feature_enabled(ctx, feature):
    if ctx.is_private:
        return True
    return bool(get_settings(ctx.chat_id).get(feature))


def require_feature(ctx, feature):
    """False و پیام مناسب اگر ادمین آن قابلیت را خاموش کرده باشد."""
    if feature_enabled(ctx, feature):
        return True
    msg = "🔕 «%s» توی این گروه خاموشه. ادمین می‌تونه از تنظیمات روشنش کنه." % FEATURE_NAMES.get(feature, feature)
    if ctx.kind == "callback":
        ctx.answer(msg, True)
    else:
        ctx.reply(msg)
    return False


def show(ctx, text, markup=None):
    """اگر از دکمه آمده همان پیام ویرایش می‌شود، وگرنه پاسخ جدید ارسال می‌شود."""
    return ctx.edit(text, markup) if ctx.kind == "callback" else ctx.reply(text, markup)


def home_row():
    return [btn("🏠 برگشت", "menu:home")]


# ─── منوی اصلی ───
def group_member_count(chat_id):
    if MEMBERS_CHECK.add(chat_id, 900):
        n = APP.api.try_call("getChatMembersCount", {"chat_id": chat_id}, retries=1)
        if not isinstance(n, int):
            n = APP.api.try_call("getChatMemberCount", {"chat_id": chat_id}, retries=1)
        if isinstance(n, int) and n > 0:
            APP.db.run("UPDATE groups SET member_count=? WHERE chat_id=?", (n, int(chat_id)))
    cnt = APP.db.val("SELECT member_count FROM groups WHERE chat_id=?", (int(chat_id),), 0)
    return cnt or APP.db.val("SELECT COUNT(*) FROM user_stats WHERE chat_id=?", (int(chat_id),), 0)


MENU_BOX = "╭──────────────────────╮\n🧠 %s\n╰──────────────────────╯" % BOT_NAME


def home_text(ctx):
    if ctx.is_private:
        return ("%s\n\nسلام %s 👋\nمن یه عضو دیجیتالِ گروهم: بازی، جوک، فال، رقابت و مدیریت.\n"
                "منو به گروهت اضافه کن تا کامل کار کنم 😎\n\n🟢 سیستم فعال"
                % (MENU_BOX, display_name(ctx.user)))
    STATS.flush()
    cid = int(ctx.chat_id)
    msgs = APP.db.val("SELECT svalue FROM group_stats WHERE chat_id=? AND skey=?",
                      (cid, "day:%s:msgs" % day_key()), 0)
    t0 = day_start()
    users = APP.db.val("SELECT COUNT(*) FROM user_stats WHERE chat_id=? AND last_active>=?", (cid, t0), 0)
    played = APP.db.val("SELECT COUNT(*) FROM game_records WHERE chat_id=? AND created_at>=?", (cid, t0), 0)
    _, gemoji, gtitle = featured_game(cid)
    fun = featured_fun(cid)
    activity = ("%d پیام از %d نفر" % (msgs, users)) if msgs else "هنوز شروع نشده 😴"
    game_line = ("%s %s (%d بازی انجام شده)" % (gemoji, gtitle, played)) if played else ("%s %s ⭐" % (gemoji, gtitle))
    return ("%s\n\n👥 اعضای گروه: %d\n🔥 فعالیت امروز: %s\n🎮 بازی امروز: %s\n😂 سرگرمی امروز: %s\n🟢 سیستم فعال"
            % (MENU_BOX, group_member_count(cid), activity, game_line, fun))


def main_menu_markup():
    rows = []
    for row in MENU_LAYOUT:
        bs = [btn(label, "menu:" + key) for label, key in row if key in SECTIONS]
        if bs:
            rows.append(bs)
    return kb(*rows)


def show_home(ctx):
    show(ctx, home_text(ctx), main_menu_markup())


@command("start", "menu")
def cmd_start(ctx):
    show_home(ctx)


@callback("menu")
def cb_menu(ctx):
    key = ctx.parts[1] if len(ctx.parts) > 1 else "home"
    if key == "home":
        show_home(ctx)
        return
    fn = SECTIONS.get(key)
    if fn is None:
        ctx.answer("این بخش فعال نیست", True)
        return
    feat = SECTION_FEATURE.get(key)
    if feat and not feature_enabled(ctx, feat):
        ctx.answer("🔕 این بخش توسط ادمین خاموش شده", True)
        return
    if key in SECTION_ADMIN and ctx.is_group and not ctx.is_admin():
        ctx.answer("⛔ این بخش فقط مخصوص ادمین‌هاست", True)
        return
    STATS.group_add(ctx.chat_id, "feat:" + key)
    fn(ctx)


@on_bot_added
def _greet_new_group(ctx):
    ctx.send("👋 سلام! من %s‌ام، عضو دیجیتالِ این گروه 🧠\n"
             "بازی، جوک، فال، رقابت و مدیریت — همه با دکمه.\n"
             "اگه پیام‌های گروه رو نمی‌بینم، منو ادمین کن 🙏" % BOT_NAME,
             kb([btn("🏠 منوی اصلی", "menu:home")]))


# ─── انتخاب گروه (برای مدیریت از پیوی) ───
def show_group_picker(ctx, prefix, title):
    rows_db = APP.db.all(
        """SELECT g.chat_id, g.title FROM groups g
           WHERE g.active=1 AND (? OR EXISTS(SELECT 1 FROM user_stats u WHERE u.chat_id=g.chat_id AND u.user_id=?))
           ORDER BY g.last_active DESC LIMIT 15""",
        (1 if ctx.is_bot_admin else 0, int(ctx.user_id)))
    rows = []
    for r in rows_db:
        if len(rows) >= 8:
            break
        if is_group_admin(r["chat_id"], ctx.user_id):
            rows.append([btn("👥 " + (r["title"] or "گروه")[:26], "%s:%d" % (prefix, r["chat_id"]))])
    if rows:
        text = title + "\n\nیکی از گروه‌هایی که ادمینشی رو انتخاب کن 👇"
    else:
        text = (title + "\n\nگروهی پیدا نکردم که توش ادمین باشی.\n"
                "منو توی گروهت اضافه کن و یه بار توش چیزی بنویس تا بشناسمش.")
    rows.append(home_row())
    show(ctx, text, kb(*rows))


# ─── تنظیمات گروه ───
def settings_text(chat_id):
    title = APP.db.val("SELECT title FROM groups WHERE chat_id=?", (int(chat_id),), None) or "گروه"
    return ("⚙️ تنظیمات «%s»\n\nهر مورد با یک لمس روشن/خاموش می‌شه.\n"
            "🎚️ احتمال پاسخ خودکار: %d%%" % (title, get_settings(chat_id)["reply_chance"]))


def settings_markup(chat_id, from_private):
    s = get_settings(chat_id)
    rows, cur = [], []
    for key, emoji, label in FEATURE_LABELS:
        cur.append(btn("%s %s %s" % (emoji, label, "✅" if s.get(key) else "❌"), "st:t:%d:%s" % (chat_id, key)))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([btn("➖", "st:c:%d:%d" % (chat_id, -CHANCE_STEP)),
                 btn("🎚️ احتمال پاسخ: %d%%" % s["reply_chance"], "st:o:%d" % chat_id),
                 btn("➕", "st:c:%d:%d" % (chat_id, CHANCE_STEP))])
    rows.append([btn("🏠 برگشت", "st:pick" if from_private else "menu:home")])
    return kb(*rows)


def show_settings(ctx, chat_id):
    show(ctx, settings_text(chat_id), settings_markup(chat_id, ctx.is_private))


def _settings_target(ctx):
    """Chat هدف را از دکمه می‌خواند و دسترسی ادمین را برای همان گروه تأیید می‌کند."""
    try:
        target = int(ctx.parts[2])
    except (IndexError, ValueError):
        ctx.answer("دکمه نامعتبر", True)
        return None
    if ctx.is_group and target != int(ctx.chat_id):
        ctx.answer("⛔ این دکمه برای گروه دیگه‌ایه", True)
        return None
    if APP.db.val("SELECT 1 FROM groups WHERE chat_id=?", (target,)) is None:
        ctx.answer("این گروه رو نمی‌شناسم", True)
        return None
    if not is_group_admin(target, ctx.user_id):
        ctx.answer("⛔ تنظیمات فقط دست ادمین‌هاست", True)
        return None
    return target


@callback("st")
def cb_settings(ctx):
    action = ctx.parts[1] if len(ctx.parts) > 1 else "o"
    if action == "pick":
        if ctx.is_group:
            show_home(ctx)
        else:
            show_group_picker(ctx, "st:o", "⚙️ تنظیمات کدوم گروه؟")
        return
    target = _settings_target(ctx)
    if target is None:
        return
    if action == "t":
        key = ctx.parts[3] if len(ctx.parts) > 3 else ""
        if key not in FEATURE_KEYS:
            ctx.answer("تنظیم نامعتبر", True)
            return
        new = toggle_setting(target, key)
        log_action(target, "setting", ctx.user_id, 0, "%s=%d" % (key, 1 if new else 0))
        ctx.answer("%s %s" % (FEATURE_NAMES[key], "روشن شد ✅" if new else "خاموش شد ❌"))
    elif action == "c":
        try:
            delta = int(ctx.parts[3])
        except (IndexError, ValueError):
            delta = 0
        if delta not in (-CHANCE_STEP, CHANCE_STEP):
            ctx.answer("مقدار نامعتبر", True)
            return
        cur = get_settings(target)["reply_chance"]
        new = max(0, min(100, cur + delta))
        if new == cur:
            ctx.answer("به حد مجاز رسیدی", False)
            return
        set_setting(target, "reply_chance", new)
        log_action(target, "setting", ctx.user_id, 0, "reply_chance=%d" % new)
        ctx.answer("احتمال پاسخ: %d%%" % new)
    show_settings(ctx, target)


@section("settings")
def sec_settings(ctx):
    if ctx.is_private:
        show_group_picker(ctx, "st:o", "⚙️ تنظیمات کدوم گروه؟")
    elif ctx.is_admin():
        show_settings(ctx, ctx.chat_id)
    else:
        ctx.answer("⛔ تنظیمات فقط دست ادمین‌هاست", True)


@command("settings")
def cmd_settings(ctx):
    if ctx.is_private:
        show_group_picker(ctx, "st:o", "⚙️ تنظیمات کدوم گروه؟")
    elif ctx.is_admin():
        show_settings(ctx, ctx.chat_id)
    else:
        ctx.reply("⛔ تنظیمات فقط دست ادمین‌هاست")


# ─── راهنما ───
HELP_PAGES = {
    "home": ("ℹ️ راهنمای %s\n\n%s یه عضو دیجیتالیِ گروهه، نه چت‌بات! 😎\n"
             "بازی می‌کنه، جوک و فال میگه، رتبه می‌ده، آمار گروه رو نگه می‌داره و گروه رو مدیریت می‌کنه.\n\n"
             "همه‌چیز با دکمه‌ست. یه موضوع انتخاب کن 👇\n\n"
             "دستورها: /menu منوی اصلی • /help راهنما • /settings تنظیمات (ادمین)") % (BOT_NAME, BOT_NAME),
    "games": ("🎮 بازی‌ها\n\nحدس عدد، حدس کلمه، معما، اطلاعات عمومی، سرعتی، ریاضی، حدس ایموجی، "
              "حدس کشور، سنگ‌کاغذ‌قیچی، تاس، دوئل، بازی گروهی و لیگ هفتگی.\n"
              "هر برد XP و امتیاز لیگ می‌ده. توی دوئل می‌تونی یکی رو به چالش بکشی ⚔️"),
    "fun": ("😂 سرگرمی\n\nجوک‌ها دسته‌بندی دارن و با «😂 خندیدم» یا «😐 خوشم نیومد» امتیاز می‌گیرن؛ "
            "جوک‌های محبوب بیشتر میان. می‌تونی جوک خودت رو بفرستی تا ادمین تأیید کنه.\n"
            "🔮 فال توی ۱۰ دسته‌ست و فقط برای خنده‌ست، پیش‌بینی واقعی نیست!\n"
            "💬 %s به بعضی حرف‌های گروه هم جواب می‌ده؛ روشن/خاموش و احتمالش توی تنظیماته.\n"
            "🧪 آزمایشگاه (داخل سرگرمی): رأی‌گیری آماری، معمای گروهی، چالش سریع و مأموریت مخفی گروه." % BOT_NAME),
    "rank": ("🏆 رتبه‌ها و مأموریت‌ها\n\nXP و Level، مدال‌ها (چندتاش مخفیه 🕵️) و لیگ هفتگی که هر هفته "
             "ریست می‌شه ولی رکوردهای تاریخی می‌مونن.\n"
             "🎯 مأموریت‌های روزانه و هفتگی با پاداش XP و مدال."),
    "memory": ("🧠 حافظه و DNA\n\n%s پیام‌های گروه رو ذخیره نمی‌کنه؛ فقط آمار سبک مثل ساعت اوج، "
               "بازی و جوک محبوب.\nاز همین آمار «DNA گروه» ساخته می‌شه: طنز، بازی، فعالیت و رقابت.\n"
               "🧪 آزمایشگاه هم نظرسنجی و چالش گروهیه." % BOT_NAME),
    "admin": ("🛡️ مدیریت (مخصوص ادمین‌ها)\n\nضد اسپم، ضد فلود، ضد لینک، فیلتر کلمات، اخطار، سکوت، بن، "
              "قوانین، خوشامد و خداحافظی، گزارش پیام‌ها و لاگ‌ها.\n"
              "هر عضو می‌تونه با ریپلای روی پیام نامناسب و زدن /report اون رو به ادمین گزارش بده.\nقوانین: /rules"),
}
HELP_TOPICS = [("🎮 بازی‌ها", "games"), ("😂 سرگرمی", "fun"), ("🏆 رتبه‌ها", "rank"),
               ("🧠 حافظه", "memory"), ("🛡️ مدیریت", "admin")]


def help_markup(page):
    rows, cur = [], []
    for label, key in HELP_TOPICS:
        if key == page:
            continue
        cur.append(btn(label, "hp:" + key))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    if page != "home":
        rows.append([btn("ℹ️ راهنما", "hp:home")])
    rows.append(home_row())
    return kb(*rows)


def show_help(ctx, page="home"):
    page = page if page in HELP_PAGES else "home"
    show(ctx, HELP_PAGES[page], help_markup(page))


@callback("hp")
def cb_help(ctx):
    show_help(ctx, ctx.parts[1] if len(ctx.parts) > 1 else "home")


@section("help")
def sec_help(ctx):
    show_help(ctx, "home")


@command("help")
def cmd_help(ctx):
    show_help(ctx, "home")


# ─── SELFTEST فاز ۲ ───
def _p2_cb(cb_id, data, user_id, chat_id, message_id, ctype="group"):
    return {"update_id": 1, "callback_query": {
        "id": cb_id, "from": {"id": user_id, "first_name": "U%d" % user_id}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": chat_id, "type": ctype, "title": "G%d" % chat_id}}}}


def _p2_msg(text, user_id, chat_id, ctype="group", mid=1):
    return {"update_id": 1, "message": {
        "message_id": mid, "date": now(), "text": text, "from": {"id": user_id, "first_name": "U%d" % user_id},
        "chat": {"id": chat_id, "type": ctype, "title": "G%d" % chat_id}}}


def _p2_make_admin(chat_id, user_id):
    ADMIN_CACHE[(chat_id, user_id)] = (True, time.monotonic() + 300)


def _p2_calls(api, name):
    return [c for c in api.calls if c[0] == name]


@selftest("کاتالوگ‌ها: بازی/جوک/فال و Seed")
def _t_p2_catalog():
    check(len(GAME_CATALOG) == 13 and len({k for k, _, _ in GAME_CATALOG}) == 13, "games")
    check(len(JOKE_CATEGORIES) == 14 and len({k for k, _, _ in JOKE_CATEGORIES}) == 14, "joke cats")
    check(len(FORTUNE_CATEGORIES) == 10 and len({k for k, _, _ in FORTUNE_CATEGORIES}) == 10, "fortune cats")
    seed_catalog()
    seed_catalog()
    check(APP.db.val("SELECT COUNT(*) FROM games") == 13, "seed duplicated")
    APP.db.run("UPDATE games SET plays=7 WHERE game_key='dice'")
    seed_catalog()
    check(APP.db.val("SELECT plays FROM games WHERE game_key='dice'") == 7, "plays reset by seed")
    return "ok"


@selftest("منوی اصلی: فقط دکمه‌های فعال، طول callback")
def _t_p2_menu():
    m = main_menu_markup()
    keys = []
    for row in m["inline_keyboard"]:
        check(1 <= len(row) <= 2, "row size")
        for b in row:
            check(len(b["callback_data"].encode()) <= CALLBACK_DATA_MAX, "callback too long")
            key = b["callback_data"].split(":", 1)[1]
            check(key in SECTIONS, "button without handler: " + key)
            keys.append(key)
    check("settings" in keys and "help" in keys, "core buttons missing")
    return "buttons: %s" % ",".join(keys)


@selftest("/start در گروه و پیوی")
def _t_p2_home():
    api = APP.api
    handle_update(_p2_msg("/start", 9101, -9101))
    sent = _p2_calls(api, "send_message")
    check(sent, "nothing sent")
    text, markup = sent[-1][1][1], sent[-1][1][2]
    check("🧠 %s" % BOT_NAME in text and "اعضای گروه" in text and "فعالیت امروز" in text, "group menu text")
    check("بازی امروز" in text and "سرگرمی امروز" in text and "سیستم فعال" in text, "menu lines")
    check(markup and markup["inline_keyboard"], "no keyboard")
    handle_update(_p2_msg("/menu", 9102, 9102, ctype="private"))
    text2 = _p2_calls(api, "send_message")[-1][1][1]
    check("سلام" in text2 and "U9102" in text2, "private greeting")
    return "ok"


@selftest("تنظیمات: Toggle فقط توسط ادمین + لاگ")
def _t_p2_settings():
    api = APP.api
    g, admin_u, user_u = -9201, 9201, 9202
    upsert_group({"id": g, "title": "T", "type": "group"})
    _p2_make_admin(g, admin_u)
    ADMIN_CACHE[(g, user_u)] = (False, time.monotonic() + 300)
    before = get_settings(g)["games"]
    handle_update(_p2_cb("p2-s1", "st:t:%d:games" % g, user_u, g, 11))
    check(get_settings(g)["games"] == before, "non-admin changed setting")
    handle_update(_p2_cb("p2-s2", "st:t:%d:games" % g, admin_u, g, 12))
    check(get_settings(g)["games"] == (0 if before else 1), "admin toggle failed")
    check(APP.db.val("SELECT COUNT(*) FROM logs WHERE kind='setting'") == 1, "no log")
    check(_p2_calls(api, "edit_message_text"), "settings panel not refreshed")
    handle_update(_p2_cb("p2-s3", "st:t:%d:goodbye_on" % g, admin_u, g, 13))
    check(get_settings(g)["goodbye_on"] == 0, "non-whitelisted key toggled via button")
    handle_update(_p2_cb("p2-s4", "st:t:%d:games" % -9999, admin_u, g, 14))
    check(get_settings(g)["games"] == (0 if before else 1), "cross-group toggle worked")
    return "ok"


@selftest("تنظیمات: احتمال پاسخ و محدوده")
def _t_p2_chance():
    g, u = -9211, 9211
    upsert_group({"id": g, "title": "T", "type": "group"})
    _p2_make_admin(g, u)
    check(get_settings(g)["reply_chance"] == 35, "default chance")
    handle_update(_p2_cb("p2-c0", "st:c:%d:5" % g, u, g, 100))
    check(get_settings(g)["reply_chance"] == 40, "step up")
    set_setting(g, "reply_chance", 95)
    handle_update(_p2_cb("p2-c1", "st:c:%d:5" % g, u, g, 101))
    check(get_settings(g)["reply_chance"] == 100, "reach 100")
    handle_update(_p2_cb("p2-c2", "st:c:%d:5" % g, u, g, 102))
    check(get_settings(g)["reply_chance"] == 100, "clamp at 100")
    set_setting(g, "reply_chance", 5)
    handle_update(_p2_cb("p2-d0", "st:c:%d:-5" % g, u, g, 200))
    check(get_settings(g)["reply_chance"] == 0, "reach 0")
    handle_update(_p2_cb("p2-d1", "st:c:%d:-5" % g, u, g, 201))
    check(get_settings(g)["reply_chance"] == 0, "clamp at 0")
    handle_update(_p2_cb("p2-e", "st:c:%d:50" % g, u, g, 300))
    check(get_settings(g)["reply_chance"] == 0, "arbitrary delta accepted")
    return "ok"


@selftest("تنظیمات از پیوی: انتخاب گروه")
def _t_p2_picker():
    api = APP.api
    g, u = -9221, 9221
    upsert_group({"id": g, "title": "گروه من", "type": "group"})
    STATS.user_touch(g, u)
    STATS.flush()
    _p2_make_admin(g, u)
    handle_update(_p2_cb("p2-p1", "st:pick", u, u, 5, ctype="private"))
    edits = _p2_calls(api, "edit_message_text")
    check(edits, "picker not shown")
    markup = edits[-1][1][3]
    cbs = [b["callback_data"] for r in markup["inline_keyboard"] for b in r]
    check("st:o:%d" % g in cbs, "group not offered: %s" % cbs)
    handle_update(_p2_cb("p2-p2", "st:o:%d" % g, u, u, 6, ctype="private"))
    check("تنظیمات" in _p2_calls(api, "edit_message_text")[-1][1][2], "private settings not rendered")
    stranger = 9222
    handle_update(_p2_cb("p2-p3", "st:t:%d:games" % g, stranger, stranger, 7, ctype="private"))
    check(get_settings(g)["games"] == 1, "stranger changed settings from private")
    return "ok"


@selftest("راهنما: همه صفحه‌ها و ناوبری")
def _t_p2_help():
    api = APP.api
    for page, text in HELP_PAGES.items():
        check(len(text) > 40, page)
        for row in help_markup(page)["inline_keyboard"]:
            for b in row:
                check(len(b["callback_data"].encode()) <= CALLBACK_DATA_MAX, "cb len")
    handle_update(_p2_cb("p2-h1", "hp:games", 9231, -9231, 21))
    check("بازی‌ها" in _p2_calls(api, "edit_message_text")[-1][1][2], "help page not shown")
    handle_update(_p2_cb("p2-h2", "menu:help", 9231, -9231, 22))
    check("راهنمای" in _p2_calls(api, "edit_message_text")[-1][1][2], "menu:help")
    handle_update(_p2_cb("p2-h3", "menu:home", 9231, -9231, 23))
    check("اعضای گروه" in _p2_calls(api, "edit_message_text")[-1][1][2], "back to home")
    return "ok"


@selftest("دسترسی بخش‌ها: خاموش بودن و ناشناخته")
def _t_p2_gates():
    api = APP.api
    g, u = -9241, 9241
    upsert_group({"id": g, "title": "T", "type": "group"})
    ADMIN_CACHE[(g, u)] = (False, time.monotonic() + 300)
    handle_update(_p2_cb("p2-g1", "menu:settings", u, g, 31))
    check(not _p2_calls(api, "edit_message_text"), "non-admin opened settings")
    handle_update(_p2_cb("p2-g2", "menu:nonexistent", u, g, 32))
    answers = [c for c in api.calls if c[0] == "answer_callback_query"]
    check(any(c[1][2] for c in answers), "unknown section must alert")
    class _C:  # ctx حداقلی برای feature_enabled
        is_private, chat_id = False, g
    set_setting(g, "jokes", 0)
    check(feature_enabled(_C, "jokes") is False and feature_enabled(_C, "games") is True, "feature flags")
    check(STATS.g.get((g, "feat:nonexistent")) is None, "unknown section counted as feature use")
    return "ok"


@selftest("زمان/انتخاب روزانه: day_start و daily_pick")
def _t_p2_daily():
    ts = int(datetime.datetime(2026, 10, 4, 12, 0, tzinfo=datetime.timezone.utc).timestamp())
    ds = day_start(ts)
    check(ds == int(datetime.datetime(2026, 10, 3, 20, 30, tzinfo=datetime.timezone.utc).timestamp()), "day_start")
    check(day_key(ds) == "2026-10-04" and day_key(ds - 1) == "2026-10-03", "boundary")
    a = daily_pick(-5, GAME_CATALOG, "game")
    check(a == daily_pick(-5, GAME_CATALOG, "game") and a in GAME_CATALOG, "daily_pick")
    return "ok"


@selftest("اضافه شدن ربات به گروه: خوشامد")
def _t_p2_bot_added():
    api = APP.api
    old = APP.bot_id
    APP.bot_id = 555000
    try:
        msg = {"message_id": 1, "date": now(), "chat": {"id": -9251, "type": "group", "title": "NewG"},
               "from": {"id": 9251, "first_name": "Owner"},
               "new_chat_members": [{"id": 555000, "is_bot": True, "first_name": "bot"}]}
        handle_update({"update_id": 5, "message": msg})
    finally:
        APP.bot_id = old
    check(APP.db.val("SELECT title FROM groups WHERE chat_id=-9251") == "NewG", "group not registered")
    sent = _p2_calls(api, "send_message")
    check(sent and "منوی اصلی" in json.dumps(sent[-1][1][2], ensure_ascii=False), "welcome not sent")
    return "ok"


# ══════════════════════════════════════════════════════════════════
# ███ END OF PHASE 2


# ══════════════════════════════════════════════════════════════════
# ███ PHASE 3 — RESPONSE ENGINE / JOKE SYSTEM / FORTUNE
# ══════════════════════════════════════════════════════════════════

# ─── ۱۰۰ Trigger/Response تأییدشده (همان متن‌های قبلی) ───
# [آیدی یکی از اعضا] و [کلمه] هنگام پاسخ جایگزین می‌شوند.
TRIGGER_DATA = [
    ("پول", ("پیدا کردی به منم بده 😭", "پیدا شد منم خبر کن، دوتایی خوشحال شیم 😂")),
    ("گشنمه", ("ما که فقط داده می‌خوریم 😎", "منم گشنمه، ولی نهایتاً می‌تونم برات متن بفرستم 😭")),
    ("بیا پیوی", ("پیوی خطرناکه همینجا بگو 😭", "نرو اینجا حداقل شاهد داریم 👀")),
    ("کی [کلمه]", ("به نظر من [آیدی یکی از اعضا] [کلمه]عه", "رأی من [آیدی یکی از اعضا]، بقیه با خودتون 😂")),
    ("سلام", ("سلام دختری؟ 😂", "سلام، بالاخره پیدات شد 👀")),
    ("خوبی", ("تو خوبی که اومدی سراغ من؟ 👀", "خوبم، فعلاً سیستم پایدار گزارش شده 😂")),
    ("چه خبر", ("خبر خاصی نیست جز اینکه هنوز آنلاینم", "هیچی، منتظریم یکی یه چیزی رو خراب کنه 😭")),
    ("خوابم میاد", ("بخواب من نگهبانی میدم 🫡", "برو بخواب، فردا هم بله سر جاشه 😂")),
    ("حوصلم سر رفته", ("گروه رو اشتباه اومدی اینجا حوصله هم نداریم 😭", "بیا یه نفر رو الکی متهم کنیم، سرگرم میشی 😂")),
    ("کجام", ("توی گپی که الان نباید اینقدر جدی بگیریش", "جایی که اینترنت هست و راه برگشت هم هست 👀")),
    ("کی بیداره", ("کسی که این پیامو خوند ولی جواب نداد 👀", "هرکی جواب بده مدرک زنده بودنشه 😂")),
    ("کی خوابه", ("اونی که الان آنلاین زده 😭", "هرکی جواب نده، مظنون اصلیه 👀")),
    ("کی ادمینه", ("اونی که الان داره پیام منو پاک می‌کنه 💀", "همونی که وقتی پیداش میشه همه یهو مؤدب میشن 😂")),
    ("کی خفنه", ("فعلاً خودم رو بذار کنار 😎", "کسی که هنوز از این گپ فرار نکرده 😂")),
    ("کی بدبخته", ("نفر بعدی که پیام میده 😭", "هرکی الان فکر می‌کنه قراره اتفاق خوبی بیفته 😂")),
    ("کی اسکله", ("به نظر من [آیدی یکی از اعضا] اسکله", "اسم نمی‌برم، ولی [آیدی یکی از اعضا] خودش می‌دونه 😂")),
    ("کی دیوونه‌ست", ("سؤال سختی پرسیدی 😂", "جواب زیاده، ظرفیت پیام کمه 😭")),
    ("کی تنبله", ("کسی که این پیامو خونده و جواب نداده", "هرکی کار داره ولی آنلاین مونده 😂")),
    ("کی گشنشه", ("نصف گروه، بدون شک 😭", "هرکی اینو خوند و رفت سمت آشپزخونه 😂")),
    ("کی پولداره", ("اگه پیدا کردی به من معرفی کن", "منم دنبالش می‌گردم، اگه زودتر پیدا کردی صدام کن 😭")),
    ("کی پایه‌ست", ("اون کسی که هنوز از گروه نرفته 😂", "کسی که با اولین «بریم؟» میگه بریم 👀")),
    ("کی مشکوکه", ("اون که زیادی ساکته 👀", "کسی که همه چی رو می‌بینه ولی هیچی نمیگه 😂")),
    ("کی جاسوسه", ("من چیزی نمی‌دونم و چیزی هم نگفتم 🤐", "اونی که بیشتر از همه می‌دونه ولی ساکته 👀")),
    ("کی بچه‌ست", ("هرکی با این سؤال مشکل داره 😭", "کسی که الان داره میگه «من نیستم» 😂")),
    ("کی خسیسه", ("کسی که هنوز پولی به من نداده", "هرکی این پیامو خوند و حسابشو قایم کرد 😂")),
    ("کی باهوشه", ("کسی که الان سکوت کرده و داره ما رو نگاه می‌کنه", "اونی که فهمید جواب این سؤال خودش یه تله‌ست 👀")),
    ("کی بدشانسه", ("برنده بعدی بازی 😭", "کسی که گفت «این دفعه قطعاً می‌برم» 😂")),
    ("کی خوش‌شانسه", ("فعلاً قرعه به اسم من نیفتاده 😭", "اونی که هنوز باختاش شروع نشده 😂")),
    ("کی معروفه", ("اونی که اسمش بیشتر از همه اینجا میاد", "کسی که بدون تگ هم همه می‌شناسنش 👀")),
    ("کی قدیمیه", ("اونی که هنوز از ما ناامید نشده 🫡", "کسی که خاطراتی داره که نباید تعریف کنه 😂")),
    ("کی جدیده", ("هرکی هنوز فکر می‌کنه اینجا جای عادی‌ایه 😭", "اونی که هنوز نمی‌دونه چه اشتباهی کرده 👀")),
    ("کی دیوونه‌ست", ("من اسامی رو لو نمی‌دم 😂", "جواب این یکی رو خود گروه بهتر می‌دونه 😭")),
    ("کی پول داره", ("سؤال امنیتی بود؟ 👀", "من اطلاعات مالی اعضا رو منتشر نمی‌کنم 😂")),
    ("کی بیکار", ("من که رسماً شغلم جواب دادنه 😎", "هرکی الان داره این پیامو می‌خونه و هیچ کاری نداره 😂")),
    ("کی آنلاین", ("تو که پرسیدی، احتمالاً خودت 👀", "هرکی جواب بده خودش رو لو داده 😂")),
    ("کی نیست", ("هرکی بعداً میاد و میگه «چی شد؟»", "اونی که همیشه پنج دقیقه بعد می‌پرسه چی شده 😭")),
    ("کی معروف نیست", ("من، چون هنوز حقوق نگرفتم 😭", "هرکی اسمشو سرچ کنی فقط خودش میاد 😂")),
    ("کی خوشگله", ("من وارد این پرونده نمی‌شم 👀", "رأی‌گیری عمومی لازم داره 😂")),
    ("کی زشته", ("من اهل افشاگری نیستم 👀", "پرونده‌ش محرمانه‌ست، بعدی 😂")),
    ("کی مظلومه", ("اونی که ادمین باهاش شوخی نداره 😭", "کسی که همیشه میگه «من کاری نکردم» 😂")),
    ("ادمین کجاست", ("احتمالاً داره پیام منو بررسی می‌کنه 💀", "همون جاییه که وقتی لازم داریم نیست 😂")),
    ("ادمین بیا", ("صداش کردی، خودش میاد؛ من مسئول نیستم 😭", "احضاریه صادر شد، منتظر حضور متهم باشید 👀")),
    ("ادمین خوابه", ("بهترین زمان برای حرف زدنه 👀", "تا بیدار نشده هرچی می‌خوای بگو 😂")),
    ("ادمین بیداره", ("پس مراقب حرفات باش 😭", "تبریک میگم، دوره آزادی تموم شد 😂")),
    ("بنم کن", ("من چرا باید همچین آرزویی رو برآورده کنم؟ 😭", "خودت اینقدر عجله داری؟ 😂")),
    ("بنم نکن", ("پس درست اومدی 😎", "بالاخره یکی قدر آزادی رو فهمید 😂")),
    ("کی بن میشه", ("من اطلاعات محرمانه نمی‌دم 👀", "هنوز کمیته تصمیم نگرفته 😂")),
    ("اخراجش کن", ("جلسه دادگاه تشکیل شد، لطفاً صبر کنید 🧑‍⚖️", "درخواست شما ثبت شد، متهم فعلاً آزاد است 😂")),
    ("ساکت", ("خودت شروع کردی 😭", "من ساکتم، این شماها هستید که ادامه میدید 😂")),
    ("خفه شو", ("بعد از شما 😌", "اول شما، من پشت سرتون میام 😂")),
    ("جوک بگو", ("یکی بلده جوک بگه ولی خودش هنوز نفهمیده 😂", "جوک بگم یا آبروی گروه رو حفظ کنیم؟ 😭")),
    ("حوصله ندارم", ("دو نفر شدیم 😭", "پس امروز روز مناسبی برای این گپ نیست 😂")),
    ("خوابم نمیبره", ("چون هنوز اینترنت روشنه 😎", "گوشی رو بذار کنار، شاید جواب داد 😂")),
    ("گرسنمه", ("این گروه حتی نون هم نداره 😭", "یکی پولدار اینجا نیست یه چیزی سفارش بدیم؟ 😂")),
    ("تشنمه", ("آب بخور قهرمان، من فقط متن دارم 😭", "بطری آب رو من نمی‌تونم از صفحه بیارم 😂")),
    ("خسته‌ام", ("از زندگی یا از گروه؟ 👀", "استراحت کن، گروه فعلاً بدون تو زنده می‌مونه 😂")),
    ("اعصاب ندارم", ("اینجا دقیقاً جای مناسبی برای تست اعصابه 😭", "پس امروز وارد بحث نشو، به نفع همه‌ست 😂")),
    ("کلافه‌ام", ("صبر کن، گروه هنوز شروع نکرده 😭", "هنوز اتفاق اصلی مونده 😂")),
    ("بدبختم", ("حداقل تنها نیستی 😭", "بیا بشین کنار بقیه بدبختا 😂")),
    ("بی‌پولم", ("خوش اومدی به باشگاه 😎", "کارت عضویتت رو صادر کردیم 😭")),
    ("پول بده", ("اول خودت الگو باش 😭", "منم منتظرم یکی به من بده 😂")),
    ("قرض میدی", ("من خودم قرض می‌خوام 😭", "با چه اعتمادی اینو از من پرسیدی؟ 😂")),
    ("پول ندارم", ("موجودی حساب: یک عدد امید واهی", "موجودی: در حد «ان‌شاءالله ماه بعد» 😭")),
    ("می‌میرم", ("نه بابا، اول بازی امشبو تموم کن 😭", "فعلاً بمون، هنوز کلی کار داریم 😂")),
    ("حالم بده", ("امیدوارم زودتر بهتر شی ❤️", "بیا فعلاً یه چیزی بخندیم شاید بهتر شد 😂")),
    ("حالم خوبه", ("پس امروز سالم به مقصد رسیدیم 😎", "خدا رو شکر، یکی حداقل اوضاعش خوبه 😂")),
    ("عاشقم", ("من شاهد بودم، ادامه بده 👀", "اسم نمی‌پرسم، خودت لو نده 😂")),
    ("عاشق کیم", ("پرونده محرمانه‌ست 👀", "من نمی‌گم، ولی خودت احتمالاً می‌دونی 😂")),
    ("کی منو دوست داره", ("کسی که هنوز پیام‌هات رو می‌خونه 👀", "اونی که بعد از دیدن این پیام فرار نکنه 😂")),
    ("کی ازم بدش میاد", ("احتمالاً همون کسی که سین می‌کنه 😭", "اسم نمی‌برم، هنوز توی گروهیم 😂")),
    ("بیا پیوی", ("نه داداش، همینجا شاهد داریم 😭", "پیوی نرو، بعداً میگن چی شد 👀")),
    ("پیوی بیا", ("من از محدوده عمومی خارج نمی‌شم 😭", "همینجا بگو، بقیه هم سهمی از داستان داشته باشن 😂")),
    ("شمارتو بده", ("برای چی؟ همینجا اینترنت داریم 😭", "اول بگو با شماره من چه نقشه‌ای داری 👀")),
    ("زنگ بزن", ("من تماس صوتی نیستم، متنم 😎", "قابلیت زنگ زدن هنوز به من نرسیده 😂")),
    ("بیا بیرون", ("من همینجا ریشه زدم 😭", "بیرون؟ من از این گپ مجوز خروج ندارم 😂")),
    ("بریم", ("کجا؟ من لوکیشن ندارم 👀", "بریم، ولی هزینه رفت‌وآمد با تو 😂")),
    ("فرار کنیم", ("از چی؟ از گروه؟ 👀", "اول بگو کجا، بعد فرار می‌کنیم 😂")),
    ("بیا بازی", ("بالاخره یک پیشنهاد عاقلانه 😎", "آماده‌ام، فقط بعد باختن گریه نکن 😭")),
    ("کی بازی میاد", ("اونی که حوصله باختن داره 😭", "هرکی اعتمادبه‌نفسش بیشتر از مهارتشه 😂")),
    ("بازی کنیم", ("انتخابت بالاخره درست شد 🫡", "بزن بریم، ببینیم کی قراره آبروشو از دست بده 😂")),
    ("چیکار میکنی", ("دارم به آینده این گروه فکر می‌کنم 😭", "منتظرم یکی یه سؤال عجیب‌تر بپرسه 😂")),
    ("بیکارم", ("خوش اومدی، منم شیفتم اینجاست 😎", "بیا حداقل بیکاریمون گروهی باشه 😂")),
    ("کسی هست", ("نه، این پیام رو روح‌ها جواب دادن 👻", "همه هستن، فقط کسی نمی‌خواد اول جواب بده 😂")),
    ("کسی بیداره", ("نه، همه خوابن و دارن تایپ می‌کنن 😭", "هرکی بیداره یه چیزی بگه تا لو بره 👀")),
    ("کی هست", ("همینایی که الان فرار نکردن", "هرکی هنوز اینجاست، خودش رو حساب کن 😂")),
    ("چی شد", ("هنوز هیچی، ولی صبر کن 😭", "تو نبودی، داستان از دستت رفت 😂")),
    ("چی میگی", ("منم دقیقاً همین سؤال رو از خودم دارم", "مهم اینه با اعتمادبه‌نفس گفتم 😂")),
    ("چرا", ("چون چرا 😭", "چون یه دلیلی باید داشته باشه دیگه 😂")),
    ("چجوری", ("با اعتماد به نفس و کمی شانس 😎", "روشـش محرمانه‌ست، لو نمی‌دیم 😂")),
    ("مگه میشه", ("توی این گروه؟ بله 😭", "اینجا چیزای عجیب‌تری هم میشه 😂")),
    ("نه", ("خیلی قاطع بود، ترسیدم 😭", "باشه، دادگاه تجدیدنظر هم نداریم 😂")),
    ("آره", ("تصمیم تاریخی ثبت شد 🫡", "بالاخره به نتیجه رسیدیم 😂")),
    ("باشه", ("این «باشه» بوی دردسر میده 👀", "باشه گفتی ولی من اعتماد نکردم 😂")),
    ("اوکی", ("اوکی ثبت شد، ادامه بده 😎", "خیلی خب، پرونده بسته شد 😂")),
    ("نمیدونم", ("بالاخره یکی راستشو گفت 😭", "منم نمی‌دونم، پس دوتایی شدیم 😂")),
    ("ولش کن", ("منم همین الان ولش کردم 😭", "باشه، این پرونده رفت بایگانی 😂")),
    ("بیخیال", ("بیخیال شدیم، بعدی 👋", "باشه، این یکی رو می‌ذاریم پای باد 😂")),
    ("ممنون", ("انجام وظیفه بود، حقوقم رو کی میدی؟ 😭", "قابلی نداشت، فقط پولشو یادت نره 😂")),
    ("دمت گرم", ("دمت گرم‌تر، ولی من باتم 😂", "قابل نداشت، بالاخره یه کاری بلدیم 😎")),
    ("خدافظ", ("رفتی؟ پس چرا هنوز آنلاین زدی؟ 👀", "خدافظ، ولی می‌دونم دوباره برمی‌گردی 😂")),
]


# ─── بانک جوک (کوتاه، خودمانی، بدون محتوای جنسی/خطرناک/توهین سنگین) ───
JOKE_BANK = {
    "daily": [
        "ساعت ۶ صبح آلارم زد. گفتم یه ربع دیگه. الان ظهره و هنوز دارم همون یه ربع رو می‌خوابم 😴",
        "دکتر گفت ورزش کنی عمرت بیشتر میشه. ولی نگفت اون عمر اضافه رو باید صرف ورزش کردن کنی 😭",
        "مامانم گفت برو یه چیزی بخر بیا. رفتم، یادم نیومد چی بود، با سه تا چیز بی‌ربط برگشتم 😂",
        "هر شب میگم از فردا رژیم می‌گیرم. فردا میشه همون روزی که سه تا ساندویچ می‌خورم تا با انرژی شروع کنم.",
        "برای بیدار شدن هشت تا آلارم گذاشتم. حالا به جای یه بار، هشت بار بیدار میشم و هشت بار دوباره می‌خوابم.",
        "چایی ریختم گذاشتم یکم خنک شه. الان سومین باره دارم چایی سرد رو گرم می‌کنم ☕",
        "مامان: ساعت چنده؟\nمن: دو.\nمامان: پس چرا نخوابیدی؟\nمن: چون می‌دونم ساعت چنده.",
        "همیشه میگم یه ساعت دیگه شروع می‌کنم. ساعت‌ها گذشته و من هنوز همون یه ساعت دیگه‌ام.",
        "ظرفا رو شستم و حس کردم یه آدم تازه‌ام. بعد دیدم هنوز سه تا لیوان تو اتاقمه 😂",
        "کارامو گذاشتم برای فردا. فردا هم گذاشتم برای فردا. دارم یه سیستم پایدار می‌سازم.",
    ],
    "school": [
        "معلم: کی تکلیفشو انجام نداده؟\nهمه بلند شدن.\nمعلم: ... پس این بار تقصیر منه؟",
        "امتحان ریاضی بود. دو ساعت به ورقه نگاه کردم. ورقه هم به من نگاه کرد. هیچ‌کدوممون حرفی برای گفتن نداشتیم.",
        "معلم: از چی بیشتر می‌ترسی؟\nمن: از کارنامه.\nمعلم: چرا؟\nمن: چون مامانم هم اونو می‌بینه.",
        "تو مدرسه میگی کاش تعطیل شه، تو تعطیلی میگی کاش مدرسه بود. دانش‌آموز موجود پیچیده‌ایه 😭",
        "معلم: بچه‌ها یه خاطره تعریف کنید.\nمن: خاطره‌ی من امتحانه.\nمعلم: خب؟\nمن: بقیه‌ش یادم نیست.",
        "زنگ آخر که میشه، همه‌ی بچه‌ها یهو آدمای متمرکزی میشن که فقط به ساعت نگاه می‌کنن.",
        "معلم: چرا دیر اومدی؟\nمن: تابلو نوشته بود «مدرسه، آهسته برانید».",
        "گفتم از ساعت ۸ شب درس می‌خونم. ساعت ۸ شد، گفتم پس از ۹. الان ۳ صبحه و دارم فیلم می‌بینم که درس خوندن یعنی چی.",
        "از بغل‌دستیم پرسیدم جواب سؤال ۴ چیه. گفت منم می‌خواستم از تو بپرسم. اینجا فهمیدیم هر دو در یک مسیریم 🤝",
        "کیف مدرسه‌م اینقدر سنگینه که فکر کنم خودش بیشتر از من درس می‌خونه 🎒",
    ],
    "family": [
        "بابام: گوشی کمتر.\n(و بعدش خودش دو ساعت ویدیوی ماشین‌های قدیمی می‌بینه)",
        "مامانم میگه فقط ۵ دقیقه کمکم کن. ۲ ساعت بعد هنوز دارم کمک می‌کنم.",
        "هر وقت مامانم میگه «فقط یه چیز ازت می‌خوام»، می‌دونم لیست بلندی در راهه 😭",
        "داداشم: یه لحظه گوشیتو بده.\nاون «یه لحظه»‌ش الان ۴۵ دقیقه‌ست.",
        "خواهرم میگه من مرتبم. اتاقش اینقدر مرتبه که فقط خودش می‌دونه چیزا کجان.",
        "مامان: چی می‌خوای شام؟\nمن: هرچی.\nمامان: نمیشه.\nمن: خب هرچی نه.\nمامان: پس هرچی بخور.",
        "بابام: ما بچه که بودیم ۵ کیلومتر پیاده می‌رفتیم مدرسه.\nمن: ولی اینترنت نداشتین.\nبابام: ... حرف حسابی زدی.",
        "مامانم: یه لحظه بیا اینجا.\nاون «یه لحظه» یعنی اون کوه ظرف رو ببین 😂",
        "هر وقت مامانم میگه آروم بشینیم صحبت کنیم، می‌دونم اتفاقی افتاده که من هنوز خبر ندارم.",
        "عمو: درسا چطوره؟\nمن: خوبه.\nعمو: معدل؟\nمن: ... خوبه.",
    ],
    "money": [
        "حقوقم مثل مهمون ناخونده‌ست؛ نیومده، خداحافظی می‌کنه.",
        "حساب بانکیم رو چک کردم، گفتم لابد عدد اشتباهی نشون میده. دوباره چک کردم. همون بود 😭",
        "اول ماه: بریم رستوران!\nآخر ماه: بیا نون و پنیر بخوریم، اینجوری خانوادگیه.",
        "پول توی جیبم مثل جن می‌مونه؛ هر وقت دنبالش می‌گردم، نیست.",
        "رفتم بانک گفتم وام می‌خوام. گفت ضامن داری؟ گفتم آره، امید. گفت امید مشخصات داره؟ گفتم نه 😂",
        "قیمتا اینقدر رفتن بالا که الان اگه چیزی ارزون ببینم شک می‌کنم یه جای کار می‌لنگه.",
        "خریدم چون تخفیف داشت. حالا دارم می‌گردم ببینم چرا خریدمش.",
        "پس‌انداز یعنی «از فردا شروع می‌کنم». شنیدم فردا هم یه روز مهمه.",
        "میگن پول خوشبختی نمیاره. ولی خودم می‌خوام امتحان کنم تا مطمئن شم.",
        "کیف پولم اینقدر خالیه که اگه یه ریال بندازم توش، صداش می‌پیچه 🪙",
    ],
    "phone": [
        "باتری گوشیم ۱٪ بود، شارژر رو پیدا کردم، برق رفت. زندگی بعضی وقتا اینقدر مصمم میشه 😭",
        "گوشیم رو دستم گرفتم و داشتم دنبالش می‌گشتم 🤦",
        "هر وقت میگم فقط پنج دقیقه گوشی، چشمم که باز میشه ساعت ۳ بامداده.",
        "گوشی جدید خریدم که دوربینش خوبه. هنوز هیچ چیز قشنگی عکس نگرفتم، فقط کلی عکس از خودم با چشمای بسته.",
        "حافظه‌ی گوشیم پر شده. ۵۰۰ تا اسکرین‌شات «بعداً می‌خونم» توشه.",
        "وقتی گوشی ۲٪ شارژ داره و داری با یکی حرف می‌زنی، اون لحظه واقعاً مهمه 😅",
        "گوشیم افتاد، صفحه‌ش نشکست؛ ولی قلبم چرا.",
        "آپدیت جدید اومد، گفت ۴۵ دقیقه صبر کن. نمی‌دونست من ۴۵ دقیقه‌ی دیگه هم کاری ندارم.",
        "گوشیمو گذاشتم رو حالت پرواز که تمرکز کنم. فقط خودم پرواز کردم؛ سمت اینستا 😂",
        "شارژر رو که پیدا کردم، فهمیدم همه‌ی شارژرای خونه مال گوشی‌های دیگه‌ست.",
    ],
    "internet": [
        "نت وقتی میگه «متصل شد» یعنی ۵۰٪ شانس داری که صفحه باز بشه.",
        "نت قطع شد. اول به خودم شک کردم، بعد به مودم، بعد به زندگی.",
        "تو شبکه اجتماعی ۵ دقیقه رفتم. دو ساعت بعد هنوز دارم ویدیوی آشپزی می‌بینم که هیچ‌وقت نمی‌پزمش.",
        "نت ۱۰ مگ داره، ولی هر وقت لازمه روی ۲ کیلوبیت کار می‌کنه.",
        "رمز وای‌فای همسایه رو پیدا کردم! ... همون موقع رمزشو عوض کرده بود.",
        "صفحه باز نمیشه، نت هم قطع نیست، سرور هم خرابه. خب پس مقصر خودمم دیگه.",
        "تو اینترنت هرچی که می‌خواستی پیدا نمی‌کنی، ولی ۲۰ تا چیز دیگه یاد می‌گیری.",
        "عکس پروفایل‌ها همه فرشته‌ان، تا وقتی که کامنت بذاری.",
        "ویدیو رو دانلود کردم که بعداً ببینم. این «بعداً» از ۲۰۲۲ تا الان ادامه داره.",
        "هر وقت میگن «نت رو ریستارت کن»، حس می‌کنم دارم به دکتر میگم یه دور دیگه امتحان کنیم.",
    ],
    "gaming": [
        "تو بازی گفتم «یه دست دیگه و تمام». الان ساعت ۴ صبحه و من هنوز در حال «تمام» کردنم 🎮",
        "دوستم میگه لگ داشتم. سه بار پشت سر هم. من فکر می‌کنم لگ اسم دیگه‌ی مهارتشه 😂",
        "همیشه تیمم اینقدر خوب بازی می‌کنه که حتی اگه ببریم، نصف دلیلش من بودم و نصفش اینکه حریف خوابش گرفته بود.",
        "گیمر واقعی کسیه که کنترلر رو از بالا پرت می‌کنه ولی هیچ‌وقت نمی‌ذاره زمین بخوره.",
        "بازی «فقط ۵ دقیقه» رو نصب کردم. ۵ ساعت بعد هنوز دارم ۵ دقیقه‌شو بازی می‌کنم.",
        "وقتی تو بازی میگن «بیا ازم یاد بگیر» بدون که یه نمایش خنده‌دار در راهه.",
        "کاراکترم ۱۰۰ بار مرد، ولی با اولین صدا زدن مامانم سریع سیو کردم و اومدم.",
        "تو بازی تقلب کردم، ولی هنوز باختم. این یعنی اعتماد به نفس هم کمکی نمی‌کنه.",
        "شارژ کنترلر تموم شد دقیقاً وقتی داشتم می‌بردم. همه‌چی یه نقشه‌ی بزرگه برای ناامید کردن منه 😭",
        "پینگم وقتی که بازی خوبه، بده؛ وقتی که بازی بده، خوبه. هماهنگی نداریم.",
    ],
    "coding": [
        "برنامه‌نویس: کدم کار نمی‌کنه!\nدوستش: قبلاً کار می‌کرد؟\nبرنامه‌نویس: آره.\nدوستش: چی عوض کردی؟\nبرنامه‌نویس: هیچی!",
        "اولین قانون برنامه‌نویسی: اگه کار می‌کنه، دست نزن.",
        "کامنت: «این کد رو بعداً تمیز می‌کنم.»\nتاریخ کامنت: ۳ سال پیش.",
        "وقتی کد بار اول بدون خطا اجرا شد، اول به خودم شک کردم 🤨",
        "برنامه‌نویس‌ها دو دسته‌ان: اونایی که میگن «روی سیستم من کار می‌کنه» و اونایی که هنوز نفهمیدن چرا.",
        "رفتم کد رو تست کنم. چاپ کرد «Hello World». حس کردم یه پیشرفت بزرگ داشتم.",
        "کدی رو نگاه کردم و فهمیدم ۶ ماه پیش نوشتمش. از اینکه کسی اینقدر بد کد می‌نویسه متأسفم... که منم.",
        "باگ پیدا کردم. بعد اسمشو گذاشتم ویژگی و فروختم 😎",
        "یه خط کد عوض کردم، ده جای دیگه خراب شد. مثل دومینو، ولی با گریه 😭",
        "برنامه‌نویس: سه تا کار مونده.\nمدیر: کی تموم میشه؟\nبرنامه‌نویس: ... سه تا کار دیگه هم اضافه شد.",
    ],
    "friends": [
        "رفیقم: امشب بریم بیرون؟\nمن: باشه.\n۳ ساعت بعد: هنوز تو گروه داریم بحث می‌کنیم کجا بریم.",
        "رفیق واقعی اونیه که وقتی میگی «گشنمه» نمیگه «برو بخور»؛ میگه «بیا با هم بریم» 🍕",
        "رفیقم برای قرار همیشه میگه «دارم میام». ۴۰ دقیقه بعد: «دارم لباس می‌پوشم».",
        "دوستم میگه راز نگه می‌داره. تا حالا هر رازی بهش گفتم، ده دقیقه بعد همه می‌دونن 😂",
        "به رفیقم گفتم عکسم قشنگه؟ گفت آره. گفتم واقعاً؟ گفت نه، ولی دوباره پرسیدی.",
        "دو نفر که پول ندارن یه جا جمع میشن و ۳ ساعت راجع به اینکه کجا برن حرف می‌زنن.",
        "رفیقم گفت: یه سؤال دارم، جواب ندی ناراحت نمیشم.\nگفتم: بپرس.\nگفت: ... یادم رفت.",
        "تو گروه وقتی هیچ‌کس جواب نمیده، همه می‌دونن که همه خوندن.",
        "رفیق قدیمی یعنی کسی که هنوز یه عکس زشت ازت داره و تهدیدت نمی‌کنه... فعلاً.",
        "رفیقم شماره‌مو با اسم «همون که پول داد» ذخیره کرده. هنوز نمی‌دونم باید ناراحت باشم یا نه.",
    ],
    "oops": [
        "به جای «ممنون» به معلم گفتم «مامان». کل کلاس ۱۰ دقیقه خندید. من ۱۰ سال.",
        "رفتم پیام بدم به دوستم «دوستت دارم»، فرستادم تو گروه خانوادگی... خب حداقل همه خوشحال شدن 😂",
        "به جای «خداحافظ» به مغازه‌دار گفتم «به شما هم همچنین». مغازه‌دار فقط سرشو تکون داد.",
        "تو کلاس آنلاین میکروفونم باز بود و داشتم با مامانم درباره‌ی معلم حرف می‌زدم 💀",
        "رفتم با یکی دست بدم، اون قصد نداشت. دستم تو هوا موند. خوشبختانه وانمود کردم دارم موهامو درست می‌کنم.",
        "به جای خونه‌ی دوستم، رفتم خونه‌ی همسایه. فقط وقتی یه نفر ناشناس گفت «بله؟» فهمیدم.",
        "رفتم آسانسور رو بزنم، دکمه‌ی زنگ خطر رو زدم. حالا من اون آدمیم که تو ساختمون معروفه.",
        "اسم یه نفر رو اشتباه صدا کردم، بعد سعی کردم درستش کنم و یه اسم دیگه گفتم. حالا اون فکر می‌کنه ۳ تا اسم داره.",
        "جلوی همه با اعتماد به نفس گفتم «من اینو بلدم». دو دقیقه بعد گفتن «پس بگو». ساکت شدم 😭",
        "پیام «کجایی؟» رو به جای رفیقم برای معلم ریاضی فرستادم. جواب داد «سر کلاس. شما کجایید؟»",
    ],
    "unlucky": [
        "بدشانسی یعنی تو یه صف می‌ایستی، صف کناری سریع‌تر پیش میره. میری اون صف، صف قبلی سریع‌تر میشه.",
        "اولین بار که چتر نیاوردم، بارون اومد. ده بار قبلش که آوردم، آفتاب بود ☔",
        "یه بار تو عمرم می‌خواستم زود بخوابم. اون شب همسایه‌ها مهمونی گرفتن.",
        "نون تست همیشه با سمت کره‌دار میفته زمین. فیزیک هم از من خوشش نمیاد.",
        "پول پیدا کردم. بعد فهمیدم مال خودم بود که ماه پیش گم کردم... و دوباره از جیبم افتاد.",
        "تو بازی گفتم «شانسی». شانس هم منو انتخاب کرد... برای باختن.",
        "خوشحال که یه ربع زودتر رسیدم ایستگاه. اتوبوس هم یه ربع زودتر رفته بود.",
        "هر وقت یه چیزی رو دور میندازم، دقیقاً همون موقع بهش نیاز پیدا می‌کنم.",
        "بدشانس‌ترین آدم دنیا کسیه که با چتر میره بیرون و باد چترشو می‌بره، بعد خودش هم می‌ره.",
        "تازه می‌خواستم برم یه چیز بخرم، که فروشگاه گفت «تا همین الان داشتیم».",
    ],
    "logic": [
        "اگه زمان طلاست، پس چرا ساعت‌ها ارزون‌ان؟",
        "اگه همه‌ی کارا رو میشه فردا انجام داد، پس چرا امروز هنوز هست؟",
        "کسی که میگه «من هیچ‌وقت دروغ نمی‌گم» داره دقیقاً همون موقع دروغ میگه.",
        "اگه ۲ نفر کاری رو ۴ ساعته انجام بدن، ۴ نفر ۲ ساعته. ولی ۱۰۰ نفر اصلاً تموم نمی‌کنن، چون ۹۹ تا نظر مختلف دارن.",
        "اگه فرار از مشکلات راه‌حل نیست، پس چرا سرعتش اینقدر زیاده؟",
        "همه میگن «ساعت خواب مهمه»، ولی هیچ‌کس نمیگه چرا ساعت ۳ بامداد اینقدر ایده‌ی خوب به ذهنم میاد.",
        "اگه آب زندگیه، پس چرا هر وقت بارون میاد همه ناراحت میشن؟",
        "اگه ۱۰ نفر ۱۰ دقیقه دیر بیان، اسمش میشه «همه منتظر بقیه بودن».",
        "اگه من همیشه حق دارم، پس چرا بقیه همیشه دلیل میارن؟",
        "اگه خواب‌بودن مهارت بود، من الان مدرک دکترا داشتم 😴",
    ],
    "dialog": [
        "– مامان، من توی کلاس هفتم شدم!\n– آفرین! چندم؟\n– ... هفتم از آخر.",
        "– امتحان چطور بود؟\n– سخت.\n– چقدر سخت؟\n– معلم هم چند بار سؤال رو خوند و گفت «یه لحظه ببینم».",
        "– چرا دیر اومدی؟\n– ترافیک.\n– ولی ترافیک نبود.\n– خب پس خودم بودم.",
        "– دکتر: باید روزی ده هزار قدم راه بری.\n– از کجا شروع کنم؟\n– از خونه.\n– پس تا آشپزخونه ۱۰ قدمه، هزار بار رفت‌وبرگشت کنم؟",
        "– فروشنده: این گوشی ضدآبه.\n– اگه بیفته تو استخر چی؟\n– گوشی سالمه...\n– پس من خودم چی؟",
        "– داداش شارژر داری؟\n– آره.\n– بده.\n– شرطش اینه که تا ۲ ساعت بعد پس بدی.\n– باشه.\n– ... خب حالا کجایی؟ ۳ روزه پیدات نیست.",
        "– بابا، یه سؤال بپرسم؟\n– بپرس.\n– چقدر پول داری؟\n– چرا؟\n– چون مامان گفت همین سؤال رو ازت نپرسم.",
        "– مامان، غذا چیه؟\n– سورپرایز!\n– دوباره مرغ؟\n– ... دیگه سورپرایز نبود.",
        "– سلام، شما معلمی؟\n– بله.\n– پس حتماً جواب سؤال ۵ رو می‌دونید.\n– ... فعلاً خودم دارم دنبالش می‌گردم.",
        "– گارسون! تو سوپ من مگس هست.\n– ببخشید آقا، داره قبل غذا آب‌تنی می‌کنه.\n– ... اینجا استخر نیست!",
    ],
    "oneliner": [
        "من آدم صبوری‌ام؛ فقط وقتی خوابم میاد صبرم تموم میشه.",
        "زندگی کوتاهه؛ دسر رو اول بخور 🍰",
        "بعضی روزا باید قوی بود. بعضی روزا هم، مثل امروز، باید خوابید.",
        "من مشکلی با درس ندارم؛ درس مشکل داره که من رو نمی‌شناسه.",
        "رژیم گرفتم؛ از فردا. که هیچ‌وقت نمیاد.",
        "حوصله‌م سر رفت، گوشی رو برداشتم، حوصله‌م بیشتر سر رفت.",
        "با ۲ فنجون قهوه تبدیل میشم به یه آدم بهتر؛ با ۳ تا، به یه جن ☕",
        "اشتباه نکردم؛ فقط تجربه‌های متنوع جمع کردم.",
        "همه‌چی درست میشه؛ یه جایی بین الان و ۲ سال دیگه.",
        "من تنبل نیستم؛ فقط حالت صرفه‌جویی انرژی دارم 🔋",
        "دیروز یه عالمه کار داشتم؛ امروز نصفش رو فراموش کردم. پیشرفت دیگه 😎",
    ],
}


# ─── بانک فال (کاملاً سرگرمی؛ ادعای پیش‌بینی واقعی ندارد) ───
FORTUNE_BANK = {
    "love": [
        "یه نفر بیشتر از چیزی که نشون میده بهت توجه داره 👀 حالا اینکه کیه دیگه فال هم اینقدر اطلاعات نداره 😭",
        "امروز یه پیام غیرمنتظره میاد؛ ممکنه از طرف کسی باشه که دلت می‌خواد، ممکنه هم تبلیغ باشه 😂",
        "ستاره‌ها میگن امروز روز خوبیه برای گفتن یه حرف مهم... یا حداقل برای خوردن بستنی 🍦",
        "یه لبخند امروز می‌تونه تو رو مهم‌ترین آدم یه جمع کنه. فقط قبلش دندونات رو چک کن 😁",
        "قلبت امروز مثل وای‌فای ضعیفه؛ یه جا خوب وصل میشه، یه جا قطع.",
        "امروز یکی دلش می‌خواد بهت پیام بده ولی نمی‌دونه با چه جمله‌ای. تو هم اولش رو نزن، تست صبره 😎",
        "عشق امروز منتظر یه «سلام» ساده‌ست؛ فقط باید یکی از شما شروع کنه 😂",
        "یه خبر خوب از طرف یه دوست نزدیک میاد؛ شاید فقط خبر اینکه امروز شارژ داره 🔋",
        "فال میگه امروز یکی بهت فکر می‌کنه. نگفت خوب یا بد 😭",
    ],
    "money": [
        "پولی وارد زندگیت میشه، مقدارش فعلاً محرمانه‌ست، خود بانک هم خبر نداره 😂",
        "امروز شاید یه تخفیف ببینی که واقعاً تخفیف باشه... و فردا قیمتش بره بالا 😭",
        "یه خرج کوچیک سر راهته؛ بجاش یه خوراکی ارزون بخور که حس لاکچری بده 🍫",
        "ستاره‌ها میگن امروز وقت خوبیه برای پس‌انداز؛ ولی ستاره‌ها هم می‌دونن که تو نمی‌کنی 😂",
        "اگه پولی به دستت رسید، قبل از خرج کردن ۵ ثانیه فکر کن؛ بعدش هم هرچی دلت خواست.",
        "یه کیف پول پر به‌زودی می‌بینی... تو بازی البته 🎮",
        "امروز یه فرصت کوچیک برای درآمد پیدا میشه؛ فقط باید گوشی رو از دستت بذاری و دنبالش بری 😎",
        "موجودی حسابت امروز معجزه نمی‌کنه، ولی امیدواری رایگانه 😅",
        "یه پول کوچیک امروز به دستت می‌رسه؛ مثل همون ۱۰۰ تومنی که تو جیب کت زمستونیت پیدا می‌کنی 🧥",
    ],
    "school": [
        "امروز احتمال داره یه چیزی رو بدون درس خوندن درست جواب بدی، ولی روی این روش حساب باز نکن 😭",
        "امروز یه معلم می‌خواد ازت سؤال بپرسه؛ آماده باش یا حداقل یه «شاید» آماده کن 😂",
        "تکلیف امروز بالاخره تموم میشه، به شرطی که وسطش یه ویدیو نبینی 😅",
        "فردا امتحانه؟ امروز بهترین روز برای شروع درس خوندنه... نه، دیروز بود. بدو!",
        "دوستی که جزوه داره امروز نجاتت میده؛ از حالا یه تشکر آماده کن 📚",
        "امروز معلمت حال خوبی داره؛ یعنی شاید تکلیف نده... شاید.",
        "یه نمره‌ی خوب در راهه؛ یکم صبر و یه ذره تلاش لازمه ✏️",
        "یه سؤال مهم تو ذهنته؛ امروز بپرس. حتی اگه کلاس بخنده، فردا اونی که فهمیده تویی 😎",
        "امروز یه برگه‌ی سفید پر میشه از جواب‌هایی که فقط خودت می‌فهمی 😂",
    ],
    "gaming": [
        "امروز تو بازی شانس باهاته؛ فقط مواظب باش دقیقاً وقتی داری می‌بری، لگ نخوری 😭",
        "یه حریف قوی امروز میاد سراغت؛ یا ازش یاد بگیر، یا گوشی رو پرت کن (نه، پرت نکن) 😂",
        "امروز یه رکورد جدید ثبت میشه... ولی شاید رکورد بدترین باخت‌ها 😅",
        "یه لول جدید در انتظارته؛ بازیِ زندگی هم آپدیت لازم داره.",
        "امروز هم‌تیمیت کمک می‌کنه؛ ولی فقط اگه تو اول کمکش کنی 🎮",
        "یه بازی جدید پیدا می‌کنی که یه هفته ازش جدا نمیشی؛ مامانت قبلاً تو فال خودش دیده بود.",
        "امروز دکمه‌ی درست رو به‌موقع می‌زنی... یا نه 😂",
        "گیم‌اور نزدیکه؛ ولی تو هنوز یه جون داری. ازش استفاده کن 🔥",
        "یه برد کوچیک بهترین حال رو بهت میده؛ امروز مراقب کنترلر باش.",
    ],
    "funny": [
        "امروز یه اتفاق بامزه می‌افته که فقط یه نفر دلیلشو می‌فهمه؛ اونم تویی که ۴ ساعت بعد یادت میاد 😂",
        "کسی امروز می‌خواد بهت بگه «یه چیزی بگم؟»؛ جوابش «نه» نیست، «بعداً»ـه 😭",
        "امروز یه جایی خنده‌ات می‌گیره و نمی‌تونی جلوشو بگیری، دقیقاً وقتی که نباید 😅",
        "فال میگه امروز یه پیام اشتباهی به یه نفر اشتباهی می‌دی؛ مراقب باش یا حداقل یه بهونه آماده کن.",
        "نون گرم، چایی تازه‌دم و هیچ کاری نداشتن؛ نصیب خیلی‌ها نمیشه ولی تو آرزوشو داری ☕",
        "یه نفر امروز یه حرفی می‌زنه که یه ساعت فکر می‌کنی؛ بعد می‌فهمی شوخی بوده 😂",
        "ستاره‌ها میگن آروم باش؛ خود ستاره‌ها هم نمی‌دونن چی میگن 😭",
        "امروز یه ناهار خوشمزه نصیبت میشه... یا حداقل تو رویا 🍽️",
        "امروز اتفاق خاصی نمی‌افته ولی تو خودت یه اتفاقی؛ همین کافیه 😎",
    ],
    "night": [
        "امشب ساعت ۳ بامداد یه ایده‌ی عالی به ذهنت میرسه؛ فردا صبح یادت نیست چی بود 🌙",
        "شب خوبیه برای گفتن یه حرف دلی؛ یا حداقل برای فرستادن یه استیکر ❤️",
        "ماه امشب باهاته؛ فقط ابر نباشه ☁️",
        "قبل خواب گوشی رو بذار کنار؛ البته می‌دونیم که نمی‌ذاری 😂",
        "یه خواب خوب امشب در انتظارته؛ اگه اون ۲ ساعت آخر رو با ویدیوهای تصادفی هدر ندی.",
        "امشب شیرینی بخور؛ شب‌ها کالری نداره (این یه فال علمی نیست 😄)",
        "امشب یکی یادته و نمی‌خواد بگه؛ تو هم نگو 🌙",
        "ستاره‌ها میگن امشب زود بخواب. ولی ما می‌دونیم که دیر می‌خوابی 😭",
        "امشب رویای قشنگی می‌بینی؛ البته فقط اگه بخوابی.",
    ],
    "today": [
        "امروز یک اتفاق کوچک می‌تونه حالت رو بهتر کنه؛ شاید یک پیام باشه، شاید یک خبر، شاید هم فقط اینترنتت قطع نشه 😂",
        "امروز یه کار مهم رو دیگه عقب نندازی؛ بعداً بهت میگم چرا... یا فردا.",
        "امروز روز خوبیه برای شروع؛ فقط معلوم نیست چی رو.",
        "یه لبخند به یه نفر بزن؛ شاید اون هم لبخند بزنه، شاید هم فکر کنه یه چیزی ازش می‌خوای 😅",
        "یکم صبر امروز بهتر از ۱۰ تا عجله‌ست؛ البته نه برای شام 🍕",
        "یه کار کوچیک رو که از دیروز مونده انجام بده؛ حس قهرمان بودن بهت دست میده 💪",
        "امروز بیشتر آب بخور؛ این فال نیست، تذکره 😂",
        "ستاره‌ها میگن امروز از ساعت ۴ به بعد روزت بهتر میشه؛ چون اون ساعت کارت تموم میشه.",
        "امروز مراقب باش اول صبح کسی بهت نگه «یه لحظه وقت داری؟» 😅",
    ],
    "lucky": [
        "عدد شانس امروزت ۷ـه؛ اگه خواستی امتحانش کنی، تو بازی «حدس عدد» بزن 🎲",
        "امروز یه شانس کوچیک در می‌زنه؛ در رو باز کن، شاید فقط پستچی باشه.",
        "رنگ شانس امروزت آبیه؛ اگه لباس آبی نداری، دوستت داره 😎",
        "شانست امروز ۷۳٪ـه؛ ۲۷٪ باقی‌مونده هم مال دوستاته.",
        "امروز اگه سکه‌ای پیدا کردی، برش دار؛ حداقل یه اتفاق کوچیک خوب افتاده 🪙",
        "یه تاس بنداز: اگه ۶ اومد، امروز خوبه؛ اگه نیومد، دوباره بنداز 🎲",
        "شانس امروز با کسیه که اول صبح بلند میشه؛ پس تو احتمالاً شانسی نداری 😂",
        "یه بلیط شانس تو راهه؛ فقط باید بفهمی کجا و برای چی 🍀",
        "عدد شانس امروز: هر عددی که دلت بخواد؛ فال قرار نیست همه‌چی رو بدونه 😅",
    ],
    "event": [
        "امروز یکی چیزی بهت پیشنهاد میده؛ قبل از جواب دادن ببین پولیه یا نه 😂",
        "یه خبر غیرمنتظره می‌شنوی؛ ۸۰٪ کوچیکه، ۲۰٪ هم می‌تونه بامزه باشه 👀",
        "امروز یکی بهت زنگ می‌زنه که مدت‌هاست خبری ازش نداشتی؛ شاید فقط شماره رو اشتباهی گرفته.",
        "اتفاق امروز: یه چیزی گم می‌کنی و ۱۰ دقیقه بعد تو دستت پیداش می‌کنی 🔍",
        "یه دعوت غیرمنتظره می‌رسه؛ اگه غذا داشت، برو 🍽️",
        "امروز یه نفر بهت میگه «خسته نباشی»؛ همین یه جمله حالتو بهتر می‌کنه.",
        "امروز یه پیام می‌رسه؛ ممکنه از طرف کسی باشه که بهش فکر می‌کردی 👀",
        "اتفاق بزرگی نمی‌افته، ولی همون اتفاق‌های کوچیک امروز بیشتر از قبل جالب میشن.",
        "امروز یه نفر سر یه چیز کوچیک باهات شوخی می‌کنه؛ تو هم جواب بده، یه روز خوب شروع میشه 😂",
    ],
    "character": [
        "تو از اون آدمایی هستی که وقتی میگن «۵ دقیقه»، ۲۵ دقیقه‌ش رو هم دیر میرسن، ولی همه دلتنگشون میشن 🔥",
        "شخصیت تو یه ترکیب جالبه از بامزه، کمی تنبل و یه عالمه استعداد پنهان 😎",
        "تو همونی هستی که تو گروه کم میگه ولی وقتی میگه همه می‌خندن 😂",
        "تو آدم قابل اعتمادی هستی؛ مخصوصاً وقتی راز بقیه رو نمی‌دونی.",
        "درونت یه رهبر خوابیده؛ فقط الان داره اون ۵ دقیقه‌ی آخر رو می‌خوابه 😴",
        "شخصیت امروزت: شجاع، بامزه و کمی گرسنه 🍕",
        "تو یه آدم خاصی هستی؛ فقط بقیه هنوز نفهمیدن چرا 😂",
        "تو مهربونی، اما وقتی گشنته یه موجود دیگه میشی 😭",
        "شخصیتت مثل یه بازیه: اولش گیج‌کننده، آخرش اعتیادآور 🎮",
    ],
}


# ─── Response Engine ───
TRIGGER_ALIASES = {
    "خداحافظ": "خدافظ", "خدانگهدار": "خدافظ", "مرسی": "ممنون", "مچکر": "ممنون", "ممنونم": "ممنون",
    "چطوری": "خوبی", "خوبین": "خوبی", "چه خبرا": "چه خبر", "ادمین کو": "ادمین کجاست",
    "کسی اینجاست": "کسی هست", "گرسنه ام": "گرسنمه", "گشنه ام": "گشنمه", "حوصله م سر رفته": "حوصلم سر رفته",
}
LOW_WEIGHT_TRIGGERS = {  # کلمات خیلی رایج: با ضریب کمتر پاسخ داده می‌شوند
    "نه", "آره", "باشه", "اوکی", "چرا", "چی شد", "چی میگی", "ولش کن", "بیخیال", "ممنون",
    "نمیدونم", "چجوری", "مگه میشه", "بریم", "میمیرم",
}
EXACT_ONLY_TRIGGERS = {"میمیرم"}  # بدون کلمه‌ی اضافه
KI_STOPWORDS = {"میاد", "میره", "میشه", "بود", "شد", "اومد", "رفت", "گفت", "زد", "خوند", "داره", "هستن", "کجاست"}
RESPONSE_SLACK = 2
CHAT_REPLY_COOLDOWN = 5
TRIGGER_COOLDOWN = 45
USER_REPLY_COOLDOWN = 12
ADDRESSED_COOLDOWN = 2
MEMBER_ACTIVE_DAYS = 14
LAST_RESPONSE = {}


def _build_trigger_index():
    index, generic = {}, ()
    for trig, resps in TRIGGER_DATA:
        if "[کلمه]" in trig:
            generic = tuple(resps)
            continue
        key = tuple(norm_text(trig).split())
        if key in index:  # دو ورودی با یک Trigger → یک مجموعه پاسخ
            index[key] = (index[key][0], index[key][1] + tuple(resps))
        else:
            index[key] = (trig, tuple(resps))
    for alias, target in TRIGGER_ALIASES.items():
        akey, tkey = tuple(norm_text(alias).split()), tuple(norm_text(target).split())
        if akey not in index and tkey in index:
            index[akey] = index[tkey]
    return index, generic


TRIGGER_INDEX, KI_GENERIC = _build_trigger_index()
MAX_TRIGGER_TOKENS = max(len(k) for k in TRIGGER_INDEX)


def find_trigger(tokens):
    """طولانی‌ترین Trigger که ابتدای پیام باشد؛ (متن، پاسخ‌ها، کلید) یا None."""
    for n in range(min(len(tokens), MAX_TRIGGER_TOKENS), 0, -1):
        key = tuple(tokens[:n])
        entry = TRIGGER_INDEX.get(key)
        if entry is None:
            continue
        joined = " ".join(key)
        slack = 0 if joined in EXACT_ONLY_TRIGGERS else RESPONSE_SLACK
        if len(tokens) - n <= slack:
            return entry[0], entry[1], joined
    return None


def _ki_word(tokens):
    if len(tokens) != 2 or tokens[0] != "کی":
        return None
    w = tokens[1]
    if w in KI_STOPWORDS or not 2 <= len(w) <= 14:
        return None
    return w


def random_member(ctx):
    """منشن یک عضو فعال اخیر (ترجیحاً غیر از خود فرستنده)."""
    rows = APP.db.all(
        """SELECT u.user_id, u.username, u.first_name, u.last_name FROM user_stats s
           JOIN users u ON u.user_id=s.user_id WHERE s.chat_id=? AND s.last_active>=?
           ORDER BY RANDOM() LIMIT 8""", (int(ctx.chat_id), now() - MEMBER_ACTIVE_DAYS * 86400))
    others = [dict(r) for r in rows if r["user_id"] != ctx.user_id]
    if others:
        return mention(random.choice(others))
    return mention(ctx.user)


def fill_response(resp, ctx, word=None):
    out = resp
    if word is not None:
        out = out.replace("[کلمه]عه", word if word.endswith("ه") else word + "عه").replace("[کلمه]", word)
    if "[آیدی یکی از اعضا]" in out:
        out = out.replace("[آیدی یکی از اعضا]", random_member(ctx))
    return out


def _is_addressed(ctx):
    if ctx.is_private:
        return True
    uname = (APP.bot_username or "").lower()
    if uname and ("@" + uname) in ctx.text.lower():
        return True
    rt = ctx.reply_to
    return bool(rt and APP.bot_id and (rt.get("from") or {}).get("id") == APP.bot_id)


def _choose_response(chat_id, key, resps):
    k = (chat_id, key)
    last = LAST_RESPONSE.get(k)
    idxs = [i for i in range(len(resps)) if i != last] or list(range(len(resps)))
    i = random.choice(idxs)
    if len(LAST_RESPONSE) > 3000:
        LAST_RESPONSE.clear()
    LAST_RESPONSE[k] = i
    return resps[i]


@on_message(priority=90)
def hook_response_engine(ctx):
    if not ctx.text or ctx.user_id is None:
        return False
    if ctx.is_group and not get_settings(ctx.chat_id).get("auto_reply"):
        return False
    toks = norm_text(ctx.text).split()
    uname = (APP.bot_username or "").lower()
    if uname:
        toks = [t for t in toks if t != uname]
    if not toks or len(toks) > MAX_TRIGGER_TOKENS + RESPONSE_SLACK:
        return False
    word = None
    found = find_trigger(toks)
    if found:
        _, resps, key = found
        weight = 0.4 if key in LOW_WEIGHT_TRIGGERS else 1.0
    else:
        word = _ki_word(toks)
        if word is None or not KI_GENERIC:
            return False
        resps, key, weight = KI_GENERIC, "ki:generic", 0.8
    chat, user = ctx.chat_id, ctx.user_id
    if _is_addressed(ctx):
        if cooldown_left(("ra", chat, user)) > 0:
            return False
        cooldown_ok(("ra", chat, user), ADDRESSED_COOLDOWN)
    else:
        if not chance(get_settings(chat).get("reply_chance", 35) * weight):
            return False
        keys = [("rc", chat), ("rt", chat, key), ("ru", chat, user)]
        if any(cooldown_left(k) > 0 for k in keys):
            return False
        for k, secs in zip(keys, (CHAT_REPLY_COOLDOWN, TRIGGER_COOLDOWN, USER_REPLY_COOLDOWN)):
            cooldown_ok(k, secs)
    ctx.reply(fill_response(_choose_response(chat, key, resps), ctx, word))
    STATS.group_add(chat, "feat:auto_reply")
    return True


# ─── Seed محتوا (جوک و فال) ───
def seed_content():
    """بانک جوک/فال داخل دیتابیس ثبت می‌شود. جوک‌های حذف‌شده دوباره برنمی‌گردند (Hash می‌ماند)."""
    t, nj, nf = now(), 0, 0
    with APP.db.tx():
        for cat, items in JOKE_BANK.items():
            for text in items:
                nj += APP.db.run(
                    "INSERT OR IGNORE INTO jokes(text, text_hash, category, status, score, created_at) "
                    "VALUES(?,?,?,'approved',60,?)", (text, text_hash(text), cat, t)).rowcount
        for cat, items in FORTUNE_BANK.items():
            for text in items:
                nf += APP.db.run("INSERT OR IGNORE INTO fortunes(category, text, text_hash) VALUES(?,?,?)",
                                 (cat, text, text_hash(text))).rowcount
    return nj, nf


on_startup(seed_content)


# ─── سیستم جوک و جوک‌متر ───
JOKE_RECENT = {}
JOKE_CAT_EMOJI = {k: e for k, e, _ in JOKE_CATEGORIES}
JOKE_MIN_LEN, JOKE_MAX_LEN = 15, 600
JOKE_SUBMIT_HOURLY_LIMIT = 3
JOKE_INPUT_TTL = 300


def joke_score(laughs, mehs):
    return 100.0 * (laughs + 3) / (laughs + mehs + 5)


def _joke_weight(score, shown):
    w = 0.2 + (max(0.0, min(100.0, score)) / 100.0) ** 2 * 1.8
    return w + 0.4 if shown < 3 else w


def pick_joke(chat_id, cat="all"):
    """انتخاب وزنی: جوک‌های محبوب بیشتر، ضعیف‌ها کمتر (ولی هیچ‌وقت حذف نمی‌شوند)."""
    if cat in JOKE_CAT_LABELS:
        rows = APP.db.all("SELECT id, score, shown, category FROM jokes WHERE status='approved' AND category=?", (cat,))
    else:
        rows = APP.db.all("SELECT id, score, shown, category FROM jokes WHERE status='approved'")
    if not rows:
        return None
    if len(JOKE_RECENT) > 2000:
        JOKE_RECENT.clear()
    recent = JOKE_RECENT.setdefault(chat_id, [])
    pool = [r for r in rows if r["id"] not in recent] or rows
    aff = {} if cat in JOKE_CAT_LABELS else _joke_affinity(chat_id)
    r = random.choices(pool, weights=[_joke_weight(x["score"], x["shown"]) * (1 + 2 * aff.get(x["category"], 0)) for x in pool])[0]
    recent.append(r["id"])
    del recent[:-40]
    return r["id"]


def joke_markup(jid, cat):
    cat = cat if cat in JOKE_CAT_LABELS else "all"
    return kb(
        [btn("😂 خندیدم", "jk:v:%d:1:%s" % (jid, cat)), btn("😐 خوشم نیومد", "jk:v:%d:0:%s" % (jid, cat))],
        [btn("🔄 یکی دیگه", "jk:n:" + cat), btn("📤 ارسال", "jk:sh:%d" % jid)],
        [btn("🗂️ دسته‌ها", "jk:cats"), btn("↩️ سرگرمی", "menu:fun")],
        home_row())


def render_joke(jid, cat="all"):
    j = APP.db.one("SELECT * FROM jokes WHERE id=?", (int(jid),))
    if j is None:
        return None, None
    text = ("😂 جوک — %s %s\n\n%s\n\n😂 جوک‌متر: %d/100\n😂 %d   😐 %d\n🆔 #%d"
            % (JOKE_CAT_EMOJI.get(j["category"], "😂"), JOKE_CAT_LABELS.get(j["category"], ""), j["text"],
               int(round(j["score"])), j["laughs"], j["mehs"], j["id"]))
    return text, joke_markup(j["id"], cat)


def show_joke(ctx, jid, cat):
    APP.db.run("UPDATE jokes SET shown=shown+1, last_shown=? WHERE id=?", (now(), jid))
    STATS.group_add(ctx.chat_id, "joke_shown")
    STATS.group_add(ctx.chat_id, "feat:joke")
    text, markup = render_joke(jid, cat)
    show(ctx, text, markup)


def joke_next(ctx, cat="all"):
    if not require_feature(ctx, "jokes"):
        return
    jid = pick_joke(ctx.chat_id, cat)
    if jid is None:
        show(ctx, "هنوز جوکی توی این دسته ندارم 😅", kb(home_row()))
        return
    show_joke(ctx, jid, cat)


def joke_cats_markup():
    rows, cur = [], []
    for key, emoji, label in JOKE_CATEGORIES:
        cur.append(btn("%s %s" % (emoji, label), "jk:n:" + key))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([btn("🎲 همه دسته‌ها", "jk:n:all"), btn("➕ ارسال جوک", "jk:sub")])
    rows.append([btn("↩️ سرگرمی", "menu:fun")])
    return kb(*rows)


def _joke_vote(ctx, jid, vote_up, cat):
    if not require_feature(ctx, "jokes"):
        return
    j = APP.db.one("SELECT * FROM jokes WHERE id=? AND status='approved'", (jid,))
    if j is None:
        ctx.answer("این جوک دیگه توی بانک نیست", True)
        return
    if j["submitted_by"] and int(j["submitted_by"]) == int(ctx.user_id):
        ctx.answer("به جوک خودت که نمی‌شه رأی داد 😅", True)
        return
    val = 1 if vote_up else -1
    ex = APP.db.one("SELECT vote FROM joke_votes WHERE joke_id=? AND user_id=?", (jid, int(ctx.user_id)))
    if ex is not None and ex["vote"] == val:
        ctx.answer("قبلاً همین رأی رو دادی 😉")
        return
    with APP.db.tx():
        if ex is None:
            APP.db.run("INSERT INTO joke_votes(joke_id, user_id, vote, ts) VALUES(?,?,?,?)",
                       (jid, int(ctx.user_id), val, now()))
            APP.db.run("UPDATE jokes SET laughs=laughs+?, mehs=mehs+? WHERE id=?", (1 if val == 1 else 0, 1 if val == -1 else 0, jid))
        else:
            APP.db.run("UPDATE joke_votes SET vote=?, ts=? WHERE joke_id=? AND user_id=?", (val, now(), jid, int(ctx.user_id)))
            APP.db.run("UPDATE jokes SET laughs=laughs+?, mehs=mehs+? WHERE id=?", (1 if val == 1 else -1, -1 if val == 1 else 1, jid))
        APP.db.run("UPDATE jokes SET score=100.0*(laughs+3)/(laughs+mehs+5) WHERE id=?", (jid,))
    if ex is None:
        STATS.user_add(ctx.chat_id, ctx.user_id, "joke_votes")
        STATS.group_add(ctx.chat_id, "joke_votes")
        mission_event(ctx.chat_id, ctx.user_id, "joke_votes")
    if val == 1:
        STATS.group_add(ctx.chat_id, "jokecat:" + j["category"])
    elif ex is not None:
        STATS.group_add(ctx.chat_id, "jokecat:" + j["category"], -1)
    ctx.answer("😂 ثبت شد" if val == 1 else "😐 ثبت شد")
    text, markup = render_joke(jid, cat)
    ctx.edit(text, markup)


def _joke_admin_markup(jid):
    return kb([btn("✅ تأیید", "jk:ap:%d" % jid), btn("❌ رد", "jk:rj:%d" % jid), btn("🗑️ حذف", "jk:dl:%d" % jid)])


def _pending_text(j):
    u = get_user(j["submitted_by"]) if j["submitted_by"] else {}
    return ("📥 جوک در انتظار تأیید\n\n%s\n\n🏷️ دسته: %s %s\n👤 از: %s\n🆔 #%d"
            % (j["text"], JOKE_CAT_EMOJI.get(j["category"], ""), JOKE_CAT_LABELS.get(j["category"], ""),
               display_name(u), j["id"]))


def _show_queue(ctx):
    j = APP.db.one("SELECT * FROM jokes WHERE status='pending' ORDER BY id LIMIT 1")
    if j is None:
        show(ctx, "📥 صف جوک‌های در انتظار خالیه ✅", kb([btn("↩️ سرگرمی", "menu:fun")]))
        return
    left = APP.db.val("SELECT COUNT(*) FROM jokes WHERE status='pending'", (), 0)
    markup = _joke_admin_markup(j["id"])
    markup["inline_keyboard"].append([btn("↩️ سرگرمی", "menu:fun")])
    show(ctx, _pending_text(j) + "\n\n📋 در صف: %d" % left, markup)


def _joke_admin_action(ctx, action, jid):
    j = APP.db.one("SELECT * FROM jokes WHERE id=?", (jid,))
    if j is None:
        ctx.answer("این جوک پیدا نشد", True)
        return
    new = {"ap": "approved", "rj": "rejected", "dl": "deleted"}[action]
    if action == "ap" and j["status"] == "approved":
        ctx.answer("قبلاً تأیید شده", False)
        return
    APP.db.run("UPDATE jokes SET status=? WHERE id=?", (new, jid))
    log_action(0, "joke_" + action, ctx.user_id, j["submitted_by"], "#%d" % jid)
    ctx.answer({"ap": "✅ تأیید شد", "rj": "❌ رد شد", "dl": "🗑️ حذف شد"}[action])
    more = APP.db.val("SELECT COUNT(*) FROM jokes WHERE status='pending'", (), 0)
    rows = [[btn("📥 بعدی (%d)" % more, "jk:q")]] if more else []
    rows.append([btn("↩️ سرگرمی", "menu:fun")])
    show(ctx, "%s جوک #%d %s." % ({"ap": "✅", "rj": "❌", "dl": "🗑️"}[action], jid,
                                  {"ap": "تأیید شد و وارد بانک اصلی شد", "rj": "رد شد", "dl": "حذف شد"}[action]), kb(*rows))


@callback("jk")
def cb_joke(ctx):
    a = ctx.parts[1] if len(ctx.parts) > 1 else "menu"
    try:
        if a == "menu":
            sec_fun(ctx)
        elif a == "cats":
            if require_feature(ctx, "jokes"):
                show(ctx, "🗂️ دسته‌ی جوک رو انتخاب کن 👇", joke_cats_markup())
        elif a == "n":
            joke_next(ctx, ctx.parts[2] if len(ctx.parts) > 2 else "all")
        elif a == "v":
            _joke_vote(ctx, int(ctx.parts[2]), ctx.parts[3] == "1", ctx.parts[4] if len(ctx.parts) > 4 else "all")
        elif a == "sh":
            _joke_share(ctx, int(ctx.parts[2]))
        elif a == "sub":
            _joke_submit_start(ctx)
        elif a == "sc":
            _joke_submit_category(ctx, ctx.parts[2])
        elif a == "x":
            input_clear(ctx.chat_id, ctx.user_id)
            sec_fun(ctx)
        elif a in ("ap", "rj", "dl", "q"):
            if not ctx.is_bot_admin:
                ctx.answer("⛔ فقط ادمین ربات", True)
            elif a == "q":
                _show_queue(ctx)
            else:
                _joke_admin_action(ctx, a, int(ctx.parts[2]))
        else:
            ctx.answer("دکمه نامعتبر", True)
    except (IndexError, ValueError):
        ctx.answer("دکمه نامعتبر", True)


def _joke_share(ctx, jid):
    if not require_feature(ctx, "jokes"):
        return
    j = APP.db.one("SELECT text FROM jokes WHERE id=? AND status='approved'", (jid,))
    if j is None:
        ctx.answer("این جوک دیگه نیست", True)
        return
    if not cooldown_ok(("jsh", ctx.chat_id, ctx.user_id, jid), 30):
        ctx.answer("قبلاً فرستادیش 😉")
        return
    ctx.send("😂 %s\n\n— %s" % (j["text"], BOT_NAME))
    ctx.answer("📤 ارسال شد")


def _joke_submit_start(ctx):
    if not require_feature(ctx, "jokes"):
        return
    rows, cur = [], []
    for key, emoji, label in JOKE_CATEGORIES:
        cur.append(btn("%s %s" % (emoji, label), "jk:sc:" + key))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([btn("❌ لغو", "jk:x")])
    show(ctx, "➕ ارسال جوک\n\nاول دسته‌ی جوکت رو انتخاب کن 👇", kb(*rows))


def _joke_submit_category(ctx, cat):
    if cat not in JOKE_CAT_LABELS:
        ctx.answer("دسته نامعتبر", True)
        return
    input_set(ctx.chat_id, ctx.user_id, "joke_submit", {"cat": cat}, ttl=JOKE_INPUT_TTL)
    show(ctx, "✍️ حالا جوکت رو همینجا بنویس (دسته: %s %s)\n\nبین %d تا %d حرف، تا ۵ دقیقه وقت داری.\n"
              "بعد از تأیید ادمین وارد بانک اصلی می‌شه."
         % (JOKE_CAT_EMOJI[cat], JOKE_CAT_LABELS[cat], JOKE_MIN_LEN, JOKE_MAX_LEN), kb([btn("❌ لغو", "jk:x")]))


@on_input("joke_submit")
def input_joke_submit(ctx, data):
    text = ctx.text.strip()
    cat = data.get("cat") if data.get("cat") in JOKE_CAT_LABELS else "daily"
    if len(text) < JOKE_MIN_LEN:
        ctx.reply("خیلی کوتاهه 😅 یکم بیشتر بنویس (حداقل %d حرف) یا لغو کن." % JOKE_MIN_LEN, kb([btn("❌ لغو", "jk:x")]))
        return
    if len(text) > JOKE_MAX_LEN:
        ctx.reply("خیلی طولانیه 😅 کوتاه‌ترش کن (حداکثر %d حرف)." % JOKE_MAX_LEN, kb([btn("❌ لغو", "jk:x")]))
        return
    input_clear(ctx.chat_id, ctx.user_id)
    recent = APP.db.val("SELECT COUNT(*) FROM jokes WHERE submitted_by=? AND created_at>?",
                        (int(ctx.user_id), now() - 3600), 0)
    if recent >= JOKE_SUBMIT_HOURLY_LIMIT:
        ctx.reply("امروز زیاد جوک فرستادی 😄 یه ساعت دیگه دوباره بفرست.")
        return
    cur = APP.db.run(
        "INSERT OR IGNORE INTO jokes(text, text_hash, category, status, score, submitted_by, submit_chat, created_at) "
        "VALUES(?,?,?,'pending',60,?,?,?)", (text, text_hash(text), cat, int(ctx.user_id), int(ctx.chat_id), now()))
    if cur.rowcount == 0:
        ctx.reply("این جوک قبلاً توی بانک یا صف هست 😉")
        return
    jid = cur.lastrowid
    STATS.user_add(ctx.chat_id, ctx.user_id, "jokes_sent")
    ctx.reply("✅ جوکت ثبت شد و بعد از تأیید ادمین وارد بانک می‌شه. مرسی! 😂")
    if ADMIN_ID:
        j = APP.db.one("SELECT * FROM jokes WHERE id=?", (jid,))
        APP.api.try_call("sendMessage", {"chat_id": ADMIN_ID, "text": safe_text(_pending_text(j)),
                                         "reply_markup": _joke_admin_markup(jid)}, retries=1)


@command("deljoke", admin="bot")
def cmd_deljoke(ctx):
    try:
        jid = int(ctx.args.strip().lstrip("#"))
    except ValueError:
        ctx.reply("استفاده: /deljoke 42  (شماره‌ی 🆔 زیر جوک)")
        return
    if APP.db.run("UPDATE jokes SET status='deleted' WHERE id=?", (jid,)).rowcount:
        log_action(0, "joke_dl", ctx.user_id, 0, "#%d" % jid)
        ctx.reply("🗑️ جوک #%d حذف شد." % jid)
    else:
        ctx.reply("جوکی با این شماره پیدا نشد.")


@command("joke")
def cmd_joke(ctx):
    joke_next(ctx, "all")


# ─── سیستم فال ───
FORTUNE_RECENT = {}
FORTUNE_COOLDOWN = 4


def pick_fortune(chat_id, cat="all"):
    if cat in FORTUNE_CAT_LABELS:
        rows = APP.db.all("SELECT id FROM fortunes WHERE category=?", (cat,))
    else:
        rows = APP.db.all("SELECT id FROM fortunes")
    if not rows:
        return None
    if len(FORTUNE_RECENT) > 2000:
        FORTUNE_RECENT.clear()
    recent = FORTUNE_RECENT.setdefault((chat_id, cat), [])
    pool = [r["id"] for r in rows if r["id"] not in recent] or [r["id"] for r in rows]
    fid = random.choice(pool)
    recent.append(fid)
    del recent[:-12]
    return fid


def fortune_markup(cat):
    cat = cat if cat in FORTUNE_CAT_LABELS else "all"
    return kb(
        [btn("🔮 دوباره فال", "fa:c:" + cat), btn("❤️ فال عشق", "fa:c:love")],
        [btn("💰 فال پول", "fa:c:money"), btn("🎮 فال گیم", "fa:c:gaming")],
        [btn("🗂️ دسته‌های دیگه", "fa:cats"), btn("↩️ سرگرمی", "menu:fun")],
        home_row())


def show_fortune(ctx, cat="all"):
    if not require_feature(ctx, "fortune"):
        return
    if not cooldown_ok(("fa", ctx.chat_id, ctx.user_id), FORTUNE_COOLDOWN):
        if ctx.kind == "callback":
            ctx.answer("🔮 یه لحظه صبر کن")
        else:
            ctx.reply("🔮 یه لحظه صبر کن")
        return
    fid = pick_fortune(ctx.chat_id, cat)
    row = APP.db.one("SELECT * FROM fortunes WHERE id=?", (fid,)) if fid else None
    if row is None:
        show(ctx, "هنوز فالی ندارم 😅", kb(home_row()))
        return
    APP.db.run("UPDATE fortunes SET shown=shown+1 WHERE id=?", (row["id"],))
    STATS.group_add(ctx.chat_id, "feat:fortune")
    STATS.group_add(ctx.chat_id, "fortune:" + row["category"])
    mission_event(ctx.chat_id, ctx.user_id, "fortunes")
    emoji = {k: e for k, e, _ in FORTUNE_CATEGORIES}.get(row["category"], "🔮")
    show(ctx, "🔮 فال %s — %s %s\n\n%s\n\n✨ فقط برای سرگرمیه، پیش‌بینی واقعی نیست"
         % (display_name(ctx.user), emoji, FORTUNE_CAT_LABELS.get(row["category"], ""), row["text"]),
         fortune_markup(cat))


def fortune_cats_markup():
    rows, cur = [], []
    for key, emoji, label in FORTUNE_CATEGORIES:
        cur.append(btn("%s %s" % (emoji, label), "fa:c:" + key))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([btn("🎲 همه دسته‌ها", "fa:c:all")])
    rows.append([btn("↩️ سرگرمی", "menu:fun")])
    return kb(*rows)


@callback("fa")
def cb_fortune(ctx):
    a = ctx.parts[1] if len(ctx.parts) > 1 else "c"
    if a == "cats":
        if require_feature(ctx, "fortune"):
            show(ctx, "🔮 فال کدوم دسته؟ 👇", fortune_cats_markup())
    elif a == "c":
        show_fortune(ctx, ctx.parts[2] if len(ctx.parts) > 2 else "all")
    else:
        ctx.answer("دکمه نامعتبر", True)


@command("fal", "fortune")
def cmd_fal(ctx):
    show_fortune(ctx, "all")


# ─── منوی سرگرمی ───
def fun_markup(ctx):
    jokes_on, fort_on = feature_enabled(ctx, "jokes"), feature_enabled(ctx, "fortune")
    rows = []
    if jokes_on:
        rows.append([btn("😂 جوک", "jk:n:all"), btn("🗂️ دسته‌های جوک", "jk:cats")])
    r2 = []
    if fort_on:
        r2.append(btn("🔮 فال", "fa:c:all"))
    if jokes_on:
        r2.append(btn("➕ ارسال جوک", "jk:sub"))
    if r2:
        rows.append(r2)
    if feature_enabled(ctx, "lab") and "lab" in SECTIONS:
        rows.append([btn("🧪 آزمایشگاه", "menu:lab")])
    if ctx.is_bot_admin:
        n = APP.db.val("SELECT COUNT(*) FROM jokes WHERE status='pending'", (), 0)
        if n:
            rows.append([btn("📥 جوک‌های در انتظار (%d)" % n, "jk:q")])
    rows.append(home_row())
    return kb(*rows)


@section("fun")
def sec_fun(ctx):
    if not (feature_enabled(ctx, "jokes") or feature_enabled(ctx, "fortune")):
        ctx.answer("🔕 جوک و فال توی این گروه خاموشه", True)
        return
    show(ctx, "😂 سرگرمی\n\nجوک بخون، فال بگیر یا جوک خودتو بفرست 👇", fun_markup(ctx))


# ─── SELFTEST فاز ۳ ───
def _p3_last_text(api, name="send_message", idx=1):
    calls = _p2_calls(api, name)
    return calls[-1][1][idx] if calls else ""


@selftest("Trigger ها: ۱۰۰ مورد، ایندکس، ادغام تکراری‌ها")
def _t_p3_trigger_data():
    check(len(TRIGGER_DATA) == 100, "expected 100 triggers, got %d" % len(TRIGGER_DATA))
    check(sum(len(r) for _, r in TRIGGER_DATA) == 200, "each trigger must have 2 responses")
    check(all(r.strip() for _, rs in TRIGGER_DATA for r in rs), "empty response")
    check(len(KI_GENERIC) == 2, "generic «کی [کلمه]» missing")
    pool = TRIGGER_INDEX[tuple(norm_text("بیا پیوی").split())][1]
    check(len(pool) == 4, "بیا پیوی pool=%d" % len(pool))
    pool2 = TRIGGER_INDEX[tuple(norm_text("کی دیوونه‌ست").split())][1]
    check(len(pool2) == 4, "کی دیوونه‌ست pool=%d" % len(pool2))
    total = sum(len(v[1]) for v in {id(v): v for v in TRIGGER_INDEX.values()}.values())
    check(total == 198, "responses in index: %d" % total)
    return "%d keys" % len(TRIGGER_INDEX)


@selftest("تشخیص Trigger: طولانی‌ترین، نرمال‌سازی، slack")
def _t_p3_matching():
    f = lambda s: find_trigger(norm_text(s).split())
    check(f("سلام")[0] == "سلام")
    check(f("سلام بچه ها")[0] == "سلام", "slack")
    check(f("پول ندارم")[0] == "پول ندارم", "longest match")
    check(f("پول داری؟")[0] == "پول", "prefix")
    check(f("سلام من امروز رفتم مدرسه") is None, "long sentence must not match")
    check(f("كي بيداره؟")[0] == "کی بیداره", "arabic letters")
    check(f("می‌میرم از خنده") is None, "exact-only trigger matched with extra words")
    check(f("می‌میرم")[0] == "می‌میرم")
    check(f("خداحافظ") is not None and f("مرسی") is not None, "aliases")
    check(f("کی خوش‌شانسه")[0] == "کی خوش‌شانسه")
    check(_ki_word(["کی", "گاو"]) == "گاو" and _ki_word(["کی", "میاد"]) is None, "generic ki")
    return "ok"


@selftest("Response Engine: احتمال، Cooldown، خاموش، خطاب مستقیم")
def _t_p3_engine():
    api, g = APP.api, -9301
    upsert_group({"id": g, "title": "E", "type": "group"})
    old_bu, old_rand = APP.bot_username, random.random
    APP.bot_username = "safabat_bot"
    random.random = lambda: 0.0
    try:
        set_setting(g, "reply_chance", 100)
        handle_update(_p2_msg("سلام", 9301, g, mid=1))
        check(len(_p2_calls(api, "send_message")) == 1, "no reply at 100%")
        handle_update(_p2_msg("سلام", 9302, g, mid=2))
        check(len(_p2_calls(api, "send_message")) == 1, "chat cooldown ignored")
        COOLDOWNS.clear()
        set_setting(g, "reply_chance", 0)
        random.random = old_rand
        for i in range(20):
            handle_update(_p2_msg("سلام", 9303, g, mid=3 + i))
        check(len(_p2_calls(api, "send_message")) == 1, "replied at 0% chance")
        handle_update(_p2_msg("@safabat_bot سلام", 9304, g, mid=30))
        check(len(_p2_calls(api, "send_message")) == 2, "mention must always get a reply")
        random.random = lambda: 0.0
        set_setting(g, "reply_chance", 100)
        set_setting(g, "auto_reply", 0)
        COOLDOWNS.clear()
        handle_update(_p2_msg("سلام", 9305, g, mid=31))
        handle_update(_p2_msg("@safabat_bot سلام", 9306, g, mid=32))
        check(len(_p2_calls(api, "send_message")) == 2, "auto_reply OFF still replies")
        handle_update(_p2_msg("سلام", 9307, 9307, ctype="private", mid=33))
        check(len(_p2_calls(api, "send_message")) == 3, "private chat must reply")
    finally:
        APP.bot_username, random.random = old_bu, old_rand
    return "ok"


@selftest("Response Engine: [کلمه] و [آیدی یکی از اعضا]")
def _t_p3_placeholders():
    g = -9311
    upsert_group({"id": g, "title": "P", "type": "group"})
    upsert_user({"id": 9311, "first_name": "Speaker"})
    upsert_user({"id": 9312, "first_name": "Other", "username": "other_u"})
    STATS.user_touch(g, 9311)
    STATS.user_touch(g, 9312)
    STATS.flush()
    ctx = Ctx("message", {"id": g, "type": "group"}, {"id": 9311, "first_name": "Speaker"}, {})
    for _, resps in TRIGGER_DATA:
        for r in resps:
            out = fill_response(r, ctx, "گاو")
            check("[" not in out and "]" not in out, "placeholder leaked: " + out)
    check("@other_u" in fill_response("به نظر من [آیدی یکی از اعضا] اسکله", ctx), "member mention")
    check("گاوعه" in fill_response("به نظر من x [کلمه]عه", ctx, "گاو"), "word suffix")
    check(fill_response("[کلمه]عه", ctx, "خفنه") == "خفنه", "word ending with ه")
    old_rand = random.random
    random.random = lambda: 0.0
    try:
        set_setting(g, "reply_chance", 100)
        handle_update(_p2_msg("کی گاو؟", 9311, g, mid=1))
    finally:
        random.random = old_rand
    out = _p3_last_text(APP.api)
    # دو قالب داریم: یکی کلمه را دارد و دیگری ندارد (طبق مشخصات)؛ هر دو باید عضو را منشن کنند
    check(out and "[" not in out and "@other_u" in out, "generic reply: %r" % out)
    check(out.startswith("رأی من") or "گاو" in out, "generic reply shape: %r" % out)
    return "ok"


@selftest("بانک جوک و فال: تعداد، دسته‌ها، Seed تکراری")
def _t_p3_banks():
    check(set(JOKE_BANK) == set(JOKE_CAT_LABELS), "joke categories mismatch")
    check(set(FORTUNE_BANK) == set(FORTUNE_CAT_LABELS), "fortune categories mismatch")
    check(all(len(v) >= 10 for v in JOKE_BANK.values()), "each joke category needs >=10")
    check(all(len(v) >= 9 for v in FORTUNE_BANK.values()), "each fortune category needs >=9")
    check(all(JOKE_MIN_LEN <= len(t) <= JOKE_MAX_LEN for v in JOKE_BANK.values() for t in v), "joke length")
    nj, nf = seed_content()
    check(nj == sum(len(v) for v in JOKE_BANK.values()), "duplicate jokes inside bank (%d)" % nj)
    check(nf == sum(len(v) for v in FORTUNE_BANK.values()), "duplicate fortunes inside bank (%d)" % nf)
    check(seed_content() == (0, 0), "seed not idempotent")
    return "%d jokes, %d fortunes" % (nj, nf)


@selftest("جوک: نمایش، رأی، تغییر رأی، رأی به جوک خودی، جوک‌متر")
def _t_p3_joke_flow():
    api, g = APP.api, -9321
    seed_content()
    check(round(joke_score(142, 19)) == 87, "jokemeter formula")
    check(_joke_weight(90, 10) > _joke_weight(30, 10) > 0, "weights")
    handle_update(_p2_cb("p3-j1", "jk:n:school", 9321, g, 41))
    text = _p3_last_text(api, "edit_message_text", 2)
    check("جوک‌متر" in text and "😐" in text, "joke not shown")
    jid = int(re.search(r"#(\d+)", text).group(1))
    check(APP.db.val("SELECT category FROM jokes WHERE id=?", (jid,)) == "school", "wrong category")
    check(APP.db.val("SELECT shown FROM jokes WHERE id=?", (jid,)) == 1, "shown counter")
    handle_update(_p2_cb("p3-j2", "jk:v:%d:1:school" % jid, 9322, g, 41))
    j = APP.db.one("SELECT laughs, mehs, score FROM jokes WHERE id=?", (jid,))
    check(j["laughs"] == 1 and j["mehs"] == 0 and abs(j["score"] - joke_score(1, 0)) < 0.01, "laugh vote")
    handle_update(_p2_cb("p3-j3", "jk:v:%d:1:school" % jid, 9322, g, 42))
    check(APP.db.val("SELECT laughs FROM jokes WHERE id=?", (jid,)) == 1, "duplicate vote counted")
    handle_update(_p2_cb("p3-j4", "jk:v:%d:0:school" % jid, 9322, g, 43))
    j = APP.db.one("SELECT laughs, mehs FROM jokes WHERE id=?", (jid,))
    check(j["laughs"] == 0 and j["mehs"] == 1, "vote switch")
    STATS.flush()
    check(user_stat_get(g, 9322, "joke_votes") == 1, "joke_votes stat counted twice")
    APP.db.run("UPDATE jokes SET submitted_by=9323 WHERE id=?", (jid,))
    handle_update(_p2_cb("p3-j5", "jk:v:%d:1:school" % jid, 9323, g, 44))
    check(APP.db.val("SELECT COUNT(*) FROM joke_votes WHERE user_id=9323") == 0, "self vote allowed")
    handle_update(_p2_cb("p3-j6", "jk:sh:%d" % (jid + 1), 9321, g, 45))
    handle_update(_p2_msg("/joke", 9324, g, mid=46))
    check("جوک‌متر" in _p3_last_text(api), "/joke command")
    return "ok"


@selftest("ارسال جوک: Pending، تأیید/رد/حذف ادمین، Duplicate")
def _t_p3_submit():
    global ADMIN_ID
    api, g, u = APP.api, -9331, 9331
    old_admin, ADMIN_ID = ADMIN_ID, 8801
    try:
        seed_content()
        handle_update(_p2_cb("p3-s1", "jk:sub", u, g, 51))
        cbs = [b["callback_data"] for r in _p2_calls(api, "edit_message_text")[-1][1][3]["inline_keyboard"] for b in r]
        check("jk:sc:school" in cbs, "category buttons")
        handle_update(_p2_cb("p3-s2", "jk:sc:school", u, g, 51))
        st = input_get(g, u)
        check(st and st["kind"] == "input:joke_submit", "input state not set")
        handle_update(_p2_msg("کوتاه", u, g, mid=60))
        check(input_get(g, u) is not None, "short text should keep waiting")
        joke = "یه جوک تستی که به اندازه کافی بلنده تا قبول بشه 😂"
        handle_update(_p2_msg(joke, u, g, mid=61))
        r = APP.db.one("SELECT * FROM jokes WHERE status='pending'")
        check(r and r["category"] == "school" and r["submitted_by"] == u, "pending joke not stored")
        check(input_get(g, u) is None, "input not cleared")
        notes = [c for c in api.calls if c[0] == "try_call" and c[1][0] == "sendMessage"]
        check(notes and notes[-1][1][1]["chat_id"] == 8801 and "jk:ap:%d" % r["id"] in json.dumps(notes[-1][1][1]), "admin not notified")
        handle_update(_p2_cb("p3-s3", "jk:sc:school", u, g, 52))
        handle_update(_p2_msg(joke, u, g, mid=62))
        check(APP.db.val("SELECT COUNT(*) FROM jokes WHERE status='pending'") == 1, "duplicate accepted")
        handle_update(_p2_cb("p3-s4", "jk:ap:%d" % r["id"], 9332, g, 53))
        check(APP.db.val("SELECT status FROM jokes WHERE id=?", (r["id"],)) == "pending", "non-admin approved")
        handle_update(_p2_cb("p3-s5", "jk:ap:%d" % r["id"], 8801, 8801, 54, ctype="private"))
        check(APP.db.val("SELECT status FROM jokes WHERE id=?", (r["id"],)) == "approved", "admin approve failed")
        handle_update(_p2_cb("p3-s6", "jk:dl:%d" % r["id"], 8801, 8801, 55, ctype="private"))
        check(APP.db.val("SELECT status FROM jokes WHERE id=?", (r["id"],)) == "deleted", "admin delete failed")
        handle_update(_p2_cb("p3-s7", "jk:sc:school", u, g, 56))
        handle_update(_p2_msg(joke, u, g, mid=63))
        check(APP.db.val("SELECT COUNT(*) FROM jokes WHERE text=?", (joke,)) == 1, "deleted joke resubmitted")
        victim = APP.db.val("SELECT id FROM jokes WHERE status='approved' LIMIT 1")
        handle_update(_p2_msg("/deljoke %d" % victim, 8801, 8801, ctype="private", mid=64))
        check(APP.db.val("SELECT status FROM jokes WHERE id=?", (victim,)) == "deleted", "/deljoke")
        handle_update(_p2_msg("/deljoke %d" % victim, 9399, 9399, ctype="private", mid=65))
    finally:
        ADMIN_ID = old_admin
    return "ok"


@selftest("فال: دسته‌ها، Cooldown، خاموش بودن")
def _t_p3_fortune():
    api, g = APP.api, -9341
    seed_content()
    upsert_group({"id": g, "title": "F", "type": "group"})
    handle_update(_p2_cb("p3-f1", "fa:c:love", 9341, g, 71))
    text = _p3_last_text(api, "edit_message_text", 2)
    check("عشق" in text and "سرگرمی" in text, "fortune text")
    n = len(_p2_calls(api, "edit_message_text"))
    handle_update(_p2_cb("p3-f2", "fa:c:love", 9341, g, 72))
    check(len(_p2_calls(api, "edit_message_text")) == n, "fortune cooldown ignored")
    handle_update(_p2_cb("p3-f3", "fa:c:all", 9342, g, 73))
    check(len(_p2_calls(api, "edit_message_text")) == n + 1, "random fortune")
    handle_update(_p2_cb("p3-f4", "fa:cats", 9343, g, 74))
    cbs = [b["callback_data"] for r in _p2_calls(api, "edit_message_text")[-1][1][3]["inline_keyboard"] for b in r]
    check(all(("fa:c:" + k) in cbs for k, _, _ in FORTUNE_CATEGORIES), "all 10 categories reachable")
    set_setting(g, "fortune", 0)
    k = len(_p2_calls(api, "edit_message_text"))
    handle_update(_p2_cb("p3-f5", "fa:c:money", 9344, g, 75))
    check(len(_p2_calls(api, "edit_message_text")) == k, "fortune OFF still works")
    handle_update(_p2_msg("/fal", 9345, 9345, ctype="private", mid=76))
    check("فال" in _p3_last_text(api), "/fal in private")
    return "ok"


@selftest("منوی سرگرمی: دکمه‌ها و خاموش بودن")
def _t_p3_fun_menu():
    api, g = APP.api, -9351
    upsert_group({"id": g, "title": "M", "type": "group"})
    check("menu:fun" in json.dumps(main_menu_markup()), "fun missing from main menu")
    handle_update(_p2_cb("p3-m1", "menu:fun", 9351, g, 81))
    cbs = [b["callback_data"] for r in _p2_calls(api, "edit_message_text")[-1][1][3]["inline_keyboard"] for b in r]
    check("jk:n:all" in cbs and "fa:c:all" in cbs and "jk:sub" in cbs, "fun buttons: %s" % cbs)
    set_setting(g, "jokes", 0)
    set_setting(g, "fortune", 0)
    k = len(_p2_calls(api, "edit_message_text"))
    handle_update(_p2_cb("p3-m2", "menu:fun", 9352, g, 82))
    check(len(_p2_calls(api, "edit_message_text")) == k, "fun menu should be blocked")
    return "ok"


# ══════════════════════════════════════════════════════════════════
# ███ END OF PHASE 3


# ══════════════════════════════════════════════════════════════════
# ███ PHASE 4 — GAME ENGINE (همه بازی‌ها)
# ══════════════════════════════════════════════════════════════════
import secrets

GAME_XP = {
    "number_win": 40, "number_bonus": 5, "word_win": 35, "word_bonus": 3,
    "riddle": 25, "trivia": 20, "emoji": 20, "country": 20,
    "speed": 30, "speed_fast": 10, "math_each": 8, "math_perfect": 12,
    "rps_win": 25, "rps_draw": 8, "dice_win": 20, "dice_draw": 8,
    "duel_win": 40, "duel_draw": 10, "group_correct": 15, "group_first": 10, "lose": 3,
}
GAME_DAILY_XP_CAP = 800          # سقف XP بازی‌ها برای هر نفر در هر گروه در روز
GAME_START_COOLDOWN = 2
NUMBER_MAX_TRIES = 7
HANGMAN_LIVES = 6
SOLO_TTL = 600
ALPHABET = "ابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی"
SR = random.SystemRandom()       # تصادفی غیرقابل پیش‌بینی (ضد تقلب)
GAME_STARTERS = {}               # کلید بازی -> تابع شروع (ctx)
GAME_RESULT_HOOKS = []           # فازهای بعدی (دستاورد/مأموریت/سطح) اینجا وصل می‌شوند
PAIR_REWARDS = {}


def game_starter(key):
    def deco(fn):
        if key in GAME_STARTERS:
            raise RuntimeError("duplicate game: " + key)
        GAME_STARTERS[key] = fn
        return fn
    return deco


def on_game_result(fn):
    GAME_RESULT_HOOKS.append(fn)
    return fn


# ─── Session بازی ───
def new_sid():
    return "g" + secrets.token_hex(4)


def game_kind(key):
    return "game:" + key


def game_start(ctx, key, data, ttl=SOLO_TTL, solo=True):
    """session جدید. برای بازی‌های تک‌نفره، جلسه‌ی قبلی همان کاربر پاک می‌شود."""
    if solo:
        for s in session_find(ctx.chat_id, game_kind(key), ctx.user_id):
            session_del(s["sid"])
    sid = new_sid()
    d = dict(data)
    d["owner"] = int(ctx.user_id)
    session_put(sid, ctx.chat_id, ctx.user_id, game_kind(key), d, ttl)
    return sid


def session_get_raw(sid):
    r = APP.db.one("SELECT * FROM sessions WHERE sid=?", (sid,))
    if r is None:
        return None
    try:
        data = json.loads(r["data"])
    except (TypeError, ValueError):
        data = {}
    return {"sid": r["sid"], "chat_id": r["chat_id"], "user_id": r["user_id"], "kind": r["kind"],
            "data": data, "created_at": r["created_at"], "expires_at": r["expires_at"]}


def game_load(ctx, sid, key=None, owned=False):
    s = session_get(sid)
    ok = (s is not None and s["chat_id"] == int(ctx.chat_id) and s["kind"].startswith("game:")
          and (key is None or s["kind"] == game_kind(key)))
    if not ok:
        ctx.answer("⌛ این بازی تموم شده یا منقضی شده", True)
        return None
    if owned and not callback_owner_ok(ctx, s["data"].get("owner", 0)):
        return None
    return s


def game_save(s, data):
    session_put(s["sid"], s["chat_id"], s["user_id"], s["kind"], data, max(5, s["expires_at"] - now()))


def game_end_markup(key, extra=None):
    rows = [[btn("🔁 دوباره", "gm:" + key), btn("🎮 بازی‌ها", "gm:hub")]]
    if extra:
        rows.insert(0, extra)
    rows.append(home_row())
    return kb(*rows)


# ─── XP / ثبت نتیجه ───
def xp_multiplier(chat_id):
    t = now()
    rows = APP.db.all("SELECT multiplier FROM events WHERE chat_id IN (?, 0) AND active=1 AND starts_at<=? AND ends_at>?",
                      (int(chat_id), t, t))
    m = 1.0
    for r in rows:
        m *= float(r["multiplier"] or 1)
    return min(m, 4.0)


def pair_reward_ok(a, b, limit=3):
    day = day_key()
    if len(PAIR_REWARDS) > 500:
        for k in [k for k in PAIR_REWARDS if k[0] != day]:
            del PAIR_REWARDS[k]
    k = (day, min(int(a), int(b)), max(int(a), int(b)))
    if PAIR_REWARDS.get(k, 0) >= limit:
        return False
    PAIR_REWARDS[k] = PAIR_REWARDS.get(k, 0) + 1
    return True


def record_result(chat_id, user_id, game_key, result, score=0, xp=0, token=None):
    """نتیجه‌ی بازی را یک‌بار ثبت می‌کند (token = جلوگیری از پاداش تکراری).
    result: win | loss | draw. خروجی: dict شامل xp واقعی، capped، dup."""
    cid, uid, t = int(chat_id), int(user_id), now()
    if token is not None and not reward_once("gr:%s:%d" % (token, uid)):
        return {"xp": 0, "dup": True, "capped": False, "result": result, "game_key": game_key,
                "chat_id": cid, "user_id": uid, "score": score}
    raw = int(round(xp * xp_multiplier(cid))) if xp > 0 else 0
    used = APP.db.val("SELECT COALESCE(SUM(xp),0) FROM game_records WHERE chat_id=? AND user_id=? AND created_at>=?",
                      (cid, uid, day_start(t)), 0)
    granted = max(0, min(raw, GAME_DAILY_XP_CAP - used))
    win = 1 if result == "win" else 0
    with APP.db.tx():
        APP.db.run("INSERT INTO game_records(chat_id, user_id, game_key, result, score, xp, created_at) VALUES(?,?,?,?,?,?,?)",
                   (cid, uid, game_key, result, int(score), granted, t))
        APP.db.run("INSERT OR IGNORE INTO user_stats(chat_id, user_id, first_active) VALUES(?,?,?)", (cid, uid, t))
        APP.db.run(
            """UPDATE user_stats SET games_played=games_played+1, games_won=games_won+?, games_lost=games_lost+?,
               draws=draws+?, streak=CASE WHEN ?=1 THEN streak+1 ELSE 0 END,
               best_streak=MAX(best_streak, CASE WHEN ?=1 THEN streak+1 ELSE 0 END),
               xp=xp+?, last_active=? WHERE chat_id=? AND user_id=?""",
            (win, 1 if result == "loss" else 0, 1 if result == "draw" else 0, win, win, granted, t, cid, uid))
        if granted > 0:
            APP.db.run(
                """INSERT INTO league_scores(chat_id, week_key, user_id, points) VALUES(?,?,?,?)
                   ON CONFLICT(chat_id, week_key, user_id) DO UPDATE SET points=points+excluded.points""",
                (cid, week_key(t), uid, granted))
        APP.db.run("UPDATE games SET plays=plays+1 WHERE game_key=?", (game_key,))
    STATS.group_add(cid, "game_plays")
    STATS.group_add(cid, "game:" + game_key)
    if win:
        STATS.group_add(cid, "game_wins")
    info = {"xp": granted, "dup": False, "capped": raw > granted, "result": result, "game_key": game_key,
            "chat_id": cid, "user_id": uid, "score": score}
    for fn in list(GAME_RESULT_HOOKS):
        _safe_run(fn, info)
    return info


def xp_line(info):
    if info.get("dup"):
        return ""
    if info["xp"] > 0:
        return "\n⭐ +%d XP" % info["xp"]
    if info.get("capped"):
        return "\n⭐ سقف XP امروزت پر شده؛ فردا ادامه بده 😉"
    return ""


def uname(uid):
    return display_name(get_user(uid))


# ─── مرکز بازی ───
def hub_text(ctx):
    r = APP.db.one("SELECT games_played, games_won, streak FROM user_stats WHERE chat_id=? AND user_id=?",
                   (int(ctx.chat_id), int(ctx.user_id)))
    line = ("\n\n🎮 بازی‌های تو: %d • 🥇 برد: %d • 🔥 Streak: %d" % (r["games_played"], r["games_won"], r["streak"])) if r else ""
    return "🎮 مرکز بازی\n\nیه بازی انتخاب کن 👇" + line


def hub_markup():
    rows, cur = [], []
    for key, emoji, title in GAME_CATALOG:
        if key in GAME_STARTERS:
            cur.append(btn("%s %s" % (emoji, title), "gm:" + key))
            if len(cur) == 2:
                rows.append(cur)
                cur = []
    if cur:
        rows.append(cur)
    rows.append(home_row())
    return kb(*rows)


def show_hub(ctx):
    STATS.group_add(ctx.chat_id, "feat:games")
    show(ctx, hub_text(ctx), hub_markup())


@section("games", feature="games")
def sec_games(ctx):
    show_hub(ctx)


@callback("gm")
def cb_games(ctx):
    a = ctx.parts[1] if len(ctx.parts) > 1 else "hub"
    if a == "hub":
        if require_feature(ctx, "games"):
            show_hub(ctx)
    elif a == "quit":
        _game_quit(ctx, ctx.parts[2] if len(ctx.parts) > 2 else "")
    elif a in GAME_STARTERS:
        if not require_feature(ctx, "games"):
            return
        if not cooldown_ok(("gs", ctx.chat_id, ctx.user_id), GAME_START_COOLDOWN):
            ctx.answer("⏳ یه لحظه صبر کن")
            return
        GAME_STARTERS[a](ctx)
    else:
        ctx.answer("این بازی فعال نیست", True)


def _game_quit(ctx, sid):
    s = game_load(ctx, sid, owned=True)
    if s is None:
        return
    key = s["kind"][5:]
    d = s["data"]
    if key == "number_guess":
        input_clear(ctx.chat_id, ctx.user_id)
        info = record_result(ctx.chat_id, ctx.user_id, key, "loss", d.get("tries", 0), 0, token=sid)
        text = "🏳️ تسلیم شدی! عدد %d بود." % d["secret"]
    elif key == "word_guess":
        info = record_result(ctx.chat_id, ctx.user_id, key, "loss", 0, 0, token=sid)
        text = "🏳️ تسلیم شدی! کلمه «%s» بود." % d["word"]
    else:
        ctx.answer("این بازی تسلیم نداره", True)
        return
    session_del(sid)
    show(ctx, text, game_end_markup(key))


# ─── 🎯 حدس عدد ───
def number_hint(guess, secret):
    diff = abs(guess - secret)
    direction = "⬆️ بالاتره" if secret > guess else "⬇️ پایین‌تره"
    if diff <= 3:
        heat = "🔥🔥 خیلی نزدیکی!"
    elif diff <= 10:
        heat = "🔥 نزدیک شدی"
    elif diff >= 40:
        heat = "🧊 خیلی دوری"
    else:
        heat = ""
    return direction + ((" — " + heat) if heat else "")


@game_starter("number_guess")
def start_number(ctx):
    sid = game_start(ctx, "number_guess", {"secret": SR.randint(1, 100), "tries": 0})
    input_set(ctx.chat_id, ctx.user_id, "game_num", {"sid": sid}, ttl=SOLO_TTL)
    show(ctx, "🎯 حدس عدد\n\nیه عدد بین ۱ تا ۱۰۰ انتخاب کردم. تو %d تا حدس داری.\n"
              "عددت رو همینجا بنویس و بفرست ✍️" % NUMBER_MAX_TRIES,
         kb([btn("🏳️ تسلیم", "gm:quit:" + sid)]))


@on_input("game_num")
def input_number(ctx, data):
    t = norm_text(ctx.text).replace(" ", "")
    if not t.isdigit() or len(t) > 3:
        return False  # حرف عادی؛ مزاحم گفتگو نشو
    s = session_get(data.get("sid", ""))
    if s is None or s["chat_id"] != int(ctx.chat_id) or s["kind"] != game_kind("number_guess"):
        input_clear(ctx.chat_id, ctx.user_id)
        return False
    guess = int(t)
    if not 1 <= guess <= 100:
        ctx.reply("عدد باید بین ۱ تا ۱۰۰ باشه 🙂")
        return True
    d = s["data"]
    d["tries"] += 1
    left = NUMBER_MAX_TRIES - d["tries"]
    if guess == d["secret"]:
        info = record_result(ctx.chat_id, ctx.user_id, "number_guess", "win", d["tries"],
                             GAME_XP["number_win"] + left * GAME_XP["number_bonus"], token=s["sid"])
        session_del(s["sid"])
        input_clear(ctx.chat_id, ctx.user_id)
        ctx.reply("🎉 آفرین! عدد %d بود و با %d حدس پیداش کردی." % (guess, d["tries"]) + xp_line(info),
                  game_end_markup("number_guess"))
    elif left <= 0:
        info = record_result(ctx.chat_id, ctx.user_id, "number_guess", "loss", d["tries"], GAME_XP["lose"], token=s["sid"])
        session_del(s["sid"])
        input_clear(ctx.chat_id, ctx.user_id)
        ctx.reply("😅 حدس‌هات تموم شد! عدد %d بود." % d["secret"] + xp_line(info), game_end_markup("number_guess"))
    else:
        game_save(s, d)
        ctx.reply("%s\n🎯 حدس‌های باقی‌مونده: %d" % (number_hint(guess, d["secret"]), left),
                  kb([btn("🏳️ تسلیم", "gm:quit:" + s["sid"])]))
    return True


# ─── 🔤 حدس کلمه (دکمه‌ای) ───
WORD_BANK = {
    "میوه": ["سیب", "گلابی", "انگور", "هلو", "انار", "توت", "خربزه", "هندوانه", "پرتقال", "نارنگی", "گیلاس", "انجیر", "موز", "لیمو", "خرما"],
    "حیوان": ["شیر", "ببر", "فیل", "گرگ", "روباه", "خرس", "زرافه", "پلنگ", "عقاب", "کبوتر", "دلفین", "کروکودیل", "میمون", "خرگوش", "قورباغه", "پروانه", "عنکبوت", "شترمرغ", "طوطی", "کانگورو", "سنجاب"],
    "شهر ایران": ["تهران", "شیراز", "اصفهان", "تبریز", "مشهد", "کرمان", "یزد", "اهواز", "رشت", "ساری", "زنجان", "همدان", "بوشهر", "ارومیه", "سمنان", "بندرعباس"],
    "غذا": ["کباب", "قورمه", "قیمه", "پلو", "سوپ", "کوکو", "دلمه", "لازانیا", "ماکارونی", "پیتزا", "ساندویچ", "سمبوسه", "فلافل", "حلوا", "بستنی", "شکلات", "پنیر", "عدس", "حلیم"],
    "وسایل": ["کتاب", "مداد", "میز", "صندلی", "تلویزیون", "یخچال", "کامپیوتر", "موبایل", "ساعت", "عینک", "چتر", "کلید", "قفل", "دوربین", "چراغ", "پنجره", "قیچی"],
    "طبیعت": ["دریا", "کوه", "جنگل", "بیابان", "رودخانه", "زمین", "ستاره", "خورشید", "باران", "برف", "طوفان", "غار", "صحرا"],
    "شغل": ["دکتر", "معلم", "پلیس", "نجار", "خلبان", "راننده", "قصاب", "نقاش", "مهندس", "پرستار", "کشاورز"],
    "ورزش": ["فوتبال", "والیبال", "بسکتبال", "شنا", "کشتی", "تنیس", "شطرنج", "بوکس", "کاراته", "ژیمناستیک"],
}


def word_text(d):
    shown = " ".join(c if c in d["guessed"] else "_" for c in d["word"])
    wrong = [c for c in d["guessed"] if c not in d["word"]]
    return ("🔤 حدس کلمه\n\n💡 دسته: %s\n🔡 %d حرفی\n\n%s\n\n%s\n❌ اشتباه‌ها: %s"
            % (d["cat"], len(d["word"]), shown, "❤️" * d["lives"] + "🖤" * (HANGMAN_LIVES - d["lives"]),
               " ".join(wrong) if wrong else "—"))


def word_markup(sid, d):
    rows, cur = [], []
    for i, ch in enumerate(ALPHABET):
        if ch in d["guessed"]:
            continue
        cur.append(btn(ch, "wg:%s:%d" % (sid, i)))
        if len(cur) == 6:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([btn("🏳️ تسلیم", "gm:quit:" + sid)])
    return kb(*rows)


@game_starter("word_guess")
def start_word(ctx):
    cat = SR.choice(list(WORD_BANK))
    d = {"word": SR.choice(WORD_BANK[cat]), "cat": cat, "guessed": [], "lives": HANGMAN_LIVES}
    sid = game_start(ctx, "word_guess", d)
    show(ctx, word_text(d), word_markup(sid, d))


@callback("wg")
def cb_word(ctx):
    try:
        sid, i = ctx.parts[1], int(ctx.parts[2])
        letter = ALPHABET[i]
    except (IndexError, ValueError):
        ctx.answer("دکمه نامعتبر", True)
        return
    s = game_load(ctx, sid, "word_guess", owned=True)
    if s is None:
        return
    d = s["data"]
    if letter in d["guessed"]:
        ctx.answer("این حرف رو قبلاً زدی")
        return
    d["guessed"].append(letter)
    hit = letter in d["word"]
    if not hit:
        d["lives"] -= 1
    if all(c in d["guessed"] for c in d["word"]):
        info = record_result(ctx.chat_id, ctx.user_id, "word_guess", "win", d["lives"],
                             GAME_XP["word_win"] + d["lives"] * GAME_XP["word_bonus"], token=sid)
        session_del(sid)
        ctx.edit("🎉 درست حدس زدی: «%s»\n❤️ جون باقی‌مونده: %d" % (d["word"], d["lives"]) + xp_line(info),
                 game_end_markup("word_guess"))
    elif d["lives"] <= 0:
        info = record_result(ctx.chat_id, ctx.user_id, "word_guess", "loss", 0, GAME_XP["lose"], token=sid)
        session_del(sid)
        ctx.edit("💀 جون‌هات تموم شد! کلمه «%s» بود." % d["word"] + xp_line(info), game_end_markup("word_guess"))
    else:
        game_save(s, d)
        ctx.answer("✅ هست!" if hit else "❌ نیست")
        ctx.edit(word_text(d), word_markup(sid, d))


# ─── بانک سؤال‌ها ───
# (سؤال، جواب درست، [سه گزینه‌ی غلط])
RIDDLES = [
    ("چیست که هرچه بیشتر از آن برداری، بزرگ‌تر می‌شود؟", "چاله", ["کوه", "دیوار", "آب"]),
    ("چیست که پا ندارد اما راه می‌رود؟", "ساعت", ["ماشین", "توپ", "کفش"]),
    ("چیست که دندان دارد ولی گاز نمی‌گیرد؟", "شانه", ["سگ", "گوسفند", "عروسک"]),
    ("چیست که هرچه بیشتر خشک می‌کند، خودش خیس‌تر می‌شود؟", "حوله", ["لباس", "خورشید", "کاغذ"]),
    ("چیست که همیشه جلوی ماست ولی نمی‌توانیم ببینیمش؟", "آینده", ["دیوار", "هوا", "سایه"]),
    ("چه چیزی با گفتنش از بین می‌رود؟", "سکوت", ["موسیقی", "باد", "آب"]),
    ("چیست که یک دهان دارد ولی هیچ‌وقت غذا نمی‌خورد؟", "رودخانه", ["شیر", "انسان", "ماهی"]),
    ("کدام ماه‌ها ۲۸ روز دارند؟", "همه‌ی ماه‌ها", ["فقط بهمن", "فقط اسفند", "فقط دی"]),
    ("چیست که سر دارد ولی مغز ندارد؟", "کبریت", ["شیر", "ماهی", "کلاه"]),
    ("چه چیزی همیشه می‌آید ولی هیچ‌وقت نمی‌رسد؟", "فردا", ["دیروز", "امروز", "ساعت"]),
    ("چیست که شکسته نمی‌شود حتی اگر هیچ‌وقت نیفتد؟", "قول", ["شیشه", "تخم‌مرغ", "لیوان"]),
    ("چیست که بالا می‌رود ولی هیچ‌وقت پایین نمی‌آید؟", "سن", ["توپ", "هواپیما", "بادکنک"]),
    ("چیست که مال توست ولی دیگران بیشتر از تو از آن استفاده می‌کنند؟", "اسم", ["پول", "موبایل", "لباس"]),
    ("هرچه بیشتر داشته باشی، کمتر می‌بینی؛ چیست؟", "تاریکی", ["نور", "پول", "دوست"]),
    ("چه چیزی را می‌شود گرفت ولی نمی‌شود پرتاب کرد؟", "سرما", ["توپ", "سنگ", "نور"]),
    ("هر شب می‌آید ولی هیچ‌وقت نمی‌ماند؛ چیست؟", "خواب", ["ستاره", "نور", "سکوت"]),
]

TRIVIA_CATS = [
    ("football", "⚽", "فوتبال"), ("countries", "🌍", "کشورها"), ("history", "📜", "تاریخ"),
    ("science", "🔬", "علوم"), ("tech", "💻", "تکنولوژی"), ("cinema", "🎬", "سینما"),
    ("games", "🎮", "بازی‌ها"), ("general", "🧠", "اطلاعات عمومی"),
]
TRIVIA_LABELS = {k: t for k, _, t in TRIVIA_CATS}

TRIVIA_BANK = {
    "football": [
        ("جام جهانی ۲۰۲۲ در کدام کشور برگزار شد؟", "قطر", ["روسیه", "برزیل", "آلمان"]),
        ("فوتبالیست پرتغالی معروف با پیراهن شماره ۷ و ۵ توپ طلا کیست؟", "کریستیانو رونالدو", ["لوئیس فیگو", "پپه", "ژائو فلیکس"]),
        ("کدام باشگاه بیشترین قهرمانی لیگ قهرمانان اروپا را دارد؟", "رئال مادرید", ["بارسلونا", "میلان", "لیورپول"]),
        ("یک بازی فوتبال معمولاً چند دقیقه است (بدون وقت اضافه)؟", "۹۰ دقیقه", ["۶۰ دقیقه", "۷۵ دقیقه", "۱۲۰ دقیقه"]),
        ("در فوتبال هر تیم چند بازیکن داخل زمین دارد؟", "۱۱ نفر", ["۹ نفر", "۱۰ نفر", "۱۲ نفر"]),
        ("ایران در جام جهانی ۱۹۹۸ در یک بازی تاریخی کدام تیم را شکست داد؟", "آمریکا", ["آلمان", "برزیل", "اسکاتلند"]),
        ("تا پایان جام جهانی ۲۰۲۲ کدام کشور بیشترین قهرمانی جام جهانی را داشت؟", "برزیل", ["آلمان", "ایتالیا", "آرژانتین"]),
    ],
    "countries": [
        ("پایتخت ژاپن کجاست؟", "توکیو", ["اوساکا", "کیوتو", "سئول"]),
        ("پایتخت ترکیه کجاست؟", "آنکارا", ["استانبول", "ازمیر", "بورسا"]),
        ("بزرگ‌ترین کشور جهان از نظر مساحت کدام است؟", "روسیه", ["چین", "کانادا", "آمریکا"]),
        ("پایتخت استرالیا کجاست؟", "کانبرا", ["سیدنی", "ملبورن", "پرت"]),
        ("کدام کشور شکل چکمه دارد؟", "ایتالیا", ["اسپانیا", "یونان", "فرانسه"]),
        ("پایتخت برزیل کجاست؟", "برازیلیا", ["ریو دو ژانیرو", "سائوپائولو", "بوئنوس‌آیرس"]),
        ("پایتخت کانادا کجاست؟", "اتاوا", ["تورنتو", "ونکوور", "مونترال"]),
    ],
    "history": [
        ("جنگ جهانی اول در چه سالی شروع شد؟", "۱۹۱۴", ["۱۹۳۹", "۱۸۷۰", "۱۹۰۵"]),
        ("کوروش کبیر بنیان‌گذار کدام سلسله بود؟", "هخامنشیان", ["ساسانیان", "اشکانیان", "صفویان"]),
        ("تخت جمشید مربوط به کدام دوره است؟", "هخامنشیان", ["ساسانیان", "قاجار", "صفویه"]),
        ("آمریکا در چه سالی استقلال خود را اعلام کرد؟", "۱۷۷۶", ["۱۸۶۵", "۱۷۸۹", "۱۹۰۱"]),
        ("اولین انسانی که روی ماه قدم گذاشت کیست؟", "نیل آرمسترانگ", ["یوری گاگارین", "باز آلدرین", "جان گلن"]),
        ("انقلاب مشروطه ایران در چه سالی شروع شد؟", "۱۲۸۵ شمسی", ["۱۲۹۹ شمسی", "۱۳۵۷ شمسی", "۱۳۲۰ شمسی"]),
    ],
    "science": [
        ("نزدیک‌ترین سیاره به خورشید کدام است؟", "عطارد", ["زهره", "مریخ", "زمین"]),
        ("فرمول شیمیایی آب چیست؟", "H2O", ["CO2", "O2", "NaCl"]),
        ("سریع‌ترین جانور خشکی کدام است؟", "یوزپلنگ", ["شیر", "اسب", "غزال"]),
        ("بدن انسان بالغ چند استخوان دارد؟", "۲۰۶", ["۱۰۶", "۳۰۶", "۲۵۶"]),
        ("کدام گاز بیشترین درصد هوای زمین را دارد؟", "نیتروژن", ["اکسیژن", "کربن‌دی‌اکسید", "هیدروژن"]),
        ("به کدام سیاره «سیاره‌ی سرخ» می‌گویند؟", "مریخ", ["مشتری", "زحل", "نپتون"]),
    ],
    "tech": [
        ("CPU مخفف چیست؟", "واحد پردازش مرکزی", ["حافظه موقت", "کارت گرافیک", "منبع تغذیه"]),
        ("سیستم‌عامل اندروید توسط کدام شرکت توسعه داده می‌شود؟", "گوگل", ["اپل", "مایکروسافت", "نوکیا"]),
        ("WWW مخفف کدام عبارت است؟", "World Wide Web", ["World Web Window", "Wide World Wire", "Web World Wide"]),
        ("هر بایت چند بیت است؟", "۸", ["۴", "۱۶", "۱۰"]),
        ("کدام زبان برنامه‌نویسی نامش یک مار است؟", "پایتون", ["جاوا", "روبی", "سوئیفت"]),
        ("کدام‌یک مرورگر وب است؟", "کروم", ["ویندوز", "اکسل", "اسکایپ"]),
    ],
    "cinema": [
        ("کارگردان فیلم «جدایی نادر از سیمین» کیست؟", "اصغر فرهادی", ["مجید مجیدی", "عباس کیارستمی", "جعفر پناهی"]),
        ("«جدایی نادر از سیمین» اسکار کدام بخش را در سال ۲۰۱۲ برد؟", "بهترین فیلم خارجی‌زبان", ["بهترین کارگردانی", "بهترین فیلمنامه", "بهترین بازیگر"]),
        ("«جک اسپارو» شخصیت کدام مجموعه فیلم است؟", "دزدان دریایی کارائیب", ["هری پاتر", "ارباب حلقه‌ها", "جنگ ستارگان"]),
        ("هری پاتر در کدام مدرسه درس می‌خواند؟", "هاگوارتز", ["دورمسترنگ", "بوباتون", "ریونکلاو"]),
        ("شخصیت اصلی انیمیشن «شیرشاه» چه نام دارد؟", "سیمبا", ["تیمون", "زازو", "پومبا"]),
        ("فیلم «تایتانیک» درباره‌ی غرق شدن چه چیزی است؟", "یک کشتی", ["یک هواپیما", "یک قطار", "یک زیردریایی"]),
    ],
    "games": [
        ("شغل شخصیت اصلی بازی «سوپر ماریو» چیست؟", "لوله‌کش", ["آشپز", "پلیس", "نجار"]),
        ("کدام موجود بازی ماینکرفت منفجر می‌شود؟", "کریپر", ["زامبی", "اسکلت", "اندرمن"]),
        ("فورتنایت اساساً از کدام سبک بازی است؟", "بتل‌رویال", ["ماشین‌سواری", "پازل", "ورزشی"]),
        ("پوکیمون زرد معروف با دم رعدی کدام است؟", "پیکاچو", ["چارمندر", "اسکوئرتل", "بولباسور"]),
        ("در شطرنج کدام مهره فقط مورب حرکت می‌کند؟", "فیل", ["رخ", "اسب", "وزیر"]),
        ("کنسول پلی‌استیشن ساخته‌ی کدام شرکت است؟", "سونی", ["مایکروسافت", "نینتندو", "سگا"]),
    ],
    "general": [
        ("سال کبیسه چند روز دارد؟", "۳۶۶", ["۳۶۵", "۳۶۴", "۳۶۰"]),
        ("بزرگ‌ترین اقیانوس جهان کدام است؟", "آرام", ["اطلس", "هند", "منجمد شمالی"]),
        ("کدام عدد اول است؟", "۱۳", ["۹", "۱۵", "۲۱"]),
        ("یک ساعت چند ثانیه است؟", "۳۶۰۰", ["۶۰۰", "۱۲۰۰", "۷۲۰۰"]),
        ("ترکیب رنگ آبی و زرد چه رنگی می‌شود؟", "سبز", ["نارنجی", "بنفش", "قهوه‌ای"]),
        ("طولانی‌ترین رودخانه‌ی ایران که کاملاً داخل کشور است کدام است؟", "کارون", ["زاینده‌رود", "ارس", "سفیدرود"]),
    ],
}

EMOJI_BANK = [
    ("🦁👑", "شیرشاه", ["علاءالدین", "یخ‌زده", "مولان"]),
    ("🧊👸❄️", "یخ‌زده", ["مولان", "شرک", "سیندرلا"]),
    ("🕷️🧑🏙️", "مرد عنکبوتی", ["بتمن", "سوپرمن", "آیرون‌من"]),
    ("⚡👓🧙", "هری پاتر", ["ارباب حلقه‌ها", "نارنیا", "پرسی جکسون"]),
    ("🚢🧊💔", "تایتانیک", ["آواتار", "گلادیاتور", "پرل هاربر"]),
    ("🐟🔍🐠", "در جستجوی نمو", ["شرک", "ماشین‌ها", "شیرشاه"]),
    ("🍎📱", "اپل", ["سامسونگ", "نوکیا", "شیائومی"]),
    ("⚽🏆🌍", "جام جهانی", ["المپیک", "لیگ برتر", "جام ملت‌ها"]),
    ("🐢🐇🏁", "لاک‌پشت و خرگوش", ["شیر و موش", "روباه و کلاغ", "گرگ و میش"]),
    ("🎅🎄", "کریسمس", ["نوروز", "ولنتاین", "هالووین"]),
    ("🌊🏄☀️", "موج‌سواری", ["شنا", "قایق‌رانی", "ماهیگیری"]),
    ("📚🏫✏️", "مدرسه", ["بیمارستان", "فرودگاه", "رستوران"]),
    ("✈️🌴🏖️", "تعطیلات", ["امتحان", "بیماری", "اضافه‌کاری"]),
    ("🍫🏭🎩", "چارلی و کارخانه شکلات‌سازی", ["هری پاتر", "شرک", "ماتیلدا"]),
    ("🚀🌑👨‍🚀", "سفر به ماه", ["غواصی", "کوهنوردی", "سفر به جنگل"]),
    ("🥶🌨️⛄", "زمستان", ["تابستان", "بهار", "پاییز"]),
    ("🎤🎶🌟", "خواننده", ["نقاش", "آشپز", "پزشک"]),
    ("🏠🔑🚪", "خانه", ["مدرسه", "بیمارستان", "اداره"]),
]

# (کشور، پرچم، پایتخت)
COUNTRIES = [
    ("ایران", "🇮🇷", "تهران"), ("ترکیه", "🇹🇷", "آنکارا"), ("ژاپن", "🇯🇵", "توکیو"), ("برزیل", "🇧🇷", "برازیلیا"),
    ("آلمان", "🇩🇪", "برلین"), ("فرانسه", "🇫🇷", "پاریس"), ("ایتالیا", "🇮🇹", "رم"), ("اسپانیا", "🇪🇸", "مادرید"),
    ("آرژانتین", "🇦🇷", "بوئنوس‌آیرس"), ("انگلیس", "🇬🇧", "لندن"), ("آمریکا", "🇺🇸", "واشینگتن"), ("کانادا", "🇨🇦", "اتاوا"),
    ("روسیه", "🇷🇺", "مسکو"), ("چین", "🇨🇳", "پکن"), ("هند", "🇮🇳", "دهلی نو"), ("مصر", "🇪🇬", "قاهره"),
    ("عربستان", "🇸🇦", "ریاض"), ("کره جنوبی", "🇰🇷", "سئول"), ("مکزیک", "🇲🇽", "مکزیکوسیتی"), ("استرالیا", "🇦🇺", "کانبرا"),
    ("پرتغال", "🇵🇹", "لیسبون"), ("هلند", "🇳🇱", "آمستردام"), ("سوئد", "🇸🇪", "استکهلم"), ("نروژ", "🇳🇴", "اسلو"),
    ("یونان", "🇬🇷", "آتن"), ("آذربایجان", "🇦🇿", "باکو"), ("افغانستان", "🇦🇫", "کابل"), ("عراق", "🇮🇶", "بغداد"),
    ("قطر", "🇶🇦", "دوحه"), ("سوئیس", "🇨🇭", "برن"),
]


# ─── موتور سؤال چندگزینه‌ای ───
def build_choice(q, correct, wrongs):
    opts = [correct] + list(wrongs)[:3]
    SR.shuffle(opts)
    return {"q": q, "opts": opts, "ans": opts.index(correct)}


def make_math(level):
    """سؤال ریاضی امن (بدون eval). خروجی: (سؤال چندگزینه‌ای، عبارت، جواب)."""
    if level <= 1:
        a, b = SR.randint(5, 40), SR.randint(2, 30)
        op = SR.choice("+-")
        if op == "-" and b > a:
            a, b = b, a
        ans = a + b if op == "+" else a - b
        expr = "%d %s %d" % (a, op, b)
    elif level <= 3:
        a, b = SR.randint(2, 12), SR.randint(2, 12)
        ans, expr = a * b, "%d × %d" % (a, b)
    else:
        a, b, c = SR.randint(2, 9), SR.randint(2, 9), SR.randint(1, 30)
        ans, expr = a * b + c, "%d × %d + %d" % (a, b, c)
    wrongs = set()
    while len(wrongs) < 3:
        w = ans + SR.choice([-10, -5, -3, -2, -1, 1, 2, 3, 5, 10])
        if w >= 0 and w != ans:
            wrongs.add(w)
    return build_choice("%s = ؟" % expr, str(ans), [str(w) for w in sorted(wrongs)]), expr, ans


def trivia_question(cat="all"):
    key = cat if cat in TRIVIA_BANK else SR.choice(list(TRIVIA_BANK))
    q, ok, wrong = SR.choice(TRIVIA_BANK[key])
    return key, build_choice(q, ok, wrong)


def country_question():
    name, flag, cap = SR.choice(COUNTRIES)
    wrongs = SR.sample([c[0] for c in COUNTRIES if c[0] != name], 3)
    if SR.random() < 0.5:
        q = "این پرچم مال کدوم کشوره؟\n\n%s" % flag
    else:
        q = "پایتختش «%s»ـه؛ کدوم کشوره؟" % cap
    return build_choice(q, name, wrongs)


def quiz_markup(sid, opts, extra=None):
    short = all(len(o) <= 13 for o in opts)
    rows, cur = [], []
    for i, o in enumerate(opts):
        cur.append(btn(o, "qz:%s:%d" % (sid, i)))
        if not short or len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    if extra:
        rows.append(extra)
    return kb(*rows)


def start_quiz(ctx, key, title, q, xp_key, mode="solo", ttl=180, extra_data=None, new_message=False, extra_row=None):
    data = {"mode": mode, "q": q["q"], "opts": q["opts"], "ans": q["ans"], "xp": xp_key, "title": title, "t0": now()}
    if extra_data:
        data.update(extra_data)
    sid = game_start(ctx, key, data, ttl, solo=mode in ("solo", "mathrun"))
    text = "%s\n\n%s" % (title, q["q"])
    markup = quiz_markup(sid, q["opts"], extra_row(sid) if extra_row else None)
    if new_message:
        res = ctx.send(text, markup)
        s = session_get(sid)
        s["data"]["msg"] = (res or {}).get("message_id")
        game_save(s, s["data"])
    else:
        show(ctx, text, markup)
    return sid


@callback("qz")
def cb_quiz(ctx):
    try:
        sid, i = ctx.parts[1], int(ctx.parts[2])
    except (IndexError, ValueError):
        ctx.answer("دکمه نامعتبر", True)
        return
    s = game_load(ctx, sid)
    if s is None:
        return
    d = s["data"]
    if "opts" not in d or not 0 <= i < len(d["opts"]):
        ctx.answer("گزینه نامعتبر", True)
        return
    handler = {"solo": _quiz_solo, "race": _quiz_race, "group": _quiz_group, "mathrun": _math_answer}.get(d.get("mode"))
    if handler is None:
        ctx.answer("دکمه نامعتبر", True)
        return
    handler(ctx, s, i)


def _quiz_solo(ctx, s, i):
    if not callback_owner_ok(ctx, s["data"].get("owner", 0)):
        return
    d, key = s["data"], s["kind"][5:]
    correct = i == d["ans"]
    session_del(s["sid"])
    info = record_result(ctx.chat_id, ctx.user_id, key, "win" if correct else "loss", 1 if correct else 0,
                         GAME_XP[d["xp"]] if correct else GAME_XP["lose"], token=s["sid"])
    answer = d["opts"][d["ans"]]
    verdict = ("✅ درسته! جواب: %s" if correct else "❌ اشتباه! جواب درست: %s") % answer
    extra = [btn("➡️ سؤال بعدی", "tv:c:" + d.get("cat", "all"))] if key == "trivia" else None
    markup = game_end_markup(key, extra)
    if key == "trivia":  # «دوباره» همان دسته‌ی انتخاب‌شده
        markup["inline_keyboard"][1][0] = btn("🗂️ دسته‌ها", "gm:trivia")
    ctx.edit("%s\n\n%s\n\n%s" % (d["title"], d["q"], verdict) + xp_line(info), markup)


def _quiz_race(ctx, s, i):
    d, uid = s["data"], int(ctx.user_id)
    if uid in d.get("out", []):
        ctx.answer("تو این دور رو از دست دادی 😅", True)
        return
    if i != d["ans"]:
        d.setdefault("out", []).append(uid)
        game_save(s, d)
        ctx.answer("❌ اشتباه! این دور برای تو تموم شد", True)
        return
    session_del(s["sid"])
    elapsed = max(0, now() - d.get("t0", now()))
    xp = GAME_XP["speed"] + (GAME_XP["speed_fast"] if elapsed <= 5 else 0)
    info = record_result(ctx.chat_id, uid, "speed", "win", elapsed, xp, token=s["sid"])
    ctx.edit("%s\n\n%s\n\n🏆 %s برنده شد! (%d ثانیه)\n✅ جواب: %s%s"
             % (d["title"], d["q"], display_name(ctx.user), elapsed, d["opts"][d["ans"]], xp_line(info)),
             kb([btn("⚡ دور بعد", "gm:speed"), btn("🎮 بازی‌ها", "gm:hub")]))


def _quiz_group(ctx, s, i):
    d = s["data"]
    key = str(ctx.user_id)
    if key in d["answers"]:
        ctx.answer("قبلاً جواب دادی 😉")
        return
    d["answers"][key] = [i, max(0, now() - d["t0"])]
    game_save(s, d)
    ctx.answer("✅ جوابت ثبت شد")
    ctx.edit(_group_text(d), quiz_markup(s["sid"], d["opts"], [btn("🏁 پایان", "gg:end:" + s["sid"])]))


def _group_text(d):
    return "%s\n\n%s\n\n👥 %d نفر جواب دادن — تا پایان وقت می‌تونی جواب بدی" % (d["title"], d["q"], len(d["answers"]))


@game_starter("riddle")
def start_riddle(ctx):
    q, ok, wrong = SR.choice(RIDDLES)
    start_quiz(ctx, "riddle", "🧩 معما", build_choice(q, ok, wrong), "riddle")


@game_starter("trivia")
def start_trivia_menu(ctx):
    rows, cur = [], []
    for key, emoji, label in TRIVIA_CATS:
        cur.append(btn("%s %s" % (emoji, label), "tv:c:" + key))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([btn("🎲 همه دسته‌ها", "tv:c:all"), btn("🎮 بازی‌ها", "gm:hub")])
    show(ctx, "🧠 اطلاعات عمومی\n\nدسته‌ی سؤال رو انتخاب کن 👇", kb(*rows))


@callback("tv")
def cb_trivia(ctx):
    cat = ctx.parts[2] if len(ctx.parts) > 2 else "all"
    if not require_feature(ctx, "games"):
        return
    if not cooldown_ok(("gs", ctx.chat_id, ctx.user_id), GAME_START_COOLDOWN):
        ctx.answer("⏳ یه لحظه صبر کن")
        return
    key, q = trivia_question(cat)
    start_quiz(ctx, "trivia", "🧠 اطلاعات عمومی — %s" % TRIVIA_LABELS[key], q, "trivia", extra_data={"cat": cat})


@game_starter("emoji_guess")
def start_emoji(ctx):
    emojis, ok, wrong = SR.choice(EMOJI_BANK)
    start_quiz(ctx, "emoji_guess", "😀 حدس ایموجی", build_choice("این ایموجی‌ها چی میگن؟\n\n%s" % emojis, ok, wrong), "emoji")


@game_starter("country_guess")
def start_country(ctx):
    start_quiz(ctx, "country_guess", "🌍 حدس کشور", country_question(), "country")


# ─── 🔢 ریاضی (۵ سؤال) ───
MATH_ROUNDS = 5


def _math_title(n, score):
    return "🔢 ریاضی — سؤال %d از %d (امتیاز: %d)" % (n + 1, MATH_ROUNDS, score)


@game_starter("math")
def start_math(ctx):
    q, _, _ = make_math(0)
    start_quiz(ctx, "math", _math_title(0, 0), q, "math_each", mode="mathrun", ttl=300, extra_data={"n": 0, "score": 0})


def _math_answer(ctx, s, i):
    if not callback_owner_ok(ctx, s["data"].get("owner", 0)):
        return
    d = s["data"]
    ok = i == d["ans"]
    d["score"] += 1 if ok else 0
    d["n"] += 1
    if d["n"] >= MATH_ROUNDS:
        session_del(s["sid"])
        xp = d["score"] * GAME_XP["math_each"] + (GAME_XP["math_perfect"] if d["score"] == MATH_ROUNDS else 0)
        info = record_result(ctx.chat_id, ctx.user_id, "math", "win" if d["score"] >= 3 else "loss",
                             d["score"], max(xp, GAME_XP["lose"]), token=s["sid"])
        ctx.edit("🔢 ریاضی تموم شد!\n\n%s آخرین جواب\n🏁 امتیاز: %d از %d" % ("✅" if ok else "❌", d["score"], MATH_ROUNDS)
                 + xp_line(info), game_end_markup("math"))
        return
    q, _, _ = make_math(d["n"])
    d.update({"q": q["q"], "opts": q["opts"], "ans": q["ans"], "title": _math_title(d["n"], d["score"])})
    session_put(s["sid"], s["chat_id"], s["user_id"], s["kind"], d, 300)
    ctx.answer("✅ درست" if ok else "❌ غلط")
    ctx.edit("%s\n\n%s" % (d["title"], d["q"]), quiz_markup(s["sid"], d["opts"]))


# ─── ⚡ سرعتی (اولین جواب درست برنده است) ───
SPEED_TTL = 45


def speed_question():
    if SR.random() < 0.5:
        return make_math(SR.randint(0, 3))[0]
    return trivia_question("all")[1]


@game_starter("speed")
def start_speed(ctx):
    if session_find(ctx.chat_id, game_kind("speed")):
        ctx.answer("⚡ یه دور سرعتی هنوز بازه!", True)
        return
    start_quiz(ctx, "speed", "⚡ سرعتی — اولین جواب درست برنده‌ست!", speed_question(), "speed",
               mode="race", ttl=SPEED_TTL, extra_data={"out": []}, new_message=True)
    ctx.answer("⚡ شروع شد!")


# ─── ✊ سنگ کاغذ قیچی (۳ راند) ───
RPS_NAMES = ["✊ سنگ", "📄 کاغذ", "✂️ قیچی"]
RPS_ROUNDS = 3


def rps_outcome(a, b):
    """1 = a برنده، -1 = b برنده، 0 = مساوی (۰ سنگ، ۱ کاغذ، ۲ قیچی)."""
    if a == b:
        return 0
    return 1 if (a - b) % 3 == 1 else -1


def rps_markup(sid):
    return kb([btn(RPS_NAMES[c], "rp:%s:%d" % (sid, c)) for c in range(3)])


def rps_text(d):
    head = "✊ سنگ کاغذ قیچی\n\nراند %d از %d — تو %d | من %d" % (min(d["round"] + 1, RPS_ROUNDS), RPS_ROUNDS, d["u"], d["b"])
    return head + ("\n\n" + "\n".join(d["log"]) if d["log"] else "\n\nانتخابت رو بزن 👇")


@game_starter("rps")
def start_rps(ctx):
    d = {"u": 0, "b": 0, "round": 0, "log": []}
    sid = game_start(ctx, "rps", d)
    show(ctx, rps_text(d), rps_markup(sid))


@callback("rp")
def cb_rps(ctx):
    try:
        sid, c = ctx.parts[1], int(ctx.parts[2])
        assert 0 <= c <= 2
    except (IndexError, ValueError, AssertionError):
        ctx.answer("دکمه نامعتبر", True)
        return
    s = game_load(ctx, sid, "rps", owned=True)
    if s is None:
        return
    d = s["data"]
    bot_c = SR.randrange(3)
    res = rps_outcome(c, bot_c)
    d["u"] += 1 if res == 1 else 0
    d["b"] += 1 if res == -1 else 0
    d["round"] += 1
    d["log"].append("%s %s — %s  %s" % ("راند %d:" % d["round"], RPS_NAMES[c], RPS_NAMES[bot_c],
                                      {1: "✅ بردی", -1: "❌ باختی", 0: "🤝 مساوی"}[res]))
    if d["round"] >= RPS_ROUNDS:
        session_del(sid)
        if d["u"] > d["b"]:
            result, xp, verdict = "win", GAME_XP["rps_win"], "🏆 بردی!"
        elif d["u"] < d["b"]:
            result, xp, verdict = "loss", GAME_XP["lose"], "😅 این بار من بردم!"
        else:
            result, xp, verdict = "draw", GAME_XP["rps_draw"], "🤝 مساوی شدیم!"
        info = record_result(ctx.chat_id, ctx.user_id, "rps", result, d["u"], xp, token=sid)
        ctx.edit(rps_text(d) + "\n\n" + verdict + xp_line(info), game_end_markup("rps"))
    else:
        game_save(s, d)
        ctx.edit(rps_text(d), rps_markup(sid))


# ─── 🎲 تاس (با تاس تصادفی واقعی) ───
def roll_two():
    a, b = SR.randint(1, 6), SR.randint(1, 6)
    return a, b, a + b


@game_starter("dice")
def start_dice(ctx):
    sid = game_start(ctx, "dice", {})
    show(ctx, "🎲 تاس\n\nهر کدوم مجموع دو تاسش بیشتر باشه برنده‌ست. تاس بنداز 👇",
         kb([btn("🎲 بنداز", "dc:" + sid)]))


@callback("dc")
def cb_dice(ctx):
    s = game_load(ctx, ctx.parts[1] if len(ctx.parts) > 1 else "", "dice", owned=True)
    if s is None:
        return
    session_del(s["sid"])
    u1, u2, ut = roll_two()
    b1, b2, bt = roll_two()
    if ut > bt:
        result, xp, verdict = "win", GAME_XP["dice_win"], "🏆 بردی!"
    elif ut < bt:
        result, xp, verdict = "loss", GAME_XP["lose"], "😅 من بردم!"
    else:
        result, xp, verdict = "draw", GAME_XP["dice_draw"], "🤝 مساوی!"
    info = record_result(ctx.chat_id, ctx.user_id, "dice", result, ut, xp, token=s["sid"])
    ctx.edit("🎲 تاس\n\n🎲 تو: %d + %d = %d\n🤖 من: %d + %d = %d\n\n%s%s" % (u1, u2, ut, b1, b2, bt, verdict, xp_line(info)),
             game_end_markup("dice"))


# ─── ⚔️ دوئل ───
DUEL_INVITE_TTL = 300
DUEL_PLAY_TTL = 600
DUEL_RPS_MAX_ROUNDS = 5
DUEL_MATH_ROUNDS = 3
DUEL_GAMES = [("rps", "✊ سنگ کاغذ قیچی"), ("dice", "🎲 تاس"), ("math", "🔢 ریاضی سریع")]


def _edit_msg(chat_id, mid, text, markup=None):
    """ویرایش پیام بدون ctx (برای Sweeper). اگر پیام نباشد، پیام جدید می‌فرستد."""
    p = {"chat_id": chat_id, "text": safe_text(text)}
    if markup:
        p["reply_markup"] = markup
    if mid is None:
        return APP.api.try_call("sendMessage", p, retries=1)
    p["message_id"] = mid
    return APP.api.try_call("editMessageText", p, retries=1)


def game_save_ttl(s, data, ttl):
    session_put(s["sid"], s["chat_id"], s["user_id"], s["kind"], data, ttl)


def create_duel(ctx, target_id=0):
    if not ctx.is_group:
        ctx.answer("⚔️ دوئل فقط داخل گروه کار می‌کنه", True)
        if ctx.kind == "message":
            ctx.reply("⚔️ دوئل فقط داخل گروه کار می‌کنه")
        return
    if not cooldown_ok(("duel", ctx.chat_id, ctx.user_id), 10):
        ctx.answer("⏳ یکم صبر کن", False)
        return
    tid = int(target_id or 0)
    if tid == int(ctx.user_id):
        ctx.reply("خودت رو که نمی‌شه به دوئل دعوت کرد 😅") if ctx.kind == "message" else ctx.answer("😅", True)
        return
    for s in session_find(ctx.chat_id, game_kind("duel"), ctx.user_id):
        if s["data"].get("state") == "invite":
            session_del(s["sid"])
    d = {"state": "invite", "chal": int(ctx.user_id), "cn": display_name(ctx.user), "target": tid,
         "tn": uname(tid) if tid else ""}
    sid = game_start(ctx, "duel", d, DUEL_INVITE_TTL, solo=False)
    if tid:
        text = "⚔️ %s، %s را به دوئل دعوت کرده." % (d["cn"], mention(get_user(tid)))
        markup = kb([btn("قبول ⚔️", "du:ac:" + sid), btn("رد ❌", "du:no:" + sid)])
    else:
        text = "⚔️ %s همه رو به دوئل دعوت کرده! اولین نفری که قبول کنه حریفشه." % d["cn"]
        markup = kb([btn("قبول ⚔️", "du:ac:" + sid), btn("❌ لغو", "du:no:" + sid)])
    res = ctx.send(text, markup)
    s = session_get(sid)
    s["data"]["msg"] = (res or {}).get("message_id")
    game_save(s, s["data"])
    STATS.user_add(ctx.chat_id, ctx.user_id, "duels_sent")
    mission_event(ctx.chat_id, ctx.user_id, "duels_sent")
    ctx.answer("⚔️ دعوت ارسال شد")


@game_starter("duel")
def start_duel_hub(ctx):
    create_duel(ctx, 0)


@command("duel", private=False)
def cmd_duel(ctx):
    target = 0
    rt = ctx.reply_to
    if rt and (rt.get("from") or {}).get("id") and not rt["from"].get("is_bot"):
        target = int(rt["from"]["id"])
        upsert_user(rt["from"])
    elif ctx.args.startswith("@"):
        row = APP.db.one("SELECT user_id FROM users WHERE lower(username)=?", (ctx.args.split()[0][1:].lower(),))
        if row is None:
            ctx.reply("این یوزرنیم رو نمی‌شناسم 🤔 روی پیامش ریپلای کن و /duel بزن")
            return
        target = row["user_id"]
    create_duel(ctx, target)


def _duel_head(d):
    return "⚔️ %s  ⚔️  %s" % (d["cn"], d["tn"])


def _duel_new_math(d):
    q, _, _ = make_math(d["qn"])
    d.update({"q": q["q"], "opts": q["opts"], "ans": q["ans"], "out": []})


def _duel_render(d, sid):
    a, b = str(d["chal"]), str(d["target"])
    g = d.get("game")
    if g == "rps":
        text = ("%s\n\n✊ سنگ کاغذ قیچی — راند %d (اولین نفر با ۲ برد)\nامتیاز: %d | %d\n%s\n\n"
                "هر دو نفر انتخاب کنید (انتخاب مخفیه) 👇"
                % (_duel_head(d), d["round"] + 1, d["sc"][a], d["sc"][b], "\n".join(d["log"])))
        done = ["%s ✅" % (d["cn"] if int(k) == d["chal"] else d["tn"]) for k in d["picks"]]
        if done:
            text += "\n" + " ".join(done)
        return text, kb([btn(RPS_NAMES[c], "du:rp:%s:%d" % (sid, c)) for c in range(3)])
    if g == "dice":
        who = ["%s ✅" % (d["cn"] if int(k) == d["chal"] else d["tn"]) for k in d["rolls"]]
        text = "%s\n\n🎲 تاس — هر دو نفر یه بار تاس بندازید (مجموع دو تاس).\n%s" % (_duel_head(d), " ".join(who))
        if d.get("ties"):
            text += "\n🤝 مساوی شد، دوباره!"
        return text, kb([btn("🎲 بنداز", "du:dc:" + sid)])
    text = ("%s\n\n🔢 ریاضی سریع — سؤال %d از %d\nامتیاز: %d | %d\n\n%s\n\nاولین جواب درست امتیاز می‌گیره 👇"
            % (_duel_head(d), d["qn"] + 1, DUEL_MATH_ROUNDS, d["sc"][a], d["sc"][b], d["q"]))
    rows = [[btn(o, "du:mt:%s:%d" % (sid, i)) for i, o in enumerate(d["opts"])]]
    return text, kb(*rows)


def _duel_finish(ctx, s, d, winner, summary):
    """winner: uid برنده یا 0 برای مساوی."""
    session_del(s["sid"])
    a, b, chat = d["chal"], d["target"], s["chat_id"]
    ok = pair_reward_ok(a, b)
    lines = [_duel_head(d), "", summary, ""]
    if winner == 0:
        for u in (a, b):
            info = record_result(chat, u, "duel", "draw", 0, GAME_XP["duel_draw"] if ok else 0, token=s["sid"])
            lines.append("🤝 %s%s" % (uname(u), xp_line(info).replace("\n", " ")))
        lines.insert(3, "🤝 دوئل مساوی شد!")
    else:
        loser = b if winner == a else a
        w = record_result(chat, winner, "duel", "win", 1, GAME_XP["duel_win"] if ok else 0, token=s["sid"])
        l = record_result(chat, loser, "duel", "loss", 0, GAME_XP["lose"] if ok else 0, token=s["sid"])
        STATS.user_add(chat, winner, "duels_won")
        lines.insert(3, "🏆 %s برنده‌ی دوئل شد!" % uname(winner))
        lines.append("🏆 %s%s" % (uname(winner), xp_line(w).replace("\n", " ")))
        lines.append("😅 %s%s" % (uname(loser), xp_line(l).replace("\n", " ")))
    if not ok:
        lines.append("\nℹ️ سقف پاداش دوئل بین این دو نفر امروز پر شده.")
    ctx.edit("\n".join(lines), kb([btn("⚔️ دوئل جدید", "gm:duel"), btn("🎮 بازی‌ها", "gm:hub")]))


@callback("du")
def cb_duel(ctx):
    try:
        act, sid = ctx.parts[1], ctx.parts[2]
    except IndexError:
        ctx.answer("دکمه نامعتبر", True)
        return
    s = game_load(ctx, sid, "duel")
    if s is None:
        return
    d, uid = s["data"], int(ctx.user_id)
    players = (d["chal"], d["target"])
    if act == "ac":
        if d["state"] != "invite":
            ctx.answer("این دوئل قبلاً شروع شده", True)
        elif uid == d["chal"]:
            ctx.answer("دعوت خودت رو که نمی‌تونی قبول کنی 😅", True)
        elif d["target"] and uid != d["target"]:
            ctx.answer("این دعوت برای تو نیست ✋", True)
        else:
            d.update({"target": uid, "tn": display_name(ctx.user), "state": "pick"})
            game_save_ttl(s, d, DUEL_PLAY_TTL)
            ctx.answer("⚔️ قبول شد!")
            ctx.edit("%s\n\nیه بازی انتخاب کنید 👇" % _duel_head(d),
                     kb(*[[btn(label, "du:g:%s:%s" % (sid, key))] for key, label in DUEL_GAMES]))
    elif act == "no":
        if uid not in (d["chal"], d["target"]):
            ctx.answer("این دکمه برای تو نیست ✋", True)
            return
        session_del(sid)
        ctx.answer("❌ لغو شد")
        ctx.edit("❌ دوئل %s لغو شد." % ("رد" if uid == d["target"] else ""))
    elif act == "g":
        if d["state"] != "pick" or uid not in players:
            ctx.answer("فقط شرکت‌کننده‌ها می‌تونن بازی رو انتخاب کنن", True)
            return
        key = ctx.parts[3] if len(ctx.parts) > 3 else ""
        a, b = str(d["chal"]), str(d["target"])
        if key == "rps":
            d.update({"game": "rps", "state": "play", "picks": {}, "sc": {a: 0, b: 0}, "round": 0, "log": []})
        elif key == "dice":
            d.update({"game": "dice", "state": "play", "rolls": {}, "ties": 0})
        elif key == "math":
            d.update({"game": "math", "state": "play", "sc": {a: 0, b: 0}, "qn": 0})
            _duel_new_math(d)
        else:
            ctx.answer("بازی نامعتبر", True)
            return
        game_save_ttl(s, d, DUEL_PLAY_TTL)
        text, markup = _duel_render(d, sid)
        ctx.edit(text, markup)
    elif act in ("rp", "dc", "mt"):
        if d["state"] != "play" or uid not in players:
            ctx.answer("تو توی این دوئل نیستی ✋", True)
            return
        if act == "rp" and d["game"] == "rps":
            _duel_rps(ctx, s, d, int(ctx.parts[3]))
        elif act == "dc" and d["game"] == "dice":
            _duel_dice(ctx, s, d)
        elif act == "mt" and d["game"] == "math":
            _duel_math(ctx, s, d, int(ctx.parts[3]))
        else:
            ctx.answer("دکمه نامعتبر", True)
    else:
        ctx.answer("دکمه نامعتبر", True)


def _duel_rps(ctx, s, d, c):
    if not 0 <= c <= 2:
        ctx.answer("دکمه نامعتبر", True)
        return
    uid = str(ctx.user_id)
    if uid in d["picks"]:
        ctx.answer("قبلاً انتخاب کردی؛ منتظر حریف 😉")
        return
    d["picks"][uid] = c
    a, b = str(d["chal"]), str(d["target"])
    if len(d["picks"]) < 2:
        game_save_ttl(s, d, DUEL_PLAY_TTL)
        ctx.answer("✅ انتخابت ثبت شد (مخفیه)")
        text, markup = _duel_render(d, s["sid"])
        ctx.edit(text, markup)
        return
    ca, cb = d["picks"][a], d["picks"][b]
    res = rps_outcome(ca, cb)
    d["sc"][a] += 1 if res == 1 else 0
    d["sc"][b] += 1 if res == -1 else 0
    d["round"] += 1
    d["log"].append("راند %d: %s — %s  %s" % (d["round"], RPS_NAMES[ca], RPS_NAMES[cb],
                                            {1: "✅ " + d["cn"], -1: "✅ " + d["tn"], 0: "🤝 مساوی"}[res]))
    d["picks"] = {}
    winner = None
    if d["sc"][a] >= 2:
        winner = d["chal"]
    elif d["sc"][b] >= 2:
        winner = d["target"]
    elif d["round"] >= DUEL_RPS_MAX_ROUNDS:
        winner = d["chal"] if d["sc"][a] > d["sc"][b] else d["target"] if d["sc"][b] > d["sc"][a] else 0
    if winner is None:
        game_save_ttl(s, d, DUEL_PLAY_TTL)
        ctx.answer("راند تموم شد")
        text, markup = _duel_render(d, s["sid"])
        ctx.edit(text, markup)
    else:
        _duel_finish(ctx, s, d, winner, "\n".join(d["log"]))


def _duel_dice(ctx, s, d):
    uid = str(ctx.user_id)
    if uid in d["rolls"]:
        ctx.answer("قبلاً تاس انداختی 😉")
        return
    d["rolls"][uid] = list(roll_two())
    a, b = str(d["chal"]), str(d["target"])
    if len(d["rolls"]) < 2:
        game_save_ttl(s, d, DUEL_PLAY_TTL)
        ctx.answer("🎲 انداختی؛ منتظر حریف")
        text, markup = _duel_render(d, s["sid"])
        ctx.edit(text, markup)
        return
    ra, rb = d["rolls"][a], d["rolls"][b]
    summary = "🎲 %s: %d + %d = %d\n🎲 %s: %d + %d = %d" % (d["cn"], ra[0], ra[1], ra[2], d["tn"], rb[0], rb[1], rb[2])
    if ra[2] == rb[2]:
        d["ties"] += 1
        d["rolls"] = {}
        if d["ties"] >= 3:
            _duel_finish(ctx, s, d, 0, summary)
        else:
            game_save_ttl(s, d, DUEL_PLAY_TTL)
            ctx.answer("🤝 مساوی! دوباره")
            text, markup = _duel_render(d, s["sid"])
            ctx.edit(text, markup)
        return
    _duel_finish(ctx, s, d, d["chal"] if ra[2] > rb[2] else d["target"], summary)


def _duel_math(ctx, s, d, i):
    uid = str(ctx.user_id)
    if uid in d["out"]:
        ctx.answer("این سؤال برای تو تموم شد 😅", True)
        return
    if not 0 <= i < len(d["opts"]):
        ctx.answer("گزینه نامعتبر", True)
        return
    a, b = str(d["chal"]), str(d["target"])
    advance = False
    if i == d["ans"]:
        d["sc"][uid] += 1
        advance = True
        ctx.answer("✅ درست! امتیاز تو")
    else:
        d["out"].append(uid)
        ctx.answer("❌ اشتباه! این سؤال برای تو تموم شد", True)
        advance = len(d["out"]) >= 2
    if advance:
        d["qn"] += 1
        finished = d["sc"][a] >= 2 or d["sc"][b] >= 2 or d["qn"] >= DUEL_MATH_ROUNDS
        if finished:
            winner = d["chal"] if d["sc"][a] > d["sc"][b] else d["target"] if d["sc"][b] > d["sc"][a] else 0
            _duel_finish(ctx, s, d, winner, "🔢 امتیاز نهایی: %s %d | %s %d" % (d["cn"], d["sc"][a], d["tn"], d["sc"][b]))
            return
        _duel_new_math(d)
    game_save_ttl(s, d, DUEL_PLAY_TTL)
    text, markup = _duel_render(d, s["sid"])
    ctx.edit(text, markup)


# ─── 👥 بازی گروهی ───
GROUP_TTL = 40


@game_starter("group_game")
def start_group_game(ctx):
    if not ctx.is_group:
        ctx.answer("👥 بازی گروهی فقط داخل گروه کار می‌کنه", True)
        return
    if session_find(ctx.chat_id, game_kind("group_game")):
        ctx.answer("👥 یه دور بازی گروهی هنوز بازه!", True)
        return
    _, q = trivia_question("all")
    start_quiz(ctx, "group_game", "👥 بازی گروهی — همه می‌تونن جواب بدن!", q, "group_correct", mode="group",
               ttl=GROUP_TTL, extra_data={"answers": {}, "starter": int(ctx.user_id)}, new_message=True,
               extra_row=lambda sid: [btn("🏁 پایان", "gg:end:" + sid)])
    ctx.answer("👥 شروع شد!")


def finalize_group(s, ctx=None):
    d, key = s["data"], s["kind"][5:]
    session_del(s["sid"])
    chat = s["chat_id"]
    correct = sorted((el, int(u)) for u, (idx, el) in d["answers"].items() if idx == d["ans"])
    wrong = sorted(int(u) for u, (idx, el) in d["answers"].items() if idx != d["ans"])
    lines = ["👥 نتیجه‌ی بازی گروهی", "", d["q"], "✅ جواب: %s" % d["opts"][d["ans"]], ""]
    if not d["answers"]:
        lines.append("کسی جواب نداد 😴")
    medals = ["🥇", "🥈", "🥉"]
    for rank, (el, u) in enumerate(correct):
        xp = GAME_XP["group_correct"] + (GAME_XP["group_first"] if rank == 0 else 0)
        info = record_result(chat, u, key, "win", el, xp, token=s["sid"])
        lines.append("%s %s — %d ثانیه%s" % (medals[rank] if rank < 3 else "✅", uname(u), el, xp_line(info).replace("\n", " ")))
    for u in wrong:
        info = record_result(chat, u, key, "loss", 0, GAME_XP["lose"], token=s["sid"])
        lines.append("❌ %s%s" % (uname(u), xp_line(info).replace("\n", " ")))
    markup = (kb([btn("🧩 دوباره", "lb:rid"), btn("🧪 آزمایشگاه", "menu:lab")]) if key == "lab_riddle"
              else kb([btn("🔁 دور بعد", "gm:group_game"), btn("🎮 بازی‌ها", "gm:hub")]))
    if ctx is not None and ctx.kind == "callback":
        ctx.edit("\n".join(lines), markup)
    else:
        _edit_msg(chat, d.get("msg"), "\n".join(lines), markup)


@callback("gg")
def cb_group(ctx):
    if len(ctx.parts) < 3 or ctx.parts[1] != "end":
        ctx.answer("دکمه نامعتبر", True)
        return
    s = game_load(ctx, ctx.parts[2])
    if s is None:
        return
    if s["data"].get("mode") != "group":
        ctx.answer("دکمه نامعتبر", True)
        return
    if int(ctx.user_id) != s["data"].get("starter") and not ctx.is_admin():
        ctx.answer("فقط شروع‌کننده یا ادمین می‌تونه زودتر تمومش کنه", True)
        return
    finalize_group(s, ctx)


# ─── Sweeper: بازی‌های منقضی‌شده ───
def _expire_game(s):
    d, key = s["data"], s["kind"][5:]
    mode = d.get("mode")
    if mode == "group":
        finalize_group(s, None)
        return
    if mode == "poll":
        finalize_poll(s, None)
        return
    if mode == "tap":
        finalize_tap(s, None)
        return
    session_del(s["sid"])
    if mode == "race":
        _edit_msg(s["chat_id"], d.get("msg"), "%s\n\n%s\n\n⏰ وقت تموم شد! کسی درست جواب نداد.\n✅ جواب: %s"
                  % (d["title"], d["q"], d["opts"][d["ans"]]), kb([btn("⚡ دور بعد", "gm:speed")]))
    elif key == "duel":
        text = "⌛ دعوت دوئل منقضی شد." if d.get("state") == "invite" else "⌛ دوئل نیمه‌کاره موند و منقضی شد."
        _edit_msg(s["chat_id"], d.get("msg"), text)


@periodic(10, "game_sweep")
def _task_game_sweep():
    t = now()
    rows = APP.db.all("SELECT sid FROM sessions WHERE kind LIKE 'game:%' AND expires_at<=? AND expires_at>?",
                      (t, t - SESSION_GRACE_SECONDS))
    for r in rows:
        s = session_get_raw(r["sid"])
        if s is not None:
            _safe_run(_expire_game, s)


# ─── SELFTEST فاز ۴ ───
def _p4_sess(chat, key, user=None):
    ss = session_find(chat, game_kind(key), user)
    return ss[-1] if ss else None


def _p4_cb(user, chat, data, mid, n=[0]):
    n[0] += 1
    handle_update(_p2_cb("p4-%d" % n[0], data, user, chat, mid))


def _p4_text(user, chat, text, mid=1, reply_user=None):
    upd = _p2_msg(text, user, chat, mid=mid)
    if reply_user:
        upd["message"]["reply_to_message"] = {"message_id": 1, "from": {"id": reply_user, "first_name": "U%d" % reply_user}}
    handle_update(upd)


def _p4_stat(chat, user, field):
    return user_stat_get(chat, user, field)


def _p4_results(chat, user, key):
    return [dict(r) for r in APP.db.all("SELECT result, xp FROM game_records WHERE chat_id=? AND user_id=? AND game_key=? ORDER BY id",
                                        (chat, user, key))]


@selftest("بانک بازی‌ها: کلمه، معما، سؤال، ایموجی، کشور، ریاضی")
def _t_p4_banks():
    check(len(ALPHABET) == 32 and len(set(ALPHABET)) == 32, "alphabet")
    for cat, ws in WORD_BANK.items():
        for w in ws:
            check(set(w) <= set(ALPHABET), "bad letter in word " + w)
    for q, ok, wr in RIDDLES:
        check(len({ok, *wr}) == 4, "riddle options: " + q)
    check(set(TRIVIA_BANK) == {k for k, _, _ in TRIVIA_CATS}, "trivia cats")
    for cat, items in TRIVIA_BANK.items():
        check(len(items) >= 6, "trivia %s too small" % cat)
        for q, ok, wr in items:
            check(len({ok, *wr}) == 4, "trivia options: " + q)
    for e, ok, wr in EMOJI_BANK:
        check(len({ok, *wr}) == 4, "emoji options: " + e)
    check(len({c[0] for c in COUNTRIES}) == len(COUNTRIES) == len({c[1] for c in COUNTRIES}), "countries unique")
    c = build_choice("q", "x", ["a", "b", "c"])
    check(c["opts"][c["ans"]] == "x" and len(c["opts"]) == 4, "build_choice")
    for lvl in range(5):
        for _ in range(40):
            q, expr, ans = make_math(lvl)
            tok = expr.split()
            exp = (int(tok[0]) + int(tok[2]) if tok[1] == "+" else int(tok[0]) - int(tok[2]) if tok[1] == "-"
                   else int(tok[0]) * int(tok[2]) + (int(tok[4]) if len(tok) == 5 else 0))
            check(exp == ans and q["opts"][q["ans"]] == str(ans), "math wrong: " + expr)
            check(len(set(q["opts"])) == 4 and all(int(o) >= 0 for o in q["opts"]), "math options " + expr)
    check([rps_outcome(a, b) for a, b in ((0, 2), (1, 0), (2, 1), (0, 1), (1, 2), (2, 0), (1, 1))] == [1, 1, 1, -1, -1, -1, 0], "rps table")
    check(number_hint(10, 50).startswith("⬆️") and "🧊" in number_hint(10, 50), "hint far")
    check("🔥🔥" in number_hint(50, 52) and number_hint(50, 40).startswith("⬇️"), "hint near")
    return "%d words, %d riddles, %d trivia" % (sum(len(v) for v in WORD_BANK.values()), len(RIDDLES), sum(len(v) for v in TRIVIA_BANK.values()))


@selftest("ثبت نتیجه: XP، Streak، لیگ، پاداش تکراری، سقف روزانه، رویداد")
def _t_p4_results():
    g, u = -9401, 9401
    seen = []
    hook = lambda info: seen.append(info["xp"])
    saved_hooks = list(GAME_RESULT_HOOKS)
    GAME_RESULT_HOOKS[:] = [hook]
    try:
        i = record_result(g, u, "dice", "win", 5, 20, token="t1")
        check(i["xp"] == 20 and not i["dup"], "first win")
        r = APP.db.one("SELECT * FROM user_stats WHERE chat_id=? AND user_id=?", (g, u))
        check(r["games_played"] == 1 and r["games_won"] == 1 and r["streak"] == 1 and r["xp"] == 20, "stats")
        check(APP.db.val("SELECT points FROM league_scores WHERE chat_id=? AND user_id=?", (g, u)) == 20, "league points")
        check(record_result(g, u, "dice", "win", 5, 20, token="t1")["dup"] is True, "duplicate reward")
        check(_p4_stat(g, u, "xp") == 20 and _p4_stat(g, u, "games_played") == 1, "duplicate changed stats")
        record_result(g, u, "dice", "win", 5, 20, token="t2")
        record_result(g, u, "dice", "loss", 0, 3, token="t3")
        r = APP.db.one("SELECT streak, best_streak, games_lost FROM user_stats WHERE chat_id=? AND user_id=?", (g, u))
        check(r["streak"] == 0 and r["best_streak"] == 2 and r["games_lost"] == 1, "streak logic")
        t = now()
        APP.db.run("INSERT INTO events(chat_id, ekey, title, multiplier, starts_at, ends_at, active, created_at) VALUES(?,?,?,?,?,?,1,?)",
                   (g, "xp2", "x2", 2, t - 10, t + 1000, t))
        check(record_result(g, u, "dice", "win", 5, 20, token="t4")["xp"] == 40, "event multiplier")
        APP.db.run("DELETE FROM events")
        APP.db.run("INSERT INTO game_records(chat_id, user_id, game_key, result, score, xp, created_at) VALUES(?,?,?,?,?,?,?)",
                   (g, u, "dice", "win", 0, GAME_DAILY_XP_CAP - 10 - APP.db.val("SELECT SUM(xp) FROM game_records WHERE user_id=?", (u,)), t))
        i = record_result(g, u, "dice", "win", 5, 20, token="t5")
        check(i["xp"] == 10 and i["capped"], "daily cap partial")
        i = record_result(g, u, "dice", "win", 5, 20, token="t6")
        check(i["xp"] == 0 and i["capped"] and "سقف" in xp_line(i), "daily cap full")
        check(len(seen) >= 6, "hooks not called")
    finally:
        GAME_RESULT_HOOKS[:] = saved_hooks
    check(pair_reward_ok(1, 2) and pair_reward_ok(2, 1) and pair_reward_ok(1, 2) and not pair_reward_ok(1, 2), "pair cap")
    return "ok"


@selftest("مرکز بازی و 🎯 حدس عدد (حدس‌ها، راهنما، برد/باخت/تسلیم)")
def _t_p4_number():
    api, g = APP.api, -9411
    set_setting(g, "anti_flood", 0)  # این تست پیام‌های پشت‌سرهم می‌فرستد؛ موضوعش مدیریت نیست
    _p4_cb(9411, g, "menu:games", 1)
    cbs = [b["callback_data"] for r in _p2_calls(api, "edit_message_text")[-1][1][3]["inline_keyboard"] for b in r]
    check(all("gm:" + k in cbs for k in GAME_STARTERS), "hub misses games: %s" % cbs)
    check(len(GAME_STARTERS) >= 12, "games registered: %d" % len(GAME_STARTERS))
    COOLDOWNS.clear()
    _p4_cb(9411, g, "gm:number_guess", 2)
    s = _p4_sess(g, "number_guess", 9411)
    check(s and input_get(g, 9411)["kind"] == "input:game_num", "game not started")
    secret = s["data"]["secret"]
    wrong = secret % 100 + 1
    _p4_text(9411, g, "سلام بچه‌ها", mid=3)
    check(_p4_sess(g, "number_guess", 9411)["data"]["tries"] == 0, "normal chat consumed as guess")
    _p4_text(9411, g, "150", mid=4)
    check("بین ۱ تا ۱۰۰" in _p3_last_text(api), "range check")
    _p4_text(9411, g, str(wrong), mid=5)
    check(("بالاتره" in _p3_last_text(api) or "پایین‌تره" in _p3_last_text(api)) and "5" not in "x", "hint")
    check(_p4_sess(g, "number_guess", 9411)["data"]["tries"] == 1, "tries")
    _p4_text(9411, g, str(secret), mid=6)
    res = _p4_results(g, 9411, "number_guess")
    check(res == [{"result": "win", "xp": 40 + 5 * (NUMBER_MAX_TRIES - 2)}], "win result %s" % res)
    check(_p4_sess(g, "number_guess", 9411) is None and input_get(g, 9411) is None, "session not closed")
    COOLDOWNS.clear()
    _p4_cb(9411, g, "gm:number_guess", 7)
    secret = _p4_sess(g, "number_guess", 9411)["data"]["secret"]
    for k in range(NUMBER_MAX_TRIES):
        _p4_text(9411, g, str(secret % 100 + 1), mid=10 + k)
    check([r["result"] for r in _p4_results(g, 9411, "number_guess")] == ["win", "loss"], "loss after max tries")
    COOLDOWNS.clear()
    _p4_cb(9412, g, "gm:number_guess", 20)
    sid = _p4_sess(g, "number_guess", 9412)["sid"]
    _p4_cb(9413, g, "gm:quit:" + sid, 20)
    check(_p4_sess(g, "number_guess", 9412) is not None, "stranger quit my game")
    _p4_cb(9412, g, "gm:quit:" + sid, 21)
    check(_p4_results(g, 9412, "number_guess")[0]["result"] == "loss" and _p4_sess(g, "number_guess", 9412) is None, "quit")
    set_setting(g, "games", 0)
    COOLDOWNS.clear()
    _p4_cb(9414, g, "gm:number_guess", 30)
    check(_p4_sess(g, "number_guess", 9414) is None, "games OFF still starts")
    return "ok"


@selftest("🔤 حدس کلمه (دکمه‌ای): برد، باخت، مالکیت")
def _t_p4_word():
    g = -9421
    _p4_cb(9421, g, "gm:word_guess", 1)
    s = _p4_sess(g, "word_guess", 9421)
    word = s["data"]["word"]
    check(s["data"]["lives"] == HANGMAN_LIVES and s["data"]["cat"], "start")
    _p4_cb(9429, g, "wg:%s:%d" % (s["sid"], ALPHABET.index(word[0])), 1)
    check(_p4_sess(g, "word_guess", 9421)["data"]["guessed"] == [], "stranger played")
    for ch in dict.fromkeys(word):
        _p4_cb(9421, g, "wg:%s:%d" % (s["sid"], ALPHABET.index(ch)), 2)
    res = _p4_results(g, 9421, "word_guess")
    check(res and res[0]["result"] == "win" and res[0]["xp"] == GAME_XP["word_win"] + HANGMAN_LIVES * GAME_XP["word_bonus"], "win %s" % res)
    COOLDOWNS.clear()
    _p4_cb(9422, g, "gm:word_guess", 3)
    s = _p4_sess(g, "word_guess", 9422)
    wrong = [i for i, ch in enumerate(ALPHABET) if ch not in s["data"]["word"]][:HANGMAN_LIVES]
    for k, i in enumerate(wrong):
        _p4_cb(9422, g, "wg:%s:%d" % (s["sid"], i), 4)
    check(_p4_results(g, 9422, "word_guess")[0]["result"] == "loss" and _p4_sess(g, "word_guess", 9422) is None, "loss")
    return "ok"


@selftest("🧩🧠😀🌍 بازی‌های تک‌نفره چندگزینه‌ای")
def _t_p4_solo_quiz():
    g = -9431
    plans = [("riddle", "gm:riddle", 9431, GAME_XP["riddle"]), ("trivia", "tv:c:football", 9432, GAME_XP["trivia"]),
             ("emoji_guess", "gm:emoji_guess", 9433, GAME_XP["emoji"]), ("country_guess", "gm:country_guess", 9434, GAME_XP["country"])]
    for key, start, user, xp in plans:
        COOLDOWNS.clear()
        _p4_cb(user, g, start, 1)
        s = _p4_sess(g, key, user)
        check(s and len(s["data"]["opts"]) == 4, "%s not started" % key)
        _p4_cb(9439, g, "qz:%s:%d" % (s["sid"], s["data"]["ans"]), 1)
        check(_p4_sess(g, key, user) is not None, "%s: stranger answered" % key)
        _p4_cb(user, g, "qz:%s:%d" % (s["sid"], s["data"]["ans"]), 2)
        check(_p4_results(g, user, key) == [{"result": "win", "xp": xp}], "%s win" % key)
        _p4_cb(user, g, "qz:%s:%d" % (s["sid"], s["data"]["ans"]), 3)
        check(len(_p4_results(g, user, key)) == 1, "%s: replay rewarded" % key)
        COOLDOWNS.clear()
        _p4_cb(user + 100, g, start, 4)
        s = _p4_sess(g, key, user + 100)
        _p4_cb(user + 100, g, "qz:%s:%d" % (s["sid"], (s["data"]["ans"] + 1) % 4), 5)
        check(_p4_results(g, user + 100, key) == [{"result": "loss", "xp": GAME_XP["lose"]}], "%s loss" % key)
    COOLDOWNS.clear()
    _p4_cb(9450, g, "gm:trivia", 9)
    cbs = [b["callback_data"] for r in _p2_calls(APP.api, "edit_message_text")[-1][1][3]["inline_keyboard"] for b in r]
    check(all(("tv:c:" + k) in cbs for k, _, _ in TRIVIA_CATS), "trivia categories")
    return "ok"


@selftest("🔢 ریاضی ۵ سؤالی")
def _t_p4_math():
    g, u = -9441, 9441
    _p4_cb(u, g, "gm:math", 1)
    for n in range(MATH_ROUNDS):
        s = _p4_sess(g, "math", u)
        check(s is not None, "math session lost at %d" % n)
        _p4_cb(u, g, "qz:%s:%d" % (s["sid"], s["data"]["ans"]), 10 + n)
    check(_p4_results(g, u, "math") == [{"result": "win", "xp": MATH_ROUNDS * GAME_XP["math_each"] + GAME_XP["math_perfect"]}], "perfect score")
    check("ریاضی تموم شد" in _p3_last_text(APP.api, "edit_message_text", 2), "final text")
    return "ok"


@selftest("⚡ سرعتی: اولین جواب درست، حذف خطاکار، Timeout")
def _t_p4_speed():
    api, g = APP.api, -9451
    COOLDOWNS.clear()
    _p4_cb(9451, g, "gm:speed", 1)
    s = _p4_sess(g, "speed")
    check(s and s["data"]["mode"] == "race" and s["data"]["msg"] is not None, "race not started")
    _p4_cb(9452, g, "gm:speed", 2)
    check(len(session_find(g, game_kind("speed"))) == 1, "two races at once")
    wrong = (s["data"]["ans"] + 1) % 4
    _p4_cb(9452, g, "qz:%s:%d" % (s["sid"], wrong), 3)
    _p4_cb(9452, g, "qz:%s:%d" % (s["sid"], s["data"]["ans"]), 4)
    check(not _p4_results(g, 9452, "speed"), "eliminated player won")
    _p4_cb(9453, g, "qz:%s:%d" % (s["sid"], s["data"]["ans"]), 5)
    res = _p4_results(g, 9453, "speed")
    check(res and res[0]["result"] == "win" and res[0]["xp"] in (GAME_XP["speed"], GAME_XP["speed"] + GAME_XP["speed_fast"]), "winner %s" % res)
    _p4_cb(9454, g, "qz:%s:%d" % (s["sid"], s["data"]["ans"]), 6)
    check(not _p4_results(g, 9454, "speed"), "late answer rewarded")
    COOLDOWNS.clear()
    _p4_cb(9451, g, "gm:speed", 7)
    s = _p4_sess(g, "speed")
    APP.db.run("UPDATE sessions SET expires_at=? WHERE sid=?", (now() - 1, s["sid"]))
    _task_game_sweep()
    check(_p4_sess(g, "speed") is None, "expired race not cleaned")
    check("وقت تموم شد" in _p3_last_text(api, "try_call", 1) or any("وقت تموم شد" in json.dumps(c[1][1], ensure_ascii=False)
          for c in api.calls if c[0] == "try_call"), "timeout message")
    return "ok"


@selftest("✊ سنگ کاغذ قیچی و 🎲 تاس")
def _t_p4_rps_dice():
    g = -9461
    _p4_cb(9461, g, "gm:rps", 1)
    sid = _p4_sess(g, "rps", 9461)["sid"]
    for r in range(RPS_ROUNDS):
        _p4_cb(9461, g, "rp:%s:%d" % (sid, r % 3), 10 + r)
    res = _p4_results(g, 9461, "rps")
    check(len(res) == 1 and res[0]["result"] in ("win", "loss", "draw") and _p4_sess(g, "rps", 9461) is None, "rps finish %s" % res)
    _p4_cb(9461, g, "rp:%s:0" % sid, 20)
    check(len(_p4_results(g, 9461, "rps")) == 1, "rps replay")
    COOLDOWNS.clear()
    _p4_cb(9462, g, "gm:dice", 1)
    sid = _p4_sess(g, "dice", 9462)["sid"]
    _p4_cb(9463, g, "dc:" + sid, 1)
    check(not _p4_results(g, 9463, "dice"), "stranger rolled")
    _p4_cb(9462, g, "dc:" + sid, 2)
    check("تو:" in _p3_last_text(APP.api, "edit_message_text", 2), "dice text")
    _p4_cb(9462, g, "dc:" + sid, 3)
    check(len(_p4_results(g, 9462, "dice")) == 1, "dice replay")
    return "ok"


@selftest("⚔️ دوئل: دعوت، قبول/رد، سه بازی، پاداش")
def _t_p4_duel():
    api, g = APP.api, -9471
    A, B, C = 9471, 9472, 9473
    for u in (A, B, C):
        ADMIN_CACHE[(g, u)] = (False, time.monotonic() + 300)
    upsert_user({"id": B, "first_name": "Bee", "username": "bee_u"})
    _p4_text(A, g, "/duel", mid=1, reply_user=B)
    s = _p4_sess(g, "duel", A)
    check(s and s["data"]["state"] == "invite" and s["data"]["target"] == B, "targeted invite")
    check("دوئل" in _p3_last_text(api), "invite text")
    _p4_cb(C, g, "du:ac:" + s["sid"], 1)
    _p4_cb(A, g, "du:ac:" + s["sid"], 2)
    check(_p4_sess(g, "duel", A)["data"]["state"] == "invite", "wrong person accepted")
    _p4_cb(B, g, "du:ac:" + s["sid"], 3)
    check(_p4_sess(g, "duel", A)["data"]["state"] == "pick", "accept failed")
    _p4_cb(C, g, "du:g:%s:rps" % s["sid"], 4)
    check(_p4_sess(g, "duel", A)["data"]["state"] == "pick", "stranger picked game")
    _p4_cb(A, g, "du:g:%s:rps" % s["sid"], 5)
    for k in range(2):
        _p4_cb(A, g, "du:rp:%s:0" % s["sid"], 10 + k)
        _p4_cb(A, g, "du:rp:%s:1" % s["sid"], 20 + k)
        _p4_cb(B, g, "du:rp:%s:2" % s["sid"], 30 + k)
    check(_p4_sess(g, "duel", A) is None, "duel not finished")
    check(_p4_results(g, A, "duel") == [{"result": "win", "xp": GAME_XP["duel_win"]}], "winner %s" % _p4_results(g, A, "duel"))
    check(_p4_results(g, B, "duel") == [{"result": "loss", "xp": GAME_XP["lose"]}], "loser")
    STATS.flush()
    check(_p4_stat(g, A, "duels_won") == 1 and _p4_stat(g, A, "duels_sent") == 1, "duel counters")
    COOLDOWNS.clear()
    _p4_text(A, g, "/duel", mid=40, reply_user=B)
    s = _p4_sess(g, "duel", A)
    _p4_cb(B, g, "du:no:" + s["sid"], 41)
    check(_p4_sess(g, "duel", A) is None, "decline failed")
    COOLDOWNS.clear()
    before_b, before_c = len(_p4_results(g, B, "duel")), len(_p4_results(g, C, "duel"))
    _p4_cb(B, g, "gm:duel", 50)
    s = _p4_sess(g, "duel", B)
    check(s and s["data"]["target"] == 0, "open challenge")
    _p4_cb(C, g, "du:ac:" + s["sid"], 51)
    _p4_cb(B, g, "du:g:%s:dice" % s["sid"], 52)
    _p4_cb(B, g, "du:dc:" + s["sid"], 53)
    _p4_cb(C, g, "du:dc:" + s["sid"], 54)
    for n in range(6):
        s2 = _p4_sess(g, "duel", B)
        if s2 is None:
            break
        for u in (B, C):
            _p4_cb(u, g, "du:dc:" + s["sid"], 60 + n * 2 + (u == C))
    check(_p4_sess(g, "duel", B) is None, "dice duel never finished")
    check(len(_p4_results(g, B, "duel")) == before_b + 1 and len(_p4_results(g, C, "duel")) == before_c + 1, "dice duel results")
    COOLDOWNS.clear()
    check(APP.db.val("SELECT username FROM users WHERE user_id=?", (B,)) is None,
          "expected the fake callbacks (which carry no username) to have cleared it")
    upsert_user({"id": B, "first_name": "Bee", "username": "bee_u"})  # در بله هر آپدیت username را همراه دارد
    _p4_text(A, g, "/duel @bee_u", mid=80)
    s = _p4_sess(g, "duel", A)
    check(s and s["data"]["target"] == B, "@username invite")
    _p4_cb(B, g, "du:ac:" + s["sid"], 81)
    _p4_cb(A, g, "du:g:%s:math" % s["sid"], 82)
    # سؤال ۱: A غلط می‌زند (حذف می‌شود)، دوباره زدنش بی‌اثر است، B درست می‌زند و امتیاز می‌گیرد
    d = _p4_sess(g, "duel", A)["data"]
    _p4_cb(A, g, "du:mt:%s:%d" % (s["sid"], (d["ans"] + 1) % 4), 90)
    _p4_cb(A, g, "du:mt:%s:%d" % (s["sid"], d["ans"]), 91)
    d = _p4_sess(g, "duel", A)["data"]
    check(d["sc"][str(A)] == 0 and d["qn"] == 0 and str(A) in d["out"], "eliminated player must not score")
    _p4_cb(B, g, "du:mt:%s:%d" % (s["sid"], d["ans"]), 92)
    d = _p4_sess(g, "duel", A)["data"]
    check(d["sc"][str(B)] == 1 and d["qn"] == 1 and d["out"] == [], "point to B then next question: %s" % d)
    # سؤال ۲: هر دو غلط → بدون امتیاز، سؤال بعد
    _p4_cb(A, g, "du:mt:%s:%d" % (s["sid"], (d["ans"] + 1) % 4), 93)
    _p4_cb(B, g, "du:mt:%s:%d" % (s["sid"], (d["ans"] + 1) % 4), 94)
    d = _p4_sess(g, "duel", A)["data"]
    check(d["qn"] == 2 and d["sc"][str(A)] == 0 and d["sc"][str(B)] == 1, "both wrong must skip the question: %s" % d)
    # سؤال ۳: B درست می‌زند → ۲ امتیاز و پایان
    _p4_cb(B, g, "du:mt:%s:%d" % (s["sid"], d["ans"]), 95)
    check(_p4_sess(g, "duel", A) is None, "math duel not finished")
    check(_p4_results(g, B, "duel")[-1]["result"] == "win" and _p4_results(g, A, "duel")[-1]["result"] == "loss", "math duel winner")
    return "ok"


@selftest("👥 بازی گروهی: جواب‌ها، پایان، Timeout، فقط گروه")
def _t_p4_group():
    g = -9481
    A, B, C, D = 9480, 9481, 9482, 9483
    for u in (A, B, C, D):
        ADMIN_CACHE[(g, u)] = (False, time.monotonic() + 300)
    COOLDOWNS.clear()
    _p4_cb(A, g, "gm:group_game", 1)
    s = _p4_sess(g, "group_game")
    check(s and s["data"]["mode"] == "group", "round not started")
    ans = s["data"]["ans"]
    _p4_cb(B, g, "qz:%s:%d" % (s["sid"], ans), 2)
    _p4_cb(C, g, "qz:%s:%d" % (s["sid"], (ans + 1) % 4), 3)
    _p4_cb(D, g, "qz:%s:%d" % (s["sid"], ans), 4)
    _p4_cb(D, g, "qz:%s:%d" % (s["sid"], (ans + 2) % 4), 5)
    check(len(_p4_sess(g, "group_game")["data"]["answers"]) == 3, "answers recorded")
    _p4_cb(C, g, "gg:end:" + s["sid"], 6)
    check(_p4_sess(g, "group_game") is not None, "non-starter ended the round")
    _p4_cb(A, g, "gg:end:" + s["sid"], 7)
    check(_p4_sess(g, "group_game") is None, "round not finalized")
    check(_p4_results(g, B, "group_game") == [{"result": "win", "xp": GAME_XP["group_correct"] + GAME_XP["group_first"]}], "first winner")
    check(_p4_results(g, D, "group_game") == [{"result": "win", "xp": GAME_XP["group_correct"]}], "second winner")
    check(_p4_results(g, C, "group_game") == [{"result": "loss", "xp": GAME_XP["lose"]}], "wrong answer")
    COOLDOWNS.clear()
    _p4_cb(A, g, "gm:group_game", 10)
    s = _p4_sess(g, "group_game")
    _p4_cb(B, g, "qz:%s:%d" % (s["sid"], s["data"]["ans"]), 11)
    APP.db.run("UPDATE sessions SET expires_at=? WHERE sid=?", (now() - 1, s["sid"]))
    _task_game_sweep()
    check(_p4_sess(g, "group_game") is None and len(_p4_results(g, B, "group_game")) == 2, "timeout finalize")
    COOLDOWNS.clear()
    handle_update(_p2_cb("p4-priv-1", "gm:group_game", 9490, 9490, 20, ctype="private"))
    check(_p4_sess(9490, "group_game") is None, "group game started in private")
    check(any(c[0] == "answer_callback_query" and c[1][2] for c in APP.api.calls), "no alert shown in private")
    return "ok"


@selftest("Sweeper: دعوت دوئل منقضی + Cleanup جلسه‌های تازه‌منقضی")
def _t_p4_sweeper():
    api, g = APP.api, -9491
    COOLDOWNS.clear()
    _p4_cb(9491, g, "gm:duel", 1)
    s = _p4_sess(g, "duel", 9491)
    APP.db.run("UPDATE sessions SET expires_at=? WHERE sid=?", (now() - 1, s["sid"]))
    run_cleanup()
    check(session_get_raw(s["sid"]) is not None, "cleanup deleted a freshly expired session")
    _task_game_sweep()
    check(session_get_raw(s["sid"]) is None, "expired duel not removed")
    check(any("منقضی" in json.dumps(c[1][1], ensure_ascii=False) for c in api.calls if c[0] == "try_call"), "expiry message")
    return "ok"


# ══════════════════════════════════════════════════════════════════
# ███ END OF PHASE 4


# ══════════════════════════════════════════════════════════════════
# ███ PHASE 5 — XP / LEVEL / ACHIEVEMENTS / MISSIONS / LEAGUE
# ══════════════════════════════════════════════════════════════════

LEVEL_COEF = 18   # XP لازم برای Level L:  18 × (L-1)²   (مثلاً XP 4820 ⇒ Level 17)


def xp_for_level(level):
    return LEVEL_COEF * (max(1, int(level)) - 1) ** 2


def level_for_xp(xp):
    xp = max(0, int(xp))
    lvl = int((xp / LEVEL_COEF) ** 0.5) + 1
    while xp_for_level(lvl + 1) <= xp:
        lvl += 1
    while lvl > 1 and xp_for_level(lvl) > xp:
        lvl -= 1
    return lvl


def send_notes(chat_id, notes):
    if notes:
        APP.api.try_call("sendMessage", {"chat_id": chat_id, "text": safe_text("\n".join(notes))}, retries=1)


def sync_level(chat_id, user_id, notes):
    r = APP.db.one("SELECT xp, level FROM user_stats WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
    if r is None:
        return
    new = level_for_xp(r["xp"])
    if new != r["level"]:
        APP.db.run("UPDATE user_stats SET level=? WHERE chat_id=? AND user_id=?", (new, int(chat_id), int(user_id)))
        if new > r["level"]:
            notes.append("⬆️ %s به Level %d رسید! 🎉" % (uname(user_id), new))


def add_xp(chat_id, user_id, amount, notes=None, league=True):
    """XP خارج از بازی‌ها (مأموریت/مدال). به‌طور پیش‌فرض به امتیاز لیگ هفته هم اضافه می‌شود."""
    amount = int(amount)
    if amount <= 0:
        return 0
    cid, uid, t = int(chat_id), int(user_id), now()
    own = notes is None
    notes = [] if own else notes
    with APP.db.tx():
        APP.db.run("INSERT OR IGNORE INTO user_stats(chat_id, user_id, first_active) VALUES(?,?,?)", (cid, uid, t))
        APP.db.run("UPDATE user_stats SET xp=xp+?, last_active=? WHERE chat_id=? AND user_id=?", (amount, t, cid, uid))
        if league:
            APP.db.run("""INSERT INTO league_scores(chat_id, week_key, user_id, points) VALUES(?,?,?,?)
                          ON CONFLICT(chat_id, week_key, user_id) DO UPDATE SET points=points+excluded.points""",
                       (cid, week_key(t), uid, amount))
    sync_level(cid, uid, notes)
    if own:
        send_notes(cid, notes)
    return amount


def set_record(chat_id, rkey, user_id, value):
    """رکورد تاریخی؛ فقط اگر بزرگ‌تر باشد جایگزین می‌شود. True یعنی رکورد شکسته شد."""
    cur = APP.db.run(
        """INSERT INTO records(chat_id, rkey, user_id, value, ts) VALUES(?,?,?,?,?)
           ON CONFLICT(chat_id, rkey) DO UPDATE SET user_id=excluded.user_id, value=excluded.value, ts=excluded.ts
           WHERE excluded.value > records.value""", (int(chat_id), rkey, int(user_id), int(value), now()))
    return cur.rowcount > 0


# ─── دستاوردها ───
ACHIEVEMENTS = [
    # کلید، ایموجی، عنوان، توضیح، مخفی؟، XP جایزه
    ("first_win", "🏆", "اولین برد", "اولین بازی‌ات رو ببر", 0, 20),
    ("streak10", "🔥", "۱۰ برد متوالی", "۱۰ بار پشت سر هم ببر", 0, 150),
    ("joke_factory", "😂", "کارخانه خنده", "به ۳۰ جوک رأی بده", 0, 60),
    ("gamer", "🎮", "گیمر گپ", "۵۰ بازی انجام بده", 0, 80),
    ("fastest", "⚡", "سریع‌ترین", "۵ بار توی بازی سرعتی برنده شو", 0, 80),
    ("weekly_champ", "👑", "قهرمان هفته", "اول لیگ هفتگی شو", 0, 100),
    ("brain", "🧠", "مغز متفکر", "۱۰ بار به سؤال اطلاعات عمومی یا معما درست جواب بده", 0, 80),
    ("old_member", "💎", "عضو قدیمی", "۳۰ روز از اولین فعالیتت توی گروه بگذره", 0, 100),
    ("detective", "🕵️", "کارآگاه", "یه مأموریت مخفی رو کشف کن", 0, 70),
    ("lucky", "🎲", "خوش‌شانس", "۵ بار توی تاس برنده شو", 0, 50),
    ("mission_hunter", "🎯", "شکارچی مأموریت", "۱۰ مأموریت انجام بده", 0, 100),
    ("weekly_hero", "🌟", "قهرمان مأموریت‌های هفته", "همه‌ی مأموریت‌های هفتگی رو توی یه هفته انجام بده", 0, 120),
    ("night_owl", "🦉", "جغد شب", "بین ۲ تا ۵ صبح بازی کن", 1, 40),
    ("comeback", "💪", "بازگشت", "بعد از ۳ باخت پشت سر هم ببر", 1, 40),
    ("math_wizard", "🧮", "ریاضی‌دان", "توی بازی ریاضی نمره‌ی کامل بگیر", 1, 50),
    ("duelist", "⚔️", "دوئل‌باز", "۵ دوئل ببر", 1, 80),
]
ACH = {a[0]: a for a in ACHIEVEMENTS}


def seed_achievements():
    with APP.db.tx():
        for key, emoji, title, desc, secret, xp in ACHIEVEMENTS:
            APP.db.run("""INSERT INTO achievements(akey, title, emoji, description, secret, reward_xp) VALUES(?,?,?,?,?,?)
                          ON CONFLICT(akey) DO UPDATE SET title=excluded.title, emoji=excluded.emoji,
                          description=excluded.description, secret=excluded.secret, reward_xp=excluded.reward_xp""",
                       (key, title, emoji, desc, secret, xp))


on_startup(seed_achievements)


def award_achievement(chat_id, user_id, key, notes=None, league=True):
    """True فقط بار اول (بدون پاداش تکراری)."""
    a = ACH[key]
    cid, uid = int(chat_id), int(user_id)
    if APP.db.run("INSERT OR IGNORE INTO user_achievements(chat_id, user_id, akey, earned_at) VALUES(?,?,?,?)",
                  (cid, uid, key, now())).rowcount == 0:
        return False
    own = notes is None
    notes = [] if own else notes
    notes.append("🏅 %s مدال «%s %s» رو گرفت! ⭐ +%d XP" % (uname(uid), a[1], a[2], a[5]))
    add_xp(cid, uid, a[5], notes, league)
    if own:
        send_notes(cid, notes)
    return True


def _count(sql, params):
    return APP.db.val(sql, params, 0)


def check_achievements(chat_id, user_id, info=None, notes=None):
    cid, uid = int(chat_id), int(user_id)
    STATS.flush()
    earned = {r["akey"] for r in APP.db.all("SELECT akey FROM user_achievements WHERE chat_id=? AND user_id=?", (cid, uid))}
    if len(earned) >= len(ACHIEVEMENTS):
        return
    s = APP.db.one("SELECT * FROM user_stats WHERE chat_id=? AND user_id=?", (cid, uid))
    if s is None:
        return
    wins = lambda keys: _count("SELECT COUNT(*) FROM game_records WHERE chat_id=? AND user_id=? AND result='win' AND game_key IN (%s)"
                               % ",".join("?" * len(keys)), (cid, uid, *keys))

    def comeback():
        rs = [r["result"] for r in APP.db.all("SELECT result FROM game_records WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT 4", (cid, uid))]
        return rs == ["win", "loss", "loss", "loss"]

    checks = {
        "first_win": lambda: s["games_won"] >= 1,
        "streak10": lambda: s["best_streak"] >= 10,
        "joke_factory": lambda: s["joke_votes"] >= 30,
        "gamer": lambda: s["games_played"] >= 50,
        "fastest": lambda: wins(["speed"]) >= 5,
        "brain": lambda: wins(["trivia", "riddle"]) >= 10,
        "old_member": lambda: s["first_active"] > 0 and now() - s["first_active"] >= 30 * 86400,
        "detective": lambda: _count("""SELECT COUNT(*) FROM user_missions um JOIN missions m ON m.id=um.mission_id
                                      WHERE um.chat_id=? AND um.user_id=? AND m.secret=1 AND um.done=1""", (cid, uid)) >= 1,
        "lucky": lambda: wins(["dice"]) >= 5,
        "mission_hunter": lambda: s["missions_done"] >= 10,
        "night_owl": lambda: bool(info) and 2 <= hour_of() < 5,
        "comeback": comeback,
        "math_wizard": lambda: bool(info) and info.get("game_key") == "math" and info.get("score") == MATH_ROUNDS,
        "duelist": lambda: s["duels_won"] >= 5,
    }
    own = notes is None
    notes = [] if own else notes
    for key, fn in checks.items():
        if key not in earned and fn():
            award_achievement(cid, uid, key, notes)
    if own:
        send_notes(cid, notes)


# ─── مأموریت‌ها ───
MISSIONS = [
    # کلید، عنوان، نوع، متریک، هدف، XP، مدال، مخفی؟
    ("d_games2", "۲ بازی انجام بده", "daily", "games_played", 2, 30, None, 0),
    ("d_win1", "یه بازی رو ببر", "daily", "games_won", 1, 25, None, 0),
    ("d_jokes3", "به ۳ جوک رأی بده", "daily", "joke_votes", 3, 25, None, 0),
    ("d_duel1", "یک نفر رو به دوئل دعوت کن", "daily", "duels_sent", 1, 30, None, 0),
    ("d_fortune1", "یه فال بگیر", "daily", "fortunes", 1, 15, None, 0),
    ("d_trivia2", "۲ سؤال اطلاعات عمومی جواب بده", "daily", "game:trivia", 2, 25, None, 0),
    ("d_games5", "۵ بازی انجام بده", "daily", "games_played", 5, 50, None, 0),
    ("w_games20", "۲۰ بازی انجام بده", "weekly", "games_played", 20, 150, None, 0),
    ("w_wins10", "۱۰ بار برنده شو", "weekly", "games_won", 10, 200, None, 0),
    ("w_jokes15", "به ۱۵ جوک رأی بده", "weekly", "joke_votes", 15, 120, None, 0),
    ("s_night", "شبگرد: بین ۰۰ تا ۵ صبح یه بازی انجام بده", "secret", "night_game", 1, 60, None, 1),
    ("s_streak3", "۳ برد پشت سر هم بگیر", "secret", "streak3", 1, 60, None, 1),
]
DAILY_MISSION_COUNT = 3


def seed_missions():
    with APP.db.tx():
        for mkey, title, kind, metric, target, xp, medal, secret in MISSIONS:
            APP.db.run("""INSERT INTO missions(mkey, title, kind, metric, target, reward_xp, reward_medal, secret, active)
                          VALUES(?,?,?,?,?,?,?,?,1)
                          ON CONFLICT(mkey) DO UPDATE SET title=excluded.title, kind=excluded.kind, metric=excluded.metric,
                          target=excluded.target, reward_xp=excluded.reward_xp, reward_medal=excluded.reward_medal,
                          secret=excluded.secret""", (mkey, title, kind, metric, target, xp, medal, secret))


on_startup(seed_missions)


def active_missions(chat_id):
    """[(ردیف مأموریت، period)] — ۳ روزانه‌ی ثابت در هر روز برای هر گروه، همه‌ی هفتگی‌ها و مخفی‌ها."""
    t = now()
    day, wk = day_key(t), week_key(t)
    rows = APP.db.all("SELECT * FROM missions WHERE active=1 ORDER BY id")
    daily = sorted((r for r in rows if r["kind"] == "daily"),
                   key=lambda r: hashlib.sha1(("%s:%s:%s" % (day, chat_id, r["mkey"])).encode()).hexdigest())[:DAILY_MISSION_COUNT]
    out = [(r, day) for r in daily]
    out += [(r, wk) for r in rows if r["kind"] == "weekly"]
    out += [(r, "once") for r in rows if r["kind"] == "secret"]
    return out


def _complete_mission(cid, uid, m, period, notes):
    if not reward_once("mis:%s:%s:%d:%d" % (m["mkey"], period, cid, uid)):
        return
    APP.db.run("UPDATE user_missions SET claimed=1 WHERE chat_id=? AND user_id=? AND mission_id=? AND period=?",
               (cid, uid, m["id"], period))
    APP.db.run("INSERT OR IGNORE INTO user_stats(chat_id, user_id, first_active) VALUES(?,?,?)", (cid, uid, now()))
    APP.db.run("UPDATE user_stats SET missions_done=missions_done+1 WHERE chat_id=? AND user_id=?", (cid, uid))
    STATS.group_add(cid, "missions_done")
    notes.append("🎯 %s مأموریت «%s» رو انجام داد! ⭐ +%d XP" % (uname(uid), m["title"], m["reward_xp"]))
    add_xp(cid, uid, m["reward_xp"], notes)
    if m["reward_medal"] in ACH:
        award_achievement(cid, uid, m["reward_medal"], notes)
    if m["secret"]:
        award_achievement(cid, uid, "detective", notes)
    if m["kind"] == "weekly":
        total = _count("SELECT COUNT(*) FROM missions WHERE active=1 AND kind='weekly'", ())
        done = _count("""SELECT COUNT(*) FROM user_missions um JOIN missions m ON m.id=um.mission_id
                         WHERE um.chat_id=? AND um.user_id=? AND um.period=? AND um.done=1 AND m.kind='weekly'""",
                      (cid, uid, period))
        if total and done >= total:
            award_achievement(cid, uid, "weekly_hero", notes)


def mission_event(chat_id, user_id, metric, n=1, notes=None):
    """هر رویداد قابل شمارش (بازی، رأی جوک، فال، دوئل…) از اینجا به مأموریت‌ها می‌رسد."""
    cid, uid = int(chat_id), int(user_id)
    r = APP.db.one("SELECT missions FROM group_settings WHERE chat_id=?", (cid,))
    if r is not None and not r["missions"]:
        return
    own = notes is None
    notes = [] if own else notes
    for m, period in active_missions(cid):
        if m["metric"] != metric:
            continue
        row = APP.db.one("SELECT progress, done FROM user_missions WHERE chat_id=? AND user_id=? AND mission_id=? AND period=?",
                         (cid, uid, m["id"], period))
        if row is not None and row["done"]:
            continue
        prog = (row["progress"] if row else 0) + n
        done = 1 if prog >= m["target"] else 0
        APP.db.run("""INSERT INTO user_missions(chat_id, user_id, mission_id, period, progress, done) VALUES(?,?,?,?,?,?)
                      ON CONFLICT(chat_id, user_id, mission_id, period) DO UPDATE SET progress=excluded.progress, done=excluded.done""",
                   (cid, uid, m["id"], period, min(prog, m["target"]), done))
        if done:
            _complete_mission(cid, uid, m, period, notes)
    if own:
        check_achievements(cid, uid, None, notes)
        send_notes(cid, notes)


@on_game_result
def _p5_game_hook(info):
    chat, uid = info["chat_id"], info["user_id"]
    notes = []
    sync_level(chat, uid, notes)
    mission_event(chat, uid, "games_played", 1, notes)
    mission_event(chat, uid, "game:" + info["game_key"], 1, notes)
    if info["result"] == "win":
        mission_event(chat, uid, "games_won", 1, notes)
    if user_stat_get(chat, uid, "streak") >= 3:
        mission_event(chat, uid, "streak3", 1, notes)
    if hour_of() < 5:
        mission_event(chat, uid, "night_game", 1, notes)
    set_record(chat, "best_streak", uid, user_stat_get(chat, uid, "best_streak"))
    check_achievements(chat, uid, info, notes)
    send_notes(chat, notes)


def missions_text(chat_id, user_id):
    cid, uid = int(chat_id), int(user_id)
    act = active_missions(cid)
    prog = {}
    for r in APP.db.all("SELECT mission_id, period, progress, done FROM user_missions WHERE chat_id=? AND user_id=?", (cid, uid)):
        prog[(r["mission_id"], r["period"])] = (r["progress"], r["done"])
    lines = ["🎯 مأموریت‌ها", ""]
    for kind, head in (("daily", "📅 روزانه"), ("weekly", "🗓️ هفتگی")):
        lines.append(head)
        for m, period in act:
            if m["kind"] == kind:
                p, d = prog.get((m["id"], period), (0, 0))
                lines.append("%s %s (%d/%d) ⭐%d" % ("✅" if d else "🔲", m["title"], min(p, m["target"]), m["target"], m["reward_xp"]))
        lines.append("")
    secret = [(m, period) for m, period in act if m["kind"] == "secret"]
    lines.append("🕵️ مخفی")
    for m, period in secret:
        d = prog.get((m["id"], period), (0, 0))[1]
        lines.append("✅ %s ⭐%d" % (m["title"], m["reward_xp"]) if d else "❓ یه مأموریت مخفی — پیداش کن!")
    return "\n".join(lines)


@section("missions", feature="missions")
def sec_missions(ctx):
    show(ctx, missions_text(ctx.chat_id, ctx.user_id),
         kb([btn("🔄 بروزرسانی", "menu:missions"), btn("🏆 رتبه‌ها", "menu:ranks")], home_row()))


# ─── لیگ هفتگی و رتبه‌ها ───
MEDALS = ["🥇", "🥈", "🥉"]


def league_rows(chat_id, wk, limit=10):
    return APP.db.all("SELECT user_id, points FROM league_scores WHERE chat_id=? AND week_key=? AND points>0 "
                      "ORDER BY points DESC, user_id LIMIT ?", (int(chat_id), wk, limit))


def week_reset_text():
    y, m, d = (int(x) for x in week_key().split("-"))
    start = int(datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc).timestamp()) - IRAN_UTC_OFFSET
    left = max(0, start + 7 * 86400 - now())
    return "⏳ تا ریست لیگ: %d روز و %d ساعت" % (left // 86400, (left % 86400) // 3600)


def league_text(chat_id, user_id):
    wk = week_key()
    rows = league_rows(chat_id, wk)
    lines = ["🏆 لیگ هفتگی", ""]
    if not rows:
        lines.append("هنوز کسی امتیاز نگرفته؛ یه بازی کن و اول باش! 🎮")
    for i, r in enumerate(rows):
        lines.append("%s %s — %d" % (MEDALS[i] if i < 3 else "%d." % (i + 1), uname(r["user_id"]), r["points"]))
    mine = APP.db.val("SELECT points FROM league_scores WHERE chat_id=? AND week_key=? AND user_id=?", (int(chat_id), wk, int(user_id)), 0)
    if mine and all(r["user_id"] != int(user_id) for r in rows):
        rank = APP.db.val("SELECT COUNT(*) FROM league_scores WHERE chat_id=? AND week_key=? AND points>?", (int(chat_id), wk, mine), 0) + 1
        lines += ["", "تو: رتبه #%d با %d امتیاز" % (rank, mine)]
    lines += ["", week_reset_text(), "رکوردهای تاریخی با ریست هفتگی پاک نمی‌شن 👑"]
    return "\n".join(lines)


def ranks_markup():
    return kb([btn("🏆 لیگ هفتگی", "rk:lg"), btn("⭐ رتبه کلی", "rk:xp")],
              [btn("👑 رکوردها", "rk:rec"), btn("🏅 مدال‌ها", "rk:md")],
              [btn("👤 پروفایل من", "rk:pf"), btn("🎯 مأموریت‌ها", "menu:missions")], home_row())


def sub_markup(extra=None):
    rows = [extra] if extra else []
    rows.append([btn("↩️ رتبه‌ها", "menu:ranks"), btn("🏠 برگشت", "menu:home")])
    return kb(*rows)


@game_starter("league")
def start_league(ctx):
    show(ctx, league_text(ctx.chat_id, ctx.user_id),
         kb([btn("🔄", "gm:league"), btn("⭐ رتبه کلی", "rk:xp")], [btn("🎮 بازی‌ها", "gm:hub"), btn("🏠 برگشت", "menu:home")]))


@section("ranks")
def sec_ranks(ctx):
    show(ctx, "🏆 رتبه‌ها\n\nچی رو می‌خوای ببینی؟ 👇", ranks_markup())


def xp_rank_text(chat_id):
    rows = APP.db.all("SELECT user_id, xp, level FROM user_stats WHERE chat_id=? AND xp>0 ORDER BY xp DESC, user_id LIMIT 10", (int(chat_id),))
    lines = ["⭐ رتبه‌ی کلی (XP)", ""]
    if not rows:
        lines.append("هنوز کسی XP نگرفته 😴")
    for i, r in enumerate(rows):
        lines.append("%s %s — Level %d • %d XP" % (MEDALS[i] if i < 3 else "%d." % (i + 1), uname(r["user_id"]), r["level"], r["xp"]))
    return "\n".join(lines)


RECORD_LABELS = {"best_week_points": "🏆 بیشترین امتیاز در یک هفته", "best_streak": "🔥 طولانی‌ترین برد متوالی"}


def records_text(chat_id):
    lines = ["👑 رکوردهای تاریخی", ""]
    got = False
    for key, label in RECORD_LABELS.items():
        r = APP.db.one("SELECT user_id, value FROM records WHERE chat_id=? AND rkey=?", (int(chat_id), key))
        if r:
            got = True
            lines.append("%s: %s — %d" % (label, uname(r["user_id"]), r["value"]))
    champs = APP.db.all("SELECT mkey, mvalue FROM group_memory WHERE chat_id=? AND mkey LIKE 'league_champ:%' ORDER BY mkey DESC LIMIT 5", (int(chat_id),))
    if champs:
        got = True
        lines += ["", "👑 قهرمان‌های هفته‌های قبل:"]
        for c in champs:
            u, p = c["mvalue"].split(":")
            lines.append("• هفته‌ی %s: %s (%s امتیاز)" % (c["mkey"].split(":", 1)[1], uname(int(u)), p))
    if not got:
        lines.append("هنوز رکوردی ثبت نشده؛ بازی کن تا اولین رکورد مال تو باشه! 🎮")
    return "\n".join(lines)


def medals_text(chat_id, user_id):
    earned = {r["akey"]: r["earned_at"] for r in APP.db.all("SELECT akey, earned_at FROM user_achievements WHERE chat_id=? AND user_id=?",
                                                          (int(chat_id), int(user_id)))}
    lines = ["🏅 مدال‌های %s (%d از %d)" % (uname(user_id), len(earned), len(ACHIEVEMENTS)), ""]
    for key, emoji, title, desc, secret, xp in ACHIEVEMENTS:
        if key in earned:
            lines.append("%s %s — %s" % (emoji, title, desc))
        elif secret:
            lines.append("❓ مدال مخفی")
        else:
            lines.append("🔒 %s — %s" % (title, desc))
    return "\n".join(lines)


def profile_text(chat_id, user_id):
    cid, uid = int(chat_id), int(user_id)
    STATS.flush()
    s = APP.db.one("SELECT * FROM user_stats WHERE chat_id=? AND user_id=?", (cid, uid))
    name = uname(uid)
    if s is None:
        return "👤 %s\n\nهنوز بازی نکرده 🎮" % name
    rank = APP.db.val("SELECT COUNT(*) FROM user_stats WHERE chat_id=? AND xp>?", (cid, s["xp"]), 0) + 1
    medals = APP.db.val("SELECT COUNT(*) FROM user_achievements WHERE chat_id=? AND user_id=?", (cid, uid), 0)
    fav = APP.db.one("SELECT game_key, COUNT(*) c FROM game_records WHERE chat_id=? AND user_id=? GROUP BY game_key ORDER BY c DESC LIMIT 1", (cid, uid))
    lvl = level_for_xp(s["xp"])
    return ("👤 %s\n\n⭐ Level %d\n⚡ XP %d  (تا Level بعد: %d)\n🎮 بازی‌ها %d\n🥇 بردها %d\n🔥 Streak %d (رکورد %d)\n"
            "🏆 رتبه #%d\n🏅 مدال‌ها %d\n🎮 بازی محبوب: %s"
            % (name, lvl, s["xp"], xp_for_level(lvl + 1) - s["xp"], s["games_played"], s["games_won"], s["streak"],
               s["best_streak"], rank, medals, GAME_TITLES.get(fav["game_key"], "—") if fav else "—"))


def profile_markup(user_id):
    row = [btn("🏅 مدال‌ها", "rk:md:%d" % user_id)]
    if "stats" in SECTIONS:
        row.append(btn("📊 آمار کامل", "menu:stats"))
    return sub_markup(row)


@callback("rk")
def cb_ranks(ctx):
    a = ctx.parts[1] if len(ctx.parts) > 1 else "lg"
    try:
        target = int(ctx.parts[2]) if len(ctx.parts) > 2 else int(ctx.user_id)
    except ValueError:
        ctx.answer("دکمه نامعتبر", True)
        return
    if a == "lg":
        show(ctx, league_text(ctx.chat_id, ctx.user_id), sub_markup([btn("🔄", "rk:lg")]))
    elif a == "xp":
        show(ctx, xp_rank_text(ctx.chat_id), sub_markup())
    elif a == "rec":
        show(ctx, records_text(ctx.chat_id), sub_markup())
    elif a == "md":
        show(ctx, medals_text(ctx.chat_id, target), sub_markup([btn("👤 پروفایل", "rk:pf:%d" % target)]))
    elif a == "pf":
        show(ctx, profile_text(ctx.chat_id, target), profile_markup(target))
    else:
        ctx.answer("دکمه نامعتبر", True)


@command("profile")
def cmd_profile(ctx):
    rt = ctx.reply_to
    target = int(rt["from"]["id"]) if rt and (rt.get("from") or {}).get("id") and not rt["from"].get("is_bot") else int(ctx.user_id)
    if rt and target != int(ctx.user_id):
        upsert_user(rt["from"])
    ctx.reply(profile_text(ctx.chat_id, target), profile_markup(target))


# ─── پایان هفته‌ی لیگ: قهرمان، رکورد، اعلام ───
def finalize_league_week(chat_id, wk):
    cid = int(chat_id)
    mem_set(cid, "league_done:" + wk, "1")
    rows = league_rows(cid, wk, 3)
    if not rows:
        return None
    champ = rows[0]
    mem_set(cid, "league_champ:" + wk, "%d:%d" % (champ["user_id"], champ["points"]))
    set_record(cid, "best_week_points", champ["user_id"], champ["points"])
    notes = ["🏆 نتیجه‌ی لیگ هفته‌ی گذشته", ""]
    notes += ["%s %s — %d" % (MEDALS[i], uname(r["user_id"]), r["points"]) for i, r in enumerate(rows)]
    notes += ["", "👑 %s قهرمان هفته شد!" % uname(champ["user_id"])]
    award_achievement(cid, champ["user_id"], "weekly_champ", notes, league=False)
    send_notes(cid, notes)
    return champ["user_id"]


@periodic(300, "league_rollover")
def _task_league_rollover():
    for r in APP.db.all("SELECT DISTINCT chat_id, week_key FROM league_scores WHERE week_key<?", (week_key(),)):
        if mem_get(r["chat_id"], "league_done:" + r["week_key"]) is None:
            finalize_league_week(r["chat_id"], r["week_key"])


# ─── SELFTEST فاز ۵ ───
def _p5_notes(api):
    return [c[1][1]["text"] for c in api.calls if c[0] == "try_call" and c[1][0] == "sendMessage" and "text" in c[1][1]]


@selftest("سطح: فرمول، مرزها، مثال Level 17")
def _t_p5_levels():
    check(level_for_xp(0) == 1 and level_for_xp(17) == 1 and level_for_xp(18) == 2, "low levels")
    check(level_for_xp(4820) == 17 and xp_for_level(17) == 4608, "spec example")
    for lv in range(1, 80):
        check(level_for_xp(xp_for_level(lv)) == lv, "level %d lower bound" % lv)
        if lv > 1:
            check(level_for_xp(xp_for_level(lv) - 1) == lv - 1, "level %d just below" % lv)
    return "ok"


@selftest("دستاوردها: تعریف، اعطا بدون تکرار، Level-up")
def _t_p5_achievements():
    api, g, u = APP.api, -9501, 9501
    seed_achievements()
    check(len(ACH) == len(ACHIEVEMENTS) >= 14 and sum(a[4] for a in ACHIEVEMENTS) >= 3, "achievement set")
    check(APP.db.val("SELECT COUNT(*) FROM achievements") == len(ACHIEVEMENTS), "seed")
    record_result(g, u, "dice", "win", 5, 20, token="a1")
    check(APP.db.val("SELECT COUNT(*) FROM user_achievements WHERE chat_id=? AND user_id=? AND akey='first_win'", (g, u)) == 1, "first_win")
    check(user_stat_get(g, u, "xp") == 20 + ACH["first_win"][5], "xp = game + medal reward")
    check(user_stat_get(g, u, "level") == level_for_xp(40) == 2, "level synced")
    text = "\n".join(_p5_notes(api))
    check("اولین برد" in text and "Level 2" in text, "notice missing: " + text)
    n = len(api.calls)
    record_result(g, u, "dice", "win", 5, 20, token="a2")
    check(APP.db.val("SELECT COUNT(*) FROM user_achievements WHERE chat_id=? AND user_id=? AND akey='first_win'", (g, u)) == 1, "duplicate medal")
    check(user_stat_get(g, u, "xp") == 60, "xp after second win: %d" % user_stat_get(g, u, "xp"))
    return "ok"


@selftest("دستاوردها: مخفی‌ها، Streak، گیمر، سریع، عضو قدیمی")
def _t_p5_hidden():
    g = -9511
    for i in range(3):
        record_result(g, 9511, "dice", "loss", 0, 0, token="c%d" % i)
    record_result(g, 9511, "dice", "win", 1, 20, token="c9")
    earned = lambda u, k: APP.db.val("SELECT COUNT(*) FROM user_achievements WHERE chat_id=? AND user_id=? AND akey=?", (g, u, k)) == 1
    check(earned(9511, "comeback"), "comeback")
    record_result(g, 9512, "math", "win", MATH_ROUNDS, 60, token="m1")
    check(earned(9512, "math_wizard"), "math_wizard")
    old_hour = globals()["hour_of"]
    globals()["hour_of"] = lambda ts=None: 3
    try:
        record_result(g, 9513, "dice", "win", 1, 20, token="n1")
    finally:
        globals()["hour_of"] = old_hour
    check(earned(9513, "night_owl"), "night_owl")
    record_result(g, 9514, "dice", "win", 1, 20, token="s1")
    APP.db.run("UPDATE user_stats SET best_streak=10, games_played=50, duels_won=5, first_active=? WHERE chat_id=? AND user_id=?",
               (now() - 31 * 86400, g, 9514))
    for i in range(5):
        APP.db.run("INSERT INTO game_records(chat_id, user_id, game_key, result, score, xp, created_at) VALUES(?,?,?,?,?,?,?)",
                   (g, 9514, "speed", "win", 1, 0, now()))
        APP.db.run("INSERT INTO game_records(chat_id, user_id, game_key, result, score, xp, created_at) VALUES(?,?,?,?,?,?,?)",
                   (g, 9514, "dice", "win", 1, 0, now()))
    for i in range(10):
        APP.db.run("INSERT INTO game_records(chat_id, user_id, game_key, result, score, xp, created_at) VALUES(?,?,?,?,?,?,?)",
                   (g, 9514, "trivia", "win", 1, 0, now()))
    check_achievements(g, 9514)
    for k in ("streak10", "gamer", "duelist", "old_member", "fastest", "lucky", "brain"):
        check(earned(9514, k), k)
    before = user_stat_get(g, 9514, "xp")
    check_achievements(g, 9514)
    check(user_stat_get(g, 9514, "xp") == before, "re-check gave XP again")
    return "ok"


@selftest("مأموریت‌ها: انتخاب روزانه، پیشرفت، پاداش یک‌باره، هفتگی، مخفی، خاموش")
def _t_p5_missions():
    g, u = -9521, 9521
    seed_missions()
    seed_achievements()
    kinds = [m[2] for m in MISSIONS]
    check(kinds.count("daily") >= 6 and kinds.count("weekly") == 3 and kinds.count("secret") == 2, "mission pool")
    act = active_missions(g)
    check(sum(1 for m, _ in act if m["kind"] == "daily") == DAILY_MISSION_COUNT, "daily count")
    check([m["mkey"] for m, _ in act] == [m["mkey"] for m, _ in active_missions(g)], "daily pick must be stable")
    m, period = next((m, p) for m, p in act if m["kind"] == "daily")
    xp0 = user_stat_get(g, u, "xp")
    mission_event(g, u, m["metric"], m["target"])
    row = APP.db.one("SELECT done, claimed FROM user_missions WHERE chat_id=? AND user_id=? AND mission_id=?", (g, u, m["id"]))
    check(row and row["done"] == 1 and row["claimed"] == 1, "daily not completed")
    check(user_stat_get(g, u, "xp") >= xp0 + m["reward_xp"], "mission reward missing")
    xp1 = user_stat_get(g, u, "xp")
    mission_event(g, u, m["metric"], m["target"])
    check(user_stat_get(g, u, "xp") == xp1 and user_stat_get(g, u, "missions_done") >= 1, "mission rewarded twice")
    weekly = [m for m, _ in act if m["kind"] == "weekly"]
    for w in weekly:
        mission_event(g, u, w["metric"], w["target"])
    check(APP.db.val("SELECT COUNT(*) FROM user_achievements WHERE chat_id=? AND user_id=? AND akey='weekly_hero'", (g, u)) == 1, "weekly_hero")
    mission_event(g, u, "streak3")
    check(APP.db.val("SELECT COUNT(*) FROM user_achievements WHERE chat_id=? AND user_id=? AND akey='detective'", (g, u)) == 1, "detective")
    STATS.flush()
    check(APP.db.val("SELECT svalue FROM group_stats WHERE chat_id=? AND skey='missions_done'", (g,)) >= 5, "missions_done counter")
    g2 = -9522
    upsert_group({"id": g2, "title": "off", "type": "group"})
    set_setting(g2, "missions", 0)
    mission_event(g2, u, "games_played", 99)
    check(APP.db.val("SELECT COUNT(*) FROM user_missions WHERE chat_id=?", (g2,)) == 0, "missions OFF still progress")
    return "ok"


@selftest("اتصال رویدادها به مأموریت: جوک، فال، دوئل، بازی")
def _t_p5_wiring():
    g, u = -9531, 9531
    seen = []
    orig = globals()["mission_event"]
    globals()["mission_event"] = lambda chat, user, metric, n=1, notes=None: seen.append(metric)
    try:
        seed_content()
        handle_update(_p2_cb("p5-w1", "jk:n:all", u, g, 1))
        jid = int(re.search(r"#(\d+)", _p3_last_text(APP.api, "edit_message_text", 2)).group(1))
        handle_update(_p2_cb("p5-w2", "jk:v:%d:1:all" % jid, u + 1, g, 1))
        handle_update(_p2_cb("p5-w3", "fa:c:love", u, g, 2))
        COOLDOWNS.clear()
        handle_update(_p2_cb("p5-w4", "gm:duel", u, g, 3))
        COOLDOWNS.clear()
        handle_update(_p2_cb("p5-w5", "gm:dice", u, g, 4))
        sid = _p4_sess(g, "dice", u)["sid"]
        handle_update(_p2_cb("p5-w6", "dc:" + sid, u, g, 4))
    finally:
        globals()["mission_event"] = orig
    for metric in ("joke_votes", "fortunes", "duels_sent", "games_played", "game:dice"):
        check(metric in seen, "metric %s never fired: %s" % (metric, seen))
    return "ok"


@selftest("لیگ هفتگی: رتبه، رتبه‌ی من، Reset هفتگی با حفظ رکورد")
def _t_p5_league():
    api, g = APP.api, -9541
    for k, n in ((9541, 3), (9542, 2), (9543, 1)):
        for i in range(n):
            record_result(g, k, "dice", "win", 1, 20, token="l%d-%d" % (k, i))
    check(league_rows(g, week_key())[0]["user_id"] == 9541, "order")
    check([r["points"] for r in league_rows(g, week_key())] == sorted([r["points"] for r in league_rows(g, week_key())], reverse=True), "sorted")
    handle_update(_p2_cb("p5-l1", "rk:lg", 9543, g, 1))
    text = _p3_last_text(api, "edit_message_text", 2)
    check("🥇" in text and "تا ریست" in text, "league view")
    for k in range(9550, 9562):
        APP.db.run("INSERT INTO league_scores(chat_id, week_key, user_id, points) VALUES(?,?,?,?)", (g, week_key(), k, 5))
    check("رتبه #" not in league_text(g, 9541), "top player should not see rank line")
    APP.db.run("UPDATE league_scores SET points=1 WHERE chat_id=? AND user_id=9543", (g,))
    handle_update(_p2_cb("p5-l2", "gm:league", 9543, g, 2))
    check("🏆 لیگ هفتگی" in _p3_last_text(api, "edit_message_text", 2), "league from games hub")
    g2, prev = -9542, week_key(now() - 8 * 86400)
    for u, p in ((9571, 500), (9572, 300), (9573, 100)):
        APP.db.run("INSERT INTO league_scores(chat_id, week_key, user_id, points) VALUES(?,?,?,?)", (g2, prev, u, p))
    APP.db.run("INSERT INTO league_scores(chat_id, week_key, user_id, points) VALUES(?,?,?,?)", (g2, week_key(), 9572, 40))
    _task_league_rollover()
    check(mem_get(g2, "league_done:" + prev) == "1" and mem_get(g2, "league_champ:" + prev) == "9571:500", "champion memory")
    rec = APP.db.one("SELECT user_id, value FROM records WHERE chat_id=? AND rkey='best_week_points'", (g2,))
    check(rec and rec["user_id"] == 9571 and rec["value"] == 500, "record")
    check(APP.db.val("SELECT COUNT(*) FROM user_achievements WHERE chat_id=? AND user_id=9571 AND akey='weekly_champ'", (g2,)) == 1, "weekly_champ medal")
    check(any("قهرمان هفته" in t for t in _p5_notes(api)), "announcement")
    n, xp = len(api.calls), user_stat_get(g2, 9571, "xp")
    _task_league_rollover()
    check(len(api.calls) == n and user_stat_get(g2, 9571, "xp") == xp, "rollover repeated")
    check(league_rows(g2, week_key())[0]["points"] == 40 and league_rows(g2, week_key())[0]["user_id"] == 9572, "champion bonus must not enter the new week")
    check(len(league_rows(g2, prev)) == 3, "history must stay")
    check(user_stat_get(g2, 9571, "xp") >= ACH["weekly_champ"][5], "champion still gets the XP")
    handle_update(_p2_cb("p5-l3", "rk:rec", 9572, g2, 3))
    check("500" in _p3_last_text(api, "edit_message_text", 2), "records view")
    return "ok"


@selftest("پروفایل، مدال‌ها، رتبه‌ها، منوی مأموریت")
def _t_p5_profile():
    api, g = APP.api, -9551
    seed_achievements()
    seed_missions()
    for i in range(2):
        record_result(g, 9551, "dice", "win", 1, 20, token="p%d" % i)
    record_result(g, 9552, "dice", "loss", 0, 3, token="q0")
    handle_update(_p2_msg("/profile", 9551, g, mid=1))
    text = _p3_last_text(api)
    for part in ("Level", "XP", "رتبه #1", "مدال‌ها", "بازی محبوب: تاس"):
        check(part in text, "profile misses %r: %s" % (part, text))
    check(("menu:stats" in json.dumps(_p2_calls(api, "send_message")[-1][1][2])) == ("stats" in SECTIONS), "stats button gating")
    upd = _p2_msg("/profile", 9553, g, mid=2)
    upd["message"]["reply_to_message"] = {"message_id": 1, "from": {"id": 9551, "first_name": "U9551"}}
    handle_update(upd)
    check("U9551" in _p3_last_text(api), "profile of replied user")
    handle_update(_p2_cb("p5-p1", "rk:md:9551", 9553, g, 3))
    text = _p3_last_text(api, "edit_message_text", 2)
    check("🏆 اولین برد" in text and "❓ مدال مخفی" in text and "🔒" in text, "medals page: " + text)
    handle_update(_p2_cb("p5-p2", "rk:xp", 9553, g, 4))
    check("U9551" in _p3_last_text(api, "edit_message_text", 2), "xp ranking")
    handle_update(_p2_cb("p5-p3", "menu:missions", 9551, g, 5))
    text = _p3_last_text(api, "edit_message_text", 2)
    check("📅 روزانه" in text and "🗓️ هفتگی" in text and "❓ یه مأموریت مخفی" in text, "missions page")
    check("شبگرد" not in text and "۳ برد پشت سر هم" not in text, "secret mission title leaked")
    mission_event(g, 9551, "streak3")
    handle_update(_p2_cb("p5-p4", "menu:missions", 9551, g, 6))
    check("۳ برد پشت سر هم" in _p3_last_text(api, "edit_message_text", 2), "completed secret should be revealed")
    keys = json.dumps(main_menu_markup())
    check("menu:ranks" in keys and "menu:missions" in keys, "main menu buttons")
    return "ok"


# ══════════════════════════════════════════════════════════════════
# ███ END OF PHASE 5


# ══════════════════════════════════════════════════════════════════
# ███ PHASE 6 — MEMORY / DNA / STATISTICS / LIGHTWEIGHT LEARNING
# ══════════════════════════════════════════════════════════════════

WEEKDAYS_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]  # به ترتیب weekday() پایتون
FEATURE_STAT_LABELS = {
    "games": "🎮 بازی‌ها", "joke": "😂 جوک", "fortune": "🔮 فال", "auto_reply": "💬 پاسخ خودکار",
    "missions": "🎯 مأموریت‌ها", "ranks": "🏆 رتبه‌ها", "lab": "🧪 آزمایشگاه",
}
DNA_HALF = {"humor": 60, "game": 40, "activity": 600, "compete": 30}  # نیمه‌ی اشباع هر بُعد (برای گروه تک‌نفره)
DNA_MIN_EVENTS = 10
MAX_OCCASIONS = 10
OCCASION_HOUR = 9
OCC_RE = re.compile(r"^(.{2,40}?)[\s،,:؛\-]+(\d{1,2})/(\d{1,2})$")


# ─── تاریخ شمسی (برای مناسبت‌های گروه) ───
def gregorian_to_jalali(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy, gy = 979, gy - 1600
    else:
        jy, gy = 0, gy - 621
    gy2 = gy + 1 if gm > 2 else gy
    days = 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 - 80 + gd + g_d_m[gm - 1]
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def jalali_today(ts=None):
    d = iran_dt(ts)
    return gregorian_to_jalali(d.year, d.month, d.day)


# ─── شمارنده‌های آماری گروه ───
def gget(chat_id, key, default=0):
    STATS.flush()
    return APP.db.val("SELECT svalue FROM group_stats WHERE chat_id=? AND skey=?", (int(chat_id), key), default)


def gprefix(chat_id, prefix):
    """{کلید بدون prefix: مقدار} برای همه‌ی شمارنده‌هایی که با prefix شروع می‌شوند."""
    STATS.flush()
    rows = APP.db.all("SELECT skey, svalue FROM group_stats WHERE chat_id=? AND substr(skey,1,?)=? AND svalue>0",
                      (int(chat_id), len(prefix), prefix))
    return {r["skey"][len(prefix):]: r["svalue"] for r in rows}


def top_of(d):
    return max(d.items(), key=lambda kv: (kv[1], kv[0])) if d else None


def peak_hour(chat_id):
    t = top_of(gprefix(chat_id, "hour:"))
    return (int(t[0]), t[1]) if t else None


def favorite_game(chat_id):
    plays = {k: v for k, v in gprefix(chat_id, "game:").items() if k in GAME_TITLES}
    t = top_of(plays)
    return (t[0], t[1]) if t else None


def favorite_joke_category(chat_id):
    t = top_of({k: v for k, v in gprefix(chat_id, "jokecat:").items() if k in JOKE_CAT_LABELS})
    return (t[0], t[1]) if t else None


def popular_features(chat_id, limit=3):
    feats = {k: v for k, v in gprefix(chat_id, "feat:").items() if k in FEATURE_STAT_LABELS}
    return sorted(feats.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


# ─── DNA گروه ───
def active_members(chat_id, days=30):
    return APP.db.val("SELECT COUNT(*) FROM user_stats WHERE chat_id=? AND last_active>=?", (int(chat_id), now() - days * 86400), 0)


def dna_scores(chat_id):
    """درصدها فقط از شمارنده‌های واقعی دیتابیس: pct = 100 × raw / (raw + K·√اعضای فعال)."""
    cid = int(chat_id)
    votes, shown = gget(cid, "joke_votes"), gget(cid, "joke_shown")
    fortunes = sum(gprefix(cid, "fortune:").values())
    humor = votes * 2 + shown + fortunes + gget(cid, "feat:auto_reply")
    game = gget(cid, "game_plays") * 2
    msgs = gget(cid, "total:msgs")
    duels = APP.db.val("SELECT COALESCE(SUM(duels_sent),0) FROM user_stats WHERE chat_id=?", (cid,), 0)
    compete = duels * 3 + gget(cid, "game:group_game") + gget(cid, "missions_done") * 2 + gget(cid, "game_wins")
    raw = {"humor": humor, "game": game, "activity": msgs, "compete": compete}
    scale = max(1, active_members(cid)) ** 0.5
    pct = {k: int(round(100.0 * v / (v + DNA_HALF[k] * scale))) if v > 0 else 0 for k, v in raw.items()}
    return {"raw": raw, "pct": pct, "enough": humor + game + compete + msgs >= DNA_MIN_EVENTS}


def bar(pct, width=10):
    full = int(round(width * pct / 100.0))
    return "█" * full + "░" * (width - full)


DNA_TYPES = {"humor": "گروه خنده‌دار 😂", "game": "گروه گیمر 🎮", "activity": "گروه پرحرف 💬", "compete": "گروه رقابتی 🏆"}


def dna_text(chat_id):
    cid = int(chat_id)
    title = APP.db.val("SELECT title FROM groups WHERE chat_id=?", (cid,), None) or "گروه"
    d = dna_scores(cid)
    if not d["enough"]:
        return "🧬 DNA گروه «%s»\n\nهنوز داده‌ی کافی ندارم 🧪\nچند تا بازی کنید، جوک بخونید و گپ بزنید؛ بعد DNA گروهتون ساخته می‌شه." % title
    p = d["pct"]
    lines = ["🧬 DNA گروه «%s»" % title, "",
             "😂 طنز     %s %d%%" % (bar(p["humor"]), p["humor"]),
             "🎮 بازی    %s %d%%" % (bar(p["game"]), p["game"]),
             "💬 فعالیت  %s %d%%" % (bar(p["activity"]), p["activity"]),
             "🏆 رقابت   %s %d%%" % (bar(p["compete"]), p["compete"]), ""]
    ph = peak_hour(cid)
    if ph:
        lines.append("🔥 ساعت اوج: %02d:00" % ph[0])
    fg = favorite_game(cid)
    if fg:
        lines.append("🎮 بازی محبوب: %s" % GAME_TITLES[fg[0]])
    fj = favorite_joke_category(cid)
    if fj:
        lines.append("😂 دسته جوک محبوب: %s" % JOKE_CAT_LABELS[fj[0]])
    lines += ["", "🧬 تیپ گروه: %s" % DNA_TYPES[max(p.items(), key=lambda kv: (kv[1], kv[0]))[0]]]
    return "\n".join(lines)


# ─── مناسبت‌های گروه ───
def occasions(chat_id):
    out = []
    for r in APP.db.all("SELECT mkey, mvalue FROM group_memory WHERE chat_id=? AND substr(mkey,1,4)='occ:' ORDER BY mkey", (int(chat_id),)):
        date, _, title = r["mvalue"].partition("|")
        out.append((r["mkey"], date, title))
    return out


def parse_occasion(text):
    """«تولد گروه 7/15» → (عنوان، ماه، روز) یا None."""
    m = OCC_RE.match(norm_digits(text.strip()))
    if not m:
        return None
    title, mo, da = m.group(1).strip(), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= da <= (31 if mo <= 6 else 30)):
        return None
    return title, mo, da


def norm_digits(text):
    return str(text).translate(_DIGITS)


def add_occasion(chat_id, title, mo, da):
    if len(occasions(chat_id)) >= MAX_OCCASIONS:
        return False
    key = "occ:" + hashlib.sha1(("%s|%d|%d" % (norm_text(title), mo, da)).encode()).hexdigest()[:6]
    mem_set(chat_id, key, "%02d/%02d|%s" % (mo, da, title))
    return True


def occasions_markup(ctx):
    rows = []
    if ctx.is_admin():
        for key, date, title in occasions(ctx.chat_id):
            rows.append([btn("🗑️ %s" % title[:24], "mm:od:" + key[4:])])
        rows.append([btn("➕ افزودن مناسبت", "mm:add")])
    rows.append([btn("↩️ حافظه", "menu:memory"), btn("🏠 برگشت", "menu:home")])
    return kb(*rows)


def occasions_text(chat_id):
    items = occasions(chat_id)
    lines = ["🎂 مناسبت‌های گروه", ""]
    if items:
        lines += ["• %s — %s (شمسی)" % (t, d) for _, d, t in items]
        lines += ["", "روز مناسبت، صفابات توی گروه تبریک می‌گه 🎉"]
    else:
        lines.append("هنوز مناسبتی ثبت نشده. ادمین می‌تونه تولد گروه، سالگرد و… رو اضافه کنه.")
    return "\n".join(lines)


@on_input("occasion")
def input_occasion(ctx, data):
    if not ctx.is_admin():
        input_clear(ctx.chat_id, ctx.user_id)
        return False
    parsed = parse_occasion(ctx.text)
    if parsed is None:
        ctx.reply("فرمتش درست نیست 🤔 مثل این بنویس:\nتولد گروه 7/15\n(ماه/روز شمسی) یا لغو کن.", kb([btn("❌ لغو", "mm:oc")]))
        return True
    input_clear(ctx.chat_id, ctx.user_id)
    title, mo, da = parsed
    if add_occasion(ctx.chat_id, title, mo, da):
        ctx.reply("✅ «%s» برای %02d/%02d ثبت شد 🎂" % (title, mo, da), kb([btn("🎂 مناسبت‌ها", "mm:oc")]))
    else:
        ctx.reply("به سقف %d مناسبت رسیدی؛ اول یکی رو حذف کن." % MAX_OCCASIONS, kb([btn("🎂 مناسبت‌ها", "mm:oc")]))
    return True


@callback("mm")
def cb_memory(ctx):
    a = ctx.parts[1] if len(ctx.parts) > 1 else "oc"
    if a == "oc":
        input_clear(ctx.chat_id, ctx.user_id)
        show(ctx, occasions_text(ctx.chat_id), occasions_markup(ctx))
        return
    if not ctx.is_group or not ctx.is_admin():
        ctx.answer("⛔ فقط ادمین‌های گروه", True)
        return
    if a == "add":
        input_set(ctx.chat_id, ctx.user_id, "occasion", {}, ttl=300)
        show(ctx, "🎂 مناسبت جدید رو بنویس:\n\nعنوان + ماه/روز شمسی\nمثال: تولد گروه 7/15", kb([btn("❌ لغو", "mm:oc")]))
    elif a == "od" and len(ctx.parts) > 2:
        mem_del(ctx.chat_id, "occ:" + ctx.parts[2])
        ctx.answer("🗑️ حذف شد")
        show(ctx, occasions_text(ctx.chat_id), occasions_markup(ctx))
    else:
        ctx.answer("دکمه نامعتبر", True)


@periodic(600, "occasions")
def _task_occasions():
    if hour_of() < OCCASION_HOUR:
        return
    today = "%02d/%02d" % jalali_today()[1:]
    day = day_key()
    chats = {r["chat_id"] for r in APP.db.all(
        "SELECT DISTINCT m.chat_id FROM group_memory m JOIN groups g ON g.chat_id=m.chat_id "
        "WHERE substr(m.mkey,1,4)='occ:' AND g.active=1")}
    for cid in chats:
        if mem_get(cid, "occ_done") == day:
            continue
        mem_set(cid, "occ_done", day)
        hits = [t for _, d, t in occasions(cid) if d == today]
        if hits:
            send_notes(cid, ["🎉 امروز %s! به همه تبریک می‌گم 🥳" % "، ".join("«%s»" % h for h in hits)])


# ─── حافظه‌ی گروه ───
def memory_lines(chat_id):
    cid = int(chat_id)
    STATS.flush()
    lines = []
    wk_games = APP.db.all("SELECT game_key, COUNT(*) c FROM game_records WHERE chat_id=? AND created_at>=? GROUP BY game_key ORDER BY c DESC, game_key LIMIT 2",
                          (cid, now() - 7 * 86400))
    if wk_games and (len(wk_games) == 1 or wk_games[0]["c"] > wk_games[1]["c"]):
        lines.append("این گروه این هفته بیشتر از هر چیزی «%s» بازی کرده." % GAME_TITLES.get(wk_games[0]["game_key"], wk_games[0]["game_key"]))
    days = [day_key(now() - i * 86400) for i in range(7)]
    per_day = {d: gget(cid, "day:%s:msgs" % d) for d in days}
    total = sum(per_day.values())
    if total:
        best = max(per_day.items(), key=lambda kv: (kv[1], kv[0]))
        wd = WEEKDAYS_FA[datetime.date.fromisoformat(best[0]).weekday()]
        lines.append("این هفته %d پیام ثبت شد؛ شلوغ‌ترین روز %s بود." % (total, wd))
    ph = peak_hour(cid)
    if ph:
        lines.append("ساعت اوج گروه %02d:00 ـه." % ph[0])
    fj = favorite_joke_category(cid)
    if fj:
        lines.append("جوک‌های دسته‌ی «%s» بیشتر از بقیه خندونده." % JOKE_CAT_LABELS[fj[0]])
    pf = popular_features(cid)
    if pf:
        lines.append("محبوب‌ترین قابلیت‌ها: " + "، ".join(FEATURE_STAT_LABELS[k] for k, _ in pf) + ".")
    plays, wins, missions = gget(cid, "game_plays"), gget(cid, "game_wins"), gget(cid, "missions_done")
    if plays or missions:
        lines.append("تا حالا %d بازی (%d برد) و %d مأموریت انجام شده." % (plays, wins, missions))
    act = APP.db.all("SELECT user_id FROM user_stats WHERE chat_id=? AND last_active>=? AND msgs_count>0 ORDER BY msgs_count DESC, user_id LIMIT 3",
                     (cid, now() - 7 * 86400))
    if act:
        lines.append("فعال‌ترین‌ها: " + "، ".join(uname(r["user_id"]) for r in act) + ".")
    oc = occasions(cid)
    if oc:
        lines.append("مناسبت‌ها: " + "، ".join("%s (%s)" % (t, d) for _, d, t in oc[:3]) + ".")
    if APP.db.val("SELECT rules_text FROM group_settings WHERE chat_id=?", (cid,), None):
        lines.append("قوانین گروه ثبت شده.")
    return lines


def memory_text(chat_id):
    lines = memory_lines(chat_id)
    if not lines:
        return "🧠 حافظه‌ی گروه\n\nهنوز چیزی یاد نگرفتم 😄 بیشتر باهام کار کنید تا آمار گروه رو یاد بگیرم.\n(من پیام‌های گروه رو ذخیره نمی‌کنم؛ فقط شمارنده‌های سبک)"
    return "🧠 حافظه‌ی گروه\n\n" + "\n".join("• " + l for l in lines) + "\n\n(فقط آمار سبک؛ متن پیام‌ها ذخیره نمی‌شه)"


@section("memory")
def sec_memory(ctx):
    show(ctx, memory_text(ctx.chat_id), kb([btn("🎂 مناسبت‌ها", "mm:oc"), btn("🧬 DNA گروه", "menu:dna")], home_row()))


@section("dna")
def sec_dna(ctx):
    show(ctx, dna_text(ctx.chat_id), kb([btn("🔄", "menu:dna"), btn("🧠 حافظه", "menu:memory")], home_row()))


# ─── آمار ───
def group_stats_text(chat_id):
    cid = int(chat_id)
    STATS.flush()
    title = APP.db.val("SELECT title FROM groups WHERE chat_id=?", (cid,), None) or "گروه"
    t0 = day_start()
    known = APP.db.val("SELECT COUNT(*) FROM user_stats WHERE chat_id=?", (cid,), 0)
    today = APP.db.val("SELECT COUNT(*) FROM user_stats WHERE chat_id=? AND last_active>=?", (cid, t0), 0)
    week = APP.db.val("SELECT COUNT(*) FROM user_stats WHERE chat_id=? AND last_active>=?", (cid, now() - 7 * 86400), 0)
    s = APP.db.one("SELECT COALESCE(SUM(games_played),0) p, COALESCE(SUM(games_won),0) w, COALESCE(SUM(games_lost),0) l, "
                   "COALESCE(SUM(draws),0) d FROM user_stats WHERE chat_id=?", (cid,))
    lines = ["📊 آمار گروه «%s»" % title, "",
             "👥 اعضای شناخته‌شده: %d • فعال امروز: %d • این هفته: %d" % (known, today, week),
             "💬 پیام‌ها: %d (امروز %d)" % (gget(cid, "total:msgs"), gget(cid, "day:%s:msgs" % day_key())),
             "🎮 بازی‌ها: %d • 🥇 برد: %d • ❌ باخت: %d • 🤝 مساوی: %d" % (s["p"], s["w"], s["l"], s["d"]),
             "😂 رأی‌های جوک: %d" % gget(cid, "joke_votes"),
             "🎯 مأموریت‌های انجام‌شده: %d" % gget(cid, "missions_done")]
    fg = favorite_game(cid)
    if fg:
        lines.append("🎮 بازی محبوب: %s (%d بار)" % (GAME_TITLES[fg[0]], fg[1]))
    ph = peak_hour(cid)
    if ph:
        lines.append("🔥 ساعت اوج: %02d:00" % ph[0])
    recs = []
    for key, label in RECORD_LABELS.items():
        r = APP.db.one("SELECT user_id, value FROM records WHERE chat_id=? AND rkey=?", (cid, key))
        if r:
            recs.append("%s: %s — %d" % (label, uname(r["user_id"]), r["value"]))
    if recs:
        lines += ["", "👑 رکوردها"] + recs
    return "\n".join(lines)


def user_stats_text(chat_id, user_id):
    cid, uid = int(chat_id), int(user_id)
    STATS.flush()
    s = APP.db.one("SELECT * FROM user_stats WHERE chat_id=? AND user_id=?", (cid, uid))
    name = uname(uid)
    if s is None:
        return "📊 آمار %s\n\nهنوز آماری ثبت نشده 🎮" % name
    rate = int(round(100.0 * s["games_won"] / s["games_played"])) if s["games_played"] else 0
    fav = APP.db.one("SELECT game_key, COUNT(*) c FROM game_records WHERE chat_id=? AND user_id=? GROUP BY game_key ORDER BY c DESC, game_key LIMIT 1", (cid, uid))
    lines = ["📊 آمار %s" % name, "",
             "⭐ Level %d • ⚡ %d XP" % (level_for_xp(s["xp"]), s["xp"]),
             "🎮 بازی‌ها: %d • 🥇 برد: %d • ❌ باخت: %d • 🤝 مساوی: %d" % (s["games_played"], s["games_won"], s["games_lost"], s["draws"]),
             "📈 نرخ برد: %d%%" % rate,
             "🔥 Streak: %d (رکورد %d)" % (s["streak"], s["best_streak"]),
             "😂 رأی‌های جوک: %d • ➕ جوک‌های ارسالی: %d" % (s["joke_votes"], s["jokes_sent"]),
             "🎯 مأموریت‌ها: %d" % s["missions_done"],
             "⚔️ دوئل: %d دعوت • %d برد" % (s["duels_sent"], s["duels_won"]),
             "💬 پیام‌ها: %d" % s["msgs_count"]]
    if fav:
        lines.append("🎮 بازی محبوب: %s" % GAME_TITLES.get(fav["game_key"], fav["game_key"]))
    return "\n".join(lines)


def stats_markup(active):
    return kb([btn("👥 آمار گروه", "sx:grp"), btn("👤 آمار من", "sx:me")],
              [btn("🧬 DNA", "menu:dna"), btn("🏆 رتبه‌ها", "menu:ranks")], home_row())


@section("stats")
def sec_stats(ctx):
    show(ctx, group_stats_text(ctx.chat_id) if ctx.is_group else user_stats_text(ctx.chat_id, ctx.user_id), stats_markup("grp"))


@callback("sx")
def cb_stats(ctx):
    a = ctx.parts[1] if len(ctx.parts) > 1 else "grp"
    try:
        target = int(ctx.parts[2]) if len(ctx.parts) > 2 else int(ctx.user_id)
    except ValueError:
        ctx.answer("دکمه نامعتبر", True)
        return
    if a == "me":
        show(ctx, user_stats_text(ctx.chat_id, target), stats_markup("me"))
    elif a == "grp":
        show(ctx, group_stats_text(ctx.chat_id), stats_markup("grp"))
    else:
        ctx.answer("دکمه نامعتبر", True)


# ─── یادگیری سبک: انتخاب‌ها با آمار خود گروه سازگار می‌شوند ───
def learned_pick(chat_id, items, weights, salt=""):
    """انتخاب وزنی که در طول یک روز برای هر گروه ثابت است (وزن‌ها از شمارنده‌های همان گروه)."""
    h = int(hashlib.sha1(("%s:%s:%s" % (day_key(), chat_id, salt)).encode("utf-8")).hexdigest()[:12], 16) / float(16 ** 12)
    point = h * sum(weights)
    acc = 0.0
    for item, w in zip(items, weights):
        acc += w
        if point < acc:
            return item
    return items[-1]


def featured_game(chat_id):
    plays = gprefix(chat_id, "game:")
    return learned_pick(chat_id, GAME_CATALOG, [1 + plays.get(k, 0) for k, _, _ in GAME_CATALOG], "game")


def featured_fun(chat_id):
    jc, fc = gprefix(chat_id, "jokecat:"), gprefix(chat_id, "fortune:")
    items = [("جوک " + t) for _, _, t in JOKE_CATEGORIES] + [("فال " + t) for _, _, t in FORTUNE_CATEGORIES]
    weights = [1 + jc.get(k, 0) for k, _, _ in JOKE_CATEGORIES] + [1 + fc.get(k, 0) for k, _, _ in FORTUNE_CATEGORIES]
    return learned_pick(chat_id, items, weights, "fun")


def _joke_affinity(chat_id):
    """سهم هر دسته از خنده‌های این گروه (۰ تا ۱)؛ جوک‌های دسته‌ی محبوب کمی بیشتر نمایش داده می‌شوند."""
    cats = {k: v for k, v in gprefix(chat_id, "jokecat:").items() if k in JOKE_CAT_LABELS and v > 0}
    total = sum(cats.values())
    return {k: v / total for k, v in cats.items()} if total else {}


# ─── SELFTEST فاز ۶ ───
def _p6_set(chat, **kv):
    for k, v in kv.items():
        APP.db.run("INSERT INTO group_stats(chat_id, skey, svalue) VALUES(?,?,?) ON CONFLICT(chat_id, skey) DO UPDATE SET svalue=excluded.svalue",
                   (chat, k.replace("__", ":"), v))


@selftest("تاریخ شمسی: تبدیل میلادی به شمسی")
def _t_p6_jalali():
    cases = {(2026, 3, 21): (1405, 1, 1), (2026, 9, 23): (1405, 7, 1), (2026, 10, 4): (1405, 7, 12),
             (2024, 3, 20): (1403, 1, 1), (2023, 3, 21): (1402, 1, 1), (2025, 3, 20): (1403, 12, 30),
             (2000, 1, 1): (1378, 10, 11)}
    for g, j in cases.items():
        check(gregorian_to_jalali(*g) == j, "%s → %s (expected %s)" % (g, gregorian_to_jalali(*g), j))
    return "ok"


@selftest("DNA: درصد از آمار واقعی، اشباع، داده‌ی ناکافی")
def _t_p6_dna():
    g = -9601
    upsert_group({"id": g, "title": "D", "type": "group"})
    check("داده‌ی کافی ندارم" in dna_text(g) and not dna_scores(g)["enough"], "empty group")
    STATS.user_touch(g, 1)
    STATS.flush()
    _p6_set(g, joke_votes=30, joke_shown=40, game_plays=20, total__msgs=300, game_wins=8, game__word_guess=12, game__dice=8,
            jokecat__school=9, jokecat__daily=3, hour__21=50, hour__10=5)
    d = dna_scores(g)
    check(d["enough"] and all(0 <= v <= 100 for v in d["pct"].values()), "range")
    scale = 1.0
    check(d["raw"]["humor"] == 30 * 2 + 40 and d["pct"]["humor"] == int(round(100 * 100 / (100 + 60 * scale))), "humor formula %s" % d)
    _p6_set(g, joke_votes=3000)
    check(dna_scores(g)["pct"]["humor"] > d["pct"]["humor"] and dna_scores(g)["pct"]["humor"] < 100, "saturation")
    text = dna_text(g)
    for part in ("😂 طنز", "🎮 بازی", "💬 فعالیت", "🏆 رقابت", "ساعت اوج: 21:00", "بازی محبوب: حدس کلمه", "دسته جوک محبوب: مدرسه", "تیپ گروه"):
        check(part in text, "DNA text misses %r\n%s" % (part, text))
    check(bar(0) == "░" * 10 and bar(100) == "█" * 10 and bar(50) == "█" * 5 + "░" * 5, "bar")
    return "ok"


@selftest("حافظه‌ی گروه: جمله‌های آماری، بدون ذخیره‌ی پیام")
def _t_p6_memory():
    g = -9611
    upsert_group({"id": g, "title": "M", "type": "group"})
    check("هنوز چیزی یاد نگرفتم" in memory_text(g), "empty memory")
    upsert_user({"id": 9611, "first_name": "U9611"})  # در عمل هر کاربری با اولین پیامش ثبت می‌شود
    for i in range(3):
        record_result(g, 9611, "word_guess", "win", 1, 10, token="mw%d" % i)
    record_result(g, 9611, "dice", "win", 1, 10, token="md")
    today = day_key()
    _p6_set(g, **{"day__%s__msgs" % today: 42, "hour__21": 30, "jokecat__school": 5, "feat__games": 9, "feat__joke": 4, "game_plays": 4, "game_wins": 4})
    STATS.user_add(g, 9611, "msgs_count", 7)
    STATS.user_touch(g, 9611)
    add_occasion(g, "تولد گروه", 7, 15)
    text = memory_text(g)
    for part in ("این هفته بیشتر از هر چیزی «حدس کلمه» بازی کرده", "42 پیام", "21:00", "«مدرسه»", "🎮 بازی‌ها، 😂 جوک",
                 "فعال‌ترین‌ها: U", "تولد گروه (07/15)"):
        check(part in text, "memory misses %r\n%s" % (part, text))
    check(APP.db.val("SELECT COUNT(*) FROM group_memory WHERE chat_id=? AND mvalue LIKE '%سلام%'", (g,)) == 0, "message text stored")
    return "ok"


@selftest("آمار: کاربر و گروه مطابق دیتابیس")
def _t_p6_stats():
    api, g = APP.api, -9621
    upsert_group({"id": g, "title": "S", "type": "group"})
    record_result(g, 9621, "dice", "win", 1, 20, token="s1")
    record_result(g, 9621, "dice", "loss", 0, 3, token="s2")
    record_result(g, 9622, "dice", "draw", 0, 8, token="s3")
    STATS.group_add(g, "joke_votes", 4)
    STATS.group_add(g, "total:msgs", 10)
    STATS.group_add(g, "day:%s:msgs" % day_key(), 6)
    STATS.group_add(g, "hour:20", 3)
    text = group_stats_text(g)
    for part in ("بازی‌ها: 3", "برد: 1", "باخت: 1", "مساوی: 1", "رأی‌های جوک: 4", "پیام‌ها: 10 (امروز 6)", "ساعت اوج: 20:00", "بازی محبوب: تاس"):
        check(part in text, "group stats misses %r\n%s" % (part, text))
    ut = user_stats_text(g, 9621)
    check("بازی‌ها: 2" in ut and "نرخ برد: 50%" in ut and "برد: 1" in ut, "user stats\n" + ut)
    check("هنوز آماری ثبت نشده" in user_stats_text(g, 9999), "unknown user")
    handle_update(_p2_cb("p6-s1", "menu:stats", 9621, g, 1))
    check("آمار گروه" in _p3_last_text(api, "edit_message_text", 2), "stats section")
    handle_update(_p2_cb("p6-s2", "sx:me", 9621, g, 2))
    check("آمار" in _p3_last_text(api, "edit_message_text", 2) and "نرخ برد" in _p3_last_text(api, "edit_message_text", 2), "my stats")
    handle_update(_p2_msg("/profile", 9621, g, mid=3))
    check("menu:stats" in json.dumps(_p2_calls(api, "send_message")[-1][1][2]), "profile → full stats button")
    return "ok"


@selftest("مناسبت‌ها: افزودن فقط توسط ادمین، فرمت، سقف، حذف، تبریک روزانه")
def _t_p6_occasions():
    api, g = APP.api, -9631
    adm, usr = 9631, 9632
    upsert_group({"id": g, "title": "O", "type": "group"})
    ADMIN_CACHE[(g, adm)] = (True, time.monotonic() + 300)
    ADMIN_CACHE[(g, usr)] = (False, time.monotonic() + 300)
    check(parse_occasion("تولد گروه ۷/۱۵") == ("تولد گروه", 7, 15) and parse_occasion("x 13/1") is None and parse_occasion("سالگرد 7/31") is None
          and parse_occasion("فقط متن") is None and parse_occasion("عنوان 12/30") == ("عنوان", 12, 30), "parse")
    handle_update(_p2_cb("p6-o1", "mm:add", usr, g, 1))
    check(input_get(g, usr) is None, "non-admin started adding")
    handle_update(_p2_cb("p6-o2", "mm:add", adm, g, 2))
    check(input_get(g, adm)["kind"] == "input:occasion", "admin input not set")
    handle_update(_p2_msg("بد فرمت", adm, g, mid=3))
    check(input_get(g, adm) is not None and not occasions(g), "bad format must keep waiting")
    jm, jd = jalali_today()[1:]
    handle_update(_p2_msg("جشن تست %d/%d" % (jm, jd), adm, g, mid=4))
    check(len(occasions(g)) == 1 and input_get(g, adm) is None, "occasion not saved")
    old_hour = globals()["hour_of"]
    globals()["hour_of"] = lambda ts=None: 3
    try:
        _task_occasions()
        check(not any("جشن تست" in t for t in _p5_notes(api)), "announced before %d:00" % OCCASION_HOUR)
        globals()["hour_of"] = lambda ts=None: 10
        _task_occasions()
        check(sum("جشن تست" in t for t in _p5_notes(api)) == 1, "announcement missing")
        _task_occasions()
        check(sum("جشن تست" in t for t in _p5_notes(api)) == 1, "announced twice in a day")
    finally:
        globals()["hour_of"] = old_hour
    key = occasions(g)[0][0][4:]
    handle_update(_p2_cb("p6-o3", "mm:od:" + key, usr, g, 5))
    check(len(occasions(g)) == 1, "non-admin deleted")
    handle_update(_p2_cb("p6-o4", "mm:od:" + key, adm, g, 6))
    check(not occasions(g), "admin delete failed")
    for i in range(MAX_OCCASIONS):
        check(add_occasion(g, "m%d" % i, 1, i + 1), "add %d" % i)
    check(not add_occasion(g, "extra", 2, 2), "limit")
    return "ok"


@selftest("یادگیری سبک: انتخاب روزانه وزنی و پایدار، علاقه‌ی جوک")
def _t_p6_learning():
    g = -9641
    items = ["a", "b", "c"]
    check(learned_pick(g, items, [0, 0, 1000], "x") == "c" and learned_pick(g, items, [1000, 0, 0], "x") == "a", "weights")
    check(learned_pick(g, items, [1, 1, 1], "x") == learned_pick(g, items, [1, 1, 1], "x"), "daily stable")
    _p6_set(g, game__rps=500)
    picks = {featured_game(g)[0] for _ in range(5)}
    check(picks == {featured_game(g)[0]} and len(picks) == 1, "featured game must be stable within a day")
    hits = sum(1 for n in range(40) if learned_pick("chat%d" % n, [k for k, _, _ in GAME_CATALOG], [1 + (500 if k == "rps" else 0) for k, _, _ in GAME_CATALOG], "g") == "rps")
    check(hits >= 30, "learned preference barely visible: %d/40" % hits)
    check(featured_fun(g) in ["جوک " + t for _, _, t in JOKE_CATEGORIES] + ["فال " + t for _, _, t in FORTUNE_CATEGORIES], "featured fun")
    check(_joke_affinity(g) == {}, "no data → no affinity")
    _p6_set(g, jokecat__school=30, jokecat__gaming=10)
    aff = _joke_affinity(g)
    check(abs(aff["school"] - 0.75) < 1e-9 and abs(aff["gaming"] - 0.25) < 1e-9, "affinity %s" % aff)
    seed_content()
    captured = {}
    orig = random.choices
    random.choices = lambda pool, weights=None, **k: (captured.update(pool=pool, weights=weights) or [pool[0]])
    try:
        pick_joke(g, "all")
    finally:
        random.choices = orig
    cats = {r["id"]: r["category"] for r in APP.db.all("SELECT id, category FROM jokes")}
    w_school = [w for r, w in zip(captured["pool"], captured["weights"]) if cats[r["id"]] == "school"]
    w_other = [w for r, w in zip(captured["pool"], captured["weights"]) if cats[r["id"]] == "family"]
    check(w_school and w_other and min(w_school) > max(w_other) * 1.5, "affinity not applied to weights")
    check(pick_joke(-9642, "school") is not None, "specific category still works")
    return "ok"


@selftest("منوی اصلی: حافظه، DNA، آمار وصل‌اند")
def _t_p6_menu():
    api, g = APP.api, -9651
    upsert_group({"id": g, "title": "N", "type": "group"})
    keys = json.dumps(main_menu_markup())
    for k in ("menu:memory", "menu:dna", "menu:stats"):
        check(k in keys, k + " missing from main menu")
    for k in ("memory", "dna", "stats"):
        n = len(_p2_calls(api, "edit_message_text"))
        handle_update(_p2_cb("p6-m-" + k, "menu:" + k, 9651, g, 1 + n))
        check(len(_p2_calls(api, "edit_message_text")) == n + 1, k + " screen did not render")
    handle_update(_p2_msg("/start", 9651, g, mid=30))
    check("بازی امروز" in _p3_last_text(api), "home still renders with learned picks")
    return "ok"


# ══════════════════════════════════════════════════════════════════
# ███ END OF PHASE 6


# ══════════════════════════════════════════════════════════════════
# ███ PHASE 7 — MODERATION / REPORTS / WARNINGS / FILTERS / ADMIN PANEL
# ══════════════════════════════════════════════════════════════════

MOD_TOGGLES = [("anti_spam", "🛡️", "ضد اسپم"), ("anti_flood", "🌊", "ضد فلود"), ("anti_link", "🔗", "ضد لینک"),
               ("filter_enabled", "🔤", "فیلتر کلمات"), ("auto_mod", "🤖", "اتومود")]
MOD_KEYS = {k for k, _, _ in MOD_TOGGLES}
MOD_NAMES = {k: n for k, _, n in MOD_TOGGLES}
LEVEL_NAMES = {1: "ملایم", 2: "متوسط", 3: "سخت"}
FLOOD_LIMITS = {1: (8, 10), 2: (6, 10), 3: (4, 10)}      # (تعداد پیام، ثانیه)
SPAM_REPEAT = {1: 4, 2: 3, 3: 2}                          # چند پیام یکسان در ۶۰ ثانیه
MUTE_MINUTES_BY_LEVEL = {1: 60, 2: 360, 3: 1440}
WARN_WINDOW_DAYS = 30
MAX_FILTER_WORDS = 100
MAX_MUTE_MINUTES = 30 * 1440
CLEAN_MAX = 100
NOTICE_COOLDOWN = 30
REASONS = {"filter": "کلمه‌ی ممنوعه", "link": "ارسال لینک", "flood": "ارسال پشت سر هم", "spam": "تکرار پیام"}
REPORT_REASONS = [("spam", "اسپم"), ("ads", "تبلیغات"), ("insult", "توهین"), ("inappropriate", "محتوای نامناسب"), ("other", "سایر")]
REPORT_HOURLY_LIMIT = 5
DEFAULT_WELCOME = "👋 خوش اومدی {name} امیدواریم از گروه جان سالم به در ببری 😂"
DEFAULT_GOODBYE = "👋 {name} از گروه رفت. دلمون براش تنگ می‌شه... شاید 😂"
NO_RULES_TEXT = "هنوز قانونی برای این گروه ثبت نشده. ادمین می‌تونه از 🛡️ مدیریت ← 📜 قوانین ثبتش کنه."
LINK_RE = re.compile(r"(https?://|www\.|\b(?:t|ble|telegram|wa|instagram)\.(?:me|ir|ly|com)/|"
                     r"\b[a-z0-9-]+\.(?:com|ir|org|net|me|io|ly|xyz|info|co|link|site|online)\b)", re.I)
SPAM_HIST = {}
FILTER_CACHE = {}
RESTRICT_API = {"ok": None}   # None = هنوز امتحان نشده، False = بله این متد را ندارد


def pre_message(fn):
    PRE_HOOKS.append(fn)
    return fn


# ─── وضعیت محدودیت‌ها ───
def get_restriction(chat_id, user_id):
    return APP.db.one(
        "SELECT kind, until FROM restrictions WHERE chat_id=? AND user_id=? AND (until=0 OR until>?) "
        "ORDER BY CASE kind WHEN 'ban' THEN 0 ELSE 1 END LIMIT 1", (int(chat_id), int(user_id), now()))


def count_active_warnings(chat_id, user_id):
    return APP.db.val("SELECT COUNT(*) FROM warnings WHERE chat_id=? AND user_id=? AND created_at>?",
                      (int(chat_id), int(user_id), now() - WARN_WINDOW_DAYS * 86400), 0)


def _try_restrict(chat_id, user_id, muted):
    """بهترین تلاش برای اعمال محدودیت روی خود بله (اگر آن متد وجود داشته باشد). اجرای اصلی با حذف پیام است."""
    if RESTRICT_API["ok"] is False:
        return False
    try:
        APP.api.call("restrictChatMember", {"chat_id": chat_id, "user_id": user_id,
                                            "permissions": {"can_send_messages": not muted}}, retries=1)
        RESTRICT_API["ok"] = True
        return True
    except BaleAPIError as e:
        LOG.warning("restrictChatMember: %s", redact(e))
        if e.code == 404:
            RESTRICT_API["ok"] = False
        return False


def mute_user(chat_id, user_id, minutes, by=0, reason=""):
    cid, uid = int(chat_id), int(user_id)
    minutes = max(0, min(int(minutes), MAX_MUTE_MINUTES))
    until = now() + minutes * 60 if minutes else 0
    APP.db.run("""INSERT INTO restrictions(chat_id, user_id, kind, until, reason, by_id, created_at) VALUES(?,?,'mute',?,?,?,?)
                  ON CONFLICT(chat_id, user_id, kind) DO UPDATE SET until=excluded.until, reason=excluded.reason,
                  by_id=excluded.by_id, created_at=excluded.created_at""", (cid, uid, until, safe_text(reason, 200), int(by), now()))
    _try_restrict(cid, uid, True)
    log_action(cid, "mute", by, uid, ("%d دقیقه" % minutes) if minutes else "دائم")
    STATS.group_add(cid, "mod:mute")
    return until


def unmute_user(chat_id, user_id, by=0):
    cid, uid = int(chat_id), int(user_id)
    had = APP.db.run("DELETE FROM restrictions WHERE chat_id=? AND user_id=? AND kind='mute'", (cid, uid)).rowcount > 0
    if had:
        _try_restrict(cid, uid, False)
        log_action(cid, "unmute", by, uid, "")
    return had


def ban_user(chat_id, user_id, by=0, reason=""):
    """(True اگر خود بله هم بن را اعمال کرد). ثبت داخلی همیشه انجام می‌شود و با حذف پیام‌ها اجرا می‌شود."""
    cid, uid = int(chat_id), int(user_id)
    APP.db.run("""INSERT INTO restrictions(chat_id, user_id, kind, until, reason, by_id, created_at) VALUES(?,?,'ban',0,?,?,?)
                  ON CONFLICT(chat_id, user_id, kind) DO UPDATE SET reason=excluded.reason, by_id=excluded.by_id, created_at=excluded.created_at""",
               (cid, uid, safe_text(reason, 200), int(by), now()))
    applied = True
    try:
        APP.api.ban_chat_member(cid, uid)
    except BaleAPIError as e:
        applied = False
        LOG.warning("banChatMember: %s", redact(e))
    log_action(cid, "ban", by, uid, reason)
    STATS.group_add(cid, "mod:ban")
    return applied


def unban_user(chat_id, user_id, by=0):
    cid, uid = int(chat_id), int(user_id)
    had = APP.db.run("DELETE FROM restrictions WHERE chat_id=? AND user_id=? AND kind='ban'", (cid, uid)).rowcount > 0
    try:
        APP.api.unban_chat_member(cid, uid)
    except BaleAPIError as e:
        LOG.warning("unbanChatMember: %s", redact(e))
    if had:
        log_action(cid, "unban", by, uid, "")
    return had


def add_warning(chat_id, user_id, reason, by=0, s=None):
    """(تعداد اخطار فعال، دقیقه‌ی سکوت خودکار یا ۰)."""
    cid, uid = int(chat_id), int(user_id)
    s = s or get_settings(cid)
    APP.db.run("INSERT INTO warnings(chat_id, user_id, reason, by_id, created_at) VALUES(?,?,?,?,?)",
               (cid, uid, safe_text(reason, 200), int(by), now()))
    log_action(cid, "warn", by, uid, reason)
    STATS.group_add(cid, "mod:warn")
    n = count_active_warnings(cid, uid)
    if n >= s["warn_limit"]:
        minutes = MUTE_MINUTES_BY_LEVEL.get(s["mod_level"], 360)
        mute_user(cid, uid, minutes, by, "اخطار بیش از حد")
        APP.db.run("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (cid, uid))
        return n, minutes
    return n, 0


# ─── تشخیص تخلف ───
def filter_words(chat_id):
    cid = int(chat_id)
    if cid not in FILTER_CACHE:
        FILTER_CACHE[cid] = {r["word"] for r in APP.db.all("SELECT word FROM filter_words WHERE chat_id=?", (cid,))}
    return FILTER_CACHE[cid]


def filter_hit(chat_id, text):
    words = filter_words(chat_id)
    if not words:
        return False
    padded = " %s " % norm_text(text)
    return any((" %s " % w) in padded for w in words)


def spam_hit(chat_id, user_id, text, level):
    n = norm_text(text)
    if len(n) < 4:  # پیام‌های خیلی کوتاه/عددی (مثل حدس عدد) اسپم حساب نمی‌شوند
        return False
    h = hashlib.sha1(n.encode("utf-8")).hexdigest()[:10]
    t, key = time.monotonic(), (int(chat_id), int(user_id))
    hist = [(ts, hh) for ts, hh in SPAM_HIST.get(key, []) if t - ts <= 60]
    hist.append((t, h))
    SPAM_HIST[key] = hist[-8:]
    return sum(1 for _, hh in hist if hh == h) >= SPAM_REPEAT.get(level, 3)


def detect_violation(ctx, s):
    cid, uid, lvl = int(ctx.chat_id), int(ctx.user_id), s["mod_level"]
    if s["filter_enabled"] and filter_hit(cid, ctx.text):
        return "filter"
    if s["anti_link"] and LINK_RE.search(ctx.text):
        return "link"
    if s["anti_flood"]:
        count, window = FLOOD_LIMITS.get(lvl, FLOOD_LIMITS[2])
        if not RATE.allow(("flood", cid, uid), count, window):
            return "flood"
    if s["anti_spam"] and spam_hit(cid, uid, ctx.text, lvl):
        return "spam"
    return None


def notice(chat_id, user_id, text):
    if cooldown_ok(("mnote", int(chat_id), int(user_id)), NOTICE_COOLDOWN):
        APP.api.try_call("sendMessage", {"chat_id": chat_id, "text": safe_text(text)}, retries=1)


def punish(ctx, code, s):
    cid, uid = int(ctx.chat_id), int(ctx.user_id)
    ctx.delete()
    STATS.group_add(cid, "mod:" + code)
    log_action(cid, "mod_" + code, 0, uid, "")
    line = "🚫 %s: %s — پیامت پاک شد." % (mention(ctx.user), REASONS[code])
    if s["auto_mod"] and s["mod_level"] >= 2:
        n, minutes = add_warning(cid, uid, REASONS[code], 0, s)
        line += (" ⛔ به‌خاطر اخطارهای زیاد %d دقیقه سکوت شدی." % minutes) if minutes else (" ⚠️ اخطار %d از %d" % (n, s["warn_limit"]))
    notice(cid, uid, line)


@pre_message
def hook_moderation(ctx):
    if not ctx.is_group or ctx.user_id is None:
        return False
    cid, uid = int(ctx.chat_id), int(ctx.user_id)
    r = get_restriction(cid, uid)
    if r is not None:
        if is_group_admin(cid, uid):
            return False
        ctx.delete()
        if r["kind"] == "ban" and cooldown_ok(("reban", cid, uid), 60):
            try:
                APP.api.ban_chat_member(cid, uid)
            except BaleAPIError as e:
                LOG.warning("re-ban failed: %s", redact(e))
        return True
    if not ctx.text or ctx.text.startswith("/"):
        return False
    s = get_settings(cid)
    code = detect_violation(ctx, s)
    if code is None or is_group_admin(cid, uid):
        return False
    punish(ctx, code, s)
    return True


@periodic(60, "mod_sweeper")
def _task_mod_sweeper():
    for r in APP.db.all("SELECT chat_id, user_id FROM restrictions WHERE kind='mute' AND until>0 AND until<=?", (now(),)):
        APP.db.run("DELETE FROM restrictions WHERE chat_id=? AND user_id=? AND kind='mute'", (r["chat_id"], r["user_id"]))
        _try_restrict(r["chat_id"], r["user_id"], False)
        log_action(r["chat_id"], "unmute", 0, r["user_id"], "پایان مدت")
    t = time.monotonic()
    for k in [k for k, h in SPAM_HIST.items() if not h or t - h[-1][0] > 60]:
        del SPAM_HIST[k]


# ─── دستورهای مدیریتی ───
def parse_duration(token):
    """«30» دقیقه، «2h» ساعت، «1d» روز، «0» یا «دائم» = دائمی. None یعنی نامعتبر."""
    t = norm_digits(str(token or "")).strip().lower()
    if t in ("", ):
        return 60
    if t in ("0", "دائم", "دایم", "perm"):
        return 0
    m = re.match(r"^(\d{1,9})\s*(m|h|d|دقیقه|ساعت|روز)?$", t)
    if not m:
        return None
    n = int(m.group(1))
    mult = {"h": 60, "ساعت": 60, "d": 1440, "روز": 1440}.get(m.group(2) or "m", 1)
    return min(n * mult, MAX_MUTE_MINUTES)


def resolve_target(ctx):
    """(user_id یا None، بقیه‌ی آرگومان‌ها). از ریپلای، @username یا شناسه‌ی عددی."""
    tokens = ctx.args.split()
    rt = ctx.reply_to
    if rt and (rt.get("from") or {}).get("id") and not rt["from"].get("is_bot"):
        upsert_user(rt["from"])
        return int(rt["from"]["id"]), tokens
    if tokens:
        t = tokens[0]
        if t.startswith("@"):
            row = APP.db.one("SELECT user_id FROM users WHERE lower(username)=?", (t[1:].lower(),))
            return (row["user_id"] if row else None), tokens[1:]
        if norm_digits(t).isdigit():
            return int(norm_digits(t)), tokens[1:]
    return None, tokens


def protected_reason(chat_id, actor_id, target_id):
    if int(target_id) == int(actor_id):
        return "خودت رو که نمی‌شه 😅"
    if APP.bot_id and int(target_id) == APP.bot_id:
        return "منو؟ 😂"
    if is_bot_admin(target_id) or is_group_admin(chat_id, target_id):
        return "ادمین‌ها محافظت‌شده‌ان 🛡️"
    return None


def _target_or_reply(ctx):
    uid, rest = resolve_target(ctx)
    if uid is None:
        ctx.reply("روی پیام کاربر ریپلای کن یا @یوزرنیمش رو بنویس 🤔")
        return None, rest
    why = protected_reason(ctx.chat_id, ctx.user_id, uid)
    if why:
        ctx.reply(why)
        return None, rest
    return uid, rest


@command("warn", admin="group", private=False)
def cmd_warn(ctx):
    uid, rest = _target_or_reply(ctx)
    if uid is None:
        return
    reason = " ".join(rest) or "اخطار دستی"
    n, minutes = add_warning(ctx.chat_id, uid, reason, ctx.user_id)
    ctx.reply("⚠️ %s اخطار گرفت (%d از %d)%s" % (mention(get_user(uid)), n, get_settings(ctx.chat_id)["warn_limit"],
                                                  (" و %d دقیقه سکوت شد ⛔" % minutes) if minutes else ""))


@command("mute", admin="group", private=False)
def cmd_mute(ctx):
    uid, rest = _target_or_reply(ctx)
    if uid is None:
        return
    minutes = parse_duration(rest[0] if rest else "")
    if minutes is None:
        ctx.reply("مدت نامعتبره. مثال: /mute 30  یا  /mute 2h  یا  /mute 1d  یا  /mute 0 (دائم)")
        return
    mute_user(ctx.chat_id, uid, minutes, ctx.user_id, "سکوت دستی")
    ctx.reply("🔇 %s %s سکوت شد." % (mention(get_user(uid)), ("%d دقیقه" % minutes) if minutes else "برای همیشه"))


@command("unmute", admin="group", private=False)
def cmd_unmute(ctx):
    uid, _ = resolve_target(ctx)
    if uid is None:
        ctx.reply("روی پیام کاربر ریپلای کن یا @یوزرنیمش رو بنویس 🤔")
        return
    ctx.reply("🔊 سکوت %s برداشته شد." % mention(get_user(uid)) if unmute_user(ctx.chat_id, uid, ctx.user_id) else "این کاربر ساکت نبود 🤷")


@command("ban", admin="group", private=False)
def cmd_ban(ctx):
    uid, rest = _target_or_reply(ctx)
    if uid is None:
        return
    applied = ban_user(ctx.chat_id, uid, ctx.user_id, " ".join(rest) or "بن دستی")
    ctx.reply("🚫 %s بن شد.%s" % (mention(get_user(uid)), "" if applied else "\n⚠️ بله بن رو اعمال نکرد (ربات باید ادمین با دسترسی بن باشه)؛ ولی پیام‌های این کاربر حذف می‌شن."))


@command("unban", admin="group", private=False)
def cmd_unban(ctx):
    uid, _ = resolve_target(ctx)
    if uid is None:
        ctx.reply("روی پیام کاربر ریپلای کن یا @یوزرنیمش رو بنویس 🤔")
        return
    ctx.reply("♻️ بن %s برداشته شد." % mention(get_user(uid)) if unban_user(ctx.chat_id, uid, ctx.user_id) else "این کاربر بن نبود 🤷")


def delete_range(chat_id, from_id, count):
    """پیام‌های قبل از from_id را (از جدید به قدیم) حذف می‌کند؛ تعداد حذف‌های موفق."""
    ok = 0
    for mid in range(int(from_id) - 1, int(from_id) - 1 - max(0, int(count)), -1):
        if mid <= 0:
            break
        if APP.api.delete_message(chat_id, mid) is not None:
            ok += 1
    return ok


@command("clean", admin="group", private=False)
def cmd_clean(ctx):
    rt = ctx.reply_to
    if rt and rt.get("message_id") and ctx.message_id:
        count = min(CLEAN_MAX, int(ctx.message_id) - int(rt["message_id"]))
    else:
        arg = norm_digits(ctx.args.strip())
        count = min(CLEAN_MAX, int(arg)) if arg.isdigit() else 10
    ok = delete_range(ctx.chat_id, ctx.message_id, count) if ctx.message_id else 0
    ctx.delete()
    log_action(ctx.chat_id, "clean", ctx.user_id, 0, "%d پیام" % ok)
    notice(ctx.chat_id, 0, "🧹 %d پیام پاک شد." % ok)


@command("rules", private=False)
def cmd_rules(ctx):
    ctx.reply("📜 قوانین گروه\n\n" + (get_settings(ctx.chat_id).get("rules_text") or NO_RULES_TEXT))


# ─── خوشامد و خداحافظی ───
def render_template(tpl, user, chat_id):
    title = APP.db.val("SELECT title FROM groups WHERE chat_id=?", (int(chat_id),), None) or "گروه"
    return tpl.replace("{name}", mention(user)).replace("{title}", title)


@on_join
def hook_welcome(ctx, u):
    cid = int(ctx.chat_id)
    if get_restriction(cid, u["id"]) is not None and get_restriction(cid, u["id"])["kind"] == "ban":
        try:
            APP.api.ban_chat_member(cid, u["id"])
        except BaleAPIError as e:
            LOG.warning("re-ban on join failed: %s", redact(e))
        return
    s = get_settings(cid)
    if s["welcome_on"]:
        ctx.send(render_template(s["welcome_text"] or DEFAULT_WELCOME, u, cid))


@on_leave
def hook_goodbye(ctx, u):
    s = get_settings(ctx.chat_id)
    if s["goodbye_on"]:
        ctx.send(render_template(s["goodbye_text"] or DEFAULT_GOODBYE, u, ctx.chat_id))


# ─── گزارش پیام ───
def notify_group_admins(chat_id, text, markup=None):
    """به ادمین‌های گروه پیوی می‌دهد؛ اگر هیچ پیوی نرسید داخل خود گروه اعلام می‌کند. تعداد پیوی‌های موفق."""
    sent = 0
    res = APP.api.try_call("getChatAdministrators", {"chat_id": chat_id}, retries=1)
    if isinstance(res, list):
        for a in res[:10]:
            u = a.get("user", a) if isinstance(a, dict) else {}
            if not u.get("id") or u.get("is_bot"):
                continue
            p = {"chat_id": u["id"], "text": safe_text(text)}
            if markup:
                p["reply_markup"] = markup
            if APP.api.try_call("sendMessage", p, retries=1) is not None:
                sent += 1
    if sent == 0:
        p = {"chat_id": chat_id, "text": safe_text(text)}
        if markup:
            p["reply_markup"] = markup
        APP.api.try_call("sendMessage", p, retries=1)
    return sent


@command("report", private=False)
def cmd_report(ctx):
    rt = ctx.reply_to
    if not rt or not rt.get("message_id") or not (rt.get("from") or {}).get("id"):
        ctx.reply("🚨 برای گزارش، روی پیام مورد نظر ریپلای کن و /report بزن.")
        return
    target = int(rt["from"]["id"])
    if target == int(ctx.user_id) or rt["from"].get("is_bot"):
        ctx.reply("این پیام رو نمی‌شه گزارش کرد 🙂")
        return
    if APP.db.val("SELECT COUNT(*) FROM reports WHERE reporter_id=? AND created_at>?", (int(ctx.user_id), now() - 3600), 0) >= REPORT_HOURLY_LIMIT:
        ctx.reply("این ساعت زیاد گزارش دادی؛ بعداً دوباره امتحان کن 🙏")
        return
    upsert_user(rt["from"])
    sid = new_sid()
    session_put(sid, ctx.chat_id, ctx.user_id, "report", {"target": target, "mid": int(rt["message_id"])}, 120)
    rows = [[btn(label, "rpt:%s:%d" % (sid, i))] for i, (_, label) in enumerate(REPORT_REASONS)]
    rows.append([btn("❌ لغو", "rpt:%s:x" % sid)])
    ctx.reply("🚨 دلیل گزارش چیه؟", kb(*rows))


@callback("rpt")
def cb_report(ctx):
    try:
        sid, pick = ctx.parts[1], ctx.parts[2]
    except IndexError:
        ctx.answer("دکمه نامعتبر", True)
        return
    s = session_get(sid)
    if s is None or s["kind"] != "report" or s["chat_id"] != int(ctx.chat_id):
        ctx.answer("⌛ این گزارش منقضی شده", True)
        return
    if not callback_owner_ok(ctx, s["user_id"]):
        return
    session_del(sid)
    if pick == "x":
        ctx.edit("❌ گزارش لغو شد.")
        return
    try:
        code, label = REPORT_REASONS[int(pick)]
    except (ValueError, IndexError):
        ctx.answer("دکمه نامعتبر", True)
        return
    d = s["data"]
    if APP.db.val("SELECT COUNT(*) FROM reports WHERE chat_id=? AND reporter_id=? AND target_message_id=?",
                  (int(ctx.chat_id), int(ctx.user_id), d["mid"]), 0):
        ctx.edit("این پیام رو قبلاً گزارش کرده بودی 🙂")
        return
    cur = APP.db.run("INSERT INTO reports(chat_id, reporter_id, target_id, target_message_id, reason, status, created_at) VALUES(?,?,?,?,?,'open',?)",
                     (int(ctx.chat_id), int(ctx.user_id), d["target"], d["mid"], code, now()))
    rid = cur.lastrowid
    log_action(ctx.chat_id, "report", ctx.user_id, d["target"], "#%d %s" % (rid, label))
    STATS.group_add(ctx.chat_id, "mod:reports")
    ctx.edit("✅ گزارشت ثبت شد و به ادمین‌ها می‌رسه. ممنون که کمک می‌کنی 🙏")
    notify_group_admins(ctx.chat_id, "🚨 گزارش جدید #%d\n📝 دلیل: %s\n👤 گزارش‌شده: %s\n🧑 گزارش‌دهنده: %s"
                        % (rid, label, uname(d["target"]), uname(ctx.user_id)),
                        kb([btn("🚨 بررسی گزارش‌ها", "mg:rp:%d" % int(ctx.chat_id))]))


# ─── پنل مدیریت ───
LOG_LABELS = {
    "mod_filter": "🔤 حذف (کلمه‌ی ممنوعه)", "mod_link": "🔗 حذف (لینک)", "mod_flood": "🌊 حذف (فلود)", "mod_spam": "🛡️ حذف (اسپم)",
    "warn": "⚠️ اخطار", "mute": "🔇 سکوت", "unmute": "🔊 رفع سکوت", "ban": "🚫 بن", "unban": "♻️ رفع بن", "report": "🚨 گزارش",
    "report_done": "✅ رسیدگی گزارش", "setting": "⚙️ تنظیمات", "clean": "🧹 پاکسازی", "rules": "📜 قوانین",
    "filter": "🔤 فیلتر کلمات", "welcome": "👋 خوشامد",
}


def _title(cid):
    return APP.db.val("SELECT title FROM groups WHERE chat_id=?", (int(cid),), None) or "گروه"


def onoff(v):
    return "ON" if v else "OFF"


def panel_text(cid):
    cid = int(cid)
    s = get_settings(cid)
    warned = APP.db.val("SELECT COUNT(DISTINCT user_id) FROM warnings WHERE chat_id=? AND created_at>?", (cid, now() - WARN_WINDOW_DAYS * 86400), 0)
    muted = APP.db.val("SELECT COUNT(*) FROM restrictions WHERE chat_id=? AND kind='mute' AND (until=0 OR until>?)", (cid, now()), 0)
    banned = APP.db.val("SELECT COUNT(*) FROM restrictions WHERE chat_id=? AND kind='ban'", (cid,), 0)
    reports = APP.db.val("SELECT COUNT(*) FROM reports WHERE chat_id=? AND status='open'", (cid,), 0)
    return ("🛡️ پنل مدیریت «%s»\n\n🛡️ Anti Spam: %s\n🔗 Anti Link: %s\n🌊 Anti Flood: %s\n🔤 Filter: %s (%d کلمه)\n🤖 Auto Mod: %s\n"
            "⚖️ شدت: %s • حد اخطار: %d\n\n⚠️ Warnings: %d نفر\n🔇 Muted: %d نفر\n🚫 Blocked: %d نفر\n🚨 گزارش باز: %d"
            % (_title(cid), onoff(s["anti_spam"]), onoff(s["anti_link"]), onoff(s["anti_flood"]), onoff(s["filter_enabled"]),
               len(filter_words(cid)), onoff(s["auto_mod"]), LEVEL_NAMES.get(s["mod_level"], "؟"), s["warn_limit"], warned, muted, banned, reports))


def panel_markup(cid, private):
    cid = int(cid)
    reports = APP.db.val("SELECT COUNT(*) FROM reports WHERE chat_id=? AND status='open'", (cid,), 0)
    return kb([btn("⚙️ تنظیمات", "mg:set:%d" % cid), btn("👥 کاربران", "mg:us:%d" % cid)],
              [btn("🚨 گزارش‌ها%s" % (" (%d)" % reports if reports else ""), "mg:rp:%d" % cid), btn("📜 قوانین", "mg:ru:%d" % cid)],
              [btn("📋 لاگ‌ها", "mg:lg:%d" % cid), btn("👋 خوشامد", "mg:wl:%d" % cid)],
              [btn("🔤 فیلتر کلمات", "mg:fw:%d" % cid), btn("🧹 پاکسازی", "mg:cl:%d" % cid)],
              [btn("🏠 برگشت", "menu:manage" if private else "menu:home")])


def show_panel(ctx, cid):
    show(ctx, panel_text(cid), panel_markup(cid, ctx.is_private))


def back_row(cid):
    return [btn("↩️ مدیریت", "mg:o:%d" % int(cid))]


def _delta(ctx):
    try:
        d = int(ctx.parts[3])
    except (IndexError, ValueError):
        return 0
    return d if d in (-1, 1) else 0


def _mg_open(ctx, cid):
    input_clear(ctx.chat_id, ctx.user_id)
    show_panel(ctx, cid)


def _mg_settings_page(ctx, cid):
    s = get_settings(cid)
    rows, cur = [], []
    for key, emoji, label in MOD_TOGGLES:
        cur.append(btn("%s %s %s" % (emoji, label, "✅" if s[key] else "❌"), "mg:t:%d:%s" % (cid, key)))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([btn("➖", "mg:lv:%d:-1" % cid), btn("⚖️ شدت: %s" % LEVEL_NAMES[s["mod_level"]], "mg:set:%d" % cid), btn("➕", "mg:lv:%d:1" % cid)])
    rows.append([btn("➖", "mg:wn:%d:-1" % cid), btn("⚠️ حد اخطار: %d" % s["warn_limit"], "mg:set:%d" % cid), btn("➕", "mg:wn:%d:1" % cid)])
    rows.append(back_row(cid))
    show(ctx, "⚙️ تنظیمات مدیریت «%s»\n\nشدت ملایم: فقط حذف پیام • متوسط: حذف + اخطار • سخت: حساس‌تر برای فلود و تکرار" % _title(cid), kb(*rows))


def _mg_toggle(ctx, cid):
    key = ctx.parts[3] if len(ctx.parts) > 3 else ""
    if key not in MOD_KEYS:
        ctx.answer("تنظیم نامعتبر", True)
        return
    new = toggle_setting(cid, key)
    log_action(cid, "setting", ctx.user_id, 0, "%s=%d" % (key, 1 if new else 0))
    ctx.answer("%s %s" % (MOD_NAMES[key], "روشن شد ✅" if new else "خاموش شد ❌"))
    _mg_settings_page(ctx, cid)


def _mg_step(ctx, cid, key, lo, hi, label):
    d = _delta(ctx)
    cur = get_settings(cid)[key]
    new = max(lo, min(hi, cur + d))
    if d == 0 or new == cur:
        ctx.answer("به حد مجاز رسیدی")
    else:
        set_setting(cid, key, new)
        log_action(cid, "setting", ctx.user_id, 0, "%s=%d" % (key, new))
        ctx.answer("%s: %d" % (label, new))
    _mg_settings_page(ctx, cid)


def _mg_level(ctx, cid):
    _mg_step(ctx, cid, "mod_level", 1, 3, "شدت")


def _mg_warnlimit(ctx, cid):
    _mg_step(ctx, cid, "warn_limit", 1, 10, "حد اخطار")


def _mg_users(ctx, cid):
    warned = APP.db.all("SELECT user_id, COUNT(*) c FROM warnings WHERE chat_id=? AND created_at>? GROUP BY user_id ORDER BY c DESC LIMIT 6",
                        (cid, now() - WARN_WINDOW_DAYS * 86400))
    muted = APP.db.all("SELECT user_id, until FROM restrictions WHERE chat_id=? AND kind='mute' AND (until=0 OR until>?) LIMIT 6", (cid, now()))
    banned = APP.db.all("SELECT user_id FROM restrictions WHERE chat_id=? AND kind='ban' LIMIT 6", (cid,))
    lines, rows = ["👥 کاربران «%s»" % _title(cid), ""], []
    lines.append("⚠️ اخطارها: " + ("، ".join("%s (%d)" % (uname(r["user_id"]), r["c"]) for r in warned) if warned else "—"))
    lines.append("🔇 ساکت‌ها: " + ("، ".join(uname(r["user_id"]) for r in muted) if muted else "—"))
    lines.append("🚫 بلاک‌ها: " + ("، ".join(uname(r["user_id"]) for r in banned) if banned else "—"))
    for r in warned:
        rows.append([btn("🧽 پاک کردن اخطار %s" % uname(r["user_id"])[:14], "mg:cw:%d:%d" % (cid, r["user_id"]))])
    for r in muted:
        rows.append([btn("🔊 رفع سکوت %s" % uname(r["user_id"])[:14], "mg:um:%d:%d" % (cid, r["user_id"]))])
    for r in banned:
        rows.append([btn("♻️ رفع بن %s" % uname(r["user_id"])[:14], "mg:ub:%d:%d" % (cid, r["user_id"]))])
    lines += ["", "برای اخطار/سکوت/بن دستی: روی پیام کاربر ریپلای کن و /warn /mute /ban بزن."]
    rows.append(back_row(cid))
    show(ctx, "\n".join(lines), kb(*rows))


def _uid_arg(ctx):
    try:
        return int(ctx.parts[3])
    except (IndexError, ValueError):
        return None


def _mg_unmute(ctx, cid):
    uid = _uid_arg(ctx)
    if uid is not None:
        unmute_user(cid, uid, ctx.user_id)
        ctx.answer("🔊 رفع سکوت شد")
    _mg_users(ctx, cid)


def _mg_unban(ctx, cid):
    uid = _uid_arg(ctx)
    if uid is not None:
        unban_user(cid, uid, ctx.user_id)
        ctx.answer("♻️ رفع بن شد")
    _mg_users(ctx, cid)


def _mg_clear_warn(ctx, cid):
    uid = _uid_arg(ctx)
    if uid is not None:
        APP.db.run("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (cid, uid))
        log_action(cid, "unwarn", ctx.user_id, uid, "")
        ctx.answer("🧽 اخطارها پاک شد")
    _mg_users(ctx, cid)


# گزارش‌ها
def _mg_reports(ctx, cid):
    r = APP.db.one("SELECT * FROM reports WHERE chat_id=? AND status='open' ORDER BY id LIMIT 1", (cid,))
    if r is None:
        show(ctx, "🚨 گزارش باز وجود نداره ✅", kb(back_row(cid)))
        return
    left = APP.db.val("SELECT COUNT(*) FROM reports WHERE chat_id=? AND status='open'", (cid,), 0)
    label = dict(REPORT_REASONS).get(r["reason"], r["reason"])
    rid = r["id"]
    text = ("🚨 گزارش #%d (در صف: %d)\n\n📝 دلیل: %s\n👤 گزارش‌شده: %s\n🧑 گزارش‌دهنده: %s\n🆔 شماره‌ی پیام: %d"
            % (rid, left, label, uname(r["target_id"]), uname(r["reporter_id"]), r["target_message_id"]))
    show(ctx, text, kb([btn("✅ رسیدگی شد", "mg:rd:%d:%d" % (cid, rid)), btn("❌ رد گزارش", "mg:rj:%d:%d" % (cid, rid))],
                       [btn("⚠️ اخطار", "mg:rw:%d:%d" % (cid, rid)), btn("🔇 سکوت ۱ ساعت", "mg:rm:%d:%d" % (cid, rid))],
                       [btn("🚫 بن", "mg:rb:%d:%d" % (cid, rid)), btn("🗑️ حذف پیام", "mg:rx:%d:%d" % (cid, rid))],
                       back_row(cid)))


def _mg_report_action(ctx, cid):
    act = ctx.parts[1]
    try:
        rid = int(ctx.parts[3])
    except (IndexError, ValueError):
        ctx.answer("دکمه نامعتبر", True)
        return
    r = APP.db.one("SELECT * FROM reports WHERE id=? AND chat_id=? AND status='open'", (rid, cid))
    if r is None:
        ctx.answer("این گزارش قبلاً رسیدگی شده", True)
        _mg_reports(ctx, cid)
        return
    target = r["target_id"]
    if act in ("rw", "rm", "rb") and (is_bot_admin(target) or is_group_admin(cid, target)):
        ctx.answer("ادمین‌ها محافظت‌شده‌ان 🛡️", True)
        return
    if act == "rw":
        add_warning(cid, target, "گزارش #%d" % rid, ctx.user_id)
    elif act == "rm":
        mute_user(cid, target, 60, ctx.user_id, "گزارش #%d" % rid)
    elif act == "rb":
        ban_user(cid, target, ctx.user_id, "گزارش #%d" % rid)
    elif act == "rx":
        APP.api.delete_message(cid, r["target_message_id"])
    status = "rejected" if act == "rj" else "done"
    APP.db.run("UPDATE reports SET status=?, handled_by=?, handled_at=? WHERE id=?", (status, int(ctx.user_id), now(), rid))
    log_action(cid, "report_done", ctx.user_id, target, "#%d %s" % (rid, act))
    ctx.answer("✅ انجام شد")
    _mg_reports(ctx, cid)


# قوانین
def _mg_rules(ctx, cid):
    rules = get_settings(cid).get("rules_text")
    show(ctx, "📜 قوانین گروه\n\n" + (rules or NO_RULES_TEXT),
         kb([btn("✏️ ویرایش", "mg:re:%d" % cid), btn("♻️ ریست", "mg:rr:%d" % cid)], back_row(cid)))


def _begin_input(ctx, cid, kind, prompt, extra=None):
    input_set(ctx.chat_id, ctx.user_id, kind, dict({"target": int(cid)}, **(extra or {})), ttl=300)
    show(ctx, prompt, kb([btn("❌ لغو", "mg:o:%d" % int(cid))]))


def _mg_rules_edit(ctx, cid):
    _begin_input(ctx, cid, "rules_edit", "✏️ متن جدید قوانین رو بنویس و بفرست (حداکثر ۱۵۰۰ حرف).")


def _mg_rules_reset(ctx, cid):
    set_setting(cid, "rules_text", None)
    log_action(cid, "rules", ctx.user_id, 0, "reset")
    ctx.answer("♻️ قوانین ریست شد")
    _mg_rules(ctx, cid)


def _input_target(ctx, data):
    cid = data.get("target")
    if cid is None or not is_group_admin(cid, ctx.user_id):
        input_clear(ctx.chat_id, ctx.user_id)
        ctx.reply("⛔ این بخش فقط مخصوص ادمینه")
        return None
    return int(cid)


@on_input("rules_edit")
def input_rules(ctx, data):
    cid = _input_target(ctx, data)
    if cid is None:
        return True
    text = ctx.text.strip()
    if len(text) < 5 or len(text) > 1500:
        ctx.reply("متن قوانین باید بین ۵ تا ۱۵۰۰ حرف باشه 🙂", kb([btn("❌ لغو", "mg:o:%d" % cid)]))
        return True
    input_clear(ctx.chat_id, ctx.user_id)
    set_setting(cid, "rules_text", text)
    log_action(cid, "rules", ctx.user_id, 0, "edit")
    ctx.reply("✅ قوانین ذخیره شد.", kb([btn("📜 قوانین", "mg:ru:%d" % cid)]))
    return True


# خوشامد / خداحافظی
def _mg_welcome(ctx, cid):
    s = get_settings(cid)
    show(ctx, "👋 خوشامد و خداحافظی «%s»\n\n%s خوشامد: %s\n%s خداحافظی: %s\n\n{name} = نام کاربر • {title} = نام گروه\n\nمتن خوشامد:\n%s\n\nمتن خداحافظی:\n%s"
         % (_title(cid), "✅" if s["welcome_on"] else "❌", onoff(s["welcome_on"]), "✅" if s["goodbye_on"] else "❌", onoff(s["goodbye_on"]),
            s["welcome_text"] or DEFAULT_WELCOME, s["goodbye_text"] or DEFAULT_GOODBYE),
         kb([btn("👋 خوشامد %s" % ("✅" if s["welcome_on"] else "❌"), "mg:wt:%d:welcome_on" % cid),
             btn("🚪 خداحافظی %s" % ("✅" if s["goodbye_on"] else "❌"), "mg:wt:%d:goodbye_on" % cid)],
            [btn("✏️ متن خوشامد", "mg:we:%d" % cid), btn("✏️ متن خداحافظی", "mg:ge:%d" % cid)],
            [btn("♻️ متن‌های پیش‌فرض", "mg:wd:%d" % cid)], back_row(cid)))


def _mg_welcome_toggle(ctx, cid):
    key = ctx.parts[3] if len(ctx.parts) > 3 else ""
    if key not in ("welcome_on", "goodbye_on"):
        ctx.answer("تنظیم نامعتبر", True)
        return
    new = toggle_setting(cid, key)
    log_action(cid, "setting", ctx.user_id, 0, "%s=%d" % (key, 1 if new else 0))
    ctx.answer("روشن شد ✅" if new else "خاموش شد ❌")
    _mg_welcome(ctx, cid)


def _mg_welcome_edit(ctx, cid):
    _begin_input(ctx, cid, "welcome_edit", "✏️ متن خوشامد رو بنویس.\n{name} = نام کاربر، {title} = نام گروه", {"which": "welcome_text"})


def _mg_goodbye_edit(ctx, cid):
    _begin_input(ctx, cid, "welcome_edit", "✏️ متن خداحافظی رو بنویس.\n{name} = نام کاربر، {title} = نام گروه", {"which": "goodbye_text"})


def _mg_welcome_default(ctx, cid):
    set_setting(cid, "welcome_text", None)
    set_setting(cid, "goodbye_text", None)
    log_action(cid, "welcome", ctx.user_id, 0, "reset")
    ctx.answer("♻️ متن‌ها پیش‌فرض شد")
    _mg_welcome(ctx, cid)


@on_input("welcome_edit")
def input_welcome(ctx, data):
    cid = _input_target(ctx, data)
    if cid is None:
        return True
    text = ctx.text.strip()
    which = data.get("which") if data.get("which") in ("welcome_text", "goodbye_text") else "welcome_text"
    if len(text) < 3 or len(text) > 400:
        ctx.reply("متن باید بین ۳ تا ۴۰۰ حرف باشه 🙂", kb([btn("❌ لغو", "mg:o:%d" % cid)]))
        return True
    input_clear(ctx.chat_id, ctx.user_id)
    set_setting(cid, which, text)
    log_action(cid, "welcome", ctx.user_id, 0, which)
    ctx.reply("✅ ذخیره شد.", kb([btn("👋 خوشامد", "mg:wl:%d" % cid)]))
    return True


# فیلتر کلمات
def _word_id(w):
    return hashlib.sha1(w.encode("utf-8")).hexdigest()[:6]


def _mg_filter(ctx, cid):
    words = sorted(filter_words(cid))
    rows = []
    for i in range(0, min(len(words), 10), 2):
        rows.append([btn("🗑️ " + w[:14], "mg:fd:%d:%s" % (cid, _word_id(w))) for w in words[i:i + 2]])
    rows.append([btn("➕ افزودن کلمه", "mg:fa:%d" % cid), btn("🧨 حذف همه", "mg:fx:%d" % cid)])
    rows.append(back_row(cid))
    show(ctx, "🔤 فیلتر کلمات «%s» (%d کلمه)\n\n%s\n\nپیام‌هایی که یکی از این کلمه‌ها/عبارت‌ها رو داشته باشن پاک می‌شن (وقتی «فیلتر» روشن باشه)."
         % (_title(cid), len(words), "، ".join(words[:30]) if words else "هنوز کلمه‌ای اضافه نشده"), kb(*rows))


def _mg_filter_add(ctx, cid):
    _begin_input(ctx, cid, "filter_add", "➕ کلمه‌ها یا عبارت‌ها رو بنویس؛ هر کدوم رو با کاما یا خط جدید جدا کن.")


def _mg_filter_del(ctx, cid):
    wid = ctx.parts[3] if len(ctx.parts) > 3 else ""
    for w in list(filter_words(cid)):
        if _word_id(w) == wid:
            APP.db.run("DELETE FROM filter_words WHERE chat_id=? AND word=?", (cid, w))
            FILTER_CACHE.pop(cid, None)
            log_action(cid, "filter", ctx.user_id, 0, "حذف: " + w)
            ctx.answer("🗑️ حذف شد")
            break
    _mg_filter(ctx, cid)


def _mg_filter_clear(ctx, cid):
    n = APP.db.run("DELETE FROM filter_words WHERE chat_id=?", (cid,)).rowcount
    FILTER_CACHE.pop(cid, None)
    log_action(cid, "filter", ctx.user_id, 0, "حذف همه (%d)" % n)
    ctx.answer("🧨 همه حذف شد")
    _mg_filter(ctx, cid)


@on_input("filter_add")
def input_filter(ctx, data):
    cid = _input_target(ctx, data)
    if cid is None:
        return True
    input_clear(ctx.chat_id, ctx.user_id)
    added = 0
    for chunk in re.split(r"[\n,،;]+", ctx.text):
        w = norm_text(chunk)
        if not 2 <= len(w) <= 30 or len(filter_words(cid)) >= MAX_FILTER_WORDS:
            continue
        added += APP.db.run("INSERT OR IGNORE INTO filter_words(chat_id, word) VALUES(?,?)", (cid, w)).rowcount
    FILTER_CACHE.pop(cid, None)
    if added:
        log_action(cid, "filter", ctx.user_id, 0, "افزودن %d" % added)
    ctx.reply("✅ %d کلمه اضافه شد." % added if added else "کلمه‌ی معتبری پیدا نکردم 🤔 (۲ تا ۳۰ حرف)", kb([btn("🔤 فیلتر کلمات", "mg:fw:%d" % cid)]))
    return True


# لاگ‌ها
def _mg_logs(ctx, cid):
    rows = APP.db.all("SELECT * FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT 15", (cid,))
    lines = ["📋 لاگ‌های «%s»" % _title(cid), ""]
    if not rows:
        lines.append("هنوز چیزی ثبت نشده.")
    for r in rows:
        who = (" %s" % uname(r["actor_id"])) if r["actor_id"] else ""
        to = (" ← %s" % uname(r["target_id"])) if r["target_id"] else ""
        lines.append("%s %s%s%s%s" % (iran_dt(r["ts"]).strftime("%m-%d %H:%M"), LOG_LABELS.get(r["kind"], r["kind"]), who, to,
                                      (" — " + r["detail"]) if r["detail"] else ""))
    show(ctx, "\n".join(lines), kb([btn("🔄", "mg:lg:%d" % cid)], back_row(cid)))


# پاکسازی
def _mg_clean_page(ctx, cid):
    if not ctx.is_group:
        show(ctx, "🧹 پاکسازی فقط داخل خود گروه ممکنه: /clean 20 (یا ریپلای روی اولین پیام و /clean)", kb(back_row(cid)))
        return
    show(ctx, "🧹 پاکسازی\n\nچند پیام بالای همین پنل پاک بشه؟", kb([btn(str(n), "mg:cn:%d:%d" % (cid, n)) for n in (10, 25, 50, 100)], back_row(cid)))


def _mg_clean_run(ctx, cid):
    if not ctx.is_group or not ctx.message_id:
        ctx.answer("فقط داخل گروه", True)
        return
    try:
        n = max(1, min(CLEAN_MAX, int(ctx.parts[3])))
    except (IndexError, ValueError):
        ctx.answer("دکمه نامعتبر", True)
        return
    ok = delete_range(cid, ctx.message_id, n)
    log_action(cid, "clean", ctx.user_id, 0, "%d پیام" % ok)
    ctx.answer("🧹 %d پیام پاک شد" % ok, True)


MG_ACTIONS = {
    "o": _mg_open, "set": _mg_settings_page, "t": _mg_toggle, "lv": _mg_level, "wn": _mg_warnlimit,
    "us": _mg_users, "um": _mg_unmute, "ub": _mg_unban, "cw": _mg_clear_warn,
    "rp": _mg_reports, "rd": _mg_report_action, "rj": _mg_report_action, "rw": _mg_report_action,
    "rm": _mg_report_action, "rb": _mg_report_action, "rx": _mg_report_action,
    "ru": _mg_rules, "re": _mg_rules_edit, "rr": _mg_rules_reset,
    "wl": _mg_welcome, "wt": _mg_welcome_toggle, "we": _mg_welcome_edit, "ge": _mg_goodbye_edit, "wd": _mg_welcome_default,
    "fw": _mg_filter, "fa": _mg_filter_add, "fd": _mg_filter_del, "fx": _mg_filter_clear,
    "lg": _mg_logs, "cl": _mg_clean_page, "cn": _mg_clean_run,
}


@callback("mg")
def cb_manage(ctx):
    action = ctx.parts[1] if len(ctx.parts) > 1 else "o"
    handler = MG_ACTIONS.get(action)
    if handler is None:
        ctx.answer("دکمه نامعتبر", True)
        return
    target = _settings_target(ctx)  # ادمین‌بودن در همان گروه + جلوگیری از دکمه‌ی گروه دیگر
    if target is None:
        return
    handler(ctx, target)


@section("manage", admin=True)
def sec_manage(ctx):
    if ctx.is_private:
        show_group_picker(ctx, "mg:o", "🛡️ مدیریت کدوم گروه؟")
    else:
        show_panel(ctx, ctx.chat_id)


@command("manage")
def cmd_manage(ctx):
    if ctx.is_private:
        show_group_picker(ctx, "mg:o", "🛡️ مدیریت کدوم گروه؟")
    elif ctx.is_admin():
        show_panel(ctx, ctx.chat_id)
    else:
        ctx.reply("⛔ این بخش فقط مخصوص ادمینه")


# ─── SELFTEST فاز ۷ ───
def _mod_off(chat):
    for k in ("anti_spam", "anti_flood", "anti_link", "filter_enabled"):
        set_setting(chat, k, 0)


def _p7_group(g, admins=(), users=()):
    upsert_group({"id": g, "title": "G%d" % -g, "type": "group"})
    for a in admins:
        ADMIN_CACHE[(g, a)] = (True, time.monotonic() + 600)
    for u in users:
        ADMIN_CACHE[(g, u)] = (False, time.monotonic() + 600)


def _p7_deletes(api):
    return [c for c in api.calls if c[0] == "delete_message"]


@selftest("تشخیص تخلف: لینک، کلمه ممنوعه، تکرار، مدت‌ها")
def _t_p7_detect():
    for t in ("https://x.com", "www.test.ir", "join ble.ir/abc", "t.me/chan", "site.com", "WWW.SPAM.ORG"):
        check(LINK_RE.search(t), "link not detected: " + t)
    for t in ("سلام", "salam", "3.5", "hello world", "قیمت ۳.۵ میلیون"):
        check(not LINK_RE.search(t), "false link: " + t)
    cases = {"": 60, "30": 30, "2h": 120, "1d": 1440, "۳۰": 30, "0": 0, "دائم": 0, "abc": None, "90m": 90, "5 ساعت": 300}
    for k, v in cases.items():
        check(parse_duration(k) == v, "parse_duration(%r)=%r expected %r" % (k, parse_duration(k), v))
    check(parse_duration("99999999d") == MAX_MUTE_MINUTES, "duration cap")
    check([spam_hit(-1, 1, "یک پیام تکراری", 2) for _ in range(3)] == [False, False, True], "spam L2")
    check([spam_hit(-1, 2, "یک پیام تکراری", 3) for _ in range(2)] == [False, True], "spam L3")
    check(not any(spam_hit(-1, 3, "ab", 3) for _ in range(5)) and not any(spam_hit(-1, 4, "12345"[:3], 3) for _ in range(5)), "short texts exempt")
    g = -9701
    _p7_group(g)
    APP.db.run("INSERT INTO filter_words(chat_id, word) VALUES(?,?)", (g, norm_text("عبارت ممنوع")))
    APP.db.run("INSERT INTO filter_words(chat_id, word) VALUES(?,?)", (g, norm_text("سگ")))
    FILTER_CACHE.pop(g, None)
    check(filter_hit(g, "این یک عبارت ممنوع است!") and filter_hit(g, "سگ!") and filter_hit(g, "عبارتِ ممنوع".replace("ِ", "")), "filter hits")
    check(not filter_hit(g, "سگک") and not filter_hit(g, "فقط ممنوع") and not filter_hit(g, "سلام"), "filter false positives")
    return "ok"


@selftest("اجرای مدیریت: حذف، اخطار، ادمین معاف، سطح، اتومود، خاموش")
def _t_p7_enforce():
    api, g = APP.api, -9711
    adm, u1, u2, u3, u4 = 9711, 9712, 9713, 9714, 9715
    _p7_group(g, [adm], [u1, u2, u3, u4])
    link = "ببین www.spam-site.com"
    handle_update(_p2_msg(link, u1, g, mid=1))
    check(len(_p7_deletes(api)) == 1, "link not deleted")
    check(count_active_warnings(g, u1) == 1 and any("اخطار 1 از 3" in t for t in _p5_notes(api)), "warning/notice")
    handle_update(_p2_msg(link, adm, g, mid=2))
    check(len(_p7_deletes(api)) == 1, "admin must be exempt")
    set_setting(g, "anti_link", 0)
    handle_update(_p2_msg(link, u2, g, mid=3))
    check(len(_p7_deletes(api)) == 1, "anti_link OFF still deletes")
    set_setting(g, "anti_link", 1)
    set_setting(g, "auto_mod", 0)
    handle_update(_p2_msg(link, u3, g, mid=4))
    check(len(_p7_deletes(api)) == 2 and count_active_warnings(g, u3) == 0, "auto_mod OFF must only delete")
    set_setting(g, "auto_mod", 1)
    set_setting(g, "mod_level", 1)
    handle_update(_p2_msg(link, u4, g, mid=5))
    check(len(_p7_deletes(api)) == 3 and count_active_warnings(g, u4) == 0, "level 1 must only delete")
    handle_update(_p2_msg("سلام عزیزان", u4, g, mid=6))
    check(len(_p7_deletes(api)) == 3, "normal text deleted")
    STATS.flush()
    check(APP.db.val("SELECT svalue FROM group_stats WHERE chat_id=? AND skey='mod:link'", (g,)) == 3, "mod counters")
    check(APP.db.val("SELECT COUNT(*) FROM logs WHERE chat_id=? AND kind='mod_link'", (g,)) == 3, "logs")
    return "ok"


@selftest("حد اخطار ⇒ سکوت خودکار، حذف پیام ساکت‌ها، رفع سکوت، پایان مدت")
def _t_p7_mute_flow():
    api, g = APP.api, -9721
    adm, u = 9721, 9722
    _p7_group(g, [adm], [u])
    set_setting(g, "warn_limit", 2)
    handle_update(_p2_msg("لینک www.a-b.com", u, g, mid=1))
    COOLDOWNS.clear()
    handle_update(_p2_msg("لینک www.c-d.com", u, g, mid=2))
    r = get_restriction(g, u)
    check(r is not None and r["kind"] == "mute", "no auto mute")
    check(abs(r["until"] - (now() + MUTE_MINUTES_BY_LEVEL[2] * 60)) < 5, "mute duration by level")
    check(count_active_warnings(g, u) == 0, "warnings not reset after mute")
    check(any(c[0] == "call" and c[1][0] == "restrictChatMember" for c in api.calls), "restrict API not attempted")
    n = len(_p7_deletes(api))
    handle_update(_p2_msg("یه پیام کاملاً عادی", u, g, mid=3))
    check(len(_p7_deletes(api)) == n + 1, "muted user's message not deleted")
    handle_update(_p2_cb("p7-m1", "mg:um:%d:%d" % (g, u), u, g, 4))
    check(get_restriction(g, u) is not None, "muted user unmuted themselves")
    handle_update(_p2_cb("p7-m2", "mg:um:%d:%d" % (g, u), adm, g, 5))
    check(get_restriction(g, u) is None, "admin unmute failed")
    n = len(_p7_deletes(api))
    handle_update(_p2_msg("حالا دوباره آزادم", u, g, mid=6))
    check(len(_p7_deletes(api)) == n, "unmuted user still deleted")
    mute_user(g, u, 1, adm)
    APP.db.run("UPDATE restrictions SET until=? WHERE chat_id=? AND user_id=?", (now() - 1, g, u))
    check(get_restriction(g, u) is None, "expired mute still active")
    mute_user(g, u, 1, adm)
    APP.db.run("UPDATE restrictions SET until=? WHERE chat_id=? AND user_id=?", (now() - 1, g, u))
    _task_mod_sweeper()
    check(APP.db.val("SELECT COUNT(*) FROM restrictions WHERE chat_id=?", (g,)) == 0, "sweeper left expired rows")
    return "ok"


@selftest("ضد فلود و ضد تکرار (سطح سخت/متوسط)")
def _t_p7_flood_spam():
    api, g = APP.api, -9731
    u1, u2 = 9731, 9732
    _p7_group(g, [], [u1, u2])
    set_setting(g, "mod_level", 3)
    set_setting(g, "auto_mod", 0)
    for i in range(4):
        handle_update(_p2_msg("پیام شماره %d" % i, u1, g, mid=i + 1))
    check(len(_p7_deletes(api)) == 0, "flood limit hit too early")
    handle_update(_p2_msg("پیام پنجم", u1, g, mid=9))
    check(len(_p7_deletes(api)) == 1 and APP.db.val("SELECT COUNT(*) FROM logs WHERE kind='mod_flood'") == 1, "flood not stopped")
    set_setting(g, "mod_level", 2)
    set_setting(g, "anti_flood", 0)
    for i in range(3):
        handle_update(_p2_msg("همین متن تکراری رو می‌فرستم", u2, g, mid=20 + i))
    check(len(_p7_deletes(api)) == 2 and APP.db.val("SELECT COUNT(*) FROM logs WHERE kind='mod_spam'") == 1, "spam repeat not stopped")
    return "ok"


@selftest("فیلتر کلمات از پنل: افزودن، حذف پیام، حذف کلمه، فقط ادمین")
def _t_p7_filter_panel():
    api, g = APP.api, -9741
    adm, u = 9741, 9742
    _p7_group(g, [adm], [u])
    handle_update(_p2_cb("p7-f1", "mg:fa:%d" % g, u, g, 1))
    check(input_get(g, u) is None, "non-admin started filter input")
    handle_update(_p2_cb("p7-f2", "mg:fa:%d" % g, adm, g, 2))
    check(input_get(g, adm)["kind"] == "input:filter_add", "admin input")
    handle_update(_p2_msg("بدکلمه, عبارت بد\nx", adm, g, mid=3))
    check(filter_words(g) == {"بدکلمه", "عبارت بد"}, "words: %s" % filter_words(g))
    handle_update(_p2_msg("این بدکلمه رو نگو", u, g, mid=4))
    check(len(_p7_deletes(api)) == 1, "filtered word not deleted")
    handle_update(_p2_msg("فقط عبارت", u, g, mid=5))
    check(len(_p7_deletes(api)) == 1, "partial phrase deleted")
    wid = _word_id("بدکلمه")
    handle_update(_p2_cb("p7-f3", "mg:fd:%d:%s" % (g, wid), u, g, 6))
    check("بدکلمه" in filter_words(g), "non-admin removed word")
    handle_update(_p2_cb("p7-f4", "mg:fd:%d:%s" % (g, wid), adm, g, 7))
    check(filter_words(g) == {"عبارت بد"}, "word delete")
    set_setting(g, "filter_enabled", 0)
    handle_update(_p2_msg("عبارت بد", u, g, mid=8))
    check(len(_p7_deletes(api)) == 1, "filter OFF still deletes")
    handle_update(_p2_cb("p7-f5", "mg:fx:%d" % g, adm, g, 9))
    check(not filter_words(g), "clear all")
    return "ok"


@selftest("دستورهای مدیریتی: warn/mute/unmute/ban/unban، محافظت، دسترسی")
def _t_p7_commands():
    global ADMIN_ID
    api, g = APP.api, -9751
    adm, adm2, u, v = 9751, 9752, 9753, 9754
    _p7_group(g, [adm, adm2], [u, v])
    upsert_user({"id": v, "first_name": "Vee", "username": "vee_u"})
    old_admin, ADMIN_ID = ADMIN_ID, 9759
    try:
        _p4_text(u, g, "/warn", mid=1, reply_user=v)
        check(count_active_warnings(g, v) == 0, "non-admin warned someone")
        _p4_text(adm, g, "/warn بی‌ادبی", mid=2, reply_user=v)
        check(count_active_warnings(g, v) == 1 and "اخطار" in _p3_last_text(api), "/warn")
        _p4_text(adm, g, "/mute 30", mid=3, reply_user=v)
        r = get_restriction(g, v)
        check(r and r["kind"] == "mute" and abs(r["until"] - (now() + 1800)) < 5, "/mute 30")
        _p4_text(adm, g, "/mute abc", mid=4, reply_user=v)
        check("مدت نامعتبره" in _p3_last_text(api), "/mute bad duration")
        _p4_text(adm, g, "/unmute", mid=5, reply_user=v)
        check(get_restriction(g, v) is None, "/unmute")
        _p4_text(adm, g, "/ban دلیل", mid=6, reply_user=v)
        check(get_restriction(g, v)["kind"] == "ban" and any(c[0] == "ban_chat_member" for c in api.calls), "/ban")
        n = len(_p7_deletes(api))
        handle_update(_p2_msg("من بن شدم", v, g, mid=7))
        check(len(_p7_deletes(api)) == n + 1, "banned user's message stays")
        upsert_user({"id": v, "first_name": "Vee", "username": "vee_u"})
        _p4_text(adm, g, "/unban @vee_u", mid=8)
        check(get_restriction(g, v) is None and any(c[0] == "unban_chat_member" for c in api.calls), "/unban by @username")
        _p4_text(adm2, g, "/ban", mid=9, reply_user=adm)
        check(get_restriction(g, adm) is None and "محافظت" in _p3_last_text(api), "admin must be protected")
        _p4_text(adm2, g, "/mute", mid=10, reply_user=adm2)
        check("خودت" in _p3_last_text(api), "self target")
        _p4_text(adm2, g, "/ban", mid=11, reply_user=ADMIN_ID)
        check(get_restriction(g, ADMIN_ID) is None, "bot owner must be protected")
        _p4_text(adm2, g, "/mute @nobody", mid=12)
        check("ریپلای" in _p3_last_text(api), "unknown target")
        mute_user(g, v, 30, adm)
        n = len(api.calls)
        handle_update(_p2_msg("/profile", v, g, mid=13))
        check(len(_p7_deletes(api)) >= 1 and not any(c[0] == "send_message" and "Level" in str(c[1]) for c in api.calls[n:]), "muted user's command executed")
    finally:
        ADMIN_ID = old_admin
    return "ok"


def _p7_reply(user, chat, text, mid, reply_mid, reply_user):
    upd = _p2_msg(text, user, chat, mid=mid)
    upd["message"]["reply_to_message"] = {"message_id": reply_mid, "from": {"id": reply_user, "first_name": "U%d" % reply_user}}
    handle_update(upd)


@selftest("گزارش پیام: ثبت، دلایل، تکراری، رسیدگی ادمین")
def _t_p7_reports():
    api, g = APP.api, -9761
    adm, rep, bad, other = 9761, 9762, 9763, 9764
    _p7_group(g, [adm], [rep, bad, other])
    _p4_text(rep, g, "/report", mid=1)
    check("ریپلای" in _p3_last_text(api), "report without reply")
    _p7_reply(rep, g, "/report", 10, 500, bad)
    s = session_find(g, "report", rep)[0]
    handle_update(_p2_cb("p7-r1", "rpt:%s:0" % s["sid"], other, g, 10))
    check(APP.db.val("SELECT COUNT(*) FROM reports") == 0, "someone else answered my report prompt")
    handle_update(_p2_cb("p7-r2", "rpt:%s:2" % s["sid"], rep, g, 10))
    r = APP.db.one("SELECT * FROM reports")
    check(r and r["status"] == "open" and r["reason"] == "insult" and r["target_id"] == bad and r["reporter_id"] == rep
          and r["target_message_id"] == 500, "report row")
    check(any("گزارش جدید" in t for t in _p5_notes(api)), "admins not notified")
    _p7_reply(rep, g, "/report", 11, 500, bad)
    s2 = session_find(g, "report", rep)[0]
    handle_update(_p2_cb("p7-r3", "rpt:%s:0" % s2["sid"], rep, g, 11))
    check(APP.db.val("SELECT COUNT(*) FROM reports") == 1, "duplicate report created")
    _p7_reply(rep, g, "/report", 12, 501, bad)
    s3 = session_find(g, "report", rep)[0]
    handle_update(_p2_cb("p7-r4", "rpt:%s:0" % s3["sid"], rep, g, 12))
    check(APP.db.val("SELECT COUNT(*) FROM reports") == 2, "different message must create a new report")
    _p7_reply(rep, g, "/report", 13, 502, bad)
    s4 = session_find(g, "report", rep)[0]
    handle_update(_p2_cb("p7-r5", "rpt:%s:x" % s4["sid"], rep, g, 13))
    check(session_find(g, "report", rep) == [] and APP.db.val("SELECT COUNT(*) FROM reports") == 2, "cancel")
    edits_before = len(_p2_calls(api, "edit_message_text"))
    handle_update(_p2_cb("p7-r6", "mg:rp:%d" % g, rep, g, 70))
    check(len(_p2_calls(api, "edit_message_text")) == edits_before, "non-admin opened reports")
    handle_update(_p2_cb("p7-r7", "mg:rp:%d" % g, adm, g, 71))
    check("گزارش #1" in _p3_last_text(api, "edit_message_text", 2), "review page")
    handle_update(_p2_cb("p7-r8", "mg:rx:%d:1" % g, adm, g, 72))
    check(any(c[0] == "delete_message" and c[1][1] == 500 for c in api.calls), "reported message not deleted")
    check(APP.db.val("SELECT status FROM reports WHERE id=1") == "done", "report not closed")
    handle_update(_p2_cb("p7-r9", "mg:rw:%d:2" % g, adm, g, 73))
    check(count_active_warnings(g, bad) == 1 and APP.db.val("SELECT status FROM reports WHERE id=2") == "done", "warn via report")
    _p7_reply(rep, g, "/report", 14, 503, adm)
    s5 = session_find(g, "report", rep)[0]
    handle_update(_p2_cb("p7-r10", "rpt:%s:1" % s5["sid"], rep, g, 14))
    rid = APP.db.val("SELECT MAX(id) FROM reports")
    handle_update(_p2_cb("p7-r11", "mg:rb:%d:%d" % (g, rid), adm, g, 81))
    check(get_restriction(g, adm) is None and APP.db.val("SELECT status FROM reports WHERE id=?", (rid,)) == "open", "admin target must be protected")
    handle_update(_p2_cb("p7-r12", "mg:rj:%d:%d" % (g, rid), adm, g, 82))
    check(APP.db.val("SELECT status FROM reports WHERE id=?", (rid,)) == "rejected", "reject")
    return "ok"


@selftest("قوانین: مشاهده، ویرایش، ریست، فقط ادمین")
def _t_p7_rules():
    api, g = APP.api, -9771
    adm, u = 9771, 9772
    _p7_group(g, [adm], [u])
    _p4_text(u, g, "/rules", mid=1)
    check("هنوز قانونی" in _p3_last_text(api), "default rules text")
    handle_update(_p2_cb("p7-ru1", "mg:re:%d" % g, u, g, 2))
    check(input_get(g, u) is None, "non-admin edit")
    handle_update(_p2_cb("p7-ru2", "mg:re:%d" % g, adm, g, 3))
    handle_update(_p2_msg("abc", adm, g, mid=4))
    check(get_settings(g)["rules_text"] is None and input_get(g, adm) is not None, "too-short rules accepted")
    handle_update(_p2_msg("۱- احترام بگذارید\n۲- اسپم نزنید", adm, g, mid=5))
    check("احترام" in get_settings(g)["rules_text"] and input_get(g, adm) is None, "rules saved")
    _p4_text(u, g, "/rules", mid=6)
    check("احترام" in _p3_last_text(api) and "قوانین گروه" in _p3_last_text(api), "rules shown")
    handle_update(_p2_cb("p7-ru3", "mg:rr:%d" % g, u, g, 7))
    check(get_settings(g)["rules_text"] is not None, "non-admin reset rules")
    handle_update(_p2_cb("p7-ru4", "mg:rr:%d" % g, adm, g, 8))
    check(get_settings(g)["rules_text"] is None, "rules reset")
    return "ok"


def _p7_join(g, user_id, name="New", leave=False):
    msg = {"message_id": 1, "date": now(), "chat": {"id": g, "type": "group", "title": "G%d" % -g}, "from": {"id": 1, "first_name": "x"}}
    msg["left_chat_member" if leave else "new_chat_members"] = {"id": user_id, "first_name": name} if leave else [{"id": user_id, "first_name": name}]
    handle_update({"update_id": 9, "message": msg})


@selftest("خوشامد و خداحافظی: پیش‌فرض، خاموش/روشن، متن دلخواه، بن‌شده‌ی برگشتی")
def _t_p7_welcome():
    api, g = APP.api, -9781
    adm = 9781
    _p7_group(g, [adm])
    _p7_join(g, 9790, "Taha")
    check("خوش اومدی Taha" in _p3_last_text(api) and "جان سالم" in _p3_last_text(api), "default welcome")
    n = len(_p2_calls(api, "send_message"))
    _p7_join(g, 9790, "Taha", leave=True)
    check(len(_p2_calls(api, "send_message")) == n, "goodbye is OFF by default")
    handle_update(_p2_cb("p7-w1", "mg:wt:%d:goodbye_on" % g, adm, g, 1))
    _p7_join(g, 9791, "Sara", leave=True)
    check("Sara" in _p3_last_text(api) and "رفت" in _p3_last_text(api), "goodbye")
    handle_update(_p2_cb("p7-w2", "mg:we:%d" % g, adm, g, 2))
    handle_update(_p2_msg("سلام {name} به {title} خوش اومدی!", adm, g, mid=3))
    _p7_join(g, 9792, "Reza")
    check(_p3_last_text(api) == "سلام Reza به %s خوش اومدی!" % _title(g), "custom welcome: %r" % _p3_last_text(api))
    handle_update(_p2_cb("p7-w3", "mg:wt:%d:welcome_on" % g, adm, g, 4))
    n = len(_p2_calls(api, "send_message"))
    _p7_join(g, 9793, "Mina")
    check(len(_p2_calls(api, "send_message")) == n, "welcome OFF still sends")
    handle_update(_p2_cb("p7-w4", "mg:wd:%d" % g, adm, g, 5))
    check(get_settings(g)["welcome_text"] is None, "reset texts")
    set_setting(g, "welcome_on", 1)
    ban_user(g, 9794, adm)
    api.calls.clear()
    _p7_join(g, 9794, "Banned")
    check(not _p2_calls(api, "send_message") and any(c[0] == "ban_chat_member" for c in api.calls), "banned user rejoin")
    return "ok"


@selftest("پنل مدیریت: نمایش، تنظیمات، شدت، حد اخطار، کاربران، لاگ، پیوی")
def _t_p7_panel():
    api, g = APP.api, -9791
    adm, u, adm2 = 9791, 9792, 9793
    _p7_group(g, [adm, adm2], [u])
    STATS.user_touch(g, adm2)
    STATS.flush()
    check("menu:manage" in json.dumps(main_menu_markup()), "manage button missing from main menu")
    handle_update(_p2_cb("p7-p1", "menu:manage", u, g, 1))
    check(not _p2_calls(api, "edit_message_text"), "non-admin opened panel")
    handle_update(_p2_cb("p7-p2", "menu:manage", adm, g, 2))
    text = _p3_last_text(api, "edit_message_text", 2)
    for part in ("پنل مدیریت", "Anti Spam: ON", "Anti Link: ON", "Auto Mod: ON", "Warnings: 0", "Muted: 0", "Blocked: 0"):
        check(part in text, "panel misses %r\n%s" % (part, text))
    cbs = [b["callback_data"] for r in _p2_calls(api, "edit_message_text")[-1][1][3]["inline_keyboard"] for b in r]
    check(all(("mg:%s:%d" % (k, g)) in cbs for k in ("set", "us", "rp", "ru", "lg", "wl", "fw", "cl")), "panel buttons: %s" % cbs)
    handle_update(_p2_cb("p7-p3", "mg:t:%d:anti_spam" % g, adm, g, 3))
    check(get_settings(g)["anti_spam"] == 0, "toggle")
    handle_update(_p2_cb("p7-p4", "mg:t:%d:welcome_on" % g, adm, g, 4))
    check(get_settings(g)["welcome_on"] == 1, "non-moderation key toggled")
    for i, expect in enumerate((3, 3)):
        handle_update(_p2_cb("p7-p5-%d" % i, "mg:lv:%d:1" % g, adm, g, 10 + i))
        check(get_settings(g)["mod_level"] == expect, "level clamp")
    handle_update(_p2_cb("p7-p6", "mg:lv:%d:5" % g, adm, g, 20))
    check(get_settings(g)["mod_level"] == 3, "arbitrary level delta")
    handle_update(_p2_cb("p7-p7", "mg:wn:%d:-1" % g, adm, g, 21))
    check(get_settings(g)["warn_limit"] == 2, "warn limit")
    handle_update(_p2_cb("p7-p8", "mg:wn:%d:1" % -1, adm, g, 22))
    check(get_settings(g)["warn_limit"] == 2, "cross-group change")
    mute_user(g, 9801, 30, adm)
    ban_user(g, 9802, adm)
    add_warning(g, 9803, "x", adm)
    handle_update(_p2_cb("p7-p9", "mg:us:%d" % g, adm, g, 23))
    text = _p3_last_text(api, "edit_message_text", 2)
    check("اخطارها" in text and "ساکت‌ها" in text and "بلاک‌ها" in text, "users page")
    cbs = [b["callback_data"] for r in _p2_calls(api, "edit_message_text")[-1][1][3]["inline_keyboard"] for b in r]
    check("mg:um:%d:9801" % g in cbs and "mg:ub:%d:9802" % g in cbs and "mg:cw:%d:9803" % g in cbs, "user action buttons")
    handle_update(_p2_cb("p7-p10", "mg:ub:%d:9802" % g, adm, g, 24))
    handle_update(_p2_cb("p7-p11", "mg:cw:%d:9803" % g, adm, g, 25))
    check(get_restriction(g, 9802) is None and count_active_warnings(g, 9803) == 0, "page actions")
    handle_update(_p2_cb("p7-p12", "mg:lg:%d" % g, adm, g, 26))
    text = _p3_last_text(api, "edit_message_text", 2)
    check("سکوت" in text and "بن" in text and "اخطار" in text, "logs page\n" + text)
    handle_update(_p2_cb("p7-p13", "menu:manage", adm2, adm2, 30, ctype="private"))
    cbs = [b["callback_data"] for r in _p2_calls(api, "edit_message_text")[-1][1][3]["inline_keyboard"] for b in r]
    check("mg:o:%d" % g in cbs, "private picker: %s" % cbs)
    handle_update(_p2_cb("p7-p14", "mg:o:%d" % g, adm2, adm2, 31, ctype="private"))
    check("پنل مدیریت" in _p3_last_text(api, "edit_message_text", 2), "panel from private")
    handle_update(_p2_cb("p7-p15", "mg:t:%d:anti_link" % g, 9899, 9899, 32, ctype="private"))
    check(get_settings(g)["anti_link"] == 1, "stranger toggled from private")
    return "ok"


@selftest("پاکسازی: /clean با تعداد، ریپلای، سقف، دکمه‌ی پنل")
def _t_p7_clean():
    api, g = APP.api, -9801
    adm = 9801
    _p7_group(g, [adm])
    _p4_text(adm, g, "/clean 5", mid=100)
    ids = sorted(c[1][1] for c in _p7_deletes(api))
    check(ids == [95, 96, 97, 98, 99, 100], "ids deleted: %s" % ids)
    api.calls.clear()
    _p4_text(adm, g, "/clean", mid=200, reply_user=9802)
    upd = _p2_msg("/clean", adm, g, mid=300)
    upd["message"]["reply_to_message"] = {"message_id": 290, "from": {"id": 9802, "first_name": "x"}}
    api.calls.clear()
    handle_update(upd)
    ids = sorted(c[1][1] for c in _p7_deletes(api))
    check(ids == list(range(290, 300)) + [300], "reply range: %s" % ids)
    api.calls.clear()
    COOLDOWNS.clear()
    _p4_text(adm, g, "/clean 500", mid=1000)
    check(len(_p7_deletes(api)) == CLEAN_MAX + 1, "cap: %d" % len(_p7_deletes(api)))
    api.calls.clear()
    handle_update(_p2_cb("p7-c1", "mg:cn:%d:10" % g, adm, g, 2000))
    check(sorted(c[1][1] for c in _p7_deletes(api)) == list(range(1990, 2000)), "panel clean")
    api.calls.clear()
    handle_update(_p2_cb("p7-c2", "mg:cn:%d:10" % g, 9899, g, 2001))
    check(not _p7_deletes(api), "non-admin cleaned")
    return "ok"


# ══════════════════════════════════════════════════════════════════
# ███ END OF PHASE 7


# ══════════════════════════════════════════════════════════════════
# ███ PHASE 8 — LAB / EVENTS / SECURITY / CLEANUP
# ══════════════════════════════════════════════════════════════════

LAB_POLL_TTL = 600
LAB_TAP_TTL = 90
LAB_TAP_ROUNDS = 3
LAB_RIDDLE_TTL = 60
LAB_COOLDOWN = 45                 # فاصله‌ی دو آزمایش در یک گروه
LAB_XP_VOTE = 5
LAB_XP_TAP = (15, 8)
LAB_XP_SECRET = 30
LAB_DAILY_XP_ACTS = 10            # سقف دفعات XP آزمایشگاه برای هر نفر در روز
LAB_MAJORITY_MIN_MEMBERS = 3
TAP_EMOJIS = ["🍕", "🍔", "🍟", "🍦", "🍩", "🍎", "🍇", "🍉", "⚽", "🏀", "🎮", "🎲", "🚗", "✈️", "🐱", "🐶"]
GAME_TITLES["lab_riddle"] = "معمای گروهی"

POLL_BANK = [
    ("اگر این گروه یک کشور بود اسمش چی بود؟", ["جمهوری خنده‌ستان 😂", "پادشاهی بی‌خوابی 😴", "اتحادیه‌ی گشنه‌ها 🍕", "امپراتوری میم‌ها 🐸"]),
    ("اگر این گروه یک غذا بود چی بود؟", ["پیتزا 🍕", "آش رشته 🍜", "ساندویچ بی‌نظم 🥪", "سالاد (که کسی نمی‌خوره) 🥗"]),
    ("اگر این گروه یک وسیله‌ی نقلیه بود؟", ["پراید پرسرعت 🚗", "اتوبوس شلوغ 🚌", "دوچرخه‌ی پنچر 🚲", "موشک 🚀"]),
    ("گروه چه ساعتی بیشتر زنده‌ست؟", ["صبح ☀️", "ظهر 🍽️", "شب 🌙", "نصفه‌شب 🦉"]),
    ("اگه این گروه یه فیلم بود ژانرش چی بود؟", ["کمدی 😂", "ترسناک 👻", "اکشن 💥", "درام (همیشه) 😭"]),
    ("بهترین دسر دنیا؟", ["بستنی 🍦", "کیک 🍰", "شکلات 🍫", "حلوا"]),
    ("تعطیلات رویایی؟", ["دریا 🌊", "کوهستان ⛰️", "جنگل 🌲", "خونه و خواب 🛌"]),
    ("برای زنده موندن توی جزیره‌ی دورافتاده چی بردارید؟", ["اینترنت 📶", "غذا 🍞", "دوست 👥", "شارژر 🔋"]),
    ("اگه این گروه یه حیوان بود؟", ["گربه‌ی تنبل 🐱", "سگ پرانرژی 🐶", "طوطی پرحرف 🦜", "پاندا 🐼"]),
    ("بدترین حس دنیا؟", ["قطع شدن نت 📵", "باتری ۱٪ 🪫", "امتحان بدون درس 📚", "دیر رسیدن ⏰"]),
]
MAJORITY_BANK = ["بامزه‌ترین عضو گروه کیه؟", "کی بیشتر دیر جواب میده؟", "کی بیشتر از همه گشنه‌ست؟", "کی بیشتر وقت‌ها خوابه؟",
                 "بهترین گیمر گروه کیه؟", "کی بیشتر میم می‌فرسته؟", "کی بیشتر وقتش رو توی گوشی می‌گذرونه؟", "باهوش‌ترین عضو گروه کیه؟"]
SECRET_GOALS = [
    {"key": "games", "hint": "امروز گروه باید حسابی بازی کنه 🎮", "target": 15},
    {"key": "players", "hint": "همه باید توی بازی‌ها شرکت کنن 👥", "target": 6},
    {"key": "msgs", "hint": "امروز گروه باید پرحرف باشه 💬", "target": 60},
]


def lab_xp_ok(chat_id, user_id):
    """سقف روزانه‌ی XP آزمایشگاه برای هر نفر (ضد اسپم آزمایش)."""
    prefix = "labxp:%s:%d:%d:" % (day_key(), int(chat_id), int(user_id))
    n = APP.db.val("SELECT COUNT(*) FROM reward_ledger WHERE substr(token,1,?)=?", (len(prefix), prefix), 0)
    if n >= LAB_DAILY_XP_ACTS:
        return False
    reward_once(prefix + str(n + 1))
    return True


def random_members(chat_id, n, days=14):
    rows = APP.db.all("SELECT user_id FROM user_stats WHERE chat_id=? AND last_active>=? ORDER BY RANDOM() LIMIT ?",
                      (int(chat_id), now() - days * 86400, int(n)))
    return [r["user_id"] for r in rows]


def lab_gate(ctx):
    if not ctx.is_group:
        ctx.answer("🧪 آزمایشگاه مخصوص گروه‌هاست", True)
        return False
    return require_feature(ctx, "lab")


def lab_launch_ok(ctx, kind):
    if session_find(ctx.chat_id, game_kind(kind)):
        ctx.answer("🧪 یه آزمایش از همین نوع هنوز بازه!", True)
        return False
    if not cooldown_ok(("labc", ctx.chat_id), LAB_COOLDOWN):
        ctx.answer("⏳ بین آزمایش‌ها یکم صبر کنید", True)
        return False
    return True


def bar_pct(count, total, width=8):
    pct = int(round(100.0 * count / total)) if total else 0
    return "%s %d%%" % (bar(pct, width), pct), pct


# ─── 🎲 آزمایش تصادفی / 🗳️ رأی اکثریت (رأی‌گیری آماری) ───
def poll_counts(d):
    counts = [0] * len(d["opts"])
    for v in d["votes"].values():
        if 0 <= v < len(counts):
            counts[v] += 1
    return counts


def poll_text(d, final=False):
    counts = poll_counts(d)
    total = sum(counts)
    best = max(counts) if total else 0
    lines = ["🧪 آزمایشگاه — %s" % ("نتیجه" if final else "رأی‌گیری"), "", d["q"], ""]
    for o, c in zip(d["opts"], counts):
        b, _ = bar_pct(c, total)
        lines.append("%s%s\n   %s (%d)" % ("🏆 " if final and total and c == best else "▫️ ", o, b, c))
    lines.append("\n👥 %d نفر رأی دادن" % total)
    if not final:
        lines.append("هر نفر یه رأی داره و می‌تونه عوضش کنه • تا ۱۰ دقیقه یا زدن «🏁 پایان»")
    return "\n".join(lines)


def poll_markup(sid, d):
    rows = [[btn(o, "lp:%s:%d" % (sid, i))] for i, o in enumerate(d["opts"])]
    rows.append([btn("🏁 پایان", "lp:%s:e" % sid)])
    return kb(*rows)


def start_poll(ctx, kind):
    if not lab_gate(ctx) or not lab_launch_ok(ctx, "lab_poll"):
        return
    uids = []
    if kind == "maj":
        uids = random_members(ctx.chat_id, 4)
    if kind == "maj" and len(uids) >= LAB_MAJORITY_MIN_MEMBERS:
        q, opts = SR.choice(MAJORITY_BANK), [uname(u) for u in uids]
    else:
        q, opts = SR.choice(POLL_BANK)
        uids, kind = [], "rnd"
    d = {"mode": "poll", "kind": kind, "q": q, "opts": list(opts), "uids": uids, "votes": {}, "starter": int(ctx.user_id), "t0": now()}
    sid = game_start(ctx, "lab_poll", d, LAB_POLL_TTL, solo=False)
    res = ctx.send(poll_text(d), poll_markup(sid, d))
    s = session_get(sid)
    s["data"]["msg"] = (res or {}).get("message_id")
    game_save(s, s["data"])
    STATS.group_add(ctx.chat_id, "feat:lab")
    ctx.answer("🧪 شروع شد!")


def finalize_poll(s, ctx=None):
    d, chat = s["data"], s["chat_id"]
    session_del(s["sid"])
    notes = []
    for u in list(d["votes"]):
        if reward_once("poll:%s:%s" % (s["sid"], u)) and lab_xp_ok(chat, int(u)):
            add_xp(chat, int(u), LAB_XP_VOTE, notes)
    STATS.group_add(chat, "lab:polls")
    STATS.group_add(chat, "lab:votes", len(d["votes"]))
    text = poll_text(d, final=True) + ("\n\n⭐ هر شرکت‌کننده %d XP گرفت" % LAB_XP_VOTE if d["votes"] else "")
    markup = kb([btn("🧪 آزمایشگاه", "menu:lab"), btn("🏠 برگشت", "menu:home")])
    if ctx is not None and ctx.kind == "callback":
        ctx.edit(text, markup)
    else:
        _edit_msg(chat, d.get("msg"), text, markup)
    send_notes(chat, notes)


@callback("lp")
def cb_poll(ctx):
    try:
        sid, pick = ctx.parts[1], ctx.parts[2]
    except IndexError:
        ctx.answer("دکمه نامعتبر", True)
        return
    s = game_load(ctx, sid, "lab_poll")
    if s is None:
        return
    d = s["data"]
    if pick == "e":
        if int(ctx.user_id) != d["starter"] and not ctx.is_admin():
            ctx.answer("فقط شروع‌کننده یا ادمین می‌تونه تمومش کنه", True)
            return
        finalize_poll(s, ctx)
        return
    try:
        i = int(pick)
    except ValueError:
        ctx.answer("دکمه نامعتبر", True)
        return
    if not 0 <= i < len(d["opts"]):
        ctx.answer("گزینه نامعتبر", True)
        return
    key = str(ctx.user_id)
    changed = key in d["votes"]
    if changed and d["votes"][key] == i:
        ctx.answer("همین رو قبلاً انتخاب کردی 😉")
        return
    d["votes"][key] = i
    game_save(s, d)
    ctx.answer("🔁 رأیت عوض شد" if changed else "✅ رأیت ثبت شد")
    ctx.edit(poll_text(d), poll_markup(sid, d))


# ─── 🧩 معمای گروهی ───
def start_lab_riddle(ctx):
    if not lab_gate(ctx) or not lab_launch_ok(ctx, "lab_riddle"):
        return
    q, ok, wrong = SR.choice(RIDDLES)
    start_quiz(ctx, "lab_riddle", "🧩 معمای گروهی — همه می‌تونن جواب بدن!", build_choice(q, ok, wrong), "group_correct", mode="group",
               ttl=LAB_RIDDLE_TTL, extra_data={"answers": {}, "starter": int(ctx.user_id)}, new_message=True,
               extra_row=lambda sid: [btn("🏁 پایان", "gg:end:" + sid)])
    STATS.group_add(ctx.chat_id, "feat:lab")
    ctx.answer("🧩 شروع شد!")


# ─── ⚡ چالش سریع (چه کسی زودتر دکمه‌ی درست را می‌زند) ───
def _tap_new_round(d):
    d["opts"] = SR.sample(TAP_EMOJIS, 4)
    d["target"] = SR.choice(d["opts"])
    d["out"] = []


def tap_text(d):
    board = "، ".join("%s %d" % (uname(int(u)), n) for u, n in sorted(d["scores"].items(), key=lambda kv: -kv[1])[:5])
    return ("⚡ چالش سریع — راند %d از %d\n\nاولین نفری که روی «%s» بزنه امتیاز می‌گیره!%s"
            % (d["round"] + 1, LAB_TAP_ROUNDS, d["target"], ("\n\n📊 " + board) if board else ""))


def tap_markup(sid, d):
    return kb([btn(e, "tp:%s:%d:%d" % (sid, d["round"], i)) for i, e in enumerate(d["opts"])])


def post_tap_challenge(chat_id, starter=0):
    """چالش سریع را بدون نیاز به ctx می‌فرستد (برای دکمه‌ی آزمایشگاه و رویداد سریع)."""
    d = {"mode": "tap", "round": 0, "scores": {}, "owner": int(starter), "starter": int(starter), "t0": now()}
    _tap_new_round(d)
    sid = new_sid()
    session_put(sid, chat_id, starter, game_kind("lab_tap"), d, LAB_TAP_TTL)
    res = APP.api.try_call("sendMessage", {"chat_id": chat_id, "text": tap_text(d), "reply_markup": tap_markup(sid, d)}, retries=1)
    d["msg"] = (res or {}).get("message_id") if isinstance(res, dict) else None
    session_put(sid, chat_id, starter, game_kind("lab_tap"), d, LAB_TAP_TTL)
    return sid


def start_tap(ctx):
    if not lab_gate(ctx) or not lab_launch_ok(ctx, "lab_tap"):
        return
    post_tap_challenge(ctx.chat_id, ctx.user_id)
    STATS.group_add(ctx.chat_id, "feat:lab")
    ctx.answer("⚡ شروع شد!")


def finalize_tap(s, ctx=None):
    d, chat = s["data"], s["chat_id"]
    session_del(s["sid"])
    ranking = sorted(d["scores"].items(), key=lambda kv: (-kv[1], kv[0]))
    notes, lines = [], ["⚡ نتیجه‌ی چالش سریع", ""]
    if not ranking:
        lines.append("⏰ کسی شرکت نکرد.")
    for i, (u, n) in enumerate(ranking[:5]):
        xp_txt = ""
        if i < len(LAB_XP_TAP) and reward_once("tap:%s:%s" % (s["sid"], u)) and lab_xp_ok(chat, int(u)):
            add_xp(chat, int(u), LAB_XP_TAP[i], notes)
            xp_txt = " ⭐ +%d XP" % LAB_XP_TAP[i]
        lines.append("%s %s — %d امتیاز%s" % (MEDALS[i] if i < 3 else "▫️", uname(int(u)), n, xp_txt))
    STATS.group_add(chat, "lab:taps")
    markup = kb([btn("⚡ دوباره", "lb:tap"), btn("🧪 آزمایشگاه", "menu:lab")])
    if ctx is not None and ctx.kind == "callback":
        ctx.edit("\n".join(lines), markup)
    else:
        _edit_msg(chat, d.get("msg"), "\n".join(lines), markup)
    send_notes(chat, notes)


@callback("tp")
def cb_tap(ctx):
    try:
        sid, rnd, i = ctx.parts[1], int(ctx.parts[2]), int(ctx.parts[3])
    except (IndexError, ValueError):
        ctx.answer("دکمه نامعتبر", True)
        return
    s = game_load(ctx, sid, "lab_tap")
    if s is None:
        return
    d = s["data"]
    if rnd != d["round"]:
        ctx.answer("⏱️ دیر شد؛ راند بعدی رو بگیر!")
        return
    if not 0 <= i < len(d["opts"]):
        ctx.answer("گزینه نامعتبر", True)
        return
    uid = str(ctx.user_id)
    if uid in d["out"]:
        ctx.answer("این راند برای تو تموم شد 😅", True)
        return
    if d["opts"][i] != d["target"]:
        d["out"].append(uid)
        game_save(s, d)
        ctx.answer("❌ اشتباه! این راند برای تو تموم شد", True)
        return
    d["scores"][uid] = d["scores"].get(uid, 0) + 1
    d["round"] += 1
    if d["round"] >= LAB_TAP_ROUNDS:
        finalize_tap(s, ctx)
        return
    _tap_new_round(d)
    game_save(s, d)
    ctx.answer("✅ امتیاز تو!")
    ctx.edit(tap_text(d), tap_markup(sid, d))


# ─── 🕵️ مأموریت مخفی گروه ───
def secret_goal_today(chat_id):
    return daily_pick(chat_id, SECRET_GOALS, "labsecret")


def secret_progress(chat_id, goal):
    cid, t0 = int(chat_id), day_start()
    if goal["key"] == "games":
        cur = APP.db.val("SELECT COUNT(*) FROM game_records WHERE chat_id=? AND created_at>=?", (cid, t0), 0)
    elif goal["key"] == "players":
        cur = APP.db.val("SELECT COUNT(DISTINCT user_id) FROM game_records WHERE chat_id=? AND created_at>=?", (cid, t0), 0)
    else:
        cur = gget(cid, "day:%s:msgs" % day_key())
    return cur, min(100, int(100 * cur / goal["target"]))


def show_secret(ctx):
    if not lab_gate(ctx):
        return
    cid = int(ctx.chat_id)
    goal = secret_goal_today(cid)
    cur, pct = secret_progress(cid, goal)
    back = kb([btn("🔄", "lb:sec"), btn("🧪 آزمایشگاه", "menu:lab")])
    if pct < 100:
        show(ctx, "🕵️ مأموریت مخفی امروز\n\nیه هدف پنهون برای کل گروه هست!\n💡 سرنخ: %s\n\n📈 پیشرفت: %s\n\nجزئیاتش مخفیه؛ فقط درصد رو می‌بینی 😉"
             % (goal["hint"], bar_pct(cur, goal["target"], 10)[0]), back)
        return
    players = [r["user_id"] for r in APP.db.all(
        "SELECT DISTINCT user_id FROM game_records WHERE chat_id=? AND created_at>=? LIMIT 30", (cid, day_start()))]
    if goal["key"] == "msgs":
        players = [r["user_id"] for r in APP.db.all("SELECT user_id FROM user_stats WHERE chat_id=? AND last_active>=? LIMIT 30", (cid, day_start()))]
    if not reward_once("labsec:%s:%d" % (day_key(), cid)):
        show(ctx, "🕵️ مأموریت مخفی امروز قبلاً انجام شده ✅\nفردا یه هدف مخفی جدید داریم!", back)
        return
    notes = ["🕵️ مأموریت مخفی امروز انجام شد! هدف: %s" % goal["hint"]]
    for u in players:
        add_xp(cid, u, LAB_XP_SECRET, notes)
    notes.append("⭐ هر مشارکت‌کننده %d XP گرفت (%d نفر)" % (LAB_XP_SECRET, len(players)))
    STATS.group_add(cid, "lab:secrets")
    show(ctx, "🕵️ مأموریت مخفی امروز کشف شد! 🎉\n💡 %s\n⭐ %d نفر %d XP گرفتن" % (goal["hint"], len(players), LAB_XP_SECRET), back)
    send_notes(cid, notes)


# ─── منوی آزمایشگاه ───
def lab_markup():
    return kb([btn("🎲 آزمایش تصادفی", "lb:rnd"), btn("🗳️ رأی اکثریت", "lb:maj")],
              [btn("🧩 معمای گروهی", "lb:rid"), btn("⚡ چالش سریع", "lb:tap")],
              [btn("🕵️ مأموریت مخفی", "lb:sec")], home_row())


@section("lab", feature="lab")
def sec_lab(ctx):
    show(ctx, "🧪 آزمایشگاه\n\nآزمایش‌های گروهی: رأی‌گیری، معما، چالش و یه مأموریت مخفی. یکی رو انتخاب کن 👇", lab_markup())


@callback("lb")
def cb_lab(ctx):
    a = ctx.parts[1] if len(ctx.parts) > 1 else ""
    actions = {"rnd": lambda: start_poll(ctx, "rnd"), "maj": lambda: start_poll(ctx, "maj"), "rid": lambda: start_lab_riddle(ctx),
               "tap": lambda: start_tap(ctx), "sec": lambda: show_secret(ctx)}
    if a not in actions:
        ctx.answer("دکمه نامعتبر", True)
        return
    actions[a]()


# ─── ⚡ رویدادها ───
EVENT_TYPES = {
    "xp2": ("⚡ XP بازی‌ها ×2 شد!", 2.0, 30),
    "xp15": ("🔥 XP بازی‌ها ×1.5 شد!", 1.5, 45),
    "quick": ("🎲 رویداد سریع فعال شد! XP ×1.5 و یه چالش سریع", 1.5, 15),
}
EVENT_CHECK_SECONDS = 300
EVENT_CHANCE = 0.2
EVENT_MIN_GAP = 3 * 3600
EVENT_MAX_PER_DAY = 2
EVENT_MIN_ACTIVE_USERS = 3
EVENT_ACTIVE_WINDOW = 20 * 60


def active_events(chat_id):
    t = now()
    return APP.db.all("SELECT * FROM events WHERE chat_id IN (?, 0) AND active=1 AND starts_at<=? AND ends_at>? ORDER BY ends_at",
                      (int(chat_id), t, t))


def event_line(chat_id):
    ev = active_events(chat_id)
    if not ev:
        return ""
    e = ev[0]
    return "\n⚡ رویداد: %s (تا %s)" % (e["title"], iran_dt(e["ends_at"]).strftime("%H:%M"))


_home_text_base = home_text
_hub_text_base = hub_text


def home_text(ctx):  # noqa: F811 — همان منوی فاز ۲ + خط رویداد
    text = _home_text_base(ctx)
    return text + (event_line(ctx.chat_id) if ctx.is_group else "")


def hub_text(ctx):  # noqa: F811
    return _hub_text_base(ctx) + (event_line(ctx.chat_id) if ctx.is_group else "")


def launch_event(chat_id, ekey):
    title, mult, minutes = EVENT_TYPES[ekey]
    t = now()
    APP.db.run("INSERT INTO events(chat_id, ekey, title, multiplier, starts_at, ends_at, active, created_at) VALUES(?,?,?,?,?,?,1,?)",
               (int(chat_id), ekey, title, mult, t, t + minutes * 60, t))
    STATS.group_add(chat_id, "events")
    send_notes(chat_id, ["%s\n⏳ تا %d دقیقه" % (title, minutes)])
    if ekey == "quick":
        post_tap_challenge(chat_id, 0)


def can_launch_event(chat_id):
    cid, t = int(chat_id), now()
    if not get_settings(cid).get("games"):
        return False
    if APP.db.val("SELECT COUNT(*) FROM events WHERE chat_id=? AND starts_at>=?", (cid, day_start(t)), 0) >= EVENT_MAX_PER_DAY:
        return False
    if APP.db.val("SELECT COALESCE(MAX(starts_at),0) FROM events WHERE chat_id=?", (cid,), 0) > t - EVENT_MIN_GAP:
        return False
    return APP.db.val("SELECT COUNT(*) FROM user_stats WHERE chat_id=? AND last_active>=?", (cid, t - EVENT_ACTIVE_WINDOW), 0) >= EVENT_MIN_ACTIVE_USERS


@periodic(EVENT_CHECK_SECONDS, "events")
def _task_events():
    t = now()
    for e in APP.db.all("SELECT id, chat_id, title FROM events WHERE active=1 AND ends_at<=?", (t,)):
        APP.db.run("UPDATE events SET active=0 WHERE id=?", (e["id"],))
        if e["chat_id"]:
            send_notes(e["chat_id"], ["⏳ رویداد «%s» تموم شد." % e["title"]])
    for g in APP.db.all("SELECT chat_id FROM groups WHERE active=1"):
        if can_launch_event(g["chat_id"]) and random.random() < EVENT_CHANCE:
            launch_event(g["chat_id"], random.choice(list(EVENT_TYPES)))


# ─── SELFTEST فاز ۸ ───
def _p8_users(g, n, base=9000):
    for i in range(n):
        upsert_user({"id": base + i, "first_name": "M%d" % (base + i)})
        STATS.user_touch(g, base + i)
    STATS.flush()


def _p8_texts(api):
    return [c[1][1] for c in api.calls if c[0] == "send_message"]


@selftest("آزمایشگاه: منو، فقط گروه، خاموش بودن")
def _t_p8_lab_menu():
    api, g = APP.api, -9811
    upsert_group({"id": g, "title": "L", "type": "group"})
    handle_update(_p2_cb("p8-l0", "menu:fun", 9811, g, 1))
    fun_cbs = [b["callback_data"] for r in _p2_calls(api, "edit_message_text")[-1][1][3]["inline_keyboard"] for b in r]
    check("menu:lab" in fun_cbs, "lab button missing from the fun menu: %s" % fun_cbs)
    handle_update(_p2_cb("p8-l1", "menu:lab", 9811, g, 1))
    cbs = [b["callback_data"] for r in _p2_calls(api, "edit_message_text")[-1][1][3]["inline_keyboard"] for b in r]
    check(all(("lb:" + k) in cbs for k in ("rnd", "maj", "rid", "tap", "sec")), "lab buttons: %s" % cbs)
    n = len(_p2_calls(api, "send_message"))
    handle_update(_p2_cb("p8-l2", "lb:rnd", 9812, 9812, 1, ctype="private"))
    check(len(_p2_calls(api, "send_message")) == n, "lab started in private chat")
    set_setting(g, "lab", 0)
    handle_update(_p2_cb("p8-l3", "lb:rnd", 9813, g, 2))
    handle_update(_p2_cb("p8-l4", "menu:lab", 9813, g, 3))
    check(len(_p2_calls(api, "send_message")) == n and len(_p2_calls(api, "edit_message_text")) == 2, "lab OFF still works")
    return "ok"


@selftest("آزمایش تصادفی: رأی، تغییر رأی، نتیجه‌ی آماری، XP یک‌باره، Timeout")
def _t_p8_poll():
    api, g = APP.api, -9821
    upsert_group({"id": g, "title": "P", "type": "group"})
    handle_update(_p2_cb("p8-p1", "lb:rnd", 9821, g, 1))
    s = _p4_sess(g, "lab_poll")
    check(s and s["data"]["mode"] == "poll" and s["data"]["msg"] is not None and len(s["data"]["opts"]) == 4, "poll not started")
    handle_update(_p2_cb("p8-p2", "lb:rnd", 9822, g, 2))
    check(len(session_find(g, game_kind("lab_poll"))) == 1, "two polls at once")
    sid = s["sid"]
    for u, i in ((9821, 0), (9822, 0), (9823, 1)):
        handle_update(_p2_cb("p8-v%d" % u, "lp:%s:%d" % (sid, i), u, g, 10 + u))
    check(poll_counts(_p4_sess(g, "lab_poll")["data"]) == [2, 1, 0, 0], "counts")
    handle_update(_p2_cb("p8-p3", "lp:%s:0" % sid, 9823, g, 30))
    handle_update(_p2_cb("p8-p4", "lp:%s:2" % sid, 9823, g, 31))
    check(poll_counts(_p4_sess(g, "lab_poll")["data"]) == [2, 0, 1, 0], "vote change")
    handle_update(_p2_cb("p8-p5", "lp:%s:9" % sid, 9824, g, 32))
    handle_update(_p2_cb("p8-p6", "lp:%s:e" % sid, 9825, g, 33))
    check(_p4_sess(g, "lab_poll") is not None, "stranger ended poll")
    xp_before = user_stat_get(g, 9821, "xp")
    handle_update(_p2_cb("p8-p7", "lp:%s:e" % sid, 9821, g, 34))
    check(_p4_sess(g, "lab_poll") is None, "starter could not end poll")
    text = _p3_last_text(api, "edit_message_text", 2)
    check("نتیجه" in text and "67%" in text and "33%" in text.replace("33%", "33%") and "🏆" in text, "result text\n" + text)
    check(user_stat_get(g, 9821, "xp") == xp_before + LAB_XP_VOTE and user_stat_get(g, 9823, "xp") == LAB_XP_VOTE, "poll XP")
    handle_update(_p2_cb("p8-p8", "lp:%s:e" % sid, 9821, g, 35))
    check(user_stat_get(g, 9821, "xp") == xp_before + LAB_XP_VOTE, "poll XP twice")
    COOLDOWNS.clear()
    handle_update(_p2_cb("p8-p9", "lb:rnd", 9821, g, 40))
    s = _p4_sess(g, "lab_poll")
    handle_update(_p2_cb("p8-p10", "lp:%s:1" % s["sid"], 9826, g, 41))
    APP.db.run("UPDATE sessions SET expires_at=? WHERE sid=?", (now() - 1, s["sid"]))
    _task_game_sweep()
    check(_p4_sess(g, "lab_poll") is None and user_stat_get(g, 9826, "xp") == LAB_XP_VOTE, "poll timeout must finalize with results")
    return "ok"


@selftest("رأی اکثریت: گزینه‌ها اعضای فعال‌اند؛ با اعضای کم جایگزین می‌شود")
def _t_p8_majority():
    api, g = APP.api, -9831
    upsert_group({"id": g, "title": "J", "type": "group"})
    handle_update(_p2_cb("p8-m1", "lb:maj", 9831, g, 1))
    check(_p4_sess(g, "lab_poll")["data"]["kind"] == "rnd", "too few members must fall back to a normal poll")
    session_del(_p4_sess(g, "lab_poll")["sid"])
    COOLDOWNS.clear()
    _p8_users(g, 5)
    handle_update(_p2_cb("p8-m2", "lb:maj", 9831, g, 2))
    d = _p4_sess(g, "lab_poll")["data"]
    check(d["kind"] == "maj" and d["q"] in MAJORITY_BANK and len(d["opts"]) == 4 and len(set(d["uids"])) == 4, "majority poll %s" % d)
    check(all(o.startswith("M") for o in d["opts"]), "options must be member names: %s" % d["opts"])
    return "ok"


@selftest("معمای گروهی: پاسخ همه، پایان، XP، Timeout")
def _t_p8_lab_riddle():
    api, g = APP.api, -9841
    upsert_group({"id": g, "title": "R", "type": "group"})
    for u in (9841, 9842, 9843):
        ADMIN_CACHE[(g, u)] = (False, time.monotonic() + 300)
    handle_update(_p2_cb("p8-r1", "lb:rid", 9841, g, 1))
    s = _p4_sess(g, "lab_riddle")
    check(s and s["data"]["mode"] == "group", "riddle not started")
    ans = s["data"]["ans"]
    handle_update(_p2_cb("p8-r2", "qz:%s:%d" % (s["sid"], ans), 9842, g, 2))
    handle_update(_p2_cb("p8-r3", "qz:%s:%d" % (s["sid"], (ans + 1) % 4), 9843, g, 2))
    handle_update(_p2_cb("p8-r4", "gg:end:" + s["sid"], 9842, g, 3))
    check(_p4_sess(g, "lab_riddle") is not None, "non-starter ended the round")
    handle_update(_p2_cb("p8-r5", "gg:end:" + s["sid"], 9841, g, 4))
    check(_p4_sess(g, "lab_riddle") is None, "not finalized")
    check(_p4_results(g, 9842, "lab_riddle") == [{"result": "win", "xp": GAME_XP["group_correct"] + GAME_XP["group_first"]}], "winner")
    check(_p4_results(g, 9843, "lab_riddle")[0]["result"] == "loss", "wrong answer")
    check(not _p4_results(g, 9842, "group_game"), "lab riddle recorded as normal group game")
    COOLDOWNS.clear()
    handle_update(_p2_cb("p8-r6", "lb:rid", 9841, g, 10))
    s = _p4_sess(g, "lab_riddle")
    handle_update(_p2_cb("p8-r7", "qz:%s:%d" % (s["sid"], s["data"]["ans"]), 9842, g, 11))
    APP.db.run("UPDATE sessions SET expires_at=? WHERE sid=?", (now() - 1, s["sid"]))
    _task_game_sweep()
    check(_p4_sess(g, "lab_riddle") is None and len(_p4_results(g, 9842, "lab_riddle")) == 2, "timeout finalize")
    return "ok"


@selftest("چالش سریع: راندها، حذف خطاکار، راند قدیمی، نتیجه و XP")
def _t_p8_tap():
    api, g = APP.api, -9851
    upsert_group({"id": g, "title": "T", "type": "group"})
    handle_update(_p2_cb("p8-t1", "lb:tap", 9851, g, 1))
    s = _p4_sess(g, "lab_tap")
    check(s and s["data"]["mode"] == "tap" and len(set(s["data"]["opts"])) == 4 and s["data"]["target"] in s["data"]["opts"], "tap start")
    sid, mid = s["sid"], 10
    for rnd in range(LAB_TAP_ROUNDS):
        d = _p4_sess(g, "lab_tap")["data"]
        right = d["opts"].index(d["target"])
        wrong = (right + 1) % 4
        mid += 1
        handle_update(_p2_cb("p8-t-w%d" % rnd, "tp:%s:%d:%d" % (sid, rnd, wrong), 9852, g, mid))
        handle_update(_p2_cb("p8-t-w2%d" % rnd, "tp:%s:%d:%d" % (sid, rnd, right), 9852, g, mid + 100))
        d = _p4_sess(g, "lab_tap")["data"]
        check(d["scores"].get("9852", 0) == 0 and "9852" in d["out"], "eliminated player scored in round %d" % rnd)
        if rnd == 1:
            handle_update(_p2_cb("p8-t-old", "tp:%s:%d:%d" % (sid, 0, 0), 9853, g, 300))
            check(_p4_sess(g, "lab_tap")["data"]["scores"].get("9853", 0) == 0, "stale round counted")
        handle_update(_p2_cb("p8-t-r%d" % rnd, "tp:%s:%d:%d" % (sid, rnd, right), 9851 if rnd < 2 else 9853, g, mid + 200))
    check(_p4_sess(g, "lab_tap") is None, "tap not finished")
    text = _p3_last_text(api, "edit_message_text", 2)
    check("نتیجه‌ی چالش سریع" in text and "+%d XP" % LAB_XP_TAP[0] in text, "result\n" + text)
    check(user_stat_get(g, 9851, "xp") == LAB_XP_TAP[0] and user_stat_get(g, 9853, "xp") == LAB_XP_TAP[1], "tap XP")
    check(user_stat_get(g, 9852, "xp") == 0, "eliminated player got XP")
    COOLDOWNS.clear()
    handle_update(_p2_cb("p8-t2", "lb:tap", 9851, g, 400))
    s = _p4_sess(g, "lab_tap")
    APP.db.run("UPDATE sessions SET expires_at=? WHERE sid=?", (now() - 1, s["sid"]))
    _task_game_sweep()
    check(_p4_sess(g, "lab_tap") is None and "کسی شرکت نکرد" in json.dumps([c[1][1] for c in api.calls if c[0] == "try_call"], ensure_ascii=False), "tap timeout")
    return "ok"


@selftest("مأموریت مخفی گروه: پیشرفت، پاداش یک‌بار در روز برای همه‌ی مشارکت‌کننده‌ها")
def _t_p8_secret():
    api, g = APP.api, -9861
    upsert_group({"id": g, "title": "S", "type": "group"})
    goal = secret_goal_today(g)
    check(goal in SECRET_GOALS and secret_goal_today(g) == goal, "goal must be stable within a day")
    handle_update(_p2_cb("p8-s1", "lb:sec", 9861, g, 1))
    check("پیشرفت" in _p3_last_text(api, "edit_message_text", 2) and "سرنخ" in _p3_last_text(api, "edit_message_text", 2), "progress page")
    check(str(goal["target"]) not in _p3_last_text(api, "edit_message_text", 2), "secret target must stay hidden")
    t = now()
    for i in range(goal["target"]):
        APP.db.run("INSERT INTO game_records(chat_id, user_id, game_key, result, score, xp, created_at) VALUES(?,?,?,?,?,?,?)",
                   (g, 9870 + i, "dice", "win", 0, 0, t))
    _p6_set(g, **{"day__%s__msgs" % day_key(): goal["target"]})
    for i in range(goal["target"]):
        STATS.user_touch(g, 9870 + i)
    STATS.flush()
    cur, pct = secret_progress(g, goal)
    check(pct == 100, "progress %s" % ((cur, pct),))
    handle_update(_p2_cb("p8-s2", "lb:sec", 9861, g, 2))
    check("کشف شد" in _p3_last_text(api, "edit_message_text", 2), "completion page")
    first = 9870
    check(user_stat_get(g, first, "xp") == LAB_XP_SECRET, "participant XP: %d" % user_stat_get(g, first, "xp"))
    handle_update(_p2_cb("p8-s3", "lb:sec", 9862, g, 3))
    check("قبلاً انجام شده" in _p3_last_text(api, "edit_message_text", 2) and user_stat_get(g, first, "xp") == LAB_XP_SECRET, "second claim paid again")
    return "ok"


@selftest("رویدادها: شرایط پخش، ×XP، سقف روزانه، فاصله، پایان، رویداد سریع")
def _t_p8_events():
    api, g = APP.api, -9871
    upsert_group({"id": g, "title": "E", "type": "group"})
    check(not can_launch_event(g), "event with no active users")
    _p8_users(g, EVENT_MIN_ACTIVE_USERS, base=9880)
    check(can_launch_event(g), "should be eligible")
    set_setting(g, "games", 0)
    check(not can_launch_event(g), "games OFF must block events")
    set_setting(g, "games", 1)
    old_random, old_choice = random.random, random.choice
    random.random = lambda: 0.0
    random.choice = lambda seq: "xp2" if isinstance(seq, list) and "xp2" in seq else old_choice(seq)
    try:
        _task_events()
    finally:
        random.random, random.choice = old_random, old_choice
    ev = active_events(g)
    check(len(ev) == 1 and ev[0]["ekey"] == "xp2" and ev[0]["multiplier"] == 2.0, "event not launched")
    check(any("×2" in t for t in _p5_notes(api)), "announcement")
    check("رویداد" in event_line(g) and "رویداد" in hub_text(Ctx("message", {"id": g, "type": "group"}, {"id": 1}, {})), "event line")
    saved = list(GAME_RESULT_HOOKS)
    GAME_RESULT_HOOKS[:] = []
    try:
        check(record_result(g, 9880, "dice", "win", 1, 20, token="ev1")["xp"] == 40, "XP x2 during event")
    finally:
        GAME_RESULT_HOOKS[:] = saved
    check(not can_launch_event(g), "min gap ignored")
    APP.db.run("UPDATE events SET starts_at=?, ends_at=? WHERE chat_id=?", (now() - 4 * 3600, now() - 3 * 3600, g))
    APP.db.run("INSERT INTO events(chat_id, ekey, title, multiplier, starts_at, ends_at, active, created_at) VALUES(?,?,?,?,?,?,0,?)",
               (g, "xp2", "x", 2, now() - 5 * 3600, now() - 4 * 3600, now()))
    check(not can_launch_event(g), "daily cap ignored")
    n = len(_p5_notes(api))
    _task_events()
    check(not active_events(g) and any("تموم شد" in t for t in _p5_notes(api)[n:]), "event end")
    APP.db.run("DELETE FROM events")
    launch_event(g, "quick")
    check(_p4_sess(g, "lab_tap") is not None, "quick event must post a tap challenge")
    return "ok"


@selftest("Cleanup کامل: موقت‌ها پاک، XP/مدال/رکورد/آمار مهم می‌مانند")
def _t_p8_cleanup():
    g, u, t = -9881, 9881, now()
    old = t - 400 * 86400
    APP.db.run("INSERT INTO user_stats(chat_id, user_id, xp, level) VALUES(?,?,500,5)", (g, u))
    APP.db.run("INSERT INTO user_achievements(chat_id, user_id, akey, earned_at) VALUES(?,?,'first_win',?)", (g, u, old))
    APP.db.run("INSERT INTO records(chat_id, rkey, user_id, value, ts) VALUES(?,?,?,?,?)", (g, "best_streak", u, 12, old))
    APP.db.run("INSERT INTO group_stats(chat_id, skey, svalue) VALUES(?,?,?)", (g, "game_plays", 99))
    APP.db.run("INSERT INTO warnings(chat_id, user_id, reason, by_id, created_at) VALUES(?,?,?,?,?)", (g, u, "x", 0, old))
    APP.db.run("INSERT INTO warnings(chat_id, user_id, reason, by_id, created_at) VALUES(?,?,?,?,?)", (g, u, "y", 0, t))
    APP.db.run("INSERT INTO reports(chat_id, reporter_id, target_id, target_message_id, reason, status, created_at) VALUES(?,?,?,?,?,?,?)", (g, u, u, 1, "spam", "done", old))
    APP.db.run("INSERT INTO reports(chat_id, reporter_id, target_id, target_message_id, reason, status, created_at) VALUES(?,?,?,?,?,?,?)", (g, u, u, 2, "spam", "open", old))
    APP.db.run("INSERT INTO game_records(chat_id, user_id, game_key, result, score, xp, created_at) VALUES(?,?,?,?,?,?,?)", (g, u, "dice", "win", 0, 0, old))
    APP.db.run("INSERT INTO game_records(chat_id, user_id, game_key, result, score, xp, created_at) VALUES(?,?,?,?,?,?,?)", (g, u, "dice", "win", 0, 0, t))
    APP.db.run("INSERT INTO league_scores(chat_id, week_key, user_id, points) VALUES(?,?,?,?)", (g, week_key(old), u, 50))
    APP.db.run("INSERT INTO league_scores(chat_id, week_key, user_id, points) VALUES(?,?,?,?)", (g, week_key(), u, 60))
    APP.db.run("INSERT INTO events(chat_id, ekey, title, multiplier, starts_at, ends_at, active, created_at) VALUES(?,?,?,?,?,?,1,?)", (g, "xp2", "x", 2, t - 100, t - 50, t - 100))
    session_put("c-old", g, u, "game:x", {}, ttl=-(SESSION_GRACE_SECONDS + 10))
    session_put("c-new", g, u, "game:x", {}, ttl=500)
    APP.db.many("INSERT INTO logs(chat_id, kind, actor_id, target_id, detail, ts) VALUES(?,?,?,?,?,?)",
                [(g, "mod_flood", 0, u, "n%d" % i, t) for i in range(MAX_LOGS_PER_CHAT + 100)])
    out = run_cleanup()
    check(out["logs_capped"] == 100 and APP.db.val("SELECT COUNT(*) FROM logs WHERE chat_id=?", (g,)) == MAX_LOGS_PER_CHAT, "log cap %s" % out)
    check(APP.db.val("SELECT detail FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT 1", (g,)) == "n%d" % (MAX_LOGS_PER_CHAT + 99), "newest log must stay")
    check(out["warnings"] == 1 and out["reports"] == 1 and out["game_records"] == 1 and out["league"] == 1 and out["sessions"] == 1, "cleanup counts %s" % out)
    check(APP.db.val("SELECT COUNT(*) FROM reports WHERE status='open'") == 1, "open report must stay")
    check(APP.db.val("SELECT active FROM events WHERE chat_id=?", (g,)) == 0, "ended event still active")
    check(APP.db.val("SELECT xp FROM user_stats WHERE chat_id=?", (g,)) == 500, "XP lost")
    check(APP.db.val("SELECT COUNT(*) FROM user_achievements") == 1 and APP.db.val("SELECT COUNT(*) FROM records") == 1, "medals/records lost")
    check(APP.db.val("SELECT svalue FROM group_stats WHERE chat_id=? AND skey='game_plays'", (g,)) == 99, "important stats lost")
    check(session_get_raw("c-new") is not None and session_get_raw("c-old") is None, "sessions")
    return "ok"


def _p8_table_snapshot():
    return {r["name"]: APP.db.val("SELECT COUNT(*) FROM %s" % r["name"]) for r in
            APP.db.all("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


@selftest("امنیت: SQL Injection از همه‌ی ورودی‌های متنی")
def _t_p8_sql_injection():
    g, adm, u = -9891, 9891, 9892
    _p7_group(g, [adm], [u])
    evil = "x'); DROP TABLE users; DELETE FROM group_settings; --"
    for k in ("anti_flood", "anti_spam", "anti_link", "filter_enabled"):
        set_setting(g, k, 0)
    before = _p8_table_snapshot()
    handle_update(_p2_cb("p8-i1", "jk:sc:school", u, g, 1))
    handle_update(_p2_msg("جوک تستی طولانی " + evil, u, g, mid=2))
    for kind, cb in (("rules_edit", "mg:re:%d" % g), ("welcome_edit", "mg:we:%d" % g), ("filter_add", "mg:fa:%d" % g), ("occasion", "mm:add")):
        handle_update(_p2_cb("p8-i-" + kind, cb, adm, g, 10))
        handle_update(_p2_msg(evil + " 7/15" if kind == "occasion" else evil, adm, g, mid=11))
    _p4_text(adm, g, "/warn " + evil, mid=12, reply_user=u)
    handle_update(_p2_cb("p8-i2", "jk:n:" + "x'; DROP TABLE jokes;--"[:30], u, g, 3))
    handle_update(_p2_cb("p8-i3", "mg:t:%d:anti_spam'; DROP TABLE logs;--" % g, adm, g, 4))
    upsert_user({"id": u, "first_name": evil, "username": "u'--"})  # بعد از پیام‌ها تا اسم دوباره با نام تستی عوض نشود
    _p4_text(u, g, "/profile", mid=13)
    after = _p8_table_snapshot()
    check(set(before) == set(after), "a table disappeared: %s" % (set(before) - set(after)))
    check(APP.db.val("SELECT COUNT(*) FROM group_settings WHERE chat_id=?", (g,)) == 1, "group_settings wiped")
    check(get_settings(g)["rules_text"] == evil, "rules stored literally")
    check(APP.db.val("SELECT first_name FROM users WHERE user_id=?", (u,)) == evil, "name stored literally")
    check(any(evil.startswith("x'") and r["text"].endswith("--") for r in APP.db.all("SELECT text FROM jokes WHERE status='pending'")), "joke stored literally")
    return "ok"


@selftest("امنیت: Fuzz همه‌ی Callbackها و دستورها (داده‌ی خراب نباید Exception بدهد)")
def _t_p8_fuzz():
    g = -9901
    _p7_group(g)
    seed_content()
    seed_missions()
    seed_achievements()
    garbage = ["", "x", ":", "::", "a:b:c:d:e:f", "-1", "0", "9" * 18, "x" * 40, "ا" * 12, "1:2:3", "-100:5", "%s", "'; DROP TABLE users;--", "None", "💥", "0:0:0", "-9901", "-9901:1", "g1234abcd:99"]
    n, problems = 0, []

    def clip(s):
        return s.encode("utf-8")[:64].decode("utf-8", "ignore")

    for prefix in sorted(CALLBACKS):
        for gb in garbage:
            for ctype in ("group", "private"):
                n += 1
                uid = 70000 + n
                if n % 2:
                    ADMIN_CACHE[(g, uid)] = (True, time.monotonic() + 300)
                chat = g if ctype == "group" else uid
                try:
                    handle_update(_p2_cb("fz-%d" % n, clip(prefix + ":" + gb), uid, chat, 100 + n, ctype=ctype))
                except Exception as e:  # noqa: BLE001
                    problems.append("callback %r: %s: %s" % (clip(prefix + ":" + gb), type(e).__name__, e))
    for cmd in sorted(COMMANDS):
        for gb in garbage + ["@nobody 5", "@nobody", "٣٠ دقیقه"]:
            for ctype in ("group", "private"):
                n += 1
                uid = 80000 + n
                if n % 2:
                    ADMIN_CACHE[(g, uid)] = (True, time.monotonic() + 300)
                chat = g if ctype == "group" else uid
                try:
                    handle_update(_p2_msg("/%s %s" % (cmd, gb), uid, chat, ctype=ctype, mid=200 + n))
                except Exception as e:  # noqa: BLE001
                    problems.append("command /%s %r: %s: %s" % (cmd, gb, type(e).__name__, e))
    for text in ["", "🙂", "\u202e" * 5, "ا" * 4000, "\n" * 50, "😀" * 300, "کی " * 100, "سلام" + "\u200c" * 200, "/" * 50, "@" * 50]:
        for ctype in ("group", "private"):
            n += 1
            uid = 90000 + n
            try:
                handle_update(_p2_msg(text, uid, g if ctype == "group" else uid, ctype=ctype, mid=300 + n))
            except Exception as e:  # noqa: BLE001
                problems.append("message %r: %s: %s" % (text[:20], type(e).__name__, e))
    check(not problems, "%d problems, first: %s" % (len(problems), problems[:3]))
    return "%d inputs" % n


@selftest("امنیت: توکن هرگز در خطا یا لاگ نمی‌آید")
def _t_p8_token():
    global BOT_TOKEN
    old = BOT_TOKEN
    BOT_TOKEN = "123456:SECRETTOKENVALUE"
    try:
        class Boom:
            headers = {}

            def post(self, url, json=None, timeout=None):
                raise requests.ConnectionError("HTTPSConnectionPool: Max retries exceeded with url: /bot123456:SECRETTOKENVALUE/getUpdates")

        api = BaleAPI(BOT_TOKEN)
        api.s = Boom()
        try:
            api.call("getUpdates", retries=1)
            raise AssertionError("no error raised")
        except BaleAPIError as e:
            check("SECRETTOKENVALUE" not in str(e) and "SECRET" not in e.desc, "token in error: %s" % e)
        stream = []
        h = logging.Handler()
        h.emit = lambda rec: stream.append(RedactFormatter("%(message)s").format(rec))
        LOG.addHandler(h)
        try:
            LOG.warning("failed https://tapi.bale.ai/bot123456:SECRETTOKENVALUE/sendMessage and %s", BOT_TOKEN)
        finally:
            LOG.removeHandler(h)
        check(stream and "SECRETTOKENVALUE" not in stream[0], "token in log: %s" % stream)
    finally:
        BOT_TOKEN = old
    return "ok"


# ══════════════════════════════════════════════════════════════════
# ███ END OF PHASE 8
# ═══ پایان توسعه: هر ۹ فاز کامل شد؛ کد اجرایی از اینجا (main) شروع می‌شود ═══
# ══════════════════════════════════════════════════════════════════


def validate_config():
    ok = True
    if not isinstance(BOT_TOKEN, str) or len(BOT_TOKEN) < 10 or "PUT_YOUR" in BOT_TOKEN:
        print("❌ متغیر محیطی BALE_BOT_TOKEN را تنظیم کن.")
        ok = False
    if not isinstance(ADMIN_ID, int) or ADMIN_ID <= 0:
        print("⚠️ ADMIN_ID تنظیم نشده؛ بخش‌های مخصوص ادمین کار نخواهند کرد.")
    return ok


def acquire_lock():
    try:
        import fcntl
    except ImportError:
        return None
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("❌ یک نسخه دیگر از %s در حال اجراست." % BOT_NAME)
        sys.exit(1)
    f.write(str(os.getpid()))
    f.flush()
    return f


def _stop(signum, frame):
    APP.running = False


def main():
    if not validate_config():
        sys.exit(1)
    setup_logging()
    APP.lock_file = acquire_lock()
    APP.db = Database(DB_FILE)
    init_schema(APP.db)
    APP.api = BaleAPI(BOT_TOKEN)
    APP.started_at = now()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError) as e:
            LOG.debug("signal %s not installed: %s", sig, e)
    try:
        me = APP.api.get_me() or {}
    except BaleAPIError as e:
        LOG.error("نمی‌تونم به بله وصل بشم یا توکن غلطه: %s", redact(e))
        sys.exit(1)
    APP.bot_id = int(me.get("id") or 0)
    APP.bot_username = me.get("username") or ""
    LOG.info("%s آنلاین شد — @%s (id=%s)", BOT_NAME, APP.bot_username, APP.bot_id)
    APP.api.delete_webhook()  # برای Long Polling باید Webhook فعال نباشد
    for fn in STARTUP_HOOKS:
        _safe_run(fn)

    offset = int(meta_get("offset", "0") or 0)
    errors = 0
    while APP.running:
        t0 = time.monotonic()
        try:
            updates = APP.api.get_updates(offset)
            errors = 0
        except BaleAPIError as e:
            errors += 1
            LOG.warning("getUpdates: %s", redact(e))
            time.sleep(min(2 ** min(errors, 5), 30))
            run_periodic()
            continue
        for upd in updates:
            uid = upd.get("update_id")
            if isinstance(uid, int):
                offset = max(offset, uid + 1)
            try:
                handle_update(upd)
            except Exception as e:  # noqa: BLE001
                LOG.exception("update %s failed", uid)
                report_error(e)
        if updates:
            meta_set("offset", offset)
        elif time.monotonic() - t0 < 1.0:
            time.sleep(1.0)  # اگر سرور Long Polling را نگه نداشت، حلقه داغ نشود
        run_periodic()

    LOG.info("در حال خاموش شدن…")
    STATS.flush()
    meta_set("offset", offset)
    APP.db.close()


if __name__ == "__main__":
    main()
