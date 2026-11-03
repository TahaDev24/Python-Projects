#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WORLD WAR 4 🌍🔥  —  Grand Strategy Game for Bale Messenger
=============================================================
یک فایل مستقل، بدون وابستگی خارجی (فقط کتابخانهٔ استاندارد پایتون ≥ 3.9)

اجرا:
    python world_war_4.py              → اجرای بازی
    python world_war_4.py --selftest   → اجرای مجموعهٔ تست داخلی

معماری (لایه‌ها):
    Bale API  →  Dispatcher (rate-limit / dedupe / locks)  →  UI Router
        →  Game Engines (World, Economy, Market, Military, Battle, Alliance, ...)
        →  SQLite Transactions (WAL, atomic, migrations)  →  Notifications
"""

BALE_BOT_TOKEN = "PUT_YOUR_BALE_TOKEN_HERE"
ADMIN_ID = 0

import os
import sys
import re
import json
import time
import math
import random
import sqlite3
import threading
import queue
import hashlib
import traceback
import signal
import shutil
import urllib.request
import urllib.error
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone

APP_NAME = "WORLD WAR 4"
APP_VERSION = "1.0.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "world_war_4.db")
BACKUP_DIR = os.path.join(BASE_DIR, "ww4_backups")
API_BASE = "https://tapi.bale.ai/bot"
TZ_OFFSET = 12600            # Asia/Tehran = UTC+3:30 (بدون DST)
BOOT_TIME = time.time()
CLOCK_SKEW = 0               # برای تست: جلو بردن ساعت بازی


# ============================================================================
# ابزارهای عمومی
# ============================================================================
def now():
    return int(time.time()) + CLOCK_SKEW


def game_day(ts=None):
    ts = now() if ts is None else ts
    return datetime.fromtimestamp(ts + TZ_OFFSET, timezone.utc).strftime("%Y-%m-%d")


def game_week(ts=None):
    ts = now() if ts is None else ts
    d = datetime.fromtimestamp(ts + TZ_OFFSET, timezone.utc)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def day_ordinal(day_str):
    return datetime.strptime(day_str, "%Y-%m-%d").toordinal()


def ts_str(ts):
    return datetime.fromtimestamp(ts + TZ_OFFSET, timezone.utc).strftime("%m/%d %H:%M")


class GameError(Exception):
    """خطای قابل نمایش به بازیکن (اعتبارسنجی/قوانین بازی)."""


def fmt(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if abs(n) < 1000 and abs(n - round(n)) > 1e-9:
        return f"{n:,.1f}"
    return f"{int(round(n)):,}"


def short(n):
    n = float(n)
    a = abs(n)
    for lim, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= lim:
            return f"{n / lim:.1f}{suf}"
    return f"{int(round(n))}"


def pct(x, digits=0):
    return f"{x * 100:.{digits}f}%"


def bar(frac, width=10):
    frac = max(0.0, min(1.0, float(frac)))
    k = int(round(frac * width))
    return "▰" * k + "▱" * (width - k)


def dur(sec):
    sec = int(max(0, sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d} روز")
    if h:
        parts.append(f"{h} ساعت")
    if m and not d:
        parts.append(f"{m} دقیقه")
    if s and not d and not h:
        parts.append(f"{s} ثانیه")
    return " و ".join(parts) if parts else "لحظه‌ای"


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def jd(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def jl(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def rtl(text):
    """هر خط را با RLM شروع می‌کند تا متن فارسی/ترکیبی درست راست‌چین نمایش داده شود."""
    out = []
    for line in str(text).split("\n"):
        out.append(("\u200f" + line) if line.strip() else line)
    return "\n".join(out)


def stable_rand(*parts):
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(h[:12], 16))


def norm_text(s):
    s = str(s or "").strip()
    tbl = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return s.translate(tbl).replace("٬", "").replace(",", "").replace("،", "")


def parse_int(s, lo=None, hi=None):
    s = norm_text(s)
    if not re.fullmatch(r"-?\d{1,15}", s):
        raise GameError("❌ لطفاً یک عدد صحیح معتبر ارسال کنید.")
    v = int(s)
    if lo is not None and v < lo:
        raise GameError(f"❌ عدد باید حداقل {fmt(lo)} باشد.")
    if hi is not None and v > hi:
        raise GameError(f"❌ عدد باید حداکثر {fmt(hi)} باشد.")
    return v


def clean_name(s, lo=2, hi=24):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    s = re.sub(r"[<>&\u200e\u200f\u202a-\u202e]", "", s)
    if not (lo <= len(s) <= hi):
        raise GameError(f"❌ نام باید بین {lo} تا {hi} نویسه باشد.")
    return s


LOG_LOCK = threading.Lock()


def log(msg):
    with LOG_LOCK:
        print(f"[{datetime.fromtimestamp(time.time()).strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================================
# لایهٔ دیتابیس — SQLite واقعی، WAL، تراکنش اتمیک، تراکنش تو در تو با SAVEPOINT
# ============================================================================
class DB:
    def __init__(self, path):
        self.path = path
        self.local = threading.local()
        self.wlock = threading.RLock()
        self.sp_counter = 0

    def conn(self):
        c = getattr(self.local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            self.local.c = c
            self.local.depth = 0
        return c

    @contextmanager
    def tx(self):
        """تراکنش اتمیک. اگر داخل تراکنش دیگری باشیم از SAVEPOINT استفاده می‌شود."""
        with self.wlock:
            c = self.conn()
            if self.local.depth > 0:
                self.sp_counter += 1
                name = f"sp{self.sp_counter}"
                c.execute(f"SAVEPOINT {name}")
                self.local.depth += 1
                try:
                    yield c
                    c.execute(f"RELEASE {name}")
                except BaseException:
                    c.execute(f"ROLLBACK TO {name}")
                    c.execute(f"RELEASE {name}")
                    raise
                finally:
                    self.local.depth -= 1
                return
            c.execute("BEGIN IMMEDIATE")
            self.local.depth = 1
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                try:
                    c.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                self.local.depth = 0

    def close_all(self):
        c = getattr(self.local, "c", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            self.local.c = None


db = DB(DB_PATH)


def qa(c, sql, *a):
    return c.execute(sql, a).fetchall()


def q1(c, sql, *a):
    return c.execute(sql, a).fetchone()


def qv(c, sql, *a, default=None):
    r = c.execute(sql, a).fetchone()
    return default if r is None or r[0] is None else r[0]


def ex(c, sql, *a):
    return c.execute(sql, a)


def rd():
    """اتصال خواندنی (خارج از تراکنش)."""
    return db.conn()


# ============================================================================
# Schema + Migration
# ============================================================================
SCHEMA_V1 = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, actor INTEGER, action TEXT, target TEXT, detail TEXT);
CREATE TABLE error_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, ctx TEXT, message TEXT, trace TEXT);
CREATE TABLE idempotency(key TEXT PRIMARY KEY, ts INTEGER);
CREATE TABLE cooldowns(user_id INTEGER, action TEXT, until INTEGER, PRIMARY KEY(user_id, action));
CREATE TABLE sessions(user_id INTEGER PRIMARY KEY, state TEXT, data TEXT, updated INTEGER);
CREATE TABLE pending_actions(token TEXT PRIMARY KEY, user_id INTEGER, kind TEXT, payload TEXT, expires INTEGER);
CREATE TABLE notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, text TEXT, ts INTEGER, sent INTEGER DEFAULT 0, read INTEGER DEFAULT 0, tries INTEGER DEFAULT 0);
CREATE TABLE broadcasts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, admin_id INTEGER, text TEXT, total INTEGER, sent INTEGER DEFAULT 0);

CREATE TABLE regions(id TEXT PRIMARY KEY, name TEXT, emoji TEXT);
CREATE TABLE countries(code TEXT PRIMARY KEY, name TEXT, flag TEXT, region TEXT, population INTEGER, gdp REAL, industry REAL, energy REAL,
    resources REAL, stability REAL, military REAL, defense REAL, intel REAL, tech REAL, diplomacy REAL, at_war INTEGER DEFAULT 0,
    traits TEXT, base_military REAL, base_defense REAL, base_stability REAL);
CREATE TABLE provinces(id INTEGER PRIMARY KEY, country TEXT, name TEXT, terrain TEXT);
CREATE TABLE cities(id INTEGER PRIMARY KEY, province_id INTEGER, country TEXT, owner_country TEXT, name TEXT, population INTEGER,
    development REAL, terrain TEXT, coastal INTEGER, garrison REAL, garrison_max REAL, fortification INTEGER, governor_id INTEGER DEFAULT 0, captured_at INTEGER DEFAULT 0);
CREATE TABLE facilities(id INTEGER PRIMARY KEY AUTOINCREMENT, city_id INTEGER, ftype TEXT, level INTEGER, damaged_until INTEGER DEFAULT 0);
CREATE TABLE relations(a TEXT, b TEXT, value REAL, PRIMARY KEY(a, b));
CREATE TABLE treaties(id INTEGER PRIMARY KEY AUTOINCREMENT, a_user INTEGER, b_user INTEGER, kind TEXT, status TEXT, created INTEGER, until INTEGER);

CREATE TABLE players(user_id INTEGER PRIMARY KEY, username TEXT, name TEXT, country TEXT, city_id INTEGER, level INTEGER DEFAULT 1, xp INTEGER DEFAULT 0,
    xp_week REAL DEFAULT 0, created INTEGER, last_seen INTEGER, last_settle INTEGER, tax_rate REAL DEFAULT 15, stability REAL DEFAULT 70, morale REAL DEFAULT 80,
    gdp REAL DEFAULT 0, population INTEGER DEFAULT 0, mil_power REAL DEFAULT 0, eco_power REAL DEFAULT 0, ind_power REAL DEFAULT 0, int_power REAL DEFAULT 0,
    victories INTEGER DEFAULT 0, defeats INTEGER DEFAULT 0, alliance_id INTEGER DEFAULT 0, influence REAL DEFAULT 10, prestige REAL DEFAULT 0,
    banned INTEGER DEFAULT 0, ban_reason TEXT DEFAULT '', prefs TEXT DEFAULT '{}', shield_until INTEGER DEFAULT 0, activity INTEGER DEFAULT 0,
    streak INTEGER DEFAULT 0, last_daily TEXT DEFAULT '', last_attacked INTEGER DEFAULT 0, mil_capacity REAL DEFAULT 0);
CREATE TABLE resources(user_id INTEGER, res TEXT, amount REAL, PRIMARY KEY(user_id, res));
CREATE TABLE pstats(user_id INTEGER, key TEXT, value REAL, PRIMARY KEY(user_id, key));
CREATE TABLE buildings(user_id INTEGER, btype TEXT, level INTEGER, PRIMARY KEY(user_id, btype));
CREATE TABLE build_queue(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, btype TEXT, to_level INTEGER, started INTEGER, ends_at INTEGER);
CREATE TABLE techs(user_id INTEGER, tech_id TEXT, done_at INTEGER, PRIMARY KEY(user_id, tech_id));
CREATE TABLE research_queue(user_id INTEGER PRIMARY KEY, tech_id TEXT, started INTEGER, ends_at INTEGER);
CREATE TABLE inventory(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item_key TEXT, level INTEGER DEFAULT 1, qty INTEGER DEFAULT 0,
    deployed INTEGER DEFAULT 0, durability REAL DEFAULT 100, UNIQUE(user_id, item_key, level));
CREATE TABLE prod_queue(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item_key TEXT, qty INTEGER, started INTEGER, ends_at INTEGER);
CREATE TABLE player_effects(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, key TEXT, value REAL, until INTEGER, source TEXT);

CREATE TABLE market(commodity TEXT PRIMARY KEY, base_price REAL, price REAL, stock REAL, target_stock REAL, frozen INTEGER DEFAULT 0, volume REAL DEFAULT 0, updated INTEGER);
CREATE TABLE price_history(id INTEGER PRIMARY KEY AUTOINCREMENT, commodity TEXT, ts INTEGER, price REAL, stock REAL, volume REAL);
CREATE TABLE market_trades(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, user_id INTEGER, commodity TEXT, side TEXT, qty REAL, price REAL, fee REAL);
CREATE TABLE orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, commodity TEXT, side TEXT, qty REAL, limit_price REAL, status TEXT, created INTEGER, filled_at INTEGER DEFAULT 0);
CREATE TABLE offers(id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER, res TEXT, qty REAL, unit_price REAL, status TEXT, buyer_id INTEGER DEFAULT 0, created INTEGER, closed_at INTEGER DEFAULT 0);
CREATE TABLE investments(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, principal REAL, rate REAL, started INTEGER, ends_at INTEGER, status TEXT);

CREATE TABLE battles(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, mode TEXT, attacker_id INTEGER, defender_id INTEGER, target_type TEXT, target_id TEXT,
    winner TEXT, att_power REAL, def_power REAL, margin REAL, report TEXT, loot TEXT, att_loss REAL, def_loss REAL);
CREATE TABLE wars(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, a_id INTEGER, b_id INTEGER, status TEXT, score_a REAL DEFAULT 0, score_b REAL DEFAULT 0, started INTEGER, ends_at INTEGER, result TEXT);
CREATE TABLE war_rooms(id INTEGER PRIMARY KEY AUTOINCREMENT, a_id INTEGER, b_id INTEGER, status TEXT, round INTEGER DEFAULT 0, max_rounds INTEGER DEFAULT 5,
    a_hp REAL, b_hp REAL, a_hp0 REAL, b_hp0 REAL, a_action TEXT, b_action TEXT, a_last TEXT, b_last TEXT, wager REAL DEFAULT 0, created INTEGER, deadline INTEGER,
    log TEXT DEFAULT '', terrain TEXT, ai INTEGER DEFAULT 0, a_morale REAL DEFAULT 1, b_morale REAL DEFAULT 1, a_fuel REAL DEFAULT 1, b_fuel REAL DEFAULT 1, winner TEXT DEFAULT '');
CREATE TABLE battle_locks(user_id INTEGER PRIMARY KEY, ref TEXT, until INTEGER);
CREATE TABLE group_wars(id INTEGER PRIMARY KEY AUTOINCREMENT, alliance_id INTEGER, leader_id INTEGER, target_type TEXT, target_id TEXT, strategy TEXT DEFAULT 'balanced',
    status TEXT, created INTEGER, launched_at INTEGER DEFAULT 0, result TEXT DEFAULT '');
CREATE TABLE group_war_members(war_id INTEGER, user_id INTEGER, role TEXT, committed TEXT DEFAULT '{}', supplies TEXT DEFAULT '{}', intel REAL DEFAULT 0, joined INTEGER, PRIMARY KEY(war_id, user_id));

CREATE TABLE alliances(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, tag TEXT UNIQUE, leader_id INTEGER, level INTEGER DEFAULT 1, xp INTEGER DEFAULT 0,
    description TEXT DEFAULT '', policy TEXT DEFAULT 'open', min_level INTEGER DEFAULT 1, created INTEGER, power REAL DEFAULT 0);
CREATE TABLE alliance_members(alliance_id INTEGER, user_id INTEGER UNIQUE, role TEXT, joined INTEGER, contribution REAL DEFAULT 0, PRIMARY KEY(alliance_id, user_id));
CREATE TABLE alliance_treasury(alliance_id INTEGER, res TEXT, amount REAL, PRIMARY KEY(alliance_id, res));
CREATE TABLE alliance_techs(alliance_id INTEGER, tech_id TEXT, done_at INTEGER, PRIMARY KEY(alliance_id, tech_id));
CREATE TABLE alliance_projects(id INTEGER PRIMARY KEY AUTOINCREMENT, alliance_id INTEGER, kind TEXT, goal TEXT, progress TEXT, status TEXT, created INTEGER, completed_at INTEGER DEFAULT 0);
CREATE TABLE alliance_relations(a INTEGER, b INTEGER, status TEXT, since INTEGER, PRIMARY KEY(a, b));
CREATE TABLE dip_proposals(id INTEGER PRIMARY KEY AUTOINCREMENT, from_a INTEGER, to_a INTEGER, kind TEXT, status TEXT, created INTEGER);
CREATE TABLE alliance_invites(alliance_id INTEGER, user_id INTEGER, by_user INTEGER, ts INTEGER, PRIMARY KEY(alliance_id, user_id));
CREATE TABLE alliance_requests(alliance_id INTEGER, user_id INTEGER, ts INTEGER, PRIMARY KEY(alliance_id, user_id));
CREATE TABLE votes(id INTEGER PRIMARY KEY AUTOINCREMENT, alliance_id INTEGER, kind TEXT, payload TEXT, created_by INTEGER, created INTEGER, ends_at INTEGER, status TEXT, result TEXT DEFAULT '');
CREATE TABLE vote_ballots(vote_id INTEGER, user_id INTEGER, choice INTEGER, PRIMARY KEY(vote_id, user_id));
CREATE TABLE alliance_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, alliance_id INTEGER, ts INTEGER, actor INTEGER, text TEXT);

CREATE TABLE intel_reports(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, ttype TEXT, tid TEXT, kind TEXT, data TEXT, confidence REAL, accuracy REAL, source TEXT, created INTEGER);
CREATE TABLE missions(user_id INTEGER, mid TEXT, period TEXT, progress REAL DEFAULT 0, claimed INTEGER DEFAULT 0, created INTEGER, expires INTEGER, PRIMARY KEY(user_id, mid, period));
CREATE TABLE daily_claims(user_id INTEGER, day TEXT, day_no INTEGER, ts INTEGER, PRIMARY KEY(user_id, day));
CREATE TABLE achievements(user_id INTEGER, aid TEXT, ts INTEGER, PRIMARY KEY(user_id, aid));
CREATE TABLE world_events(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, title TEXT, scope TEXT, target TEXT, started INTEGER, ends_at INTEGER, params TEXT, active INTEGER DEFAULT 1, source TEXT);
"""

SCHEMA_V2 = """
CREATE INDEX idx_notif_pending ON notifications(sent, id);
CREATE INDEX idx_notif_user ON notifications(user_id, id);
CREATE INDEX idx_inv_user ON inventory(user_id);
CREATE INDEX idx_bq_end ON build_queue(ends_at);
CREATE INDEX idx_bq_user ON build_queue(user_id);
CREATE INDEX idx_pq_end ON prod_queue(ends_at);
CREATE INDEX idx_pq_user ON prod_queue(user_id);
CREATE INDEX idx_cities_gov ON cities(governor_id);
CREATE INDEX idx_cities_country ON cities(owner_country);
CREATE INDEX idx_fac_city ON facilities(city_id);
CREATE INDEX idx_hist ON price_history(commodity, ts);
CREATE INDEX idx_trades_user ON market_trades(user_id, id);
CREATE INDEX idx_battles_att ON battles(attacker_id, id);
CREATE INDEX idx_battles_def ON battles(defender_id, id);
CREATE INDEX idx_intel ON intel_reports(user_id, ttype, tid);
CREATE INDEX idx_effects ON player_effects(user_id, key);
CREATE INDEX idx_alog ON alliance_logs(alliance_id, id);
CREATE INDEX idx_audit ON audit_log(id);
CREATE INDEX idx_events_active ON world_events(active, ends_at);
CREATE INDEX idx_orders_open ON orders(status, commodity);
CREATE INDEX idx_offers_open ON offers(status, res);
"""

SCHEMA_V3 = """
ALTER TABLE players ADD COLUMN xp_week_key TEXT DEFAULT '';
ALTER TABLE cities ADD COLUMN last_battle INTEGER DEFAULT 0;
"""

SCHEMA_V4 = """
ALTER TABLE players ADD COLUMN empire_name TEXT DEFAULT '';
"""

MIGRATIONS = [(1, SCHEMA_V1), (2, SCHEMA_V2), (3, SCHEMA_V3), (4, SCHEMA_V4)]


def backup_database(tag="auto", keep=12):
    """پشتیبان‌گیری امن با sqlite backup API (سازگار با WAL و هم‌زمانی)."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dest = os.path.join(BACKUP_DIR, f"ww4_{tag}_{datetime.fromtimestamp(time.time()).strftime('%Y%m%d_%H%M%S')}.db")
        src = sqlite3.connect(db.path, timeout=30)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("ww4_") and f.endswith(".db"))
        for f in files[:-keep]:
            try:
                os.remove(os.path.join(BACKUP_DIR, f))
            except OSError:
                pass
        return dest
    except Exception as e:
        log(f"backup failed: {e}")
        return None


def current_schema_version(c):
    try:
        return qv(c, "SELECT MAX(version) FROM schema_migrations", default=0)
    except sqlite3.OperationalError:
        return 0


def run_migrations():
    c = db.conn()
    c.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER)")
    cur = current_schema_version(c)
    pending = [(v, s) for v, s in MIGRATIONS if v > cur]
    if not pending:
        return cur
    if cur > 0 and os.path.exists(db.path):
        backup_database("premigrate")
    for v, script in pending:
        with db.tx() as t:
            for stmt in [x.strip() for x in script.split(";\n") if x.strip()]:
                t.execute(stmt.rstrip(";"))
            t.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?,?)", (v, now()))
        log(f"migration v{v} applied")
    return pending[-1][0]


def integrity_ok():
    try:
        r = db.conn().execute("PRAGMA integrity_check").fetchone()
        return r is not None and r[0] == "ok"
    except Exception:
        return False


def recover_from_backup():
    """اگر فایل دیتابیس خراب شد آخرین پشتیبان سالم را جایگزین می‌کند."""
    if not os.path.isdir(BACKUP_DIR):
        return False
    files = sorted((f for f in os.listdir(BACKUP_DIR) if f.startswith("ww4_") and f.endswith(".db")), reverse=True)
    for f in files:
        p = os.path.join(BACKUP_DIR, f)
        try:
            t = sqlite3.connect(p)
            ok = t.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            t.close()
        except Exception:
            ok = False
        if ok:
            db.close_all()
            for ext in ("", "-wal", "-shm"):
                try:
                    os.remove(db.path + ext)
                except OSError:
                    pass
            shutil.copy2(p, db.path)
            log(f"database recovered from {f}")
            return True
    return False


def init_database():
    os.makedirs(os.path.dirname(db.path) or ".", exist_ok=True)
    if os.path.exists(db.path) and not integrity_ok():
        log("⚠️ database integrity check failed → trying recovery")
        if not recover_from_backup():
            raise RuntimeError("Database is corrupted and no valid backup exists")
    ver = run_migrations()
    return ver


# ============================================================================
# Meta / Audit / Errors
# ============================================================================
def meta_get(c, key, default=None):
    r = q1(c, "SELECT value FROM meta WHERE key=?", key)
    return default if r is None else r[0]


def meta_set(c, key, value):
    ex(c, "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, str(value))


def audit(c, actor, action, target="", detail=""):
    ex(c, "INSERT INTO audit_log(ts,actor,action,target,detail) VALUES(?,?,?,?,?)", now(), int(actor or 0), action, str(target), str(detail)[:900])


ERR_ALERT = {"last": 0}


def log_error(ctx, exc=None):
    tr = traceback.format_exc() if exc is not None else ""
    msg = f"{type(exc).__name__}: {exc}" if exc is not None else str(ctx)
    log(f"ERROR [{ctx}] {msg}\n{tr}")
    try:
        with db.tx() as c:
            ex(c, "INSERT INTO error_log(ts,ctx,message,trace) VALUES(?,?,?,?)", now(), str(ctx)[:200], msg[:500], tr[-2500:])
            ex(c, "DELETE FROM error_log WHERE id < (SELECT MAX(id) FROM error_log) - 500")
    except Exception:
        pass


# ============================================================================
# Security: Rate limit / Cooldown / Idempotency / Locks
# ============================================================================
SEC = {"rate_limited": 0, "duplicates": 0, "locked_out": 0, "rejected": 0, "updates": 0, "errors": 0,
       "rate_max": 10, "rate_window": 6.0, "penalty": 8.0}


class RateLimiter:
    def __init__(self):
        self.hits = defaultdict(deque)
        self.blocked = {}
        self.lock = threading.Lock()

    def allow(self, uid):
        t = time.monotonic()
        with self.lock:
            until = self.blocked.get(uid, 0)
            if t < until:
                return False
            q = self.hits[uid]
            while q and t - q[0] > SEC["rate_window"]:
                q.popleft()
            if len(q) >= SEC["rate_max"]:
                self.blocked[uid] = t + SEC["penalty"]
                SEC["rate_limited"] += 1
                q.clear()
                return False
            q.append(t)
            return True

    def reset(self):
        with self.lock:
            self.hits.clear()
            self.blocked.clear()


RATE = RateLimiter()
USER_LOCKS = defaultdict(threading.RLock)
CB_RECENT = {}
CB_LOCK = threading.Lock()


def cb_duplicate(uid, cb_id, msg_id, data):
    """جلوگیری از تکرار Callback: هم شناسهٔ یکتای callback و هم زدن سریع همان دکمه."""
    t = time.monotonic()
    with CB_LOCK:
        if len(CB_RECENT) > 5000:
            for k in [k for k, v in CB_RECENT.items() if t - v > 30]:
                CB_RECENT.pop(k, None)
        k1 = f"id:{cb_id}"
        if k1 in CB_RECENT:
            SEC["duplicates"] += 1
            return True
        CB_RECENT[k1] = t
        k2 = f"{uid}:{msg_id}:{data}"
        last = CB_RECENT.get(k2)
        CB_RECENT[k2] = t
        if last is not None and t - last < 1.2 and not data.startswith(("nav", "ui.")):
            SEC["duplicates"] += 1
            return True
    return False


def idem(c, key, ttl=None):
    """کلید یکتا؛ اگر قبلاً ثبت شده باشد False برمی‌گرداند (Idempotency)."""
    cur = ex(c, "INSERT OR IGNORE INTO idempotency(key, ts) VALUES(?,?)", key, now())
    return cur.rowcount == 1


def cooldown(c, uid, action, seconds, consume=True):
    """کول‌داون اتمیک ذخیره‌شده در دیتابیس (بعد از ری‌استارت هم پایدار است)."""
    t = now()
    r = q1(c, "SELECT until FROM cooldowns WHERE user_id=? AND action=?", uid, action)
    if r and r["until"] > t:
        raise GameError(f"⏳ این عملیات در حالت انتظار است؛ {dur(r['until'] - t)} دیگر دوباره تلاش کنید.")
    if consume and seconds > 0:
        ex(c, "INSERT INTO cooldowns(user_id,action,until) VALUES(?,?,?) ON CONFLICT(user_id,action) DO UPDATE SET until=excluded.until", uid, action, t + seconds)


def cooldown_left(c, uid, action):
    r = q1(c, "SELECT until FROM cooldowns WHERE user_id=? AND action=?", uid, action)
    return max(0, r["until"] - now()) if r else 0


def make_token(c, uid, kind, payload, ttl=300):
    tok = hashlib.sha1(f"{uid}{kind}{time.time()}{random.random()}".encode()).hexdigest()[:10]
    ex(c, "INSERT INTO pending_actions(token,user_id,kind,payload,expires) VALUES(?,?,?,?,?)", tok, uid, kind, jd(payload), now() + ttl)
    return tok


def take_token(c, uid, token, kind=None):
    """توکن تک‌مصرف (ضد اجرای دوباره)؛ فقط یک‌بار موفق می‌شود."""
    r = q1(c, "SELECT * FROM pending_actions WHERE token=? AND user_id=?", token, uid)
    if not r or r["expires"] < now() or (kind and r["kind"] != kind):
        raise GameError("⚠️ این درخواست منقضی شده یا قبلاً انجام شده است.")
    cur = ex(c, "DELETE FROM pending_actions WHERE token=?", token)
    if cur.rowcount != 1:
        raise GameError("⚠️ این درخواست قبلاً انجام شده است.")
    return jl(r["payload"])


def battle_lock_acquire(c, uid, ref, seconds=120):
    r = q1(c, "SELECT * FROM battle_locks WHERE user_id=?", uid)
    if r and r["until"] > now() and r["ref"] != ref:
        raise GameError("⚔️ شما هم‌اکنون درگیر یک نبرد/عملیات دیگر هستید.")
    ex(c, "INSERT INTO battle_locks(user_id,ref,until) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET ref=excluded.ref, until=excluded.until", uid, ref, now() + seconds)


def battle_lock_release(c, uid, ref=None):
    if ref is None:
        ex(c, "DELETE FROM battle_locks WHERE user_id=?", uid)
    else:
        ex(c, "DELETE FROM battle_locks WHERE user_id=? AND ref=?", uid, ref)


def battle_locked(c, uid):
    r = q1(c, "SELECT until FROM battle_locks WHERE user_id=?", uid)
    return bool(r and r["until"] > now())


# ============================================================================
# Sessions (ورودی متنی چندمرحله‌ای)
# ============================================================================
def session_set(c, uid, state, data=None):
    ex(c, "INSERT INTO sessions(user_id,state,data,updated) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET state=excluded.state,data=excluded.data,updated=excluded.updated",
       uid, state, jd(data or {}), now())


def session_get(c, uid):
    r = q1(c, "SELECT * FROM sessions WHERE user_id=?", uid)
    if not r or not r["state"]:
        return None, {}
    if r["updated"] < now() - 1800:
        return None, {}
    return r["state"], jl(r["data"])


def session_clear(c, uid):
    ex(c, "DELETE FROM sessions WHERE user_id=?", uid)


# ============================================================================
# Notification Engine (مرکزی)
# ============================================================================
NOTIF_KINDS = {
    "attack": "⚔️ حمله", "war": "🔥 جنگ", "mission": "🎯 مأموریت", "reward": "🎁 پاداش", "market": "🛒 بازار",
    "alliance": "🤝 اتحاد", "vote": "🗳️ رأی‌گیری", "tech": "🔬 فناوری", "building": "🏗️ ساختمان", "event": "🌍 رویداد",
    "diplomacy": "🕊️ دیپلماسی", "system": "🛡️ سیستم", "military": "🪖 ارتش", "intel": "🕵️ اطلاعات",
}


def notify(c, uid, kind, text):
    """ثبت اعلان (در همان تراکنش) — ارسال توسط Notification Worker انجام می‌شود."""
    if not uid:
        return
    ex(c, "INSERT INTO notifications(user_id,kind,text,ts) VALUES(?,?,?,?)", int(uid), kind, text, now())


def notify_many(c, uids, kind, text):
    for u in set(uids):
        notify(c, u, kind, text)


# ============================================================================
# کاتالوگ‌های بازی (داده‌محور) — منابع، ساختمان‌ها، فناوری‌ها، یگان‌ها، ...
# ============================================================================
RES = {
    "money": ("💰", "پول"), "food": ("🌾", "غذا"), "oil": ("🛢️", "نفت"), "iron": ("⛏️", "آهن"),
    "rare": ("🧪", "منابع کمیاب"), "energy": ("⚡", "انرژی"), "fuel": ("⛽", "سوخت"),
    "goods": ("📦", "کالای صنعتی"), "gems": ("💠", "جم"),
}
RES_ORDER = ["money", "food", "oil", "iron", "rare", "energy", "fuel", "goods", "gems"]
TRADABLE = ["food", "oil", "iron", "rare", "energy", "fuel", "goods"]
NO_CAP = {"money", "gems"}
BASE_PRICES = {"food": 4.0, "oil": 14.0, "iron": 9.0, "rare": 90.0, "energy": 3.0, "fuel": 22.0, "goods": 35.0}
TARGET_STOCK = {"food": 400000, "oil": 250000, "iron": 300000, "rare": 20000, "energy": 500000, "fuel": 150000, "goods": 120000}
ELASTICITY = {"food": 0.30, "oil": 0.40, "iron": 0.35, "rare": 0.55, "energy": 0.30, "fuel": 0.40, "goods": 0.35}


def rn(res):
    return RES[res][0] + " " + RES[res][1]


def cost_text(cost):
    return "، ".join(f"{RES[k][0]}{fmt(v)}" for k, v in cost.items() if v)


REGIONS = {
    "ME": ("خاورمیانه", "🏜️"), "EU": ("اروپا", "🏰"), "EA": ("آسیای شرقی", "🏯"),
    "SA": ("آسیای جنوبی", "🕌"), "AM": ("قارهٔ آمریکا", "🗽"), "AF": ("آفریقا", "🌍"),
}

TERRAINS = {
    "plains": ("دشت", "🌾", {"land_def": 1.0, "armor": 1.15, "air": 1.0, "arty": 1.0, "naval": 0.8, "fuel": 1.0, "recon": 1.05}),
    "mountain": ("کوهستان", "⛰️", {"land_def": 1.25, "armor": 0.75, "air": 0.9, "arty": 0.9, "naval": 0.5, "fuel": 1.15, "recon": 0.9}),
    "urban": ("شهری", "🏙️", {"land_def": 1.15, "armor": 0.85, "air": 0.95, "arty": 1.1, "naval": 0.7, "fuel": 1.0, "recon": 0.95}),
    "desert": ("بیابان", "🏜️", {"land_def": 0.95, "armor": 1.05, "air": 1.05, "arty": 1.0, "naval": 0.6, "fuel": 1.25, "recon": 1.1}),
    "forest": ("جنگل", "🌲", {"land_def": 1.1, "armor": 0.85, "air": 0.85, "arty": 0.95, "naval": 0.5, "fuel": 1.05, "recon": 0.85}),
    "coast": ("ساحلی", "🏖️", {"land_def": 1.0, "armor": 1.0, "air": 1.0, "arty": 1.0, "naval": 1.2, "fuel": 1.0, "recon": 1.0}),
}

FACILITIES = {
    "farmland": ("🌾", "زمین کشاورزی", {"food": 50}),
    "oil_field": ("🛢️", "میدان نفتی", {"oil": 30}),
    "iron_mine": ("⛏️", "معدن آهن", {"iron": 35}),
    "rare_deposit": ("🧪", "ذخایر کمیاب", {"rare": 1.5}),
    "power_grid": ("⚡", "شبکهٔ برق", {"energy": 45}),
    "industrial_zone": ("🏭", "منطقهٔ صنعتی", {"goods": 14}),
    "port": ("⚓", "بندر تجاری", {"money": 120}),
    "airfield": ("🛫", "فرودگاه نظامی", {}),
    "fortress": ("🏰", "قلعهٔ دفاعی", {}),
    "research_campus": ("🎓", "پردیس پژوهشی", {}),
}
FACILITY_BONUS = {"airfield": ("سقف هوایی", "+۲٪ قدرت هوایی به‌ازای هر سطح"),
                  "fortress": ("دفاع شهر", "+۴٪ دفاع شهر به‌ازای هر سطح"),
                  "research_campus": ("پژوهش", "+۲٪ سرعت تحقیق به‌ازای هر سطح")}

TRAIT_EFFECTS = {
    "oil": {"prod_oil": 0.10}, "agri": {"prod_food": 0.10}, "industry": {"prod_goods": 0.10},
    "tech": {"research_speed": 0.08}, "military": {"atk_land": 0.03, "atk_air": 0.03}, "trade": {"market_fee": -0.10},
    "intel": {"intel_power": 0.08}, "mining": {"prod_iron": 0.10, "prod_rare": 0.10}, "finance": {"tax": 0.06},
}
TRAIT_NAMES = {"oil": "🛢️ قدرت نفتی", "agri": "🌾 کشاورزی", "industry": "🏭 صنعتی", "tech": "🔬 فناور", "military": "🪖 نظامی‌گرا",
               "trade": "🏪 تجاری", "intel": "🕵️ اطلاعاتی", "mining": "⛏️ معدنی", "finance": "🏦 مالی"}

# code, flag, name, region, pop(M), gdp(B), industry, energy, resources, stability, military, defense, intel, tech, diplomacy, traits, provinces(* = ساحلی)
COUNTRIES = [
    ("IR", "🇮🇷", "ایران", "ME", 88, 420, 55, 82, 85, 62, 70, 72, 68, 55, 50, "oil,military", "تهران:تهران,کرج|خراسان:مشهد,نیشابور|خوزستان:اهواز,آبادان*|فارس:شیراز,مرودشت|هرمزگان:بندرعباس*,میناب*"),
    ("US", "🇺🇸", "آمریکا", "AM", 335, 26000, 88, 75, 70, 70, 95, 90, 92, 95, 80, "finance,tech", "کالیفرنیا:لس‌آنجلس*,سن‌دیگو*|تگزاس:هوستون*,دالاس|نیویورک:نیویورک*,بوفالو|فلوریدا:میامی*,اورلاندو"),
    ("RU", "🇷🇺", "روسیه", "EU", 144, 2000, 70, 92, 92, 60, 88, 85, 84, 72, 55, "oil,military", "مسکو:مسکو,تولا|سن‌پترزبورگ:سن‌پترزبورگ*,نووگورود|سیبری:نووسیبیرسک,کراسنویارسک|قفقاز:سوچی*,گروزنی"),
    ("CN", "🇨🇳", "چین", "EA", 1410, 17800, 95, 78, 80, 72, 90, 84, 86, 88, 65, "industry,tech", "پکن:پکن,تیانجین*|شانگهای:شانگهای*,نانجینگ|گوانگدونگ:گوانگژو,شنژن*|سیچوان:چنگدو,چونگ‌کینگ"),
    ("TR", "🇹🇷", "ترکیه", "ME", 85, 900, 62, 45, 50, 58, 70, 66, 64, 62, 62, "trade,military", "استانبول:استانبول*,ادرنه|آنکارا:آنکارا,کنیا|ازمیر:ازمیر*,بورسا"),
    ("SA", "🇸🇦", "عربستان", "ME", 36, 1100, 45, 95, 90, 66, 62, 68, 58, 50, 60, "oil,finance", "ریاض:ریاض,بریده|مکه:مکه,جده*|شرقیه:دمام*,ظهران"),
    ("IL", "🇮🇱", "اسرائیل", "ME", 10, 520, 60, 40, 30, 60, 84, 88, 94, 90, 55, "tech,intel", "تل‌آویو:تل‌آویو*,ریشون|حیفا:حیفا*,ناصره|اورشلیم:اورشلیم,بئرشبع"),
    ("IQ", "🇮🇶", "عراق", "ME", 44, 270, 30, 75, 78, 45, 44, 42, 40, 30, 40, "oil", "بغداد:بغداد,کاظمین|نینوا:موصل,تلعفر|بصره:بصره*,ناصریه"),
    ("PK", "🇵🇰", "پاکستان", "SA", 240, 340, 40, 40, 45, 48, 68, 62, 62, 45, 45, "military,agri", "پنجاب:لاهور,فیصل‌آباد|سند:کراچی*,حیدرآباد|بلوچستان:کویته,گوادر*"),
    ("IN", "🇮🇳", "هند", "SA", 1430, 3700, 72, 60, 65, 62, 82, 76, 74, 74, 68, "tech,agri", "مهاراشترا:بمبئی*,پونا|دهلی:دهلی‌نو,نویدا|کارناتاکا:بنگلور,میسور|بنگال:کلکته*,دورگاپور"),
    ("EG", "🇪🇬", "مصر", "AF", 112, 400, 38, 50, 45, 52, 66, 60, 55, 40, 58, "trade", "قاهره:قاهره,جیزه|اسکندریه:اسکندریه*,دمنهور|سینا:شرم‌الشیخ*,العریش*"),
    ("DE", "🇩🇪", "آلمان", "EU", 84, 4400, 90, 55, 40, 80, 62, 70, 70, 90, 78, "industry,tech", "باواریا:مونیخ,نورنبرگ|برلین:برلین,پوتسدام|نوردراین:کلن,دوسلدورف"),
    ("FR", "🇫🇷", "فرانسه", "EU", 68, 3000, 78, 70, 45, 72, 78, 78, 80, 84, 82, "military,tech", "ایل‌دوفرانس:پاریس,ورسای|پروانس:مارسی*,نیس*|اوورنی:لیون,گرنوبل"),
    ("GB", "🇬🇧", "بریتانیا", "EU", 67, 3100, 72, 55, 45, 74, 76, 74, 90, 86, 84, "finance,intel", "انگلستان:لندن,منچستر|اسکاتلند:ادینبورگ*,گلاسکو|ولز:کاردیف*,سوانزی*"),
    ("JP", "🇯🇵", "ژاپن", "EA", 124, 4200, 92, 40, 25, 82, 60, 76, 70, 94, 76, "tech,industry", "کانتو:توکیو*,یوکوهاما*|کانسای:اوساکا*,کیوتو|کیوشو:فوکوئوکا*,ناگاساکی*"),
    ("KR", "🇰🇷", "کره جنوبی", "EA", 52, 1700, 88, 38, 22, 74, 72, 76, 74, 90, 66, "tech,industry", "سئول:سئول,اینچئون*|گیونگسانگ:بوسان*,دگو|جئجو:جئجو*,سوگوییپو*"),
    ("BR", "🇧🇷", "برزیل", "AM", 216, 2100, 58, 62, 82, 58, 50, 50, 46, 55, 62, "agri,mining", "سائوپائولو:سائوپائولو,کامپیناس|ریو:ریودوژانیرو*,نیتروی*|برازیلیا:برازیلیا,گویانیا"),
    ("UA", "🇺🇦", "اوکراین", "EU", 38, 160, 42, 42, 60, 42, 64, 62, 58, 48, 50, "agri,military", "کیف:کیف,بوچا|خارکیف:خارکیف,پولتاوا|اودسا:اودسا*,میکولایف*"),
    ("AE", "🇦🇪", "امارات", "ME", 10, 500, 55, 88, 70, 80, 48, 56, 56, 68, 74, "oil,trade", "ابوظبی:ابوظبی*,العین|دبی:دبی*,شارجه*|رأس‌الخیمه:رأس‌الخیمه*,فجیره*"),
    ("ZA", "🇿🇦", "آفریقای جنوبی", "AF", 60, 400, 45, 40, 80, 50, 42, 44, 42, 46, 60, "mining", "گائوتنگ:ژوهانسبورگ,پرتوریا|کیپ‌تاون:کیپ‌تاون*,استلنبوش|کوازولو:دوربان*,پیترماریتزبورگ"),
]

# ============================================================================
# ساختمان‌ها
# io = تولید(+)/مصرف(-) در ساعت به‌ازای هر سطح
# ============================================================================
BUILDINGS = {
    "command_center": dict(name="مرکز فرماندهی", emoji="🏛️", max=15, cost={"money": 6000, "iron": 400, "goods": 60}, growth=1.55, time=300, upkeep=25, req={},
                           tech=None, io={}, desc="ظرفیت صف ساخت، تعداد شهرهای قابل‌حکمرانی و پاداش آمادگی نبرد"),
    "farm": dict(name="مزرعه", emoji="🌾", max=20, cost={"money": 2500, "iron": 100}, growth=1.5, time=180, upkeep=8, req={"command_center": 1},
                 tech=None, io={"food": 60}, desc="تولید غذا برای جمعیت و ارتش"),
    "mine": dict(name="معدن", emoji="⛏️", max=20, cost={"money": 3500, "iron": 150}, growth=1.5, time=220, upkeep=14, req={"command_center": 1},
                 tech=None, io={"iron": 40, "rare": 1.2, "energy": -6}, desc="تولید آهن و مقدار کمی منابع کمیاب"),
    "bank": dict(name="بانک", emoji="🏦", max=20, cost={"money": 5000, "iron": 100, "goods": 20}, growth=1.55, time=260, upkeep=10, req={"command_center": 1},
                 tech=None, io={"money": 300}, desc="درآمد پول، بونوس مالیات و سود سپرده‌ها"),
    "power_plant": dict(name="نیروگاه", emoji="⚡", max=20, cost={"money": 4500, "iron": 250, "goods": 40}, growth=1.5, time=240, upkeep=14, req={"command_center": 1},
                        tech=None, io={"energy": 120, "oil": -14}, desc="تولید انرژی (مصرف نفت)"),
    "storage": dict(name="انبار", emoji="🗄️", max=20, cost={"money": 3000, "iron": 300}, growth=1.45, time=200, upkeep=6, req={"command_center": 1},
                    tech=None, io={}, desc="افزایش سقف ذخیرهٔ منابع"),
    "factory": dict(name="کارخانهٔ صنعتی", emoji="🏭", max=20, cost={"money": 7000, "iron": 500, "goods": 30}, growth=1.55, time=320, upkeep=22, req={"command_center": 2, "power_plant": 1},
                    tech=None, io={"goods": 26, "iron": -30, "energy": -12}, desc="تبدیل آهن و انرژی به کالای صنعتی"),
    "refinery": dict(name="پالایشگاه", emoji="🛢️", max=20, cost={"money": 8000, "iron": 500, "goods": 60}, growth=1.55, time=340, upkeep=26, req={"command_center": 2, "power_plant": 1},
                     tech="ind_refining", io={"fuel": 36, "oil": -54, "energy": -10}, desc="تبدیل نفت به سوخت"),
    "barracks": dict(name="پادگان", emoji="🪖", max=15, cost={"money": 5500, "iron": 300, "goods": 40}, growth=1.55, time=280, upkeep=20, req={"command_center": 1},
                     tech=None, io={}, desc="تولید پیاده‌نظام/شناسایی/پشتیبانی/لجستیک"),
    "military_factory": dict(name="کارخانهٔ نظامی", emoji="⚙️", max=15, cost={"money": 9000, "iron": 800, "goods": 120}, growth=1.6, time=380, upkeep=34, req={"command_center": 2, "barracks": 2, "factory": 1},
                             tech="mil_mech", io={"energy": -8}, desc="تولید زره، توپخانه، پدافند و جنگ الکترونیک"),
    "air_base": dict(name="پایگاه هوایی", emoji="✈️", max=15, cost={"money": 14000, "iron": 900, "goods": 200}, growth=1.6, time=480, upkeep=48, req={"command_center": 4, "military_factory": 2},
                     tech="mil_aviation", io={"energy": -10}, desc="تولید هواپیما، بالگرد و پهپاد"),
    "naval_base": dict(name="پایگاه دریایی", emoji="⚓", max=15, cost={"money": 15000, "iron": 1200, "goods": 220}, growth=1.6, time=520, upkeep=52, req={"command_center": 4, "military_factory": 2},
                       tech="mil_naval", io={"energy": -10}, desc="تولید ناو، زیردریایی و ناو هواپیمابر"),
    "research_center": dict(name="مرکز تحقیقات", emoji="🔬", max=12, cost={"money": 8000, "iron": 300, "goods": 100}, growth=1.6, time=360, upkeep=30, req={"command_center": 2},
                            tech=None, io={"energy": -8}, desc="پژوهش فناوری‌ها و سرعت تحقیق"),
    "intelligence_center": dict(name="مرکز اطلاعات", emoji="🕵️", max=12, cost={"money": 9000, "iron": 300, "goods": 140}, growth=1.6, time=380, upkeep=34, req={"command_center": 3, "research_center": 1},
                                tech=None, io={"energy": -6}, desc="قدرت اطلاعاتی، ضدجاسوسی و شناسایی"),
    "logistics_center": dict(name="مرکز لجستیک", emoji="🚚", max=12, cost={"money": 8500, "iron": 600, "goods": 120}, growth=1.55, time=340, upkeep=28, req={"command_center": 3, "storage": 2},
                             tech="log_supply", io={}, desc="تدارکات نبرد، کاهش مصرف سوخت"),
    "defense_center": dict(name="مرکز دفاع", emoji="🛡️", max=12, cost={"money": 9500, "iron": 900, "goods": 150}, growth=1.6, time=400, upkeep=36, req={"command_center": 3, "barracks": 2},
                           tech="def_bunkers", io={}, desc="دفاع پایه و آمادگی دفاعی"),
    "trade_center": dict(name="مرکز تجارت", emoji="🏪", max=12, cost={"money": 8000, "iron": 200, "goods": 100}, growth=1.55, time=340, upkeep=24, req={"command_center": 3, "bank": 2},
                         tech="eco_commerce", io={}, desc="کاهش کارمزد بازار و آگهی‌های تجاری"),
}
BUILD_ORDER = list(BUILDINGS.keys())
START_BUILDINGS = {"command_center": 1, "farm": 1, "mine": 1, "bank": 1, "power_plant": 1, "storage": 1, "barracks": 1}

# ============================================================================
# فناوری‌ها  (id, branch, name, tier, cost_money, cost_goods, cost_rare, time_sec, prereqs, effects, description)
# ============================================================================
TECH_BRANCHES = {
    "mil": ("🪖", "نظامی"), "eco": ("💰", "اقتصاد"), "ind": ("🏭", "صنعت"), "int": ("🕵️", "اطلاعات"),
    "log": ("🚚", "لجستیک"), "def": ("🛡️", "دفاع"), "nrg": ("⚡", "انرژی"), "dip": ("🤝", "دیپلماسی"),
}
_T = []


def _tech(id_, br, name, tier, pre, fx, desc):
    _T.append((id_, br, name, tier, int(4000 * (1.9 ** (tier - 1))), int(40 * (1.8 ** (tier - 1))), int(4 * (1.7 ** (tier - 1))),
               int(420 * (2.0 ** (tier - 1))), pre, fx, desc))


_tech("mil_basic", "mil", "آموزش نظامی پایه", 1, [], {"atk_land": 0.03, "morale": 0.02}, "تمرین منظم نیروها")
_tech("mil_mech", "mil", "مکانیزه‌سازی", 2, ["mil_basic"], {"atk_land": 0.05}, "باز شدن کارخانهٔ نظامی و یگان‌های زرهی")
_tech("mil_artillery", "mil", "توپخانهٔ پیشرفته", 2, ["mil_basic"], {"atk_land": 0.03, "accuracy": 0.02}, "باز شدن توپخانه")
_tech("mil_aviation", "mil", "هوانوردی نظامی", 3, ["mil_mech"], {"atk_air": 0.06}, "باز شدن پایگاه هوایی و جنگنده‌ها")
_tech("mil_naval", "mil", "نیروی دریایی", 3, ["mil_mech"], {"atk_naval": 0.06}, "باز شدن پایگاه دریایی و ناوها")
_tech("mil_doctrine", "mil", "آموزهٔ رزم ترکیبی", 5, ["mil_aviation", "mil_naval"], {"atk_land": 0.05, "atk_air": 0.05, "atk_naval": 0.05, "accuracy": 0.03}, "هماهنگی همهٔ نیروها + ناو هواپیمابر")
_tech("eco_taxation", "eco", "نظام مالیاتی", 1, [], {"tax": 0.06}, "افزایش بازدهی مالیات")
_tech("eco_banking", "eco", "بانکداری نوین", 2, ["eco_taxation"], {"prod_money": 0.08, "deposit_rate": 0.10}, "درآمد بانک و سود سپرده بیشتر")
_tech("eco_commerce", "eco", "بازرگانی بین‌الملل", 2, ["eco_taxation"], {"market_fee": -0.10}, "باز شدن مرکز تجارت")
_tech("eco_invest", "eco", "بازار سرمایه", 3, ["eco_banking"], {"prod_money": 0.06, "deposit_rate": 0.15}, "سرمایه‌گذاری‌های بلندمدت بهتر")
_tech("eco_planning", "eco", "برنامه‌ریزی مرکزی", 4, ["eco_commerce"], {"build_cost": -0.08}, "کاهش هزینهٔ ساخت")
_tech("eco_global", "eco", "اقتصاد جهانی", 5, ["eco_invest", "eco_planning"], {"market_fee": -0.20, "tax": 0.08}, "کارمزد کمتر و مالیات بیشتر")
_tech("ind_tools", "ind", "ابزارآلات صنعتی", 1, [], {"prod_iron": 0.08}, "بهره‌وری معدن")
_tech("ind_refining", "ind", "پالایش نفت", 2, ["ind_tools"], {"prod_fuel": 0.05}, "باز شدن پالایشگاه")
_tech("ind_agri", "ind", "کشاورزی مکانیزه", 2, ["ind_tools"], {"prod_food": 0.15}, "افزایش محصول")
_tech("ind_automation", "ind", "اتوماسیون کارخانه", 3, ["ind_refining"], {"prod_goods": 0.12, "build_speed": 0.05}, "تولید کالا و سرعت ساخت")
_tech("ind_heavy", "ind", "صنایع سنگین", 4, ["ind_automation"], {"prod_goods": 0.10, "prod_speed": 0.10}, "تولید نظامی سریع‌تر")
_tech("ind_smart", "ind", "صنعت هوشمند", 5, ["ind_heavy", "ind_agri"], {"prod_all": 0.06}, "بهبود همهٔ تولیدات")
_tech("int_basic", "int", "اطلاعات پایه", 1, [], {"intel_power": 0.10}, "شبکهٔ خبری")
_tech("int_sigint", "int", "شنود و سیگنال", 2, ["int_basic"], {"recon_acc": 0.08}, "دقت شناسایی بیشتر")
_tech("int_counter", "int", "ضدجاسوسی", 3, ["int_basic"], {"counter_intel": 0.15}, "دفع عملیات اطلاعاتی دشمن")
_tech("int_cyber", "int", "جنگ سایبری", 4, ["int_sigint"], {"intel_power": 0.15, "ew": 0.06}, "باز شدن یگان‌های جنگ الکترونیک")
_tech("int_satellite", "int", "ماهواره‌های شناسایی", 4, ["int_sigint"], {"recon_acc": 0.12, "intel_power": 0.10}, "دید ماهواره‌ای")
_tech("int_ai", "int", "تحلیلگر هوش مصنوعی", 5, ["int_cyber", "int_satellite"], {"intel_power": 0.25, "accuracy": 0.05}, "تحلیل کلان‌داده")
_tech("log_supply", "log", "زنجیرهٔ تدارکات", 1, [], {"storage": 0.15}, "باز شدن مرکز لجستیک")
_tech("log_rail", "log", "شبکهٔ ریلی", 2, ["log_supply"], {"supply": 0.08, "build_speed": 0.05}, "تدارکات و ساخت سریع‌تر")
_tech("log_fuel", "log", "بهینه‌سازی سوخت", 3, ["log_rail"], {"fuel_use": -0.10}, "مصرف سوخت کمتر")
_tech("log_air", "log", "پل هوایی", 4, ["log_fuel"], {"supply": 0.10}, "تدارکات سریع")
_tech("log_auto", "log", "انبار خودکار", 4, ["log_rail"], {"storage": 0.20, "upkeep_army": -0.05}, "ظرفیت انبار و نگهداری کمتر")
_tech("log_global", "log", "لجستیک جهانی", 5, ["log_air", "log_auto"], {"supply": 0.15, "loot": 0.10}, "غنیمت و تدارکات بیشتر")
_tech("def_bunkers", "def", "پناهگاه و استحکامات", 1, [], {"def_all": 0.05}, "باز شدن مرکز دفاع")
_tech("def_radar", "def", "شبکهٔ رادار", 2, ["def_bunkers"], {"def_air": 0.08}, "دفاع هوایی بهتر")
_tech("def_fortify", "def", "قلعه‌سازی", 3, ["def_bunkers"], {"city_def": 0.10}, "دفاع شهرها")
_tech("def_adv", "def", "دفاع لایه‌ای", 4, ["def_radar", "def_fortify"], {"def_all": 0.08}, "دفاع کلی بیشتر")
_tech("def_shield", "def", "سپر دفاع هوایی", 4, ["def_radar"], {"def_air": 0.12}, "پدافند هوایی قوی")
_tech("def_total", "def", "دفاع همه‌جانبه", 5, ["def_adv", "def_shield"], {"def_all": 0.12, "morale": 0.05}, "روحیهٔ دفاعی")
_tech("nrg_grid", "nrg", "شبکهٔ هوشمند برق", 1, [], {"prod_energy": 0.10}, "انرژی بیشتر")
_tech("nrg_efficiency", "nrg", "بهره‌وری انرژی", 2, ["nrg_grid"], {"energy_use": -0.10}, "مصرف انرژی کمتر")
_tech("nrg_oil", "nrg", "استخراج پیشرفتهٔ نفت", 3, ["nrg_grid"], {"prod_oil": 0.12}, "نفت بیشتر از میادین")
_tech("nrg_renew", "nrg", "انرژی تجدیدپذیر", 4, ["nrg_efficiency"], {"prod_energy": 0.15}, "انرژی پاک")
_tech("nrg_storage", "nrg", "ذخیره‌سازی انرژی", 4, ["nrg_oil"], {"storage": 0.10, "prod_energy": 0.05}, "ذخیرهٔ بیشتر")
_tech("nrg_fusion", "nrg", "همجوشی هسته‌ای غیرنظامی", 5, ["nrg_renew", "nrg_storage"], {"prod_energy": 0.25, "upkeep_build": -0.05}, "انقلاب انرژی")
_tech("dip_embassy", "dip", "سفارتخانه‌ها", 1, [], {"influence": 0.10}, "نفوذ دیپلماتیک")
_tech("dip_trade", "dip", "پیمان‌های تجاری", 2, ["dip_embassy"], {"relation_gain": 0.15}, "بهبود روابط سریع‌تر")
_tech("dip_treaty", "dip", "حقوق معاهدات", 3, ["dip_trade"], {"treaty_slots": 1}, "یک معاهدهٔ بیشتر")
_tech("dip_alliance", "dip", "دیپلماسی ائتلاف", 3, ["dip_embassy"], {"alliance_bonus": 0.10}, "بونوس اتحاد")
_tech("dip_soft", "dip", "قدرت نرم", 4, ["dip_trade"], {"stability": 4}, "ثبات داخلی")
_tech("dip_world", "dip", "نظم نوین جهانی", 5, ["dip_treaty", "dip_alliance", "dip_soft"], {"influence": 0.25, "treaty_slots": 1}, "نفوذ جهانی")

TECHS = {t[0]: dict(id=t[0], branch=t[1], name=t[2], tier=t[3], cost={"money": t[4], "goods": t[5], "rare": t[6]}, time=t[7], pre=t[8], fx=t[9], desc=t[10]) for t in _T}

# ============================================================================
# یگان‌ها و تجهیزات
# atk def mob acc rng dur ; مشخصات صرفاً «عددهای بازی» هستند
# ============================================================================
RARITY = {"common": ("⚪", "معمولی", 1.0), "uncommon": ("🟢", "غیرمعمول", 1.1), "rare": ("🔵", "کمیاب", 1.25),
          "epic": ("🟣", "حماسی", 1.45), "legendary": ("🟠", "افسانه‌ای", 1.7), "mythic": ("🔴", "اسطوره‌ای", 2.1)}
CATEGORIES = {
    "infantry": ("🪖", "پیاده‌نظام", "barracks", "land"), "recon": ("🔭", "شناسایی", "barracks", "land"),
    "armor": ("🛡️", "زرهی", "military_factory", "land"), "artillery": ("💥", "توپخانه", "military_factory", "land"),
    "air_defense": ("📡", "پدافند هوایی", "military_factory", "air"), "aircraft": ("✈️", "هواپیما", "air_base", "air"),
    "helicopter": ("🚁", "بالگرد", "air_base", "air"), "drone": ("🛸", "پهپاد", "air_base", "air"),
    "naval": ("🚢", "ناوگان", "naval_base", "naval"), "submarine": ("🤿", "زیردریایی", "naval_base", "naval"),
    "carrier": ("🛳️", "ناو هواپیمابر", "naval_base", "naval"), "logistics": ("🚚", "لجستیک", "barracks", "support"),
    "ew": ("📶", "جنگ الکترونیک", "military_factory", "support"), "support": ("⚕️", "پشتیبانی", "barracks", "support"),
}
UNITS = {}


def _u(key, name, cat, rar, atk, dfn, mob, acc, rng, dur_, cost, maint, t, tech=None, bl=1, craft=True):
    UNITS[key] = dict(key=key, name=name, cat=cat, rarity=rar, atk=atk, df=dfn, mob=mob, acc=acc, rng=rng, dur=dur_,
                      cost=cost, maint=maint, time=t, tech=tech, bl=bl, craft=craft, kind="unit")


# پیاده‌نظام و شناسایی و پشتیبانی و لجستیک (پادگان)
_u("rifleman", "تفنگدار پیاده", "infantry", "common", 4, 5, 30, 60, 3, 30, {"money": 120, "iron": 6, "food": 4}, {"money": 0.5, "food": 0.15}, 20)
_u("sniper", "تک‌تیرانداز", "infantry", "uncommon", 9, 4, 32, 90, 8, 25, {"money": 320, "iron": 8, "food": 4}, {"money": 1.0, "food": 0.15}, 40, "mil_basic")
_u("spec_ops", "نیروی ویژه", "infantry", "epic", 22, 14, 45, 92, 10, 45, {"money": 1600, "iron": 30, "goods": 4, "rare": 0.5}, {"money": 4, "food": 0.2}, 150, "mil_basic", 3)
_u("marines", "تفنگدار دریایی", "infantry", "rare", 14, 11, 38, 75, 5, 40, {"money": 700, "iron": 20, "goods": 2}, {"money": 2, "food": 0.2}, 90, "mil_basic", 2)
_u("scout_car", "خودروی شناسایی BRDM", "recon", "common", 6, 8, 60, 70, 12, 40, {"money": 520, "iron": 40, "fuel": 4}, {"money": 1.4, "fuel": 0.3}, 60, "mil_basic")
_u("recon_team", "تیم شناسایی ویژه", "recon", "rare", 8, 6, 55, 95, 20, 30, {"money": 1100, "iron": 12, "goods": 3}, {"money": 3, "food": 0.15}, 120, "int_basic", 2)
_u("field_hospital", "بیمارستان صحرایی", "support", "uncommon", 0, 12, 25, 50, 0, 60, {"money": 900, "iron": 40, "goods": 8}, {"money": 3, "food": 0.3}, 120, "mil_basic", 2)
_u("combat_engineers", "مهندسی رزمی", "support", "uncommon", 5, 20, 30, 60, 2, 50, {"money": 800, "iron": 50, "goods": 6}, {"money": 2.5, "food": 0.2}, 110, "mil_basic", 2)
_u("field_repair", "واحد تعمیرات صحرایی", "support", "rare", 3, 16, 35, 60, 0, 70, {"money": 1500, "iron": 90, "goods": 20}, {"money": 4, "fuel": 0.3}, 200, "mil_mech", 3)
_u("supply_truck", "کامیون تدارکات", "logistics", "common", 0, 6, 55, 50, 0, 45, {"money": 650, "iron": 45, "fuel": 5}, {"money": 1.5, "fuel": 0.4}, 70, "log_supply", 1)
_u("fuel_convoy", "ستون سوخت‌رسان", "logistics", "uncommon", 0, 9, 50, 50, 0, 55, {"money": 1300, "iron": 80, "goods": 10}, {"money": 3, "fuel": 0.5}, 130, "log_supply", 2)
_u("c130", "هواپیمای ترابری C-130", "logistics", "rare", 0, 12, 90, 60, 0, 65, {"money": 4200, "iron": 220, "goods": 50, "fuel": 30}, {"money": 8, "fuel": 1.2}, 420, "log_air", 4)
# زرهی و توپخانه و پدافند و جنگ الکترونیک (کارخانهٔ نظامی)
_u("bmp3", "نفربر BMP-3", "armor", "common", 14, 15, 50, 65, 6, 60, {"money": 1800, "iron": 160, "goods": 20, "fuel": 8}, {"money": 3, "fuel": 0.6}, 130, "mil_mech", 1)
_u("t72", "تانک T-72", "armor", "uncommon", 26, 24, 42, 66, 8, 80, {"money": 3200, "iron": 320, "goods": 40, "fuel": 12}, {"money": 5, "fuel": 0.9}, 200, "mil_mech", 2)
_u("leopard2", "تانک Leopard 2A7", "armor", "epic", 42, 40, 44, 82, 9, 100, {"money": 7800, "iron": 520, "goods": 90, "rare": 2, "fuel": 16}, {"money": 9, "fuel": 1.2}, 340, "mil_mech", 4)
_u("abrams", "تانک M1 Abrams", "armor", "epic", 44, 42, 42, 84, 9, 105, {"money": 8600, "iron": 560, "goods": 95, "rare": 2.5, "fuel": 18}, {"money": 10, "fuel": 1.3}, 360, "mil_mech", 4)
_u("armata", "تانک T-14 Armata", "armor", "legendary", 60, 58, 46, 88, 10, 120, {"money": 16000, "iron": 800, "goods": 160, "rare": 6, "fuel": 22}, {"money": 16, "fuel": 1.6}, 600, "mil_doctrine", 6)
_u("d30", "هویتزر D-30", "artillery", "common", 24, 6, 20, 58, 26, 40, {"money": 1600, "iron": 180, "goods": 15}, {"money": 2.5, "fuel": 0.3}, 120, "mil_artillery", 1)
_u("paladin", "خودکششی M109 Paladin", "artillery", "uncommon", 34, 9, 30, 70, 30, 55, {"money": 3400, "iron": 300, "goods": 35, "fuel": 8}, {"money": 5, "fuel": 0.7}, 220, "mil_artillery", 2)
_u("himars", "راکت‌انداز HIMARS", "artillery", "epic", 52, 8, 40, 90, 55, 45, {"money": 7500, "iron": 400, "goods": 80, "rare": 3, "fuel": 10}, {"money": 8, "fuel": 0.8}, 340, "mil_artillery", 4)
_u("smerch", "راکت‌انداز BM-30 Smerch", "artillery", "rare", 46, 7, 32, 72, 70, 42, {"money": 5600, "iron": 380, "goods": 60, "fuel": 10}, {"money": 7, "fuel": 0.8}, 300, "mil_artillery", 3)
_u("pantsir", "پدافند Pantsir", "air_defense", "uncommon", 10, 30, 40, 78, 20, 50, {"money": 3600, "iron": 220, "goods": 45, "rare": 1}, {"money": 6, "fuel": 0.4}, 240, "def_radar", 2)
_u("s300", "پدافند S-300", "air_defense", "rare", 14, 52, 25, 84, 60, 60, {"money": 7200, "iron": 340, "goods": 80, "rare": 3}, {"money": 10, "fuel": 0.5}, 380, "def_radar", 3)
_u("patriot", "پدافند Patriot", "air_defense", "epic", 16, 66, 25, 88, 70, 60, {"money": 11500, "iron": 420, "goods": 110, "rare": 5}, {"money": 14, "fuel": 0.6}, 480, "def_shield", 4)
_u("s400", "پدافند S-400", "air_defense", "legendary", 20, 90, 25, 92, 110, 70, {"money": 19000, "iron": 560, "goods": 170, "rare": 9}, {"money": 22, "fuel": 0.8}, 720, "def_total", 6)
_u("jammer", "ایستگاه پارازیت", "ew", "uncommon", 2, 14, 35, 80, 30, 35, {"money": 3000, "iron": 120, "goods": 50, "rare": 2}, {"money": 6, "fuel": 0.2}, 240, "int_cyber", 3)
_u("krasukha", "سامانهٔ EW Krasukha-4", "ew", "epic", 4, 22, 35, 90, 45, 45, {"money": 9200, "iron": 240, "goods": 120, "rare": 6}, {"money": 12, "fuel": 0.4}, 500, "int_cyber", 5)
# هوایی (پایگاه هوایی)
_u("f16", "جنگندهٔ F-16", "aircraft", "rare", 44, 26, 95, 80, 40, 60, {"money": 12500, "iron": 380, "goods": 130, "rare": 4, "fuel": 40}, {"money": 12, "fuel": 1.6}, 480, "mil_aviation", 3)
_u("su35", "جنگندهٔ Su-35", "aircraft", "epic", 52, 30, 98, 82, 45, 62, {"money": 15500, "iron": 420, "goods": 150, "rare": 5, "fuel": 46}, {"money": 14, "fuel": 1.8}, 540, "mil_aviation", 4)
_u("rafale", "جنگندهٔ Rafale", "aircraft", "epic", 54, 32, 96, 84, 46, 64, {"money": 16500, "iron": 430, "goods": 160, "rare": 5, "fuel": 44}, {"money": 15, "fuel": 1.7}, 560, "mil_aviation", 4)
_u("f35", "جنگندهٔ F-35", "aircraft", "legendary", 72, 44, 98, 92, 55, 70, {"money": 26000, "iron": 520, "goods": 220, "rare": 10, "fuel": 52}, {"money": 22, "fuel": 2.0}, 820, "mil_doctrine", 6)
_u("j20", "جنگندهٔ J-20", "aircraft", "legendary", 70, 42, 98, 90, 58, 68, {"money": 25000, "iron": 520, "goods": 215, "rare": 10, "fuel": 52}, {"money": 21, "fuel": 2.0}, 800, "mil_doctrine", 6)
_u("b21", "بمب‌افکن B-21", "aircraft", "mythic", 110, 30, 85, 94, 140, 90, {"money": 60000, "iron": 800, "goods": 400, "rare": 30, "fuel": 90}, {"money": 40, "fuel": 3.0}, 1800, "mil_doctrine", 8, False)
_u("apache", "بالگرد AH-64 Apache", "helicopter", "rare", 34, 22, 75, 82, 14, 50, {"money": 9800, "iron": 260, "goods": 90, "rare": 3, "fuel": 22}, {"money": 10, "fuel": 1.2}, 360, "mil_aviation", 3)
_u("ka52", "بالگرد Ka-52", "helicopter", "rare", 33, 22, 74, 80, 14, 52, {"money": 9300, "iron": 250, "goods": 88, "rare": 3, "fuel": 22}, {"money": 10, "fuel": 1.2}, 350, "mil_aviation", 3)
_u("blackhawk", "بالگرد UH-60 Black Hawk", "helicopter", "uncommon", 12, 16, 70, 68, 10, 46, {"money": 5200, "iron": 180, "goods": 50, "fuel": 16}, {"money": 6, "fuel": 0.9}, 260, "mil_aviation", 2)
_u("tb2", "پهپاد Bayraktar TB2", "drone", "uncommon", 16, 6, 60, 82, 25, 22, {"money": 2800, "iron": 60, "goods": 35, "rare": 1, "fuel": 6}, {"money": 3, "fuel": 0.4}, 150, "mil_aviation", 2)
_u("shahed136", "پهپاد انتحاری Shahed-136", "drone", "common", 22, 3, 55, 60, 60, 10, {"money": 1500, "iron": 25, "goods": 12, "fuel": 3}, {"money": 1.0, "fuel": 0.1}, 60, "mil_aviation", 1)
_u("mq9", "پهپاد MQ-9 Reaper", "drone", "rare", 30, 10, 62, 90, 45, 30, {"money": 7800, "iron": 90, "goods": 70, "rare": 3, "fuel": 10}, {"money": 8, "fuel": 0.6}, 300, "int_satellite", 3)
_u("globalhawk", "پهپاد RQ-4 Global Hawk", "drone", "epic", 4, 12, 70, 96, 120, 35, {"money": 12000, "iron": 90, "goods": 90, "rare": 6, "fuel": 16}, {"money": 12, "fuel": 0.8}, 420, "int_satellite", 4)
# دریایی (پایگاه دریایی)
_u("frigate", "ناوچهٔ Type 054", "naval", "uncommon", 40, 38, 55, 72, 35, 90, {"money": 15000, "iron": 900, "goods": 150, "fuel": 60}, {"money": 16, "fuel": 2.2}, 600, "mil_naval", 2)
_u("destroyer", "ناوشکن Arleigh Burke", "naval", "epic", 70, 62, 52, 84, 55, 120, {"money": 32000, "iron": 1400, "goods": 260, "rare": 6, "fuel": 90}, {"money": 30, "fuel": 3.2}, 1100, "mil_naval", 5)
_u("kirov", "رزم‌ناو Kirov", "naval", "legendary", 105, 84, 40, 80, 80, 170, {"money": 55000, "iron": 2200, "goods": 400, "rare": 12, "fuel": 140}, {"money": 46, "fuel": 4.6}, 1600, "mil_doctrine", 6)
_u("kilo", "زیردریایی Kilo", "submarine", "rare", 46, 34, 40, 78, 45, 70, {"money": 20000, "iron": 1000, "goods": 180, "rare": 4, "fuel": 50}, {"money": 20, "fuel": 1.4}, 780, "mil_naval", 3)
_u("type212", "زیردریایی Type 212", "submarine", "epic", 58, 42, 42, 86, 55, 78, {"money": 30000, "iron": 1150, "goods": 230, "rare": 8, "fuel": 46}, {"money": 27, "fuel": 1.2}, 980, "mil_naval", 5)
_u("virginia", "زیردریایی Virginia", "submarine", "legendary", 84, 58, 46, 90, 75, 95, {"money": 48000, "iron": 1500, "goods": 300, "rare": 14, "fuel": 60}, {"money": 38, "fuel": 1.8}, 1400, "mil_doctrine", 6)
_u("liaoning", "ناو هواپیمابر Liaoning", "carrier", "legendary", 120, 110, 30, 78, 100, 260, {"money": 90000, "iron": 3600, "goods": 700, "rare": 18, "fuel": 220}, {"money": 70, "fuel": 6.0}, 2400, "mil_doctrine", 7)
_u("nimitz", "ناو هواپیمابر Nimitz", "carrier", "mythic", 160, 140, 32, 88, 130, 320, {"money": 150000, "iron": 5000, "goods": 1000, "rare": 40, "fuel": 320}, {"money": 100, "fuel": 8.0}, 3600, "mil_doctrine", 9, False)

# آیتم‌های مصرفی
ITEMS = {
    "repair_kit": dict(key="repair_kit", name="کیت تعمیر", emoji="🧰", rarity="common", kind="item", desc="۳۰ واحد دوام به همهٔ یگان‌های مستقر برمی‌گرداند", price={"money": 3000, "goods": 20}),
    "field_rations": dict(key="field_rations", name="جیرهٔ رزمی", emoji="🍱", rarity="uncommon", kind="item", desc="۶ ساعت +۱۰٪ روحیه در نبردها", price={"money": 4500, "food": 400}),
    "intel_dossier": dict(key="intel_dossier", name="پروندهٔ اطلاعاتی", emoji="🗂️", rarity="rare", kind="item", desc="۲ ساعت +۱۵٪ کیفیت شناسایی نبرد", price={"money": 9000, "goods": 60}),
    "supply_crate": dict(key="supply_crate", name="صندوق تدارکات", emoji="📦", rarity="uncommon", kind="item", desc="مقداری تصادفی از منابع پایه", price={"money": 6000}),
    "war_bond": dict(key="war_bond", name="اوراق جنگی", emoji="🎖️", rarity="epic", kind="item", desc="۶۰۰ امتیاز تجربه", price={"gems": 25}),
    "peace_charm": dict(key="peace_charm", name="طلسم صلح", emoji="🕊️", rarity="legendary", kind="item", desc="۶ ساعت سپر ضدحمله (قابل‌شکستن با حمله)", price={"gems": 60}),
    "mythic_cache": dict(key="mythic_cache", name="صندوق اسطوره‌ای", emoji="👑", rarity="mythic", kind="item", desc="یک یگان اسطوره‌ای تصادفی!", price=None),
}


def item_info(key):
    return UNITS.get(key) or ITEMS.get(key)


def item_name(key):
    i = item_info(key)
    return f"{(CATEGORIES[i['cat']][0] if i['kind'] == 'unit' else i['emoji'])} {i['name']}"


UPGRADE_MAX = 5
UPGRADE_STAT_GAIN = 0.12      # هر سطح ۱۲٪ قوی‌تر

# ============================================================================
# ماموریت‌ها  (id, period, kind, title, metric, target, mode, reward, min_level)
# ============================================================================
MISSIONS = {}


def _m(id_, period, kind, title, metric, target, reward, mode="sum", lvl=1):
    MISSIONS[id_] = dict(id=id_, period=period, kind=kind, title=title, metric=metric, target=target, reward=reward, mode=mode, lvl=lvl)


for _i, (_n, _tg, _r) in enumerate([("یک نبرد را ببر", 1, {"money": 6000, "xp": 60}), ("سه نبرد را ببر", 3, {"money": 15000, "xp": 150})]):
    _m(f"d_win{_i}", "daily", "military", f"⚔️ {_n}", "battles_won", _tg, _r)
_m("d_build", "daily", "economic", "🏗️ یک ساختمان را ارتقا بده", "buildings_upgraded", 1, {"money": 4000, "goods": 20, "xp": 50})
_m("d_trade", "daily", "trade", "🛒 ۳ معاملهٔ بازار انجام بده", "market_trades", 3, {"money": 5000, "xp": 60})
_m("d_volume", "daily", "trade", "📈 ۵۰,۰۰۰ پول حجم معاملات بساز", "market_volume", 50000, {"money": 8000, "xp": 80})
_m("d_units", "daily", "military", "🪖 ۱۰ یگان تولید کن", "units_built", 10, {"money": 5000, "iron": 200, "xp": 60})
_m("d_recon", "daily", "intelligence", "🕵️ یک عملیات اطلاعاتی انجام بده", "intel_ops", 1, {"money": 4000, "xp": 60})
_m("d_dip", "daily", "diplomatic", "🕊️ یک اقدام دیپلماتیک انجام بده", "diplomacy_actions", 1, {"money": 3500, "xp": 50, "influence": 3})
_m("d_res", "daily", "technology", "🔬 یک فناوری را تحقیق کن", "research_done", 1, {"money": 6000, "xp": 90})
_m("d_all", "daily", "alliance", "🤝 به خزانهٔ اتحاد کمک کن", "alliance_contrib", 1, {"money": 3000, "xp": 50}, lvl=2)
_m("d_daily", "daily", "economic", "🎁 پاداش روزانه را دریافت کن", "daily_claims", 1, {"money": 2000, "xp": 30})
_m("w_win", "weekly", "military", "🏆 ۱۲ نبرد را ببر", "battles_won", 12, {"money": 60000, "gems": 10, "xp": 600})
_m("w_trade", "weekly", "trade", "🏪 ۲۵ معامله انجام بده", "market_trades", 25, {"money": 50000, "gems": 8, "xp": 500})
_m("w_build", "weekly", "economic", "🏗️ ۸ ارتقای ساختمان", "buildings_upgraded", 8, {"money": 45000, "goods": 200, "gems": 8, "xp": 500})
_m("w_units", "weekly", "military", "🪖 ۱۵۰ یگان تولید کن", "units_built", 150, {"money": 55000, "gems": 8, "xp": 500})
_m("w_intel", "weekly", "intelligence", "🕵️ ۸ عملیات اطلاعاتی", "intel_ops", 8, {"money": 40000, "gems": 6, "xp": 450})
_m("w_tech", "weekly", "technology", "🔬 ۳ فناوری", "research_done", 3, {"money": 50000, "rare": 20, "gems": 8, "xp": 500})
_m("w_dip", "weekly", "diplomatic", "🕊️ ۶ اقدام دیپلماتیک", "diplomacy_actions", 6, {"money": 30000, "influence": 12, "gems": 5, "xp": 400})
_m("w_ally", "weekly", "alliance", "🤝 ۵ کمک به اتحاد", "alliance_contrib", 5, {"money": 35000, "gems": 6, "xp": 400}, lvl=2)
_m("p_first_city", "perm", "military", "🏙️ اولین شهر خارجی را فتح کن", "cities_captured", 1, {"money": 40000, "gems": 15, "xp": 500}, lvl=3)
_m("p_lvl10", "perm", "economic", "⭐ به سطح ۱۰ برس", "level", 10, {"money": 50000, "gems": 15, "xp": 0}, mode="max")
_m("p_money", "perm", "economic", "💰 ۱,۰۰۰,۰۰۰ پول جمع کن", "money_peak", 1000000, {"gems": 20, "xp": 800}, mode="max", lvl=3)
_m("p_army", "perm", "military", "🪖 قدرت نظامی ۵۰,۰۰۰ برسان", "mil_power", 50000, {"money": 60000, "gems": 15, "xp": 700}, mode="max", lvl=3)
_m("p_ally", "perm", "alliance", "🤝 عضو یک اتحاد شو", "alliance_joined", 1, {"money": 15000, "gems": 5, "xp": 200})
_m("p_wr", "perm", "military", "🎮 یک نبرد اتاق جنگ را ببر", "war_room_won", 1, {"money": 25000, "gems": 8, "xp": 300}, lvl=2)
_m("p_gw", "perm", "alliance", "👥 در یک جنگ گروهی شرکت کن", "group_war_done", 1, {"money": 30000, "gems": 10, "xp": 400}, lvl=3)
_m("p_invest", "perm", "economic", "🏦 سرمایه‌گذاری ۲۰۰,۰۰۰ پول", "invest_total", 200000, {"money": 30000, "gems": 10, "xp": 300}, lvl=3)
_m("p_offer", "perm", "trade", "🏷️ یک آگهی فروش بین بازیکنان بگذار", "offers_created", 1, {"money": 12000, "xp": 150}, lvl=2)
_m("p_treaty", "perm", "diplomatic", "📜 یک معاهدهٔ دوجانبه ببند", "treaties_signed", 1, {"money": 20000, "influence": 8, "xp": 250}, lvl=3)
_m("e_defend", "emergency", "emergency", "🚨 (اضطراری) ۲ نبرد را ببر", "battles_won", 2, {"money": 40000, "gems": 12, "xp": 400})
_m("e_supply", "emergency", "emergency", "🚨 (اضطراری) ۳ معاملهٔ بازار", "market_trades", 3, {"money": 30000, "gems": 10, "xp": 300})
_m("e_intel", "emergency", "emergency", "🚨 (اضطراری) ۳ عملیات اطلاعاتی", "intel_ops", 3, {"money": 35000, "gems": 10, "xp": 300})
_m("r_raid", "random", "military", "🎲 (تصادفی) یک نبرد PvP را ببر", "pvp_won", 1, {"money": 20000, "gems": 6, "xp": 250})
_m("r_build", "random", "economic", "🎲 (تصادفی) ۲ ارتقای ساختمان", "buildings_upgraded", 2, {"money": 18000, "goods": 60, "xp": 220})
_m("r_trade", "random", "trade", "🎲 (تصادفی) ۱۰۰,۰۰۰ حجم معاملات", "market_volume", 100000, {"money": 22000, "gems": 5, "xp": 240})
_m("r_recon", "random", "intelligence", "🎲 (تصادفی) ۳ عملیات شناسایی", "intel_ops", 3, {"money": 20000, "xp": 220})
_m("r_units", "random", "military", "🎲 (تصادفی) ۲۵ یگان تولید کن", "units_built", 25, {"money": 20000, "iron": 400, "xp": 220})

# ============================================================================
# دستاوردها (تولید داده‌محور: هر خط یک زنجیرهٔ پله‌ای)
# ============================================================================
ACHIEVEMENTS = {}


def _chain(cat, stat, title, tiers, unit_emoji, rewards_scale=1):
    for i, thr in enumerate(tiers):
        aid = f"{stat}_{i + 1}"
        reward = {"money": int(5000 * (2.2 ** i) * rewards_scale), "gems": 2 + i * 3, "xp": 100 * (i + 1) * (i + 1)}
        ACHIEVEMENTS[aid] = dict(id=aid, cat=cat, title=f"{unit_emoji} {title} {['I', 'II', 'III', 'IV', 'V', 'VI'][i]}", stat=stat, thr=thr, reward=reward,
                                 desc=f"{fmt(thr)} — {title}")


_chain("military", "battles_won", "پیروزی در نبرد", [1, 10, 50, 200, 1000], "🏆")
_chain("military", "battles_fought", "سابقهٔ نبرد", [5, 40, 150, 600], "⚔️")
_chain("military", "units_built", "تولید یگان", [20, 200, 1000, 5000], "🪖")
_chain("military", "pvp_won", "پیروزی PvP", [1, 15, 100], "🎯")
_chain("military", "war_room_won", "استاد اتاق جنگ", [1, 5, 25], "🎮")
_chain("military", "mil_power", "قدرت نظامی", [5000, 30000, 150000, 800000], "💪")
_chain("economy", "money_peak", "ثروت", [100000, 1000000, 10000000, 100000000], "💰")
_chain("economy", "buildings_upgraded", "توسعهٔ ساختمان", [3, 25, 100, 300], "🏗️")
_chain("economy", "gdp_peak", "تولید ناخالص", [5000, 25000, 100000], "🏭")
_chain("economy", "invest_total", "سرمایه‌گذار", [50000, 500000, 5000000], "🏦")
_chain("trade", "market_trades", "معامله‌گر", [5, 50, 250, 1000], "🛒")
_chain("trade", "market_volume", "حجم معاملات", [100000, 1000000, 10000000], "📈")
_chain("trade", "offers_sold", "فروشندهٔ بازار آزاد", [1, 10, 50], "🏷️")
_chain("technology", "research_done", "پژوهشگر", [1, 6, 20, 40], "🔬")
_chain("diplomacy", "diplomacy_actions", "دیپلمات", [1, 15, 60, 200], "🕊️")
_chain("diplomacy", "treaties_signed", "معاهده‌ساز", [1, 5, 15], "📜")
_chain("intelligence", "intel_ops", "جاسوس", [1, 15, 60, 250], "🕵️")
_chain("intelligence", "spec_ops_won", "عملیات ویژه", [1, 10, 40], "🥷")
_chain("territory", "cities_captured", "فاتح شهرها", [1, 5, 15, 40], "🏙️")
_chain("territory", "territory_peak", "قلمرو", [2, 4, 8, 15], "🗺️")
_chain("alliance", "alliance_contrib", "یار اتحاد", [1, 10, 50, 200], "🤝")
_chain("alliance", "alliance_votes", "شهروند فعال", [1, 10, 40], "🗳️")
_chain("alliance", "group_war_done", "جنگ گروهی", [1, 5, 20], "👥")
_chain("growth", "level", "سطح فرمانده", [5, 10, 20, 35, 50], "⭐")
_chain("growth", "tech_count", "دانش", [3, 10, 25, 48], "📚")
_chain("activity", "daily_claims", "حضور روزانه", [1, 7, 30, 100], "🎁")
_chain("activity", "logins", "تعداد ورود", [10, 100, 500], "📱")
_chain("activity", "missions_claimed", "مأموریت‌باز", [1, 20, 100, 400], "🎯")

# ============================================================================
# رویدادهای جهانی
# effects: prod_<res> ضریب تولید، price_<res> ضریب قیمت، tax، upkeep، stability(+/- امتیاز)، market_fee، build_cost، garrison، relation
# ============================================================================
EVENT_TYPES = {
    "economic_crisis": dict(title="📉 بحران اقتصادی", scope="global", dur=(3 * 3600, 8 * 3600), w=10, fx={"tax": 0.80, "prod_money": 0.85, "stability": -4, "deposit_rate": 0.5, "price_goods": 1.10},
                            desc="مالیات و درآمد بانک‌ها کاهش یافته و سود سپرده‌ها افت کرده است."),
    "energy_crisis": dict(title="🔥 بحران انرژی", scope="global", dur=(2 * 3600, 6 * 3600), w=10, fx={"prod_energy": 0.70, "price_energy": 1.6, "price_fuel": 1.3, "price_oil": 1.25},
                          desc="کمبود شدید انرژی؛ تولید برق کم و قیمت سوخت بالا رفته است."),
    "resource_boom": dict(title="⛏️ شکوفایی منابع", scope="global", dur=(3 * 3600, 8 * 3600), w=9, fx={"prod_iron": 1.30, "prod_rare": 1.30, "price_iron": 0.80, "price_rare": 0.85},
                          desc="کشف ذخایر جدید؛ تولید و عرضهٔ آهن و منابع کمیاب بالا رفته است."),
    "trade_shock": dict(title="🌪️ شوک تجاری", scope="global", dur=(1 * 3600, 4 * 3600), w=8, fx={"market_fee": 1.0, "volatility": 2.0},
                        desc="نوسان شدید بازار و کارمزد معاملات دوبرابر شده است."),
    "industrial_boom": dict(title="🏭 رونق صنعتی", scope="global", dur=(3 * 3600, 8 * 3600), w=9, fx={"prod_goods": 1.25, "build_cost": 0.90, "price_goods": 0.85},
                            desc="کارخانه‌ها با ظرفیت بالا کار می‌کنند و هزینهٔ ساخت کمتر شده است."),
    "regional_tension": dict(title="⚠️ تنش منطقه‌ای", scope="region", dur=(2 * 3600, 6 * 3600), w=9, fx={"upkeep_army": 1.15, "garrison": 1.20, "stability": -3, "relation": -6},
                             desc="تنش‌ها در منطقه بالا رفته؛ نگهداری ارتش گران‌تر و پادگان‌های دشمن قوی‌تر شده‌اند."),
    "political_crisis": dict(title="🏛️ بحران سیاسی", scope="country", dur=(2 * 3600, 6 * 3600), w=8, fx={"stability": -10, "tax": 0.90, "relation": -8},
                             desc="بی‌ثباتی سیاسی در یک کشور؛ ثبات و مالیات کاهش یافته است."),
    "resource_shortage": dict(title="🥀 کمبود منابع", scope="global", dur=(2 * 3600, 6 * 3600), w=8, fx={"prod_food": 0.75, "prod_oil": 0.75, "price_food": 1.4, "price_oil": 1.35},
                              desc="قحطی و کمبود نفت؛ تولید غذا و نفت کم و قیمت‌ها بالاست."),
}

RANKS = [(1, "سرباز"), (3, "گروهبان"), (6, "ستوان"), (10, "سروان"), (15, "سرگرد"), (20, "سرهنگ"), (30, "سرتیپ"), (40, "سرلشکر"), (55, "ژنرال"), (70, "مارشال"), (90, "فرمانده جهان")]

# ============================================================================
# اتحاد
# ============================================================================
ALLIANCE_ROLES = {
    "leader": ("👑", "رهبر", {"invite", "kick", "promote", "treasury", "war", "diplomacy", "vote", "research", "project", "edit", "groupwar", "disband"}),
    "deputy": ("⭐", "معاون", {"invite", "kick", "promote", "treasury", "war", "diplomacy", "vote", "research", "project", "groupwar"}),
    "diplomat": ("🕊️", "دیپلمات", {"invite", "diplomacy", "vote"}),
    "commander": ("🎖️", "فرمانده", {"war", "groupwar", "vote", "invite"}),
    "quartermaster": ("📦", "خزانه‌دار", {"treasury", "research", "project", "vote"}),
    "member": ("🪖", "عضو", {"vote"}),
    "recruit": ("🌱", "تازه‌وارد", set()),
}
ROLE_RANK = {"leader": 6, "deputy": 5, "diplomat": 4, "commander": 4, "quartermaster": 4, "member": 2, "recruit": 1}
PERM_NAMES = {"invite": "دعوت", "kick": "اخراج", "promote": "ارتقا/تنزل", "treasury": "خزانه", "war": "اعلام جنگ", "diplomacy": "دیپلماسی",
              "vote": "رأی/ایجاد رأی‌گیری", "research": "پژوهش اتحاد", "project": "پروژه‌ها", "edit": "ویرایش", "groupwar": "جنگ گروهی", "disband": "انحلال"}
ALLIANCE_LEVEL_XP = [0, 1000, 3000, 7000, 15000, 30000, 60000, 110000, 200000, 350000]
ALLIANCE_TECHS = {
    "at_econ": dict(name="اقتصاد مشترک", cost={"money": 200000, "goods": 500}, fx={"prod_money": 0.05}, desc="+۵٪ درآمد پول همهٔ اعضا", lvl=1),
    "at_defense": dict(name="پدافند مشترک", cost={"money": 300000, "iron": 8000}, fx={"def_all": 0.05}, desc="+۵٪ دفاع همهٔ اعضا", lvl=2),
    "at_intel": dict(name="شبکهٔ اطلاعاتی مشترک", cost={"money": 300000, "goods": 800}, fx={"intel_power": 0.08}, desc="+۸٪ قدرت اطلاعاتی", lvl=2),
    "at_logistics": dict(name="لجستیک مشترک", cost={"money": 350000, "fuel": 6000}, fx={"supply": 0.08}, desc="+۸٪ تدارکات نبرد", lvl=3),
    "at_industry": dict(name="زنجیرهٔ صنعتی", cost={"money": 450000, "iron": 20000, "goods": 2000}, fx={"prod_goods": 0.06, "prod_iron": 0.06}, desc="+۶٪ تولید کالا و آهن", lvl=3),
    "at_offense": dict(name="فرماندهی مشترک", cost={"money": 600000, "goods": 3000, "rare": 200}, fx={"atk_land": 0.04, "atk_air": 0.04, "atk_naval": 0.04}, desc="+۴٪ حملهٔ همهٔ اعضا", lvl=4),
    "at_research": dict(name="آکادمی اتحاد", cost={"money": 500000, "goods": 2500, "rare": 150}, fx={"research_speed": 0.10}, desc="+۱۰٪ سرعت تحقیق", lvl=4),
    "at_diplomacy": dict(name="دفتر دیپلماسی", cost={"money": 400000, "goods": 1000}, fx={"influence": 0.10, "stability": 2}, desc="+۱۰٪ نفوذ و +۲ ثبات", lvl=5),
}
ALLIANCE_PROJECTS = {
    "supply_depot": dict(name="انبار راهبردی", goal={"money": 250000, "iron": 12000, "goods": 1200}, fx={"storage": 0.10}, xp=1500, desc="+۱۰٪ ظرفیت انبار اعضا"),
    "war_academy": dict(name="آکادمی جنگ", goal={"money": 400000, "goods": 2500, "rare": 120}, fx={"morale": 0.05, "accuracy": 0.02}, xp=2500, desc="+۵٪ روحیه و دقت"),
    "energy_grid": dict(name="شبکهٔ انرژی ملی", goal={"money": 300000, "iron": 15000, "oil": 8000}, fx={"prod_energy": 0.08}, xp=2000, desc="+۸٪ انرژی"),
    "trade_hub": dict(name="مرکز تجارت آزاد", goal={"money": 500000, "goods": 2000}, fx={"market_fee": -0.10}, xp=2500, desc="کارمزد بازار ۱۰٪ کمتر"),
}
VOTE_KINDS = {"war": "اعلام جنگ به اتحاد", "kick": "اخراج عضو", "peace": "درخواست صلح", "spend": "هزینهٔ خزانه", "policy": "تغییر سیاست عضوگیری"}

# ============================================================================
# دیپلماسی، اقدام‌های اتاق جنگ، استراتژی‌ها، نقش‌های جنگ گروهی
# ============================================================================
TREATY_KINDS = {"nap": ("🕊️", "پیمان عدم تجاوز", 72 * 3600, 5), "trade": ("🤝", "پیمان تجاری", 72 * 3600, 8), "defense": ("🛡️", "پیمان دفاعی", 72 * 3600, 15)}
DIP_ACTIONS = {
    "aid": ("🎁", "ارسال کمک اقتصادی", "۲۰,۰۰۰ پول → روابط +۸", 20000),
    "talks": ("🗣️", "مذاکرات سطح بالا", "۴ نفوذ → روابط +۵", 0),
    "trade_deal": ("📦", "توافق تجاری", "۳۰,۰۰۰ پول + ۶ نفوذ → روابط +۱۲", 30000),
    "sanction": ("🚫", "تحریم", "۵ نفوذ → روابط −۱۵ و فشار اقتصادی", 0),
    "embassy": ("🏛️", "تأسیس سفارت", "۱۰۰,۰۰۰ پول + ۱۵ نفوذ → روابط +۲۰", 100000),
}

WR_ACTIONS = {
    "attack": ("⚔️", "حمله", dict(atk=1.00, df=0.85, take=1.00, acc=0.0)),
    "defend": ("🛡️", "دفاع", dict(atk=0.35, df=1.45, take=0.70, acc=0.0)),
    "recon": ("🔭", "شناسایی", dict(atk=0.45, df=0.95, take=1.00, acc=0.0)),
    "fast": ("⚡", "حملهٔ سریع", dict(atk=1.30, df=0.65, take=1.10, acc=-0.05)),
    "tactical": ("🧭", "جابه‌جایی تاکتیکی", dict(atk=0.60, df=1.10, take=0.75, acc=0.0)),
    "disrupt": ("📶", "اخلال (EW)", dict(atk=0.55, df=1.00, take=1.00, acc=0.0)),
    "precision": ("🎯", "عملیات دقیق", dict(atk=1.10, df=0.95, take=1.00, acc=0.12)),
    "retreat": ("🏳️", "عقب‌نشینی", dict(atk=0.0, df=1.00, take=0.55, acc=0.0)),
}
# (کنش من، کنش حریف) → ضریب اثر روی حملهٔ من
WR_COUNTER = {("attack", "defend"): 0.80, ("fast", "defend"): 0.70, ("precision", "defend"): 0.95, ("attack", "tactical"): 0.85, ("fast", "tactical"): 0.75,
              ("fast", "recon"): 1.20, ("attack", "recon"): 1.10, ("precision", "recon"): 1.15, ("precision", "tactical"): 0.90, ("attack", "fast"): 1.05, ("defend", "fast"): 1.20}

STRATEGIES = {"aggressive": ("🔥", "تهاجمی", dict(atk=1.12, df=0.90, loss=1.10)), "balanced": ("⚖️", "متعادل", dict(atk=1.0, df=1.0, loss=1.0)),
              "defensive": ("🛡️", "تدافعی", dict(atk=0.90, df=1.15, loss=0.85)), "blitz": ("⚡", "رعدآسا", dict(atk=1.18, df=0.85, loss=1.20))}
GW_ROLES = {
    "commander": ("👑", "فرمانده کل", "استراتژی را انتخاب و عملیات را اجرا می‌کند؛ بونوس ماهیت فرماندهی"),
    "ground": ("🪖", "فرماندهٔ زمینی", "یگان‌های زمینی را وارد می‌کند"),
    "air": ("✈️", "فرماندهٔ هوایی", "یگان‌های هوایی را وارد می‌کند و برتری هوایی می‌سازد"),
    "naval": ("🚢", "فرماندهٔ دریایی", "یگان‌های دریایی را وارد می‌کند"),
    "intel": ("🕵️", "افسر اطلاعات", "شناسایی انجام می‌دهد و دقت/غافلگیری را بالا می‌برد"),
    "logistics": ("🚚", "افسر لجستیک", "غذا و سوخت تدارک می‌بیند و تلفات را کم می‌کند"),
}
GW_ROLE_ORDER = ["commander", "ground", "air", "naval", "intel", "logistics"]

INVEST_KINDS = {
    "short": ("⏱️", "سپردهٔ کوتاه‌مدت", 6 * 3600, 0.035), "mid": ("📆", "سپردهٔ میان‌مدت", 24 * 3600, 0.13),
    "long": ("🏦", "اوراق بلندمدت", 72 * 3600, 0.45),
}

INTEL_OPS = {
    "military": ("🪖", "اطلاعات نظامی", 6000, 0), "economic": ("💹", "اطلاعات اقتصادی", 5000, 0),
    "province": ("🏙️", "اطلاعات شهر/استان", 4000, 0), "alliance": ("🤝", "اطلاعات اتحاد", 7000, 0),
}
INTEL_SOURCES = {"sat": ("🛰️", "ماهواره", 1.10, 2.0), "sigint": ("📡", "شنود", 1.0, 1.0), "humint": ("🕴️", "عامل انسانی", 0.9, 0.6), "cyber": ("💻", "نفوذ سایبری", 1.05, 1.4)}

DAILY_CYCLE = 30


def daily_reward_for(day_no):
    """جدول پاداش ۳۰ روزه (ساخته‌شده با فرمول، نه دستی)."""
    d = clamp(day_no, 1, DAILY_CYCLE)
    r = {"money": int(3000 + 1200 * d), "xp": 20 + 6 * d}
    if d % 3 == 0:
        r["gems"] = 2 + d // 6
    if d % 5 == 0:
        r["goods"] = 60 + 12 * d
    items = {}
    if d % 7 == 0:
        items["supply_crate"] = 1
    if d in (10, 20):
        items["repair_kit"] = 3
    if d in (14, 28):
        items["field_rations"] = 2
    if d == 21:
        items["intel_dossier"] = 1
    if d == 25:
        items["peace_charm"] = 1
    if d == 30:
        items["mythic_cache"] = 1
        r["gems"] = r.get("gems", 0) + 25
    if items:
        r["items"] = items
    return r


# ============================================================================
# WORLD ENGINE — جهان: کشور ← منطقه ← استان ← شهر ← تأسیسات
# ============================================================================
def seed_world(c):
    if qv(c, "SELECT COUNT(*) FROM countries", default=0) > 0:
        return False
    for rid, (nm, em) in REGIONS.items():
        ex(c, "INSERT INTO regions(id,name,emoji) VALUES(?,?,?)", rid, nm, em)
    pid = 0
    cid = 0
    for (code, flag, name, region, popm, gdp, ind, en, resc, stab, mil, dfn, intel, tech, dip, traits, provs) in COUNTRIES:
        ex(c, "INSERT INTO countries(code,name,flag,region,population,gdp,industry,energy,resources,stability,military,defense,intel,tech,diplomacy,at_war,traits,base_military,base_defense,base_stability) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
           code, name, flag, region, int(popm * 1_000_000), gdp, ind, en, resc, stab, mil, dfn, intel, tech, dip, traits, mil, dfn, stab)
        plist = provs.split("|")
        ncities = sum(len(p.split(":")[1].split(",")) for p in plist)
        for pi, ptxt in enumerate(plist):
            pname, cities = ptxt.split(":")
            pid += 1
            r = stable_rand("prov", code, pname)
            ex(c, "INSERT INTO provinces(id,country,name,terrain) VALUES(?,?,?,?)", pid, code, pname, r.choice(["plains", "mountain", "urban", "desert", "forest", "plains"]))
            for ci, cname in enumerate(cities.split(",")):
                cid += 1
                coastal = 1 if cname.endswith("*") else 0
                cname = cname.rstrip("*")
                rr = stable_rand("city", code, cname)
                share = popm * 1_000_000 / ncities * rr.uniform(0.6, 1.5) * (1.5 if ci == 0 else 0.7)
                popc = int(clamp(share, 150_000, 14_000_000))
                terrain = "urban" if (ci == 0 and rr.random() < 0.6) else ("coast" if coastal and rr.random() < 0.6 else rr.choice(["plains", "mountain", "desert", "forest", "urban", "plains"]))
                dev = round(clamp(0.55 + rr.random() * 0.5 + tech / 400.0, 0.5, 1.5), 2)
                garr = round((400 + mil * 9) * rr.uniform(0.8, 1.2))
                ex(c, "INSERT INTO cities(id,province_id,country,owner_country,name,population,development,terrain,coastal,garrison,garrison_max,fortification,governor_id,captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,0)",
                   cid, pid, code, code, cname, popc, dev, terrain, coastal, garr, garr, int(dfn / 12))
                # تأسیسات: ۲ تا ۳ تأسیسات با وزن‌دهی به ویژگی‌های کشور
                weights = {"farmland": 3, "oil_field": 1 + (3 if "oil" in traits else 0) + en / 60, "iron_mine": 2 + (2 if "mining" in traits else 0), "rare_deposit": 0.6 + (1.5 if "mining" in traits else 0),
                           "power_grid": 2, "industrial_zone": 1 + ind / 60, "port": 3 if coastal else 0.01, "airfield": 1, "fortress": 1, "research_campus": 0.5 + tech / 90}
                keys = list(weights)
                chosen = []
                for _ in range(rr.choice([2, 3, 3])):
                    pool = [k for k in keys if k not in chosen]
                    pick = rr.choices(pool, weights=[weights[k] for k in pool])[0]
                    chosen.append(pick)
                for ft in chosen:
                    ex(c, "INSERT INTO facilities(city_id,ftype,level,damaged_until) VALUES(?,?,?,0)", cid, ft, rr.choice([1, 1, 2, 2, 3]))
    codes = [x[0] for x in COUNTRIES]
    for i, a in enumerate(codes):
        for b in codes[i + 1:]:
            ra = next(x for x in COUNTRIES if x[0] == a)
            rb = next(x for x in COUNTRIES if x[0] == b)
            base = 12 if ra[3] == rb[3] else 0
            v = clamp(base + stable_rand("rel", a, b).uniform(-45, 45), -80, 80)
            ex(c, "INSERT INTO relations(a,b,value) VALUES(?,?,?)", a, b, round(v, 1))
    for com in TRADABLE:
        ex(c, "INSERT INTO market(commodity,base_price,price,stock,target_stock,frozen,volume,updated) VALUES(?,?,?,?,?,0,0,?)",
           com, BASE_PRICES[com], BASE_PRICES[com], TARGET_STOCK[com], TARGET_STOCK[com], now())
    meta_set(c, "inflation", 1.0)
    meta_set(c, "world_seeded", now())
    return True


def rel_key(a, b):
    return (a, b) if a < b else (b, a)


def get_relation(c, a, b):
    if a == b:
        return 100.0
    x, y = rel_key(a, b)
    return qv(c, "SELECT value FROM relations WHERE a=? AND b=?", x, y, default=0.0)


def add_relation(c, a, b, delta):
    if a == b:
        return 100.0
    x, y = rel_key(a, b)
    v = clamp(get_relation(c, a, b) + delta, -100, 100)
    ex(c, "UPDATE relations SET value=? WHERE a=? AND b=?", v, x, y)
    return v


def rel_label(v):
    return ("🟢 متحد" if v >= 60 else "🟩 دوستانه" if v >= 25 else "⬜ خنثی" if v > -25 else "🟧 خصمانه" if v > -60 else "🟥 دشمن")


def country_row(c, code):
    return q1(c, "SELECT * FROM countries WHERE code=?", code)


def city_row(c, cid):
    return q1(c, "SELECT c.*, p.name AS pname, p.terrain AS pterrain FROM cities c JOIN provinces p ON p.id=c.province_id WHERE c.id=?", cid)


def city_facilities(c, cid):
    return qa(c, "SELECT * FROM facilities WHERE city_id=?", cid)


def city_label(cr):
    return f"{cr['name']} ({cr['pname']})"


def governed_cities(c, uid):
    return qa(c, "SELECT * FROM cities WHERE governor_id=?", uid)


def country_territory(c, code):
    return qv(c, "SELECT COUNT(*) FROM cities WHERE owner_country=?", code, default=0)


def flag_of(c, code):
    r = q1(c, "SELECT flag FROM countries WHERE code=?", code)
    return r[0] if r else "🏳️"


def infl(c):
    return float(meta_get(c, "inflation", 1.0))


# ============================================================================
# منابع (Resources) — همهٔ تغییرات فقط از این توابع و داخل تراکنش
# ============================================================================
def all_res(c, uid):
    d = {k: 0.0 for k in RES_ORDER}
    for r in qa(c, "SELECT res, amount FROM resources WHERE user_id=?", uid):
        d[r["res"]] = r["amount"]
    return d


def get_res(c, uid, res):
    return qv(c, "SELECT amount FROM resources WHERE user_id=? AND res=?", uid, res, default=0.0)


def add_res(c, uid, res, amt):
    if res not in RES:
        raise GameError("منبع نامعتبر")
    if amt < 0 and get_res(c, uid, res) + 1e-6 < -amt:
        raise GameError(f"❌ {rn(res)} کافی نیست (نیاز: {fmt(-amt)}، موجودی: {fmt(get_res(c, uid, res))})")
    ex(c, "INSERT INTO resources(user_id,res,amount) VALUES(?,?,?) ON CONFLICT(user_id,res) DO UPDATE SET amount=amount+?", uid, res, amt, amt)


def can_afford(c, uid, cost):
    have = all_res(c, uid)
    return [k for k, v in cost.items() if v > 0 and have.get(k, 0) + 1e-6 < v]


def spend(c, uid, cost):
    """کسر اتمیک هزینه — اگر هر منبعی کم باشد هیچ چیزی کسر نمی‌شود."""
    cost = {k: v for k, v in cost.items() if v}
    missing = can_afford(c, uid, cost)
    if missing:
        have = all_res(c, uid)
        raise GameError("❌ منابع کافی نیست:\n" + "\n".join(f"{rn(k)}: نیاز {fmt(cost[k])} | موجودی {fmt(have[k])}" for k in missing))
    for k, v in cost.items():
        add_res(c, uid, k, -v)


def grant(c, uid, reward):
    """اعطای پاداش: منابع، xp، نفوذ، آیتم‌ها."""
    for k, v in reward.items():
        if k in RES:
            add_res(c, uid, k, v)
        elif k == "xp":
            add_xp(c, uid, v)
        elif k == "influence":
            ex(c, "UPDATE players SET influence=influence+? WHERE user_id=?", v, uid)
        elif k == "prestige":
            ex(c, "UPDATE players SET prestige=prestige+? WHERE user_id=?", v, uid)
        elif k == "items":
            for ik, qty in v.items():
                inv_add(c, uid, ik, qty)


def reward_text(reward):
    parts = []
    for k, v in reward.items():
        if k in RES:
            parts.append(f"{RES[k][0]}{fmt(v)}")
        elif k == "xp":
            parts.append(f"✨{fmt(v)} XP")
        elif k == "influence":
            parts.append(f"🕊️{fmt(v)} نفوذ")
        elif k == "prestige":
            parts.append(f"🎖️{fmt(v)} اعتبار")
        elif k == "items":
            parts.extend(f"{item_name(i)}×{q}" for i, q in v.items())
    return "  ".join(parts)


# ============================================================================
# بازیکن (Player System)
# ============================================================================
def xp_for_level(lv):
    return int(120 * (lv - 1) ** 2)


def level_from_xp(xp):
    return int(1 + math.floor(math.sqrt(max(0, xp) / 120.0)))


def rank_title(level):
    t = RANKS[0][1]
    for lv, name in RANKS:
        if level >= lv:
            t = name
    return t


def get_player(c, uid):
    return q1(c, "SELECT * FROM players WHERE user_id=?", uid)


def need_player(c, uid):
    p = get_player(c, uid)
    if not p:
        raise GameError("ابتدا با /start ثبت‌نام کنید.")
    return p


def add_xp(c, uid, amount):
    amount = int(amount)
    if amount <= 0:
        return
    p = get_player(c, uid)
    if not p:
        return
    old = p["level"]
    xp = p["xp"] + amount
    lv = level_from_xp(xp)
    wk = game_week()
    if p["xp_week_key"] != wk:
        ex(c, "UPDATE players SET xp_week=0, xp_week_key=? WHERE user_id=?", wk, uid)
    ex(c, "UPDATE players SET xp=?, level=?, xp_week=xp_week+? WHERE user_id=?", xp, lv, amount, uid)
    if lv > old:
        for L in range(old + 1, lv + 1):
            add_res(c, uid, "money", 2500 * L)
            if L % 5 == 0:
                add_res(c, uid, "gems", 5)
        notify(c, uid, "system", f"⭐ ارتقا به سطح {lv} — درجه: {rank_title(lv)}\n💰 پاداش ارتقا دریافت شد.")
        bump_max(c, uid, "level", lv)


def pick_auto_city(c):
    """انتخاب خودکار یک شهر آزاد برای ثبت‌نام — بدون دخالت بازیکن.
    شهرهای پرجمعیت‌تر و کشورهایی با تراکم کمتر بازیکن، شانس بیشتری دارند."""
    free = qa(c, "SELECT ci.*, (SELECT COUNT(*) FROM players p WHERE p.country=ci.country) AS taken "
                 "FROM cities ci WHERE ci.governor_id=0 ORDER BY ci.population DESC")
    if not free:
        raise GameError("😔 در حال حاضر هیچ شهر آزادی برای بنیان‌گذاری کشور جدید نیست.")
    min_taken = min(r["taken"] for r in free)
    pool = [r for r in free if r["taken"] <= min_taken + 2][:60]
    return random.choice(pool)


def create_player(c, uid, username, name, country, city_id, empire_name=""):
    if get_player(c, uid):
        raise GameError("شما قبلاً ثبت‌نام کرده‌اید.")
    name = clean_name(name)
    empire_name = clean_name(empire_name, 2, 30) if empire_name else ""
    cr = country_row(c, country)
    if not cr:
        raise GameError("کشور نامعتبر است.")
    city = city_row(c, city_id)
    if not city or city["owner_country"] != country or city["governor_id"]:
        raise GameError("این شهر در دسترس نیست (قبلاً توسط بازیکن دیگری انتخاب شده).")
    if qv(c, "SELECT COUNT(*) FROM players WHERE lower(name)=lower(?)", name, default=0):
        raise GameError("❌ این نام قبلاً استفاده شده است.")
    if empire_name and qv(c, "SELECT COUNT(*) FROM players WHERE lower(empire_name)=lower(?)", empire_name, default=0):
        raise GameError("❌ این نام کشور/امپراتوری قبلاً استفاده شده است.")
    t = now()
    ex(c, "INSERT INTO players(user_id,username,name,country,city_id,level,xp,created,last_seen,last_settle,stability,morale,population,shield_until,influence,xp_week_key,empire_name) "
          "VALUES(?,?,?,?,?,1,0,?,?,?,70,80,?,?,10,?,?)",
       uid, username or "", name, country, city_id, t, t, t, city["population"], t + 24 * 3600, game_week(), empire_name)
    ex(c, "UPDATE cities SET governor_id=? WHERE id=?", uid, city_id)
    start = {"money": 60000, "food": 3000, "oil": 800, "iron": 2500, "rare": 30, "energy": 800, "fuel": 300, "goods": 300, "gems": 20}
    for k in RES_ORDER:
        ex(c, "INSERT INTO resources(user_id,res,amount) VALUES(?,?,?)", uid, k, start.get(k, 0))
    for b, lv in START_BUILDINGS.items():
        ex(c, "INSERT INTO buildings(user_id,btype,level) VALUES(?,?,?)", uid, b, lv)
    inv_add(c, uid, "rifleman", 30)
    inv_add(c, uid, "repair_kit", 2)
    ex(c, "UPDATE inventory SET deployed=qty WHERE user_id=? AND item_key='rifleman'", uid)
    audit(c, uid, "register", country, name)
    disp = empire_name or f"{cr['flag']} {cr['name']}"
    notify(c, uid, "system", f"🎖️ به {APP_NAME} خوش آمدید فرمانده {name}!\n🏰 کشور شما: {disp}\n🛡️ ۲۴ ساعت سپر تازه‌کار دارید.")
    finish(c, uid)
    return get_player(c, uid)


def empire_label(c, p):
    """نام نمایشی کشور بازیکن: نام دلخواهی که تایپ کرده، یا در غیر این صورت نام کشور واقعیِ پایه."""
    if p["empire_name"]:
        return f"🏰 {p['empire_name']}"
    cr = country_row(c, p["country"])
    return f"{cr['flag']} {cr['name']}" if cr else "🏳️ ?"


# ============================================================================
# آمار، ماموریت‌ها و دستاوردها (Counters)
# ============================================================================
METRIC_MAP = defaultdict(list)
for _mid, _m_ in MISSIONS.items():
    METRIC_MAP[_m_["metric"]].append(_mid)


def stat_get(c, uid, key):
    return qv(c, "SELECT value FROM pstats WHERE user_id=? AND key=?", uid, key, default=0.0)


def all_stats(c, uid):
    return {r["key"]: r["value"] for r in qa(c, "SELECT key,value FROM pstats WHERE user_id=?", uid)}


def bump(c, uid, key, amt=1):
    ex(c, "INSERT INTO pstats(user_id,key,value) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=value+?", uid, key, amt, amt)
    for mid in METRIC_MAP.get(key, []):
        if MISSIONS[mid]["mode"] == "sum":
            ex(c, "UPDATE missions SET progress=progress+? WHERE user_id=? AND mid=? AND claimed=0 AND (expires=0 OR expires>?)", amt, uid, mid, now())


def bump_max(c, uid, key, val):
    ex(c, "INSERT INTO pstats(user_id,key,value) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=MAX(value,excluded.value)", uid, key, val)
    for mid in METRIC_MAP.get(key, []):
        if MISSIONS[mid]["mode"] == "max":
            ex(c, "UPDATE missions SET progress=MAX(progress,?) WHERE user_id=? AND mid=? AND claimed=0 AND (expires=0 OR expires>?)", val, uid, mid, now())


def day_end_ts(ts=None):
    ts = now() if ts is None else ts
    local = ts + TZ_OFFSET
    return (local // 86400 + 1) * 86400 - TZ_OFFSET


def week_end_ts(ts=None):
    ts = now() if ts is None else ts
    local = ts + TZ_OFFSET
    days = local // 86400
    dow = datetime.fromtimestamp(local, timezone.utc).weekday()   # Mon=0
    return (days + (7 - dow)) * 86400 - TZ_OFFSET


def ensure_missions(c, uid):
    p = get_player(c, uid)
    if not p:
        return
    day, week, t = game_day(), game_week(), now()
    lvl = p["level"]

    def add(mid, period, expires):
        ex(c, "INSERT OR IGNORE INTO missions(user_id,mid,period,progress,claimed,created,expires) VALUES(?,?,?,0,0,?,?)", uid, mid, period, t, expires)

    pool = sorted(m["id"] for m in MISSIONS.values() if m["period"] == "daily" and m["lvl"] <= lvl)
    if pool and not qv(c, "SELECT COUNT(*) FROM missions WHERE user_id=? AND period=?", uid, day, default=0):
        for mid in stable_rand("daily", uid, day).sample(pool, min(5, len(pool))):
            add(mid, day, day_end_ts())
    pool = sorted(m["id"] for m in MISSIONS.values() if m["period"] == "weekly" and m["lvl"] <= lvl)
    if pool and not qv(c, "SELECT COUNT(*) FROM missions WHERE user_id=? AND period=?", uid, week, default=0):
        for mid in stable_rand("weekly", uid, week).sample(pool, min(4, len(pool))):
            add(mid, week, week_end_ts())
    for m in MISSIONS.values():
        if m["period"] == "perm" and m["lvl"] <= lvl:
            add(m["id"], "perm", 0)
    pool = sorted(m["id"] for m in MISSIONS.values() if m["period"] == "random" and m["lvl"] <= lvl)
    rkey = "r" + day
    if pool and not qv(c, "SELECT COUNT(*) FROM missions WHERE user_id=? AND period=?", uid, rkey, default=0):
        for mid in stable_rand("random", uid, day).sample(pool, min(2, len(pool))):
            add(mid, rkey, day_end_ts())
    for ev in qa(c, "SELECT * FROM world_events WHERE active=1 AND ends_at>?", t):
        if jl(ev["params"]).get("emergency"):
            epool = sorted(m["id"] for m in MISSIONS.values() if m["period"] == "emergency")
            for mid in stable_rand("emg", ev["id"]).sample(epool, min(2, len(epool))):
                add(mid, f"ev{ev['id']}", ev["ends_at"])
    # پیشرفت‌های حالت max را با وضعیت فعلی همگام کن
    for key, val in (("level", lvl), ("mil_power", p["mil_power"]), ("money_peak", get_res(c, uid, "money"))):
        bump_max(c, uid, key, val)


def claim_mission(c, uid, mid, period):
    m = MISSIONS.get(mid)
    if not m:
        raise GameError("مأموریت نامعتبر است.")
    r = q1(c, "SELECT * FROM missions WHERE user_id=? AND mid=? AND period=?", uid, mid, period)
    if not r:
        raise GameError("مأموریت پیدا نشد.")
    if r["claimed"]:
        raise GameError("✅ پاداش این مأموریت قبلاً دریافت شده است.")
    if r["progress"] + 1e-9 < m["target"]:
        raise GameError("⏳ هنوز شرایط مأموریت کامل نشده است.")
    cur = ex(c, "UPDATE missions SET claimed=1 WHERE user_id=? AND mid=? AND period=? AND claimed=0", uid, mid, period)
    if cur.rowcount != 1:
        raise GameError("✅ پاداش این مأموریت قبلاً دریافت شده است.")
    grant(c, uid, m["reward"])
    bump(c, uid, "missions_claimed", 1)
    audit(c, uid, "mission_claim", mid, jd(m["reward"]))
    finish(c, uid)
    return m["reward"]


def check_achievements(c, uid):
    stats = all_stats(c, uid)
    earned = {r[0] for r in qa(c, "SELECT aid FROM achievements WHERE user_id=?", uid)}
    got = []
    for aid, a in ACHIEVEMENTS.items():
        if aid in earned:
            continue
        if stats.get(a["stat"], 0) >= a["thr"]:
            cur = ex(c, "INSERT OR IGNORE INTO achievements(user_id,aid,ts) VALUES(?,?,?)", uid, aid, now())
            if cur.rowcount == 1:
                grant(c, uid, a["reward"])
                notify(c, uid, "reward", f"🏅 دستاورد جدید: {a['title']}\n{reward_text(a['reward'])}")
                got.append(aid)
    return got


# ============================================================================
# افکت‌ها: فناوری + ویژگی کشور + اتحاد + تأسیسات + رویدادها + بافرهای موقت
# ============================================================================
EV_ADDITIVE = {"stability", "relation", "market_fee"}


def active_events(c):
    return qa(c, "SELECT * FROM world_events WHERE active=1 AND ends_at>?", now())


def event_mods(c, country=None, region=None, scope_only_global=False):
    """ضرایب رویدادهای فعال (ضربی: prod_/price_/tax/... ؛ جمعی: stability/relation/market_fee)."""
    mods = defaultdict(lambda: 1.0)
    for k in EV_ADDITIVE:
        mods[k] = 0.0
    for ev in active_events(c):
        sc = ev["scope"]
        if scope_only_global and sc != "global":
            continue
        if sc == "region" and ev["target"] != region:
            continue
        if sc == "country" and ev["target"] != country:
            continue
        for k, v in jl(ev["params"]).get("fx", {}).items():
            if k in EV_ADDITIVE:
                mods[k] += v
            else:
                mods[k] *= v
    return mods


def alliance_effects(c, aid, base_bonus=0.0):
    fx = defaultdict(float)
    if not aid:
        return fx
    al = q1(c, "SELECT level FROM alliances WHERE id=?", aid)
    if not al:
        return fx
    fx["prod_all"] += 0.01 * (al["level"] - 1)
    for r in qa(c, "SELECT tech_id FROM alliance_techs WHERE alliance_id=?", aid):
        tid = r["tech_id"]
        src = ALLIANCE_TECHS.get(tid) if not tid.startswith("proj:") else ALLIANCE_PROJECTS.get(tid[5:])
        if src:
            for k, v in src["fx"].items():
                fx[k] += v
    if base_bonus:
        for k in list(fx):
            fx[k] *= (1 + base_bonus)
    return fx


def effects(c, uid, p=None):
    """جمع همهٔ اثرهای افزایشی روی بازیکن (fx) + ضرایب رویدادها (ev)."""
    p = p or get_player(c, uid)
    fx = defaultdict(float)
    for r in qa(c, "SELECT tech_id FROM techs WHERE user_id=?", uid):
        for k, v in TECHS[r["tech_id"]]["fx"].items():
            fx[k] += v
    cr = country_row(c, p["country"])
    if cr:
        for tr in (cr["traits"] or "").split(","):
            for k, v in TRAIT_EFFECTS.get(tr, {}).items():
                fx[k] += v
    for k, v in alliance_effects(c, p["alliance_id"], fx.get("alliance_bonus", 0)).items():
        fx[k] += v
    for city in governed_cities(c, uid):
        for f in city_facilities(c, city["id"]):
            if f["damaged_until"] > now():
                continue
            if f["ftype"] == "airfield":
                fx["atk_air"] += 0.02 * f["level"]
            elif f["ftype"] == "fortress":
                fx["def_all"] += 0.015 * f["level"]
            elif f["ftype"] == "research_campus":
                fx["research_speed"] += 0.02 * f["level"]
    for r in qa(c, "SELECT key, value FROM player_effects WHERE user_id=? AND until>?", uid, now()):
        if r["key"] in ("morale_buff", "recon_boost", "prod_malus", "prod_boost"):
            continue
        fx[r["key"]] += r["value"]
    fx["influence"] += 0.0
    return fx


def temp_effect(c, uid, key):
    return qv(c, "SELECT COALESCE(SUM(value),0) FROM player_effects WHERE user_id=? AND key=? AND until>?", uid, key, now(), default=0.0)


def add_temp_effect(c, uid, key, value, seconds, source=""):
    ex(c, "INSERT INTO player_effects(user_id,key,value,until,source) VALUES(?,?,?,?,?)", uid, key, value, now() + seconds, source)


# ============================================================================
# ساختمان‌ها و هزینه‌ها
# ============================================================================
def blevels(c, uid):
    return {r["btype"]: r["level"] for r in qa(c, "SELECT btype, level FROM buildings WHERE user_id=?", uid)}


def has_tech(c, uid, tid):
    return tid is None or bool(q1(c, "SELECT 1 FROM techs WHERE user_id=? AND tech_id=?", uid, tid))


def building_cost(c, uid, btype, to_level, fx=None, p=None):
    b = BUILDINGS[btype]
    p = p or get_player(c, uid)
    fx = fx if fx is not None else effects(c, uid, p)
    ev = event_mods(c, p["country"], country_row(c, p["country"])["region"])
    mult = (b["growth"] ** (to_level - 1)) * max(0.3, 1 + fx.get("build_cost", 0)) * ev["build_cost"] * (infl(c) ** 0.5)
    return {k: int(v * mult) for k, v in b["cost"].items()}


def building_time(c, uid, btype, to_level, fx=None):
    b = BUILDINGS[btype]
    fx = fx if fx is not None else effects(c, uid)
    cc = blevels(c, uid).get("command_center", 1)
    speed = 1 + (0.05 * (cc - 1) if btype != "command_center" else 0) + fx.get("build_speed", 0)
    return max(5, int(b["time"] * (1.42 ** (to_level - 1)) / speed))


def build_slots(c, uid):
    return 1 + (blevels(c, uid).get("command_center", 1) - 1) // 3


def upgrade_requirements(c, uid, btype, to_level):
    bl = blevels(c, uid)
    b = BUILDINGS[btype]
    missing = []
    for rb, rl in b["req"].items():
        need = rl if to_level <= 1 else rl + max(0, (to_level - 2) // 3) if rb == "command_center" and btype != "command_center" else rl
        if bl.get(rb, 0) < need:
            missing.append(f"{BUILDINGS[rb]['emoji']} {BUILDINGS[rb]['name']} سطح {need}")
    if b["tech"] and not has_tech(c, uid, b["tech"]):
        missing.append(f"🔬 فناوری «{TECHS[b['tech']]['name']}»")
    if btype != "command_center" and to_level > bl.get("command_center", 1) * 2 + 2:
        missing.append(f"🏛️ مرکز فرماندهی سطح {math.ceil((to_level - 2) / 2)}")
    return missing


def start_upgrade(c, uid, btype):
    """ارتقای ساختمان: Player → Building → Level → Requirements → Cost → Transaction → Queue."""
    if btype not in BUILDINGS:
        raise GameError("ساختمان نامعتبر است.")
    p = need_player(c, uid)
    settle_player(c, uid)
    b = BUILDINGS[btype]
    bl = blevels(c, uid)
    cur = bl.get(btype, 0)
    if qv(c, "SELECT COUNT(*) FROM build_queue WHERE user_id=? AND btype=?", uid, btype, default=0):
        raise GameError("این ساختمان هم‌اکنون در حال ارتقاست.")
    to_level = cur + 1
    if to_level > b["max"]:
        raise GameError("این ساختمان به سقف سطح رسیده است.")
    miss = upgrade_requirements(c, uid, btype, to_level)
    if miss:
        raise GameError("🔒 پیش‌نیازها کامل نیست:\n" + "\n".join(miss))
    if qv(c, "SELECT COUNT(*) FROM build_queue WHERE user_id=?", uid, default=0) >= build_slots(c, uid):
        raise GameError(f"🏗️ صف ساخت پر است (ظرفیت {build_slots(c, uid)}). مرکز فرماندهی را ارتقا دهید یا منتظر بمانید.")
    fx = effects(c, uid, p)
    cost = building_cost(c, uid, btype, to_level, fx, p)
    spend(c, uid, cost)
    secs = building_time(c, uid, btype, to_level, fx)
    ex(c, "INSERT INTO build_queue(user_id,btype,to_level,started,ends_at) VALUES(?,?,?,?,?)", uid, btype, to_level, now(), now() + secs)
    audit(c, uid, "build_start", btype, f"L{to_level} cost={jd(cost)}")
    return dict(cost=cost, secs=secs, to_level=to_level)


def complete_builds(c, uid=None):
    rows = qa(c, "SELECT * FROM build_queue WHERE ends_at<=?" + (" AND user_id=?" if uid else ""), *([now(), uid] if uid else [now()]))
    for r in rows:
        ex(c, "DELETE FROM build_queue WHERE id=?", r["id"])
        ex(c, "INSERT INTO buildings(user_id,btype,level) VALUES(?,?,?) ON CONFLICT(user_id,btype) DO UPDATE SET level=excluded.level", r["user_id"], r["btype"], r["to_level"])
        b = BUILDINGS[r["btype"]]
        settle_player(c, r["user_id"], r["ends_at"])
        bump(c, r["user_id"], "buildings_upgraded", 1)
        add_xp(c, r["user_id"], 25 + 12 * r["to_level"])
        notify(c, r["user_id"], "building", f"🏗️ {b['emoji']} {b['name']} به سطح {r['to_level']} رسید.")
        finish(c, r["user_id"])
    return len(rows)


def speedup_cost(remaining):
    return max(1, int(math.ceil(remaining / 600.0)))


def speedup(c, uid, kind, qid):
    """تسریع با جم (۱ جم به‌ازای هر ۱۰ دقیقه)."""
    table = {"build": "build_queue", "prod": "prod_queue"}.get(kind)
    if kind == "research":
        r = q1(c, "SELECT * FROM research_queue WHERE user_id=?", uid)
    elif table:
        r = q1(c, f"SELECT * FROM {table} WHERE id=? AND user_id=?", qid, uid)
    else:
        raise GameError("نوع صف نامعتبر.")
    if not r:
        raise GameError("این مورد دیگر در صف نیست.")
    rem = r["ends_at"] - now()
    if rem <= 0:
        return 0
    gems = speedup_cost(rem)
    spend(c, uid, {"gems": gems})
    if kind == "research":
        ex(c, "UPDATE research_queue SET ends_at=? WHERE user_id=?", now(), uid)
    else:
        ex(c, f"UPDATE {table} SET ends_at=? WHERE id=?", now(), qid)
    audit(c, uid, "speedup", kind, f"gems={gems}")
    process_due(c, uid)
    return gems


# ============================================================================
# فناوری (Technology Tree)
# ============================================================================
def tech_status(c, uid, tid):
    t = TECHS[tid]
    if has_tech(c, uid, tid):
        return "done"
    rq = q1(c, "SELECT tech_id FROM research_queue WHERE user_id=?", uid)
    if rq and rq["tech_id"] == tid:
        return "active"
    if any(not has_tech(c, uid, p) for p in t["pre"]):
        return "locked"
    return "available"


def research_time(c, uid, tid, fx=None):
    fx = fx if fx is not None else effects(c, uid)
    rc = blevels(c, uid).get("research_center", 0)
    er = stat_get(c, uid, "energy_ratio") or 1.0
    speed = (1 + 0.06 * max(0, rc - 1) + fx.get("research_speed", 0)) * (0.5 + 0.5 * min(1.0, er))
    return max(10, int(TECHS[tid]["time"] / speed))


def start_research(c, uid, tid):
    if tid not in TECHS:
        raise GameError("فناوری نامعتبر است.")
    p = need_player(c, uid)
    settle_player(c, uid)
    t = TECHS[tid]
    st = tech_status(c, uid, tid)
    if st == "done":
        raise GameError("این فناوری قبلاً تحقیق شده است.")
    if st == "active":
        raise GameError("همین فناوری در حال تحقیق است.")
    if st == "locked":
        raise GameError("🔒 پیش‌نیازها: " + "، ".join(TECHS[x]["name"] for x in t["pre"] if not has_tech(c, uid, x)))
    if q1(c, "SELECT 1 FROM research_queue WHERE user_id=?", uid):
        raise GameError("هم‌اکنون یک تحقیق فعال دارید.")
    rc = blevels(c, uid).get("research_center", 0)
    if rc < t["tier"]:
        raise GameError(f"🔬 مرکز تحقیقات سطح {t['tier']} لازم است (فعلی: {rc}).")
    cost = {k: int(v * (infl(c) ** 0.3)) for k, v in t["cost"].items()}
    spend(c, uid, cost)
    secs = research_time(c, uid, tid)
    ex(c, "INSERT INTO research_queue(user_id,tech_id,started,ends_at) VALUES(?,?,?,?)", uid, tid, now(), now() + secs)
    audit(c, uid, "research_start", tid, jd(cost))
    return dict(cost=cost, secs=secs)


def complete_research(c, uid=None):
    rows = qa(c, "SELECT * FROM research_queue WHERE ends_at<=?" + (" AND user_id=?" if uid else ""), *([now(), uid] if uid else [now()]))
    for r in rows:
        ex(c, "DELETE FROM research_queue WHERE user_id=?", r["user_id"])
        ex(c, "INSERT OR IGNORE INTO techs(user_id,tech_id,done_at) VALUES(?,?,?)", r["user_id"], r["tech_id"], r["ends_at"])
        t = TECHS[r["tech_id"]]
        bump(c, r["user_id"], "research_done", 1)
        bump_max(c, r["user_id"], "tech_count", qv(c, "SELECT COUNT(*) FROM techs WHERE user_id=?", r["user_id"], default=0))
        add_xp(c, r["user_id"], 60 * t["tier"])
        notify(c, r["user_id"], "tech", f"🔬 فناوری «{t['name']}» تکمیل شد!\n{fx_text(t['fx'])}")
        finish(c, r["user_id"])
    return len(rows)


FX_NAMES = {"atk_land": "حملهٔ زمینی", "atk_air": "حملهٔ هوایی", "atk_naval": "حملهٔ دریایی", "morale": "روحیه", "accuracy": "دقت", "tax": "مالیات", "prod_money": "درآمد پول",
            "deposit_rate": "سود سپرده", "market_fee": "کارمزد بازار", "build_cost": "هزینهٔ ساخت", "prod_iron": "تولید آهن", "prod_fuel": "تولید سوخت", "prod_food": "تولید غذا",
            "prod_goods": "تولید کالا", "build_speed": "سرعت ساخت", "prod_speed": "سرعت تولید نظامی", "prod_all": "همهٔ تولیدات", "intel_power": "قدرت اطلاعاتی", "recon_acc": "دقت شناسایی",
            "counter_intel": "ضدجاسوسی", "ew": "جنگ الکترونیک", "storage": "ظرفیت انبار", "supply": "تدارکات", "fuel_use": "مصرف سوخت", "upkeep_army": "نگهداری ارتش", "loot": "غنیمت",
            "def_all": "دفاع", "def_air": "پدافند هوایی", "city_def": "دفاع شهرها", "prod_energy": "تولید انرژی", "energy_use": "مصرف انرژی", "prod_oil": "تولید نفت",
            "upkeep_build": "نگهداری ساختمان", "influence": "نفوذ", "relation_gain": "بهبود روابط", "treaty_slots": "ظرفیت معاهده", "alliance_bonus": "بونوس اتحاد", "stability": "ثبات",
            "research_speed": "سرعت تحقیق", "prod_rare": "تولید منابع کمیاب"}


def fx_text(fx):
    out = []
    for k, v in fx.items():
        nm = FX_NAMES.get(k, k)
        if k in ("stability", "treaty_slots"):
            out.append(f"{nm} {v:+g}")
        else:
            out.append(f"{nm} {v * 100:+.0f}٪")
    return "، ".join(out)


# ============================================================================
# انبار (Inventory)
# ============================================================================
def unit_lvl_mult(level):
    return 1 + UPGRADE_STAT_GAIN * (level - 1)


def inv_add(c, uid, key, qty, level=1, durability=100.0):
    if qty <= 0:
        return
    if not item_info(key):
        raise GameError("آیتم نامعتبر")
    r = q1(c, "SELECT * FROM inventory WHERE user_id=? AND item_key=? AND level=?", uid, key, level)
    if r:
        newq = r["qty"] + qty
        dur_ = (r["durability"] * r["qty"] + durability * qty) / newq
        ex(c, "UPDATE inventory SET qty=?, durability=? WHERE id=?", newq, dur_, r["id"])
    else:
        ex(c, "INSERT INTO inventory(user_id,item_key,level,qty,deployed,durability) VALUES(?,?,?,?,0,?)", uid, key, level, qty, durability)


def inv_stacks(c, uid, only_units=False):
    rows = qa(c, "SELECT * FROM inventory WHERE user_id=? AND qty>0 ORDER BY item_key, level", uid)
    if only_units:
        rows = [r for r in rows if r["item_key"] in UNITS]
    return rows


def inv_take_item(c, uid, key, qty=1):
    """مصرف آیتم‌های مصرفی (غیرِ یگان)."""
    r = q1(c, "SELECT * FROM inventory WHERE user_id=? AND item_key=? AND level=1", uid, key)
    if not r or r["qty"] < qty:
        raise GameError("❌ این آیتم را به تعداد کافی ندارید.")
    ex(c, "UPDATE inventory SET qty=qty-? WHERE id=?", qty, r["id"])


def inv_count(c, uid, key):
    return qv(c, "SELECT COALESCE(SUM(qty),0) FROM inventory WHERE user_id=? AND item_key=?", uid, key, default=0)


def set_deployed(c, uid, stack_id, n):
    r = q1(c, "SELECT * FROM inventory WHERE id=? AND user_id=?", stack_id, uid)
    if not r or r["item_key"] not in UNITS:
        raise GameError("یگان پیدا نشد.")
    n = int(clamp(n, 0, r["qty"]))
    ex(c, "UPDATE inventory SET deployed=? WHERE id=?", n, stack_id)
    return n


def unit_power(u, level=1, dur_=100.0):
    base = u["atk"] + 0.8 * u["df"] + 0.1 * u["mob"] + 0.15 * u["rng"]
    return base * unit_lvl_mult(level) * (0.5 + 0.5 * dur_ / 100.0)


def calc_military_power(c, uid):
    tot = 0.0
    for r in inv_stacks(c, uid, True):
        u = UNITS[r["item_key"]]
        pw = unit_power(u, r["level"], r["durability"])
        tot += pw * r["deployed"] + 0.4 * pw * (r["qty"] - r["deployed"])
    return tot


def unit_cost_mult(c, uid):
    return infl(c) ** 0.3


def start_production(c, uid, key, qty):
    """تولید یگان: Building → Tech → Slot → Cost → Queue."""
    u = UNITS.get(key)
    if not u:
        raise GameError("یگان نامعتبر.")
    if not u["craft"]:
        raise GameError("🔴 این یگان فقط از طریق پاداش‌ها/صندوق اسطوره‌ای به‌دست می‌آید.")
    qty = int(qty)
    if qty < 1:
        raise GameError("تعداد نامعتبر.")
    p = need_player(c, uid)
    settle_player(c, uid)
    bt = CATEGORIES[u["cat"]][2]
    bl = blevels(c, uid).get(bt, 0)
    if bl < u["bl"]:
        raise GameError(f"🏗️ {BUILDINGS[bt]['emoji']} {BUILDINGS[bt]['name']} سطح {u['bl']} لازم است (فعلی: {bl}).")
    if not has_tech(c, uid, u["tech"]):
        raise GameError(f"🔬 فناوری «{TECHS[u['tech']]['name']}» لازم است.")
    maxb = 40 * bl
    if qty > maxb:
        raise GameError(f"حداکثر تعداد هر سفارش با این پایگاه {fmt(maxb)} است.")
    slots = 1 + bl // 3
    used = qv(c, "SELECT COUNT(*) FROM prod_queue q WHERE q.user_id=? AND q.item_key IN (%s)" % ",".join("?" * len([k for k, x in UNITS.items() if CATEGORIES[x["cat"]][2] == bt])),
              uid, *[k for k, x in UNITS.items() if CATEGORIES[x["cat"]][2] == bt], default=0)
    if used >= slots:
        raise GameError(f"صف تولید {BUILDINGS[bt]['name']} پر است (ظرفیت {slots}).")
    fx = effects(c, uid, p)
    m = unit_cost_mult(c, uid)
    cost = {k: v * qty * m for k, v in u["cost"].items()}
    cost = {k: (int(math.ceil(v)) if v >= 1 else round(v, 2)) for k, v in cost.items()}
    spend(c, uid, cost)
    er = stat_get(c, uid, "energy_ratio") or 1.0
    speed = (1 + 0.05 * (bl - 1) + fx.get("prod_speed", 0)) * (0.5 + 0.5 * min(1.0, er))
    secs = int(clamp(u["time"] * qty / speed, 5, 86400))
    ex(c, "INSERT INTO prod_queue(user_id,item_key,qty,started,ends_at) VALUES(?,?,?,?,?)", uid, key, qty, now(), now() + secs)
    audit(c, uid, "prod_start", key, f"x{qty} cost={jd(cost)}")
    return dict(cost=cost, secs=secs)


def complete_production(c, uid=None):
    rows = qa(c, "SELECT * FROM prod_queue WHERE ends_at<=?" + (" AND user_id=?" if uid else ""), *([now(), uid] if uid else [now()]))
    for r in rows:
        ex(c, "DELETE FROM prod_queue WHERE id=?", r["id"])
        inv_add(c, r["user_id"], r["item_key"], r["qty"])
        bump(c, r["user_id"], "units_built", r["qty"])
        add_xp(c, r["user_id"], max(5, r["qty"] // 2))
        notify(c, r["user_id"], "military", f"🪖 تولید کامل شد: {item_name(r['item_key'])} × {fmt(r['qty'])}")
        finish(c, r["user_id"])
    return len(rows)


def upgrade_unit(c, uid, stack_id):
    """ارتقای سطح یک دسته یگان (همهٔ واحدهای آن دسته)."""
    r = q1(c, "SELECT * FROM inventory WHERE id=? AND user_id=?", stack_id, uid)
    if not r or r["item_key"] not in UNITS:
        raise GameError("یگان پیدا نشد.")
    u = UNITS[r["item_key"]]
    if r["level"] >= UPGRADE_MAX:
        raise GameError("به سقف سطح رسیده است.")
    lv = r["level"] + 1
    need_tech = "mil_doctrine" if lv >= 4 else "mil_basic"
    if not has_tech(c, uid, need_tech):
        raise GameError(f"🔬 برای سطح {lv} فناوری «{TECHS[need_tech]['name']}» لازم است.")
    cost = {k: v * r["qty"] * 0.35 * (1.6 ** (lv - 2)) * unit_cost_mult(c, uid) for k, v in u["cost"].items() if k in ("money", "iron", "goods", "rare")}
    cost = {k: int(math.ceil(v)) for k, v in cost.items() if v >= 0.5}
    spend(c, uid, cost)
    dst = q1(c, "SELECT * FROM inventory WHERE user_id=? AND item_key=? AND level=?", uid, r["item_key"], lv)
    if dst:
        newq = dst["qty"] + r["qty"]
        d2 = (dst["durability"] * dst["qty"] + r["durability"] * r["qty"]) / newq
        ex(c, "UPDATE inventory SET qty=?, deployed=deployed+?, durability=? WHERE id=?", newq, r["deployed"], d2, dst["id"])
        ex(c, "DELETE FROM inventory WHERE id=?", r["id"])
    else:
        ex(c, "UPDATE inventory SET level=? WHERE id=?", lv, r["id"])
    bump(c, uid, "units_upgraded", r["qty"])
    add_xp(c, uid, 20 * lv)
    audit(c, uid, "unit_upgrade", r["item_key"], f"L{lv} x{r['qty']} {jd(cost)}")
    finish(c, uid)
    return lv, cost


def repair_cost_for(c, uid, stack):
    u = UNITS[stack["item_key"]]
    missing = (100 - stack["durability"]) / 100.0
    base = {"money": u["cost"].get("money", 0) * 0.25, "goods": u["cost"].get("goods", 0) * 0.15, "iron": u["cost"].get("iron", 0) * 0.15}
    return {k: int(math.ceil(v * stack["qty"] * missing * unit_cost_mult(c, uid))) for k, v in base.items() if v * stack["qty"] * missing >= 0.5}


def repair_stack(c, uid, stack_id):
    r = q1(c, "SELECT * FROM inventory WHERE id=? AND user_id=?", stack_id, uid)
    if not r or r["item_key"] not in UNITS:
        raise GameError("یگان پیدا نشد.")
    if r["durability"] >= 99.5:
        raise GameError("این یگان سالم است.")
    cost = repair_cost_for(c, uid, r)
    spend(c, uid, cost)
    ex(c, "UPDATE inventory SET durability=100 WHERE id=?", stack_id)
    bump(c, uid, "repairs", 1)
    return cost


def use_item(c, uid, key):
    it = ITEMS.get(key)
    if not it:
        raise GameError("این آیتم قابل استفاده نیست.")
    inv_take_item(c, uid, key, 1)
    msg = ""
    if key == "repair_kit":
        ex(c, "UPDATE inventory SET durability=MIN(100, durability+30) WHERE user_id=? AND deployed>0", uid)
        msg = "🧰 ۳۰ واحد دوام به یگان‌های مستقر برگشت."
    elif key == "field_rations":
        add_temp_effect(c, uid, "morale_buff", 0.10, 6 * 3600, "ration")
        msg = "🍱 ۶ ساعت +۱۰٪ روحیه در نبرد."
    elif key == "intel_dossier":
        add_temp_effect(c, uid, "recon_boost", 0.15, 2 * 3600, "dossier")
        msg = "🗂️ ۲ ساعت +۱۵٪ کیفیت شناسایی در نبرد."
    elif key == "supply_crate":
        rr = random.Random()
        g = {k: int(rr.uniform(0.6, 1.4) * v) for k, v in {"money": 12000, "food": 1200, "iron": 900, "fuel": 250, "goods": 90}.items()}
        grant(c, uid, g)
        msg = "📦 دریافت شد: " + reward_text(g)
    elif key == "war_bond":
        add_xp(c, uid, 600)
        msg = "🎖️ ۶۰۰ XP دریافت شد."
    elif key == "peace_charm":
        ex(c, "UPDATE players SET shield_until=MAX(shield_until, ?) WHERE user_id=?", now() + 6 * 3600, uid)
        msg = "🕊️ ۶ ساعت سپر صلح فعال شد (با حملهٔ شما از بین می‌رود)."
    elif key == "mythic_cache":
        pool = [k for k, u in UNITS.items() if u["rarity"] == "mythic"]
        k = random.choice(pool)
        inv_add(c, uid, k, 1)
        msg = f"👑 یگان اسطوره‌ای دریافت شد: {item_name(k)}"
    bump(c, uid, "items_used", 1)
    finish(c, uid)
    return msg


def buy_item(c, uid, key, qty=1):
    it = ITEMS.get(key)
    if not it or not it["price"]:
        raise GameError("این آیتم فروشی نیست.")
    qty = int(clamp(qty, 1, 50))
    cost = {k: int(v * qty * (infl(c) ** 0.3 if k != "gems" else 1)) for k, v in it["price"].items()}
    spend(c, uid, cost)
    inv_add(c, uid, key, qty)
    audit(c, uid, "buy_item", key, f"x{qty} {jd(cost)}")
    return cost


# ============================================================================
# ECONOMY ENGINE — تولید، مصرف، مالیات، GDP، ثبات
# ============================================================================
CAP_FACTOR = {"food": 1.0, "oil": 0.8, "iron": 1.0, "rare": 0.05, "energy": 1.2, "fuel": 0.6, "goods": 0.5}
PRODUCERS_ORDER = ["mine", "farm", "refinery", "factory", "bank"]


def storage_cap(bl, fx, res):
    if res in NO_CAP:
        return float("inf")
    base = 30000 + 25000 * bl.get("storage", 0)
    return base * (1 + fx.get("storage", 0)) * CAP_FACTOR.get(res, 1)


def city_yields(c, uid):
    """تولید ساعتی تأسیسات شهرهای تحت حکمرانی."""
    y = defaultdict(float)
    for city in governed_cities(c, uid):
        for f in city_facilities(c, city["id"]):
            if f["damaged_until"] > now():
                continue
            for r, rate in FACILITIES[f["ftype"]][2].items():
                y[r] += rate * f["level"] * (0.6 + 0.5 * city["development"])
    return y


def army_maintenance(c, uid):
    m = defaultdict(float)
    for r in inv_stacks(c, uid, True):
        u = UNITS[r["item_key"]]
        share = 1.0 if r["deployed"] else 0.5
        active = r["deployed"] + 0.5 * (r["qty"] - r["deployed"])
        for k, v in u["maint"].items():
            m[k] += v * active
    return m


def compute_flows(res, bl, fx, ev, hours, pop, tax_rate, stability, yields, army, n_cities, prod_malus=0.0, boost=0.0):
    """تابع خالص (بدون دیتابیس) — محاسبهٔ تغییر منابع در بازهٔ hours ساعت."""
    d = defaultdict(float)
    info = {"gdp_value": 0.0}
    stock = {k: res.get(k, 0.0) for k in RES_ORDER}
    stab_mult = 0.7 + 0.3 * stability / 100.0

    def pm(r):
        return max(0.0, (1 + fx.get("prod_" + r, 0) + fx.get("prod_all", 0) + boost - prod_malus)) * ev["prod_" + r] * stab_mult

    def add(r, amt):
        stock[r] += amt
        d[r] += amt

    for r, rate in yields.items():
        amt = rate * hours * pm(r)
        add(r, amt)
        info["gdp_value"] += amt * BASE_PRICES.get(r, 1.0)
    pp = bl.get("power_plant", 0)
    if pp:
        io = BUILDINGS["power_plant"]["io"]
        oil_need = -io["oil"] * pp * hours
        r_oil = 1.0 if oil_need <= 0 else min(1.0, stock["oil"] / oil_need)
        add("oil", -oil_need * r_oil)
        e = io["energy"] * pp * hours * r_oil * pm("energy")
        add("energy", e)
        info["gdp_value"] += e * BASE_PRICES["energy"]
        info["power_oil_ratio"] = r_oil
    demand = 0.0
    for b, lv in bl.items():
        if b == "power_plant":
            continue
        e = -BUILDINGS[b]["io"].get("energy", 0)
        if e > 0:
            demand += e * lv * hours * max(0.2, 1 + fx.get("energy_use", 0))
    ratio_e = 1.0 if demand <= 0 else min(1.0, max(0.0, stock["energy"]) / demand)
    info["energy_ratio"] = ratio_e
    info["energy_demand_h"] = demand / hours if hours else 0
    add("energy", -demand * ratio_e)
    for b in PRODUCERS_ORDER:
        lv = bl.get(b, 0)
        if not lv:
            continue
        io = BUILDINGS[b]["io"]
        needs = {r: -v * lv * hours for r, v in io.items() if v < 0 and r != "energy"}
        ri = min([1.0] + [min(1.0, stock[r] / n) for r, n in needs.items() if n > 0])
        uses_e = io.get("energy", 0) < 0
        ratio = ri * (ratio_e if uses_e else 1.0)
        for r, n in needs.items():
            add(r, -n * ratio)
        for r, v in io.items():
            if v > 0:
                amt = v * lv * hours * ratio * pm(r)
                add(r, amt)
                info["gdp_value"] += amt * BASE_PRICES.get(r, 1.0)
        info[f"ratio_{b}"] = ratio
    # ---- پول: مالیات و هزینه‌ها ----
    tax = pop / 100000.0 * tax_rate * hours * stab_mult * max(0.0, 1 + fx.get("tax", 0) + 0.03 * bl.get("bank", 0)) * ev["tax"]
    info["tax"] = tax
    up_b = sum(BUILDINGS[b]["upkeep"] * lv for b, lv in bl.items()) * hours * max(0.2, 1 + fx.get("upkeep_build", 0))
    up_extra = 150.0 * max(0, n_cities - 1) * hours
    mult_army = max(0.2, 1 + fx.get("upkeep_army", 0)) * ev["upkeep_army"]
    army_money = army.get("money", 0.0) * hours * mult_army
    army_food = army.get("food", 0.0) * hours * mult_army
    army_fuel = army.get("fuel", 0.0) * hours * mult_army * max(0.2, 1 + fx.get("fuel_use", 0) - 0.02 * bl.get("logistics_center", 0))
    add("money", tax)
    money_due = up_b + up_extra + army_money
    money_paid = min(money_due, max(0.0, stock["money"]))
    add("money", -money_paid)
    unpaid = 0.0 if money_due <= 0 else 1.0 - money_paid / money_due
    food_due = pop / 20000.0 * hours + army_food
    food_paid = min(food_due, max(0.0, stock["food"]))
    add("food", -food_paid)
    starve = 0.0 if food_due <= 0 else 1.0 - food_paid / food_due
    fuel_paid = min(army_fuel, max(0.0, stock["fuel"]))
    add("fuel", -fuel_paid)
    fuel_short = 0.0 if army_fuel <= 0 else 1.0 - fuel_paid / army_fuel
    info.update(unpaid=unpaid, starve=starve, fuel_short=fuel_short, money_due_h=money_due / hours if hours else 0,
                tax_h=tax / hours if hours else 0, army_money_h=army_money / hours if hours else 0)
    info["gdp_value"] += tax
    info["gdp_rate"] = info["gdp_value"] / hours if hours else 0
    return d, info


def settle_player(c, uid, t=None, dry=False):
    """تسویهٔ تولید/مصرف بازیکن از آخرین تسویه تا اکنون (lazy) — همیشه داخل تراکنش."""
    p = get_player(c, uid)
    if not p:
        return None
    t = t or now()
    hours = (t - p["last_settle"]) / 3600.0
    if hours < 1 / 120.0 and not dry:
        return None
    if dry:
        hours = 1.0
    hours = min(hours, 72.0)
    res = all_res(c, uid)
    bl = blevels(c, uid)
    fx = effects(c, uid, p)
    cr = country_row(c, p["country"])
    ev = event_mods(c, p["country"], cr["region"])
    yields = city_yields(c, uid)
    cities = governed_cities(c, uid)
    pop = sum(x["population"] for x in cities)
    army = army_maintenance(c, uid)
    malus = temp_effect(c, uid, "prod_malus")
    boost = temp_effect(c, uid, "prod_boost")
    d, info = compute_flows(res, bl, fx, ev, hours, pop, p["tax_rate"], p["stability"], yields, army, len(cities), malus, boost)
    if dry:
        info["delta"] = dict(d)
        return info
    for r in RES_ORDER:
        dv = d.get(r, 0.0)
        if not dv:
            continue
        cap = storage_cap(bl, fx, r)
        newv = res[r] + dv
        if dv > 0 and newv > cap:
            newv = max(res[r], cap)
        newv = max(0.0, newv)
        ex(c, "INSERT INTO resources(user_id,res,amount) VALUES(?,?,?) ON CONFLICT(user_id,res) DO UPDATE SET amount=?", uid, r, newv, newv)
    target_stab = clamp(62 + fx.get("stability", 0) + ev["stability"] + (15 - p["tax_rate"]) * 1.1 + (p["prestige"] ** 0.5) * 0.5 - 30 * info["starve"] - 18 * info["unpaid"], 5, 95)
    k = min(1.0, hours / 6.0)
    stab = p["stability"] + (target_stab - p["stability"]) * k
    target_mor = clamp(58 + 0.22 * stab + (fx.get("morale", 0)) * 100 - 30 * info["starve"] - 20 * info["unpaid"] - 12 * info["fuel_short"], 10, 100)
    mor = p["morale"] + (target_mor - p["morale"]) * k
    ex(c, "UPDATE players SET last_settle=?, stability=?, morale=?, gdp=?, population=? WHERE user_id=?", t, stab, mor, info["gdp_rate"], pop, uid)
    ex(c, "INSERT INTO pstats(user_id,key,value) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value", uid, "energy_ratio", info["energy_ratio"])
    if info["starve"] > 0.3:
        notify(c, uid, "system", "🥀 قحطی! غذای کافی برای جمعیت و ارتش ندارید؛ ثبات و روحیه کاهش می‌یابد.")
    if info["unpaid"] > 0.3:
        notify(c, uid, "system", "💸 پول کافی برای هزینه‌های نگهداری ندارید؛ روحیهٔ ارتش افت می‌کند.")
    return info


def compute_powers(c, uid, p=None):
    p = p or get_player(c, uid)
    bl = blevels(c, uid)
    fx = effects(c, uid, p)
    mil = calc_military_power(c, uid)
    ind = 100.0 * (3 * bl.get("factory", 0) + 3 * bl.get("refinery", 0) + 2 * bl.get("mine", 0) + 2 * bl.get("power_plant", 0) + 4 * bl.get("military_factory", 0) + bl.get("farm", 0))
    recon = sum(r["qty"] * (UNITS[r["item_key"]]["acc"] / 100.0) * 6 for r in inv_stacks(c, uid, True) if UNITS[r["item_key"]]["cat"] in ("recon", "drone", "ew"))
    intel = (bl.get("intelligence_center", 0) * 60 + recon + 20) * (1 + fx.get("intel_power", 0))
    return dict(mil=mil, eco=p["gdp"], ind=ind, intel=intel)


def refresh_powers(c, uid):
    pw = compute_powers(c, uid)
    ex(c, "UPDATE players SET mil_power=?, eco_power=?, ind_power=?, int_power=? WHERE user_id=?", pw["mil"], pw["eco"], pw["ind"], pw["intel"], uid)
    return pw


def finish(c, uid):
    """پس از هر عملیات مهم: قدرت‌ها، آمار پیک، دستاوردها."""
    p = get_player(c, uid)
    if not p:
        return
    pw = refresh_powers(c, uid)
    bump_max(c, uid, "mil_power", pw["mil"])
    bump_max(c, uid, "money_peak", get_res(c, uid, "money"))
    bump_max(c, uid, "gdp_peak", p["gdp"])
    bump_max(c, uid, "territory_peak", qv(c, "SELECT COUNT(*) FROM cities WHERE governor_id=?", uid, default=0))
    bump_max(c, uid, "level", p["level"])
    check_achievements(c, uid)


def process_due(c, uid=None):
    n = complete_builds(c, uid) + complete_research(c, uid) + complete_production(c, uid)
    return n


def player_home(c, uid, touch=True):
    """نقطهٔ ورودی معمول هر تعامل: تکمیل صف‌ها + تسویه + مأموریت‌ها."""
    p = get_player(c, uid)
    if not p:
        return None
    process_due(c, uid)
    settle_player(c, uid)
    ensure_missions(c, uid)
    if touch:
        ex(c, "UPDATE players SET last_seen=?, activity=activity+1 WHERE user_id=?", now(), uid)
    return get_player(c, uid)


# ============================================================================
# MARKET / PRICE ENGINE — قیمت‌گذاری پویا متمرکز (عرضه/تقاضا واقعی)
# ============================================================================
def market_row(c, com):
    r = q1(c, "SELECT * FROM market WHERE commodity=?", com)
    if not r:
        raise GameError("کالای نامعتبر.")
    return r


def market_all(c):
    return {r["commodity"]: r for r in qa(c, "SELECT * FROM market")}


def _price_from_stock(base, stock, target, elastic):
    ratio = clamp(stock / max(1.0, target), 0.05, 8.0)
    return max(0.05, base * (ratio ** (-elastic)))


def market_tick(c):
    """به‌روزرسانی طبیعی بازار: عرضهٔ جهانی از تولید کشورها + میل قیمت به سمت تعادل + افت تورمی آرام."""
    last = int(meta_get(c, "market_last_tick", now() - 300))
    hours = clamp((now() - last) / 3600.0, 0.0, 6.0)
    if hours < 0.02:
        return
    evg = event_mods(c)
    for com in TRADABLE:
        m = market_row(c, com)
        world_supply = TARGET_STOCK[com] * 0.045 * hours * evg.get("prod_" + com, 1.0)
        world_demand = TARGET_STOCK[com] * 0.045 * hours
        stock = clamp(m["stock"] + world_supply - world_demand, TARGET_STOCK[com] * 0.05, TARGET_STOCK[com] * 4)
        price = _price_from_stock(m["base_price"], stock, TARGET_STOCK[com], ELASTICITY[com]) * evg.get("price_" + com, 1.0)
        price = m["price"] + (price - m["price"]) * min(1.0, hours * 0.6)
        ex(c, "UPDATE market SET stock=?, price=?, updated=? WHERE commodity=?", stock, round(price, 3), now(), com)
        ex(c, "INSERT INTO price_history(commodity,ts,price,stock,volume) VALUES(?,?,?,?,0)", com, now(), round(price, 3), stock)
    cur_infl = infl(c)
    meta_set(c, "inflation", min(4.0, cur_infl * (1 + 0.0006 * hours)))
    meta_set(c, "market_last_tick", now())
    ex(c, "DELETE FROM price_history WHERE id < (SELECT MAX(id) FROM price_history) - 20000")


def market_fee_rate(c, uid, p=None):
    p = p or get_player(c, uid)
    fx = effects(c, uid, p)
    ev = event_mods(c, p["country"], country_row(c, p["country"])["region"])
    return clamp(0.05 + fx.get("market_fee", 0) + (ev["market_fee"]) * 0.05, 0.005, 0.30)


def market_buy(c, uid, com, qty):
    if com not in TRADABLE:
        raise GameError("این کالا قابل معامله نیست.")
    qty = float(qty)
    if qty <= 0:
        raise GameError("مقدار نامعتبر.")
    market_tick(c)
    p = need_player(c, uid)
    m = market_row(c, com)
    fee = market_fee_rate(c, uid, p)
    unit_cost = m["price"] * (1 + fee)
    total = unit_cost * qty
    spend(c, uid, {"money": total})
    add_res(c, uid, com, qty)
    new_stock = max(TARGET_STOCK[com] * 0.03, m["stock"] - qty)
    new_price = _price_from_stock(m["base_price"], new_stock, TARGET_STOCK[com], ELASTICITY[com])
    ex(c, "UPDATE market SET stock=?, price=?, volume=volume+?, updated=? WHERE commodity=?", new_stock, round(new_price, 3), qty, now(), com)
    ex(c, "INSERT INTO market_trades(ts,user_id,commodity,side,qty,price,fee) VALUES(?,?,?,?,?,?,?)", now(), uid, com, "buy", qty, m["price"], total - m["price"] * qty)
    bump(c, uid, "market_trades", 1)
    bump(c, uid, "market_volume", total)
    audit(c, uid, "market_buy", com, f"x{qty} @ {unit_cost:.2f}")
    finish(c, uid)
    return dict(total=total, unit=unit_cost)


def market_sell(c, uid, com, qty):
    if com not in TRADABLE:
        raise GameError("این کالا قابل معامله نیست.")
    qty = float(qty)
    if qty <= 0:
        raise GameError("مقدار نامعتبر.")
    market_tick(c)
    p = need_player(c, uid)
    spend(c, uid, {com: qty})
    m = market_row(c, com)
    fee = market_fee_rate(c, uid, p)
    unit_rev = m["price"] * (1 - fee)
    total = unit_rev * qty
    add_res(c, uid, "money", total)
    new_stock = m["stock"] + qty
    new_price = _price_from_stock(m["base_price"], new_stock, TARGET_STOCK[com], ELASTICITY[com])
    ex(c, "UPDATE market SET stock=?, price=?, volume=volume+?, updated=? WHERE commodity=?", new_stock, round(new_price, 3), qty, now(), com)
    ex(c, "INSERT INTO market_trades(ts,user_id,commodity,side,qty,price,fee) VALUES(?,?,?,?,?,?,?)", now(), uid, com, "sell", qty, m["price"], m["price"] * qty - total)
    bump(c, uid, "market_trades", 1)
    bump(c, uid, "market_volume", total)
    audit(c, uid, "market_sell", com, f"x{qty} @ {unit_rev:.2f}")
    finish(c, uid)
    return dict(total=total, unit=unit_rev)


def order_place(c, uid, com, side, qty, limit_price):
    if com not in TRADABLE or side not in ("buy", "sell"):
        raise GameError("سفارش نامعتبر.")
    qty = float(qty)
    limit_price = float(limit_price)
    if qty <= 0 or limit_price <= 0:
        raise GameError("مقدار/قیمت نامعتبر.")
    if side == "buy":
        spend(c, uid, {"money": qty * limit_price})
    else:
        spend(c, uid, {com: qty})
    ex(c, "INSERT INTO orders(user_id,commodity,side,qty,limit_price,status,created) VALUES(?,?,?,?,?, 'open', ?)", uid, com, side, qty, limit_price, now())
    audit(c, uid, "order_place", com, f"{side} x{qty}@{limit_price}")
    return True


def order_cancel(c, uid, oid):
    r = q1(c, "SELECT * FROM orders WHERE id=? AND user_id=? AND status='open'", oid, uid)
    if not r:
        raise GameError("سفارش باز پیدا نشد.")
    ex(c, "UPDATE orders SET status='cancelled' WHERE id=?", oid)
    if r["side"] == "buy":
        add_res(c, uid, "money", r["qty"] * r["limit_price"])
    else:
        add_res(c, uid, r["commodity"], r["qty"])
    return True


def orders_match(c):
    """تطبیق سفارش‌های حدی در برابر قیمت فعلی بازار."""
    market_tick(c)
    filled = 0
    for r in qa(c, "SELECT * FROM orders WHERE status='open' ORDER BY id"):
        price = market_row(c, r["commodity"])["price"]
        if r["side"] == "buy" and price <= r["limit_price"]:
            add_res(c, r["user_id"], r["commodity"], r["qty"])
            refund = r["qty"] * (r["limit_price"] - price)
            if refund > 0:
                add_res(c, r["user_id"], "money", refund)
            ex(c, "UPDATE orders SET status='filled', filled_at=? WHERE id=?", now(), r["id"])
            notify(c, r["user_id"], "market", f"✅ سفارش خرید {rn(r['commodity'])} × {fmt(r['qty'])} در قیمت {fmt(price)} اجرا شد.")
            filled += 1
        elif r["side"] == "sell" and price >= r["limit_price"]:
            add_res(c, r["user_id"], "money", r["qty"] * price)
            ex(c, "UPDATE orders SET status='filled', filled_at=? WHERE id=?", now(), r["id"])
            notify(c, r["user_id"], "market", f"✅ سفارش فروش {rn(r['commodity'])} × {fmt(r['qty'])} در قیمت {fmt(price)} اجرا شد.")
            filled += 1
    return filled


def offer_create(c, uid, res, qty, unit_price):
    qty = float(qty)
    unit_price = float(unit_price)
    if res not in RES or qty <= 0 or unit_price <= 0:
        raise GameError("آگهی نامعتبر.")
    spend(c, uid, {res: qty})
    ex(c, "INSERT INTO offers(seller_id,res,qty,unit_price,status,created) VALUES(?,?,?,?, 'open', ?)", uid, res, qty, unit_price, now())
    bump(c, uid, "offers_created", 1)
    return True


def offer_cancel(c, uid, oid):
    r = q1(c, "SELECT * FROM offers WHERE id=? AND seller_id=? AND status='open'", oid, uid)
    if not r:
        raise GameError("آگهی پیدا نشد.")
    ex(c, "UPDATE offers SET status='cancelled', closed_at=? WHERE id=?", now(), oid)
    add_res(c, uid, r["res"], r["qty"])
    return True


def offer_buy(c, uid, oid):
    r = q1(c, "SELECT * FROM offers WHERE id=? AND status='open'", oid)
    if not r:
        raise GameError("این آگهی دیگر در دسترس نیست.")
    if r["seller_id"] == uid:
        raise GameError("نمی‌توانید آگهی خودتان را بخرید.")
    total = r["qty"] * r["unit_price"]
    spend(c, uid, {"money": total})
    cur = ex(c, "UPDATE offers SET status='sold', buyer_id=?, closed_at=? WHERE id=? AND status='open'", uid, now(), oid)
    if cur.rowcount != 1:
        add_res(c, uid, "money", total)
        raise GameError("این آگهی هم‌زمان توسط شخص دیگری خریداری شد.")
    fee = total * 0.04
    add_res(c, r["seller_id"], "money", total - fee)
    add_res(c, uid, r["res"], r["qty"])
    bump(c, r["seller_id"], "offers_sold", 1)
    bump(c, uid, "market_trades", 1)
    notify(c, r["seller_id"], "market", f"🏷️ آگهی {rn(r['res'])} × {fmt(r['qty'])} فروخته شد (+{fmt(total - fee)} 💰).")
    return total


def invest_start(c, uid, kind, amount):
    if kind not in INVEST_KINDS:
        raise GameError("نوع سرمایه‌گذاری نامعتبر.")
    amount = float(amount)
    if amount < 1000:
        raise GameError("حداقل مبلغ سرمایه‌گذاری ۱۰۰۰ پول است.")
    spend(c, uid, {"money": amount})
    _, _, secs, rate = INVEST_KINDS[kind]
    fx = effects(c, uid)
    rate = rate * (1 + fx.get("deposit_rate", 0))
    ex(c, "INSERT INTO investments(user_id,kind,principal,rate,started,ends_at,status) VALUES(?,?,?,?,?,?, 'active')", uid, kind, amount, rate, now(), now() + secs)
    bump(c, uid, "invest_total", amount)
    return dict(secs=secs, rate=rate, payout=amount * (1 + rate))


def invest_collect(c, uid=None):
    rows = qa(c, "SELECT * FROM investments WHERE status='active' AND ends_at<=?" + (" AND user_id=?" if uid else ""), *([now(), uid] if uid else [now()]))
    for r in rows:
        ex(c, "UPDATE investments SET status='done' WHERE id=?", r["id"])
        payout = r["principal"] * (1 + r["rate"])
        add_res(c, r["user_id"], "money", payout)
        notify(c, r["user_id"], "market", f"🏦 سپردهٔ شما سررسید شد: +{fmt(payout)} 💰")
        finish(c, r["user_id"])
    return len(rows)


# ============================================================================
# BATTLE ENGINE — نبرد چندمرحله‌ای قابل‌توضیح
# مراحل: Recon → Preparation → Engagement → Resolution
# فاکتورها: قدرت نظامی، فناوری، اطلاعات، روحیه، لجستیک، دفاع، زمین، آماده‌سازی، تجهیزات، سطح فرمانده، تصادف
# ============================================================================
def commander_level(p):
    return 1 + p["level"] // 4


def side_battle_profile(c, uid, terrain="plains", is_defender=False, city=None):
    """محاسبهٔ نیمرخ کامل یک طرف نبرد برای گزارش قابل‌توضیح."""
    p = get_player(c, uid)
    fx = effects(c, uid, p)
    tmod = TERRAINS[terrain][2]
    stacks = inv_stacks(c, uid, True)
    atk = defn = mob_sum = acc_sum = w = 0.0
    for r in stacks:
        if r["deployed"] <= 0:
            continue
        u = UNITS[r["item_key"]]
        n = r["deployed"]
        lvlmult = unit_lvl_mult(r["level"])
        durf = 0.5 + 0.5 * r["durability"] / 100.0
        kind = CATEGORIES[u["cat"]][3]
        tk = {"land": "armor" if u["cat"] in ("armor",) else "land_def", "air": "air", "naval": "naval", "support": "land_def"}.get(kind, "land_def")
        tfac = tmod.get(tk, 1.0) if kind == "land" else tmod.get(kind, 1.0)
        a = u["atk"] * lvlmult * durf * n * tfac
        d = u["df"] * lvlmult * durf * n * tfac
        if kind == "land":
            a *= (1 + fx.get("atk_land", 0))
        elif kind == "air":
            a *= (1 + fx.get("atk_air", 0))
        elif kind == "naval":
            a *= (1 + fx.get("atk_naval", 0))
        atk += a
        defn += d
        mob_sum += u["mob"] * n
        acc_sum += u["acc"] * n
        w += n
    garrison_def = 0.0
    if is_defender and city is not None:
        garrison_def = city["garrison"] * (1 + fx.get("def_all", 0) + fx.get("city_def", 0)) * (1 + 0.04 * city["fortification"])
        defn += garrison_def
    morale = clamp(0.55 + 0.45 * p["morale"] / 100.0 + fx.get("morale", 0) + temp_effect(c, uid, "morale_buff"), 0.35, 1.7)
    logi = clamp(0.6 + 0.4 * (stat_get(c, uid, "energy_ratio") or 1.0) + fx.get("supply", 0), 0.4, 1.5)
    tech_bonus = 1 + 0.01 * qv(c, "SELECT COUNT(*) FROM techs WHERE user_id=?", uid, default=0)
    intel_p = compute_powers(c, uid, p)["intel"]
    cl = commander_level(p)
    defn_all = defn * (1 + fx.get("def_all", 0))
    accuracy = (acc_sum / w / 100.0) if w else 0.6
    accuracy = clamp(accuracy + fx.get("accuracy", 0) + temp_effect(c, uid, "recon_boost"), 0.2, 0.98)
    return dict(uid=uid, atk=atk * morale * logi * tech_bonus, df=defn_all * morale * logi * tech_bonus, morale=morale, logi=logi,
                tech_bonus=tech_bonus, intel=intel_p, cl=cl, accuracy=accuracy, mobility=(mob_sum / w if w else 30), n_units=w, garrison=garrison_def, terrain=terrain)


def resolve_battle_core(att, dfn, rng=None, wr_action_mult=None):
    """هستهٔ خالص نبرد (بدون DB) — بازمی‌گرداند margin و شرح مراحل."""
    rng = rng or random.Random()
    stages = []
    recon_edge = clamp((att["intel"] - dfn["intel"]) / max(1.0, att["intel"] + dfn["intel"]), -0.4, 0.4)
    recon_bonus_att = 1 + 0.15 * max(0, recon_edge)
    recon_bonus_def = 1 + 0.15 * max(0, -recon_edge)
    stages.append(("🔭 شناسایی", f"برتری اطلاعاتی: {'مهاجم' if recon_edge > 0 else 'مدافع'} ({abs(recon_edge) * 100:.0f}٪)"))
    prep_att = 1 + 0.10 * (att["cl"] / 10.0)
    prep_def = 1 + 0.14 * (dfn["cl"] / 10.0)
    stages.append(("🧭 آماده‌سازی", f"سطح فرماندهی مهاجم {att['cl']} / مدافع {dfn['cl']}"))
    surprise = clamp(0.5 + (att["accuracy"] - dfn["accuracy"]) * 0.5, 0.15, 0.85)
    crit = rng.random() < (surprise - 0.5) * 0.4 + 0.12
    a_power = att["atk"] * recon_bonus_att * prep_att * (1.25 if crit else 1.0)
    d_power = dfn["df"] * recon_bonus_def * prep_def
    if wr_action_mult:
        a_power *= wr_action_mult.get("atk", 1.0)
        d_power *= wr_action_mult.get("df", 1.0)
    noise = rng.uniform(0.90, 1.10)
    a_power *= noise
    stages.append(("⚔️ درگیری", f"قدرت مؤثر مهاجم {fmt(a_power)} در برابر دفاع {fmt(d_power)}" + (" — ⚡ ضربهٔ غافلگیرکننده!" if crit else "")))
    total = a_power + d_power
    margin = 0.0 if total <= 0 else (a_power - d_power) / total
    win = margin > 0.03
    draw = abs(margin) <= 0.03
    stages.append(("🏁 نتیجه", "🟢 پیروزی مهاجم" if win else ("🟡 مساوی/عقب‌نشینی" if draw else "🔴 شکست مهاجم")))
    att_loss = clamp(0.05 + (0.35 if not win else 0.12) - 0.10 * (att["mobility"] / 100.0), 0.03, 0.55) * (1.3 if crit and not win else 1.0)
    def_loss = clamp(0.05 + (0.35 if win else 0.10), 0.03, 0.55) * (1.3 if crit and win else 1.0)
    return dict(win=win, draw=draw, margin=margin, a_power=a_power, d_power=d_power, att_loss=att_loss, def_loss=def_loss, crit=crit, stages=stages)


def apply_losses(c, uid, loss_ratio):
    """کاهش دوام یگان‌های مستقر متناسب با نسبت تلفات؛ برخی واحدها ممکن است کاملاً از بین بروند."""
    lost_units = 0
    for r in inv_stacks(c, uid, True):
        if r["deployed"] <= 0:
            continue
        dur_drop = loss_ratio * random.uniform(60, 100)
        newdur = r["durability"] - dur_drop
        if newdur <= 0:
            killed = max(1, int(r["deployed"] * clamp(loss_ratio, 0.05, 0.9)))
            killed = min(killed, r["deployed"])
            newq = r["qty"] - killed
            lost_units += killed
            if newq <= 0:
                ex(c, "DELETE FROM inventory WHERE id=?", r["id"])
            else:
                ex(c, "UPDATE inventory SET qty=?, deployed=?, durability=100 WHERE id=?", newq, max(0, r["deployed"] - killed), r["id"])
        else:
            ex(c, "UPDATE inventory SET durability=? WHERE id=?", max(1.0, newdur), r["id"])
    return lost_units


def battle_report_text(res, att_name, def_name):
    lines = [f"⚔️ گزارش نبرد: {att_name} ← {def_name}", ""]
    for title, desc in res["stages"]:
        lines.append(f"{title}: {desc}")
    lines.append("")
    lines.append(f"📊 قدرت مهاجم: {fmt(res['a_power'])}  |  قدرت مدافع: {fmt(res['d_power'])}")
    lines.append(f"📈 حاشیهٔ نتیجه: {res['margin'] * 100:+.1f}٪")
    return "\n".join(lines)


def attack_player(c, att_uid, def_uid):
    """نبرد کامل بازیکن-به-بازیکن با قفل، سپر، تلفات، غنیمت."""
    if att_uid == def_uid:
        raise GameError("نمی‌توانید به خودتان حمله کنید.")
    ap = need_player(c, att_uid)
    dp = get_player(c, def_uid)
    if not dp:
        raise GameError("این بازیکن پیدا نشد.")
    if dp["shield_until"] > now():
        raise GameError("🛡️ این فرمانده در حال حاضر تحت سپر محافظ است.")
    if ap["shield_until"] > now():
        ex(c, "UPDATE players SET shield_until=0 WHERE user_id=?", att_uid)
    cooldown(c, att_uid, "attack", 45)
    if battle_locked(c, att_uid) or battle_locked(c, def_uid):
        raise GameError("⚔️ یکی از طرفین هم‌اکنون درگیر نبرد دیگری است.")
    battle_lock_acquire(c, att_uid, f"pvp{def_uid}", 30)
    battle_lock_acquire(c, def_uid, f"pvp{att_uid}", 30)
    try:
        settle_player(c, att_uid)
        settle_player(c, def_uid)
        att = side_battle_profile(c, att_uid)
        city = q1(c, "SELECT * FROM cities WHERE governor_id=?", def_uid)
        dfn = side_battle_profile(c, def_uid, is_defender=True, city=city)
        if att["n_units"] < 1:
            raise GameError("هیچ یگانی مستقر (deployed) ندارید.")
        res = resolve_battle_core(att, dfn)
        loot = {}
        if res["win"]:
            for r_ in ("money", "food", "iron", "fuel", "goods"):
                have = get_res(c, def_uid, r_)
                take = min(have, have * 0.08 + 500)
                if take > 0:
                    add_res(c, def_uid, r_, -take)
                    add_res(c, att_uid, r_, take)
                    loot[r_] = loot.get(r_, 0) + take
            ex(c, "UPDATE players SET victories=victories+1, last_attacked=? WHERE user_id=?", now(), att_uid)
            ex(c, "UPDATE players SET defeats=defeats+1 WHERE user_id=?", def_uid)
            bump(c, att_uid, "battles_won", 1)
            bump(c, att_uid, "pvp_won", 1)
            add_xp(c, att_uid, 80)
            add_xp(c, def_uid, 25)
            ex(c, "UPDATE players SET shield_until=? WHERE user_id=?", now() + 900, def_uid)
        else:
            ex(c, "UPDATE players SET defeats=defeats+1 WHERE user_id=?", att_uid)
            add_xp(c, def_uid, 40)
        bump(c, att_uid, "battles_fought", 1)
        bump(c, def_uid, "battles_fought", 1)
        alost = apply_losses(c, att_uid, res["att_loss"])
        dlost = apply_losses(c, def_uid, res["def_loss"])
        rep = battle_report_text(res, ap["name"], dp["name"])
        ex(c, "INSERT INTO battles(ts,mode,attacker_id,defender_id,target_type,target_id,winner,att_power,def_power,margin,report,loot,att_loss,def_loss) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           now(), "pvp", att_uid, def_uid, "player", str(def_uid), "att" if res["win"] else "def", res["a_power"], res["d_power"], res["margin"], rep, jd(loot), alost, dlost)
        notify(c, def_uid, "attack", f"🚨 {ap['name']} به شما حمله کرد!\n{'باختید 😔' if res['win'] else 'دفاع موفق بود! 🛡️'}\n{reward_text(loot) if loot else ''}")
        audit(c, att_uid, "attack_player", str(def_uid), f"win={res['win']}")
        finish(c, att_uid)
        finish(c, def_uid)
        return res, rep, loot
    finally:
        battle_lock_release(c, att_uid, f"pvp{def_uid}")
        battle_lock_release(c, def_uid, f"pvp{att_uid}")


def attack_city(c, att_uid, city_id):
    """حمله به شهر یک کشور NPC یا بازیکن دیگر برای فتح قلمرو."""
    ap = need_player(c, att_uid)
    city = city_row(c, att_uid and city_id)
    if not city:
        raise GameError("شهر پیدا نشد.")
    if city["owner_country"] == ap["country"] and not city["governor_id"]:
        raise GameError("این شهر متعلق به کشور خودتان است.")
    if city["governor_id"] == att_uid:
        raise GameError("این شهر تحت حکمرانی خودتان است.")
    def_uid = city["governor_id"] or 0
    if def_uid and get_player(c, def_uid) and get_player(c, def_uid)["shield_until"] > now():
        raise GameError("🛡️ فرماندهٔ این شهر تحت سپر محافظ است.")
    cooldown(c, att_uid, "attack_city", 60)
    if battle_locked(c, att_uid):
        raise GameError("⚔️ هم‌اکنون درگیر نبرد دیگری هستید.")
    battle_lock_acquire(c, att_uid, f"city{city_id}", 30)
    try:
        settle_player(c, att_uid)
        if def_uid:
            settle_player(c, def_uid)
        att = side_battle_profile(c, att_uid, terrain=city["terrain"])
        if att["n_units"] < 1:
            raise GameError("هیچ یگانی مستقر ندارید.")
        if def_uid:
            dfn = side_battle_profile(c, def_uid, terrain=city["terrain"], is_defender=True, city=city)
        else:
            cr = country_row(c, city["owner_country"])
            npc_power = (cr["defense"] * 40 + city["garrison"]) * (1 + 0.05 * city["fortification"])
            dfn = dict(atk=0, df=npc_power, morale=1.0, logi=1.0, tech_bonus=1.0, intel=cr["intel"] * 8, cl=3, accuracy=0.6, mobility=30, n_units=1, garrison=city["garrison"], terrain=city["terrain"])
        res = resolve_battle_core(att, dfn)
        alost = apply_losses(c, att_uid, res["att_loss"])
        dlost = apply_losses(c, def_uid, res["def_loss"]) if def_uid else 0
        captured = False
        if res["win"] and res["margin"] > 0.15:
            old_gov = city["governor_id"]
            ex(c, "UPDATE cities SET governor_id=?, garrison=garrison*0.3, captured_at=?, last_battle=? WHERE id=?", att_uid, now(), now(), city_id)
            if old_gov:
                bump(c, old_gov, "cities_lost", 1)
                notify(c, old_gov, "attack", f"🏙️ شهر {city['name']} توسط {ap['name']} فتح شد!")
            bump(c, att_uid, "cities_captured", 1)
            add_xp(c, att_uid, 300)
            notify(c, att_uid, "military", f"🏙️ شهر {city['name']} را فتح کردید!")
            captured = True
        elif res["win"]:
            ex(c, "UPDATE cities SET garrison=garrison*0.7, last_battle=? WHERE id=?", now(), city_id)
        else:
            ex(c, "UPDATE cities SET garrison=MIN(garrison_max, garrison*1.05) WHERE id=?", city_id)
        bump(c, att_uid, "battles_fought", 1)
        bump(c, att_uid, "battles_won", 1 if res["win"] else 0)
        rep = battle_report_text(res, ap["name"], city["name"])
        ex(c, "INSERT INTO battles(ts,mode,attacker_id,defender_id,target_type,target_id,winner,att_power,def_power,margin,report,loot,att_loss,def_loss) VALUES(?,?,?,?,?,?,?,?,?,?,?,'{}',?,?)",
           now(), "city", att_uid, def_uid, "city", str(city_id), "att" if res["win"] else "def", res["a_power"], res["d_power"], res["margin"], rep, alost, dlost)
        audit(c, att_uid, "attack_city", str(city_id), f"win={res['win']} captured={captured}")
        finish(c, att_uid)
        if def_uid:
            finish(c, def_uid)
        return res, rep, captured
    finally:
        battle_lock_release(c, att_uid, f"city{city_id}")


# ============================================================================
# WAR ROOM — نبرد نوبتی PvP (چند دور، انتخاب کنش، ضدکنش)
# ============================================================================
def war_room_create(c, att_uid, def_uid, rounds=5):
    if att_uid == def_uid:
        raise GameError("نمی‌توانید با خودتان بجنگید.")
    ap = need_player(c, att_uid)
    dp = get_player(c, def_uid)
    if not dp:
        raise GameError("بازیکن پیدا نشد.")
    if dp["shield_until"] > now():
        raise GameError("🛡️ این فرمانده تحت سپر است.")
    if q1(c, "SELECT 1 FROM war_rooms WHERE (a_id=? OR b_id=?) AND status='active'", att_uid, att_uid):
        raise GameError("شما هم‌اکنون در یک اتاق جنگ فعال هستید.")
    if q1(c, "SELECT 1 FROM war_rooms WHERE (a_id=? OR b_id=?) AND status='active'", def_uid, def_uid):
        raise GameError("حریف هم‌اکنون در نبرد دیگری است.")
    a = side_battle_profile(c, att_uid)
    b = side_battle_profile(c, def_uid)
    if a["n_units"] < 1:
        raise GameError("یگان مستقر ندارید.")
    hp_a = a["atk"] + a["df"] + 1
    hp_b = b["atk"] + b["df"] + 1
    terrain = random.choice(list(TERRAINS))
    wid = ex(c, "INSERT INTO war_rooms(a_id,b_id,status,round,max_rounds,a_hp,b_hp,a_hp0,b_hp0,created,deadline,terrain,a_morale,b_morale,a_fuel,b_fuel) "
                "VALUES(?,?, 'active',0,?,?,?,?,?,?,?,?,1,1,1,1)", att_uid, def_uid, rounds, hp_a, hp_b, hp_a, hp_b, now(), now() + 3600, terrain).lastrowid
    notify(c, def_uid, "attack", f"⚔️ {ap['name']} شما را به اتاق جنگ دعوت کرد! برای پاسخ وارد ⚔️ جنگ شوید.")
    audit(c, att_uid, "war_room_create", str(def_uid))
    return wid


def war_room_act(c, wid, uid, action):
    if action not in WR_ACTIONS:
        raise GameError("کنش نامعتبر.")
    wr = q1(c, "SELECT * FROM war_rooms WHERE id=?", wid)
    if not wr or wr["status"] != "active":
        raise GameError("این اتاق جنگ فعال نیست.")
    if uid not in (wr["a_id"], wr["b_id"]):
        raise GameError("شما عضو این نبرد نیستید.")
    side = "a" if uid == wr["a_id"] else "b"
    if wr[f"{side}_action"]:
        raise GameError("قبلاً برای این دور کنش انتخاب کرده‌اید.")
    ex(c, f"UPDATE war_rooms SET {side}_action=? WHERE id=?", action, wid)
    wr = q1(c, "SELECT * FROM war_rooms WHERE id=?", wid)
    if wr["a_action"] and wr["b_action"]:
        return war_room_resolve_round(c, wid)
    return None


def war_room_resolve_round(c, wid):
    wr = q1(c, "SELECT * FROM war_rooms WHERE id=?", wid)
    a = side_battle_profile(c, wr["a_id"], terrain=wr["terrain"])
    b = side_battle_profile(c, wr["b_id"], terrain=wr["terrain"])
    aa, ba = wr["a_action"], wr["b_action"]
    am = dict(WR_ACTIONS[aa][2])
    bm = dict(WR_ACTIONS[ba][2])
    am["atk"] *= WR_COUNTER.get((aa, ba), 1.0)
    bm["atk"] *= WR_COUNTER.get((ba, aa), 1.0)
    rng = random.Random()
    dmg_to_b = a["atk"] * am["atk"] * wr["a_morale"] * wr["a_fuel"] * rng.uniform(0.85, 1.15)
    dmg_to_a = b["atk"] * bm["atk"] * wr["b_morale"] * wr["b_fuel"] * rng.uniform(0.85, 1.15)
    mit_b = 1.0 / max(0.3, bm["df"])
    mit_a = 1.0 / max(0.3, am["df"])
    dmg_to_b *= mit_b * bm["take"]
    dmg_to_a *= mit_a * am["take"]
    new_a_hp = max(0, wr["a_hp"] - dmg_to_a)
    new_b_hp = max(0, wr["b_hp"] - dmg_to_b)
    line = f"دور {wr['round'] + 1}: {WR_ACTIONS[aa][0]}{WR_ACTIONS[aa][1]} در برابر {WR_ACTIONS[ba][0]}{WR_ACTIONS[ba][1]} → آسیب به B: {fmt(dmg_to_b)} | آسیب به A: {fmt(dmg_to_a)}"
    log_ = (wr["log"] + "\n" + line).strip()
    rnd = wr["round"] + 1
    retreat = aa == "retreat" or ba == "retreat"
    done = new_a_hp <= 0 or new_b_hp <= 0 or rnd >= wr["max_rounds"] or retreat
    status = "done" if done else "active"
    winner = ""
    if done:
        if new_a_hp <= 0 and new_b_hp <= 0:
            winner = "draw"
        elif new_a_hp <= 0 or (retreat and aa == "retreat" and new_a_hp <= new_b_hp):
            winner = "b"
        elif new_b_hp <= 0 or (retreat and ba == "retreat" and new_b_hp <= new_a_hp):
            winner = "a"
        else:
            winner = "a" if new_a_hp >= new_b_hp else "b"
    ex(c, "UPDATE war_rooms SET round=?, a_hp=?, b_hp=?, a_action='', b_action='', a_last=?, b_last=?, log=?, status=?, winner=? WHERE id=?",
       rnd, new_a_hp, new_b_hp, aa, ba, log_, status, winner, wid)
    if done:
        _war_room_finish(c, wid)
    return dict(round=rnd, dmg_to_a=dmg_to_a, dmg_to_b=dmg_to_b, a_hp=new_a_hp, b_hp=new_b_hp, done=done, winner=winner, line=line)


def _war_room_finish(c, wid):
    wr = q1(c, "SELECT * FROM war_rooms WHERE id=?", wid)
    a_id, b_id, winner = wr["a_id"], wr["b_id"], wr["winner"]
    battle_lock_release(c, a_id)
    battle_lock_release(c, b_id)
    if winner == "a":
        win_id, lose_id = a_id, b_id
    elif winner == "b":
        win_id, lose_id = b_id, a_id
    else:
        win_id = lose_id = 0
    if win_id:
        ex(c, "UPDATE players SET victories=victories+1 WHERE user_id=?", win_id)
        ex(c, "UPDATE players SET defeats=defeats+1 WHERE user_id=?", lose_id)
        bump(c, win_id, "battles_won", 1)
        bump(c, win_id, "war_room_won", 1)
        add_xp(c, win_id, 150)
        add_xp(c, lose_id, 50)
        loot_money = min(get_res(c, lose_id, "money") * 0.05, 20000)
        if loot_money > 0:
            add_res(c, lose_id, "money", -loot_money)
            add_res(c, win_id, "money", loot_money)
        notify(c, win_id, "attack", f"🏆 در اتاق جنگ پیروز شدید! +{fmt(loot_money)} 💰")
        notify(c, lose_id, "attack", "💥 در اتاق جنگ شکست خوردید.")
    else:
        notify(c, a_id, "attack", "🤝 اتاق جنگ با نتیجهٔ مساوی پایان یافت.")
        notify(c, b_id, "attack", "🤝 اتاق جنگ با نتیجهٔ مساوی پایان یافت.")
    apply_losses(c, a_id, 0.15 if winner == "b" else 0.08)
    apply_losses(c, b_id, 0.15 if winner == "a" else 0.08)
    bump(c, a_id, "battles_fought", 1)
    bump(c, b_id, "battles_fought", 1)
    finish(c, a_id)
    finish(c, b_id)


def war_room_expire_check(c):
    n = 0
    for wr in qa(c, "SELECT * FROM war_rooms WHERE status='active' AND deadline<=?", now()):
        for side, other in (("a", "b"), ("b", "a")):
            if not wr[f"{side}_action"]:
                ex(c, f"UPDATE war_rooms SET {side}_action='retreat' WHERE id=?", wr["id"])
        wr2 = q1(c, "SELECT * FROM war_rooms WHERE id=?", wr["id"])
        if wr2["a_action"] and wr2["b_action"]:
            war_room_resolve_round(c, wr["id"])
            n += 1
    return n


# ============================================================================
# GROUP WAR — جنگ گروهی مبتنی بر نقش (اتحادها)
# ============================================================================
def group_war_create(c, alliance_id, leader_id, target_type, target_id, strategy="balanced"):
    if strategy not in STRATEGIES:
        strategy = "balanced"
    gwid = ex(c, "INSERT INTO group_wars(alliance_id,leader_id,target_type,target_id,strategy,status,created) VALUES(?,?,?,?,?, 'forming', ?)",
              alliance_id, leader_id, target_type, str(target_id), strategy, now()).lastrowid
    ex(c, "INSERT INTO group_war_members(war_id,user_id,role,joined) VALUES(?,?,?,?)", gwid, leader_id, "commander", now())
    return gwid


def group_war_join(c, gwid, uid, role):
    gw = q1(c, "SELECT * FROM group_wars WHERE id=?", gwid)
    if not gw or gw["status"] != "forming":
        raise GameError("این جنگ گروهی دیگر عضوگیری نمی‌کند.")
    if role not in GW_ROLES:
        raise GameError("نقش نامعتبر.")
    ex(c, "INSERT INTO group_war_members(war_id,user_id,role,joined) VALUES(?,?,?,?) ON CONFLICT(war_id,user_id) DO UPDATE SET role=excluded.role", gwid, uid, role, now())
    return True


def group_war_launch(c, gwid, launcher_id):
    gw = q1(c, "SELECT * FROM group_wars WHERE id=?", gwid)
    if not gw or gw["status"] != "forming":
        raise GameError("این جنگ گروهی در وضعیت آماده‌سازی نیست.")
    if gw["leader_id"] != launcher_id:
        raise GameError("فقط فرمانده می‌تواند عملیات را آغاز کند.")
    members = qa(c, "SELECT * FROM group_war_members WHERE war_id=?", gwid)
    if len(members) < 2:
        raise GameError("حداقل ۲ عضو برای جنگ گروهی لازم است.")
    strat = STRATEGIES[gw["strategy"]][2]
    tot_atk = tot_intel = tot_logi = 0.0
    for m in members:
        prof = side_battle_profile(c, m["user_id"])
        tot_atk += prof["atk"] * strat["atk"]
        tot_intel += prof["intel"] * (1.3 if m["role"] == "intel" else 1.0)
        tot_logi += prof["logi"] * (1.3 if m["role"] == "logistics" else 1.0)
    if gw["target_type"] == "city":
        city = city_row(c, int(gw["target_id"]))
        def_uid = city["governor_id"]
        if def_uid:
            dfn = side_battle_profile(c, def_uid, terrain=city["terrain"], is_defender=True, city=city)
        else:
            cr = country_row(c, city["owner_country"])
            dfn = dict(atk=0, df=(cr["defense"] * 50 + city["garrison"]), morale=1, logi=1, tech_bonus=1, intel=cr["intel"] * 10, cl=4, accuracy=0.62, mobility=30, n_units=1, garrison=city["garrison"], terrain=city["terrain"])
    else:
        def_uid = int(gw["target_id"])
        c2 = q1(c, "SELECT * FROM cities WHERE governor_id=?", def_uid)
        dfn = side_battle_profile(c, def_uid, is_defender=True, city=c2)
    att = dict(atk=tot_atk * (1 + min(0.3, tot_intel / 10000.0)), df=tot_atk * 0.3, morale=1, logi=tot_logi / max(1, len(members)), tech_bonus=1, intel=tot_intel, cl=5, accuracy=0.75, mobility=40, n_units=len(members))
    res = resolve_battle_core(att, dfn)
    for m in members:
        loss = res["att_loss"] * strat["loss"]
        apply_losses(c, m["user_id"], loss)
        bump(c, m["user_id"], "battles_fought", 1)
        bump(c, m["user_id"], "group_war_done", 1)
        if res["win"]:
            bump(c, m["user_id"], "battles_won", 1)
            add_xp(c, m["user_id"], 200)
        finish(c, m["user_id"])
    captured = False
    if res["win"] and res["margin"] > 0.18 and gw["target_type"] == "city":
        cid = int(gw["target_id"])
        ex(c, "UPDATE cities SET governor_id=?, garrison=garrison*0.3, captured_at=?, last_battle=? WHERE id=?", gw["leader_id"], now(), now(), cid)
        bump(c, gw["leader_id"], "cities_captured", 1)
        captured = True
    ex(c, "UPDATE group_wars SET status='done', launched_at=?, result=? WHERE id=?", now(), jd(dict(win=res["win"], margin=res["margin"], captured=captured)), gwid)
    ex(c, "INSERT INTO alliance_logs(alliance_id,ts,actor,text) VALUES(?,?,?,?)", gw["alliance_id"], now(), launcher_id,
       f"👥 جنگ گروهی {'موفق' if res['win'] else 'ناموفق'} — حاشیه {res['margin'] * 100:+.1f}٪")
    return res, captured


# ============================================================================
# ALLIANCE SYSTEM
# ============================================================================
def alliance_of(c, uid):
    p = get_player(c, uid)
    return q1(c, "SELECT * FROM alliances WHERE id=?", p["alliance_id"]) if p and p["alliance_id"] else None


def alliance_member_row(c, aid, uid):
    return q1(c, "SELECT * FROM alliance_members WHERE alliance_id=? AND user_id=?", aid, uid)


def alliance_has_perm(c, uid, perm):
    p = get_player(c, uid)
    if not p or not p["alliance_id"]:
        return False
    m = alliance_member_row(c, p["alliance_id"], uid)
    if not m:
        return False
    return perm in ALLIANCE_ROLES[m["role"]][2]


def alliance_create(c, uid, name, tag):
    p = need_player(c, uid)
    if p["alliance_id"]:
        raise GameError("شما هم‌اکنون عضو یک اتحاد هستید.")
    name = clean_name(name, 3, 30)
    tag = re.sub(r"[^A-Za-z0-9آ-ی]", "", str(tag))[:6].upper()
    if len(tag) < 2:
        raise GameError("تگ اتحاد باید حداقل ۲ نویسه باشد.")
    if q1(c, "SELECT 1 FROM alliances WHERE lower(name)=lower(?) OR upper(tag)=upper(?)", name, tag):
        raise GameError("این نام یا تگ قبلاً استفاده شده است.")
    spend(c, uid, {"money": 50000})
    aid = ex(c, "INSERT INTO alliances(name,tag,leader_id,level,xp,created) VALUES(?,?,?,1,0,?)", name, tag, uid, now()).lastrowid
    ex(c, "INSERT INTO alliance_members(alliance_id,user_id,role,joined) VALUES(?,?,'leader',?)", aid, uid, now())
    ex(c, "UPDATE players SET alliance_id=? WHERE user_id=?", aid, uid)
    for r in RES_ORDER:
        ex(c, "INSERT OR IGNORE INTO alliance_treasury(alliance_id,res,amount) VALUES(?,?,0)", aid, r)
    bump(c, uid, "alliance_joined", 1)
    audit(c, uid, "alliance_create", str(aid), name)
    finish(c, uid)
    return aid


def alliance_disband(c, uid):
    p = need_player(c, uid)
    if not alliance_has_perm(c, uid, "disband"):
        raise GameError("فقط رهبر می‌تواند اتحاد را منحل کند.")
    aid = p["alliance_id"]
    for m in qa(c, "SELECT user_id FROM alliance_members WHERE alliance_id=?", aid):
        ex(c, "UPDATE players SET alliance_id=0 WHERE user_id=?", m["user_id"])
        notify(c, m["user_id"], "alliance", "⚠️ اتحاد شما منحل شد.")
    ex(c, "DELETE FROM alliance_members WHERE alliance_id=?", aid)
    ex(c, "DELETE FROM alliance_treasury WHERE alliance_id=?", aid)
    ex(c, "DELETE FROM alliances WHERE id=?", aid)
    return True


def alliance_invite(c, uid, target_uid):
    p = need_player(c, uid)
    if not alliance_has_perm(c, uid, "invite"):
        raise GameError("اجازهٔ دعوت‌کردن ندارید.")
    tp = get_player(c, target_uid)
    if not tp:
        raise GameError("بازیکن پیدا نشد.")
    if tp["alliance_id"]:
        raise GameError("این بازیکن هم‌اکنون عضو اتحادی دیگر است.")
    ex(c, "INSERT OR REPLACE INTO alliance_invites(alliance_id,user_id,by_user,ts) VALUES(?,?,?,?)", p["alliance_id"], target_uid, uid, now())
    al = q1(c, "SELECT * FROM alliances WHERE id=?", p["alliance_id"])
    notify(c, target_uid, "alliance", f"🤝 دعوت به اتحاد «{al['name']}» [{al['tag']}] دریافت کردید.")
    return True


def alliance_join_request(c, uid, aid):
    p = need_player(c, uid)
    if p["alliance_id"]:
        raise GameError("شما هم‌اکنون عضو یک اتحاد هستید.")
    al = q1(c, "SELECT * FROM alliances WHERE id=?", aid)
    if not al:
        raise GameError("اتحاد پیدا نشد.")
    if p["level"] < al["min_level"]:
        raise GameError(f"حداقل سطح موردنیاز برای این اتحاد: {al['min_level']}")
    if al["policy"] == "open":
        return alliance_accept_member(c, aid, uid)
    ex(c, "INSERT OR REPLACE INTO alliance_requests(alliance_id,user_id,ts) VALUES(?,?,?)", aid, uid, now())
    notify(c, al["leader_id"], "alliance", f"📨 درخواست عضویت جدید در «{al['name']}» از {p['name']}")
    return "pending"


def alliance_accept_member(c, aid, uid):
    p = get_player(c, uid)
    ex(c, "INSERT OR IGNORE INTO alliance_members(alliance_id,user_id,role,joined) VALUES(?,?,'recruit',?)", aid, uid, now())
    ex(c, "UPDATE players SET alliance_id=? WHERE user_id=?", aid, uid)
    ex(c, "DELETE FROM alliance_invites WHERE alliance_id=? AND user_id=?", aid, uid)
    ex(c, "DELETE FROM alliance_requests WHERE alliance_id=? AND user_id=?", aid, uid)
    bump(c, uid, "alliance_joined", 1)
    al = q1(c, "SELECT * FROM alliances WHERE id=?", aid)
    ex(c, "INSERT INTO alliance_logs(alliance_id,ts,actor,text) VALUES(?,?,?,?)", aid, now(), uid, f"🎉 {p['name']} به اتحاد پیوست.")
    notify(c, al["leader_id"], "alliance", f"🎉 {p['name']} به اتحاد پیوست.")
    finish(c, uid)
    return "joined"


def alliance_invite_accept(c, uid, aid):
    r = q1(c, "SELECT * FROM alliance_invites WHERE alliance_id=? AND user_id=?", aid, uid)
    if not r:
        raise GameError("دعوتی یافت نشد.")
    return alliance_accept_member(c, aid, uid)


def alliance_leave(c, uid):
    p = need_player(c, uid)
    if not p["alliance_id"]:
        raise GameError("شما عضو هیچ اتحادی نیستید.")
    m = alliance_member_row(c, p["alliance_id"], uid)
    if m["role"] == "leader":
        raise GameError("رهبر باید ابتدا رهبری را واگذار کند یا اتحاد را منحل کند.")
    ex(c, "DELETE FROM alliance_members WHERE alliance_id=? AND user_id=?", p["alliance_id"], uid)
    ex(c, "UPDATE players SET alliance_id=0 WHERE user_id=?", uid)
    ex(c, "INSERT INTO alliance_logs(alliance_id,ts,actor,text) VALUES(?,?,?,?)", p["alliance_id"], now(), uid, f"👋 {p['name']} اتحاد را ترک کرد.")
    return True


def alliance_kick(c, uid, target_uid):
    p = need_player(c, uid)
    if not alliance_has_perm(c, uid, "kick"):
        raise GameError("اجازهٔ اخراج ندارید.")
    m = alliance_member_row(c, p["alliance_id"], target_uid)
    if not m:
        raise GameError("این بازیکن عضو اتحاد شما نیست.")
    if m["role"] == "leader":
        raise GameError("نمی‌توانید رهبر را اخراج کنید.")
    ex(c, "DELETE FROM alliance_members WHERE alliance_id=? AND user_id=?", p["alliance_id"], target_uid)
    ex(c, "UPDATE players SET alliance_id=0 WHERE user_id=?", target_uid)
    notify(c, target_uid, "alliance", "🚪 از اتحاد اخراج شدید.")
    return True


def alliance_promote(c, uid, target_uid, new_role):
    p = need_player(c, uid)
    if not alliance_has_perm(c, uid, "promote"):
        raise GameError("اجازهٔ ارتقا/تنزل ندارید.")
    if new_role not in ALLIANCE_ROLES or new_role == "leader":
        raise GameError("نقش نامعتبر.")
    m = alliance_member_row(c, p["alliance_id"], target_uid)
    if not m:
        raise GameError("عضو یافت نشد.")
    my_role = alliance_member_row(c, p["alliance_id"], uid)["role"]
    if ROLE_RANK[new_role] >= ROLE_RANK[my_role] and uid != target_uid:
        raise GameError("نمی‌توانید نقشی برابر یا بالاتر از خودتان بدهید.")
    ex(c, "UPDATE alliance_members SET role=? WHERE alliance_id=? AND user_id=?", new_role, p["alliance_id"], target_uid)
    notify(c, target_uid, "alliance", f"🎖️ نقش شما به «{ALLIANCE_ROLES[new_role][1]}» تغییر کرد.")
    return True


def alliance_transfer_leadership(c, uid, target_uid):
    p = need_player(c, uid)
    m = alliance_member_row(c, p["alliance_id"], uid)
    if not m or m["role"] != "leader":
        raise GameError("فقط رهبر می‌تواند رهبری را واگذار کند.")
    if not alliance_member_row(c, p["alliance_id"], target_uid):
        raise GameError("این کاربر عضو اتحاد نیست.")
    ex(c, "UPDATE alliance_members SET role='leader' WHERE alliance_id=? AND user_id=?", p["alliance_id"], target_uid)
    ex(c, "UPDATE alliance_members SET role='deputy' WHERE alliance_id=? AND user_id=?", p["alliance_id"], uid)
    ex(c, "UPDATE alliances SET leader_id=? WHERE id=?", target_uid, p["alliance_id"])
    return True


def alliance_contribute(c, uid, res, amount):
    p = need_player(c, uid)
    if not p["alliance_id"]:
        raise GameError("عضو هیچ اتحادی نیستید.")
    if res not in RES:
        raise GameError("منبع نامعتبر.")
    amount = float(amount)
    spend(c, uid, {res: amount})
    ex(c, "INSERT INTO alliance_treasury(alliance_id,res,amount) VALUES(?,?,?) ON CONFLICT(alliance_id,res) DO UPDATE SET amount=amount+?", p["alliance_id"], res, amount, amount)
    ex(c, "UPDATE alliance_members SET contribution=contribution+? WHERE alliance_id=? AND user_id=?", amount * BASE_PRICES.get(res, 1) if res != "money" else amount, p["alliance_id"], uid)
    xpgain = int(amount * BASE_PRICES.get(res, 1) / 200) if res != "money" else int(amount / 200)
    ex(c, "UPDATE alliances SET xp=xp+? WHERE id=?", xpgain, p["alliance_id"])
    _alliance_check_levelup(c, p["alliance_id"])
    bump(c, uid, "alliance_contrib", 1)
    finish(c, uid)
    return True


def _alliance_check_levelup(c, aid):
    al = q1(c, "SELECT * FROM alliances WHERE id=?", aid)
    lvl = al["level"]
    while lvl < len(ALLIANCE_LEVEL_XP) and al["xp"] >= ALLIANCE_LEVEL_XP[lvl]:
        lvl += 1
    if lvl != al["level"]:
        ex(c, "UPDATE alliances SET level=? WHERE id=?", lvl, aid)
        for m in qa(c, "SELECT user_id FROM alliance_members WHERE alliance_id=?", aid):
            notify(c, m["user_id"], "alliance", f"🎉 اتحاد به سطح {lvl} ارتقا یافت!")


def alliance_treasury(c, aid):
    return {r["res"]: r["amount"] for r in qa(c, "SELECT * FROM alliance_treasury WHERE alliance_id=?", aid)}


def alliance_research_start(c, uid, tech_key):
    p = need_player(c, uid)
    if not alliance_has_perm(c, uid, "research"):
        raise GameError("اجازهٔ پژوهش اتحاد ندارید.")
    if tech_key not in ALLIANCE_TECHS:
        raise GameError("فناوری اتحاد نامعتبر.")
    if q1(c, "SELECT 1 FROM alliance_techs WHERE alliance_id=? AND tech_id=?", p["alliance_id"], tech_key):
        raise GameError("این فناوری قبلاً تکمیل شده است.")
    al = q1(c, "SELECT * FROM alliances WHERE id=?", p["alliance_id"])
    t = ALLIANCE_TECHS[tech_key]
    if al["level"] < t["lvl"]:
        raise GameError(f"سطح اتحاد {t['lvl']} لازم است (فعلی: {al['level']}).")
    treas = alliance_treasury(c, p["alliance_id"])
    missing = [k for k, v in t["cost"].items() if treas.get(k, 0) < v]
    if missing:
        raise GameError("💰 خزانهٔ اتحاد کافی نیست: " + "، ".join(rn(k) for k in missing))
    for k, v in t["cost"].items():
        ex(c, "UPDATE alliance_treasury SET amount=amount-? WHERE alliance_id=? AND res=?", v, p["alliance_id"], k)
    ex(c, "INSERT INTO alliance_techs(alliance_id,tech_id,done_at) VALUES(?,?,?)", p["alliance_id"], tech_key, now())
    ex(c, "INSERT INTO alliance_logs(alliance_id,ts,actor,text) VALUES(?,?,?,?)", p["alliance_id"], now(), uid, f"🔬 فناوری اتحاد «{t['name']}» تحقیق شد.")
    for m in qa(c, "SELECT user_id FROM alliance_members WHERE alliance_id=?", p["alliance_id"]):
        notify(c, m["user_id"], "alliance", f"🔬 فناوری اتحاد «{t['name']}» فعال شد: {t['desc']}")
    return True


def alliance_project_start(c, uid, proj_key):
    p = need_player(c, uid)
    if not alliance_has_perm(c, uid, "project"):
        raise GameError("اجازهٔ مدیریت پروژه ندارید.")
    if proj_key not in ALLIANCE_PROJECTS:
        raise GameError("پروژه نامعتبر.")
    if q1(c, "SELECT 1 FROM alliance_projects WHERE alliance_id=? AND kind=? AND status='active'", p["alliance_id"], proj_key):
        raise GameError("این پروژه هم‌اکنون فعال است.")
    proj = ALLIANCE_PROJECTS[proj_key]
    ex(c, "INSERT INTO alliance_projects(alliance_id,kind,goal,progress,status,created) VALUES(?,?,?,?, 'active', ?)",
       p["alliance_id"], proj_key, jd(proj["goal"]), jd({k: 0 for k in proj["goal"]}), now())
    return True


def alliance_project_contribute(c, uid, project_id, res, amount):
    p = need_player(c, uid)
    proj = q1(c, "SELECT * FROM alliance_projects WHERE id=? AND alliance_id=? AND status='active'", project_id, p["alliance_id"])
    if not proj:
        raise GameError("پروژه پیدا نشد.")
    goal = jl(proj["goal"])
    if res not in goal:
        raise GameError("این منبع برای این پروژه لازم نیست.")
    amount = float(amount)
    spend(c, uid, {res: amount})
    progress = jl(proj["progress"])
    progress[res] = min(goal[res], progress.get(res, 0) + amount)
    ex(c, "UPDATE alliance_projects SET progress=? WHERE id=?", jd(progress), project_id)
    ex(c, "UPDATE alliance_members SET contribution=contribution+? WHERE alliance_id=? AND user_id=?", amount * BASE_PRICES.get(res, 1) if res != "money" else amount, p["alliance_id"], uid)
    if all(progress.get(k, 0) >= v for k, v in goal.items()):
        ex(c, "UPDATE alliance_projects SET status='done', completed_at=? WHERE id=?", now(), project_id)
        ex(c, "INSERT INTO alliance_techs(alliance_id,tech_id,done_at) VALUES(?,?,?)", p["alliance_id"], f"proj:{proj['kind']}", now())
        ex(c, "UPDATE alliances SET xp=xp+? WHERE id=?", ALLIANCE_PROJECTS[proj["kind"]]["xp"], p["alliance_id"])
        _alliance_check_levelup(c, p["alliance_id"])
        pk = ALLIANCE_PROJECTS[proj["kind"]]
        for m in qa(c, "SELECT user_id FROM alliance_members WHERE alliance_id=?", p["alliance_id"]):
            notify(c, m["user_id"], "alliance", f"🏗️ پروژهٔ «{pk['name']}» تکمیل شد: {pk['desc']}")
    finish(c, uid)
    return jl(q1(c, "SELECT progress FROM alliance_projects WHERE id=?", project_id)["progress"])


def alliance_vote_create(c, uid, kind, payload, hours=24):
    p = need_player(c, uid)
    if not alliance_has_perm(c, uid, "vote"):
        raise GameError("اجازهٔ ایجاد رأی‌گیری ندارید.")
    if kind not in VOTE_KINDS:
        raise GameError("نوع رأی‌گیری نامعتبر.")
    vid = ex(c, "INSERT INTO votes(alliance_id,kind,payload,created_by,created,ends_at,status) VALUES(?,?,?,?,?,?, 'open')",
             p["alliance_id"], kind, jd(payload), uid, now(), now() + hours * 3600).lastrowid
    for m in qa(c, "SELECT user_id FROM alliance_members WHERE alliance_id=?", p["alliance_id"]):
        notify(c, m["user_id"], "vote", f"🗳️ رأی‌گیری جدید: {VOTE_KINDS[kind]}")
    return vid


def alliance_vote_cast(c, uid, vote_id, choice):
    p = need_player(c, uid)
    v = q1(c, "SELECT * FROM votes WHERE id=? AND alliance_id=? AND status='open'", vote_id, p["alliance_id"])
    if not v:
        raise GameError("رأی‌گیری فعال پیدا نشد.")
    if v["ends_at"] < now():
        raise GameError("زمان این رأی‌گیری به پایان رسیده است.")
    ex(c, "INSERT INTO vote_ballots(vote_id,user_id,choice) VALUES(?,?,?) ON CONFLICT(vote_id,user_id) DO UPDATE SET choice=excluded.choice", vote_id, uid, int(choice))
    bump(c, uid, "alliance_votes", 1)
    return True


def alliance_vote_tally(c, vote_id):
    yes = qv(c, "SELECT COUNT(*) FROM vote_ballots WHERE vote_id=? AND choice=1", vote_id, default=0)
    no = qv(c, "SELECT COUNT(*) FROM vote_ballots WHERE vote_id=? AND choice=0", vote_id, default=0)
    return yes, no


def alliance_votes_close_due(c):
    n = 0
    for v in qa(c, "SELECT * FROM votes WHERE status='open' AND ends_at<=?", now()):
        yes, no = alliance_vote_tally(c, v["id"])
        passed = yes > no
        ex(c, "UPDATE votes SET status='closed', result=? WHERE id=?", jd(dict(yes=yes, no=no, passed=passed)), v["id"])
        ex(c, "INSERT INTO alliance_logs(alliance_id,ts,actor,text) VALUES(?,?,0,?)", v["alliance_id"], now(), f"🗳️ رأی‌گیری «{VOTE_KINDS.get(v['kind'], v['kind'])}» {'تصویب شد ✅' if passed else 'رد شد ❌'} ({yes}-{no})")
        n += 1
    return n


def alliance_leaderboard_rows(c, order_by="power", limit=20):
    col = {"power": "power", "level": "level"}.get(order_by, "power")
    rows = qa(c, "SELECT * FROM alliances ORDER BY " + col + " DESC LIMIT ?", limit)
    return rows


def alliance_refresh_power(c, aid):
    p = qv(c, "SELECT COALESCE(SUM(pl.mil_power+pl.eco_power*0.2+pl.ind_power*0.3),0) FROM alliance_members am JOIN players pl ON pl.user_id=am.user_id WHERE am.alliance_id=?", aid, default=0)
    ex(c, "UPDATE alliances SET power=? WHERE id=?", p, aid)
    return p


# ============================================================================
# DIPLOMACY — روابط دوجانبه، معاهدات
# ============================================================================
def dip_action(c, uid, target_uid, action):
    if action not in DIP_ACTIONS:
        raise GameError("اقدام نامعتبر.")
    p = need_player(c, uid)
    tp = get_player(c, target_uid)
    if not tp:
        raise GameError("بازیکن پیدا نشد.")
    if target_uid == uid:
        raise GameError("قابل‌اجرا روی خودتان نیست.")
    cooldown(c, uid, f"dip_{action}", 120)
    emoji, name, desc, cost = DIP_ACTIONS[action]
    fx = effects(c, uid, p)
    gain_mult = 1 + fx.get("relation_gain", 0)
    if action == "aid":
        spend(c, uid, {"money": cost})
        d = 8 * gain_mult
    elif action == "talks":
        p2 = need_player(c, uid)
        if p2["influence"] < 4:
            raise GameError("نفوذ کافی نیست.")
        ex(c, "UPDATE players SET influence=influence-4 WHERE user_id=?", uid)
        d = 5 * gain_mult
    elif action == "trade_deal":
        if p["influence"] < 6:
            raise GameError("نفوذ کافی نیست.")
        spend(c, uid, {"money": cost})
        ex(c, "UPDATE players SET influence=influence-6 WHERE user_id=?", uid)
        d = 12 * gain_mult
    elif action == "sanction":
        if p["influence"] < 5:
            raise GameError("نفوذ کافی نیست.")
        ex(c, "UPDATE players SET influence=influence-5 WHERE user_id=?", uid)
        d = -15
    else:  # embassy
        if p["influence"] < 15:
            raise GameError("نفوذ کافی نیست.")
        spend(c, uid, {"money": cost})
        ex(c, "UPDATE players SET influence=influence-15 WHERE user_id=?", uid)
        d = 20 * gain_mult
    newrel = add_relation_players(c, uid, target_uid, d)
    bump(c, uid, "diplomacy_actions", 1)
    notify(c, target_uid, "diplomacy", f"{emoji} {p['name']} اقدام «{name}» انجام داد. روابط: {fmt(newrel)}")
    finish(c, uid)
    return newrel


def relation_key(a, b):
    return (a, b) if a < b else (b, a)


def get_relation_players(c, a, b):
    if a == b:
        return 100.0
    x, y = relation_key(a, b)
    return qv(c, "SELECT value FROM player_relations WHERE a=? AND b=?", x, y, default=0.0) if _has_player_relations(c) else _rel_fallback(c, x, y)


_PR_INIT = {"done": False}


def _has_player_relations(c):
    if not _PR_INIT["done"]:
        c.execute("CREATE TABLE IF NOT EXISTS player_relations(a INTEGER, b INTEGER, value REAL, PRIMARY KEY(a,b))")
        _PR_INIT["done"] = True
    return True


def _rel_fallback(c, x, y):
    return 0.0


def add_relation_players(c, a, b, delta):
    _has_player_relations(c)
    x, y = relation_key(a, b)
    cur = qv(c, "SELECT value FROM player_relations WHERE a=? AND b=?", x, y, default=0.0)
    v = clamp(cur + delta, -100, 100)
    ex(c, "INSERT INTO player_relations(a,b,value) VALUES(?,?,?) ON CONFLICT(a,b) DO UPDATE SET value=excluded.value", x, y, v)
    return v


def treaty_propose(c, uid, target_uid, kind):
    if kind not in TREATY_KINDS:
        raise GameError("نوع معاهده نامعتبر.")
    p = need_player(c, uid)
    _, _, dur_, cost_infl = TREATY_KINDS[kind]
    ex(c, "INSERT INTO treaties(a_user,b_user,kind,status,created,until) VALUES(?,?,?, 'pending', ?, 0)", uid, target_uid, kind, now())
    tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    notify(c, target_uid, "diplomacy", f"📜 پیشنهاد {TREATY_KINDS[kind][0]} {TREATY_KINDS[kind][1]} از {p['name']} دریافت شد.")
    return tid


def treaty_respond(c, uid, treaty_id, accept):
    t = q1(c, "SELECT * FROM treaties WHERE id=? AND b_user=? AND status='pending'", treaty_id, uid)
    if not t:
        raise GameError("این پیشنهاد یافت نشد.")
    if not accept:
        ex(c, "UPDATE treaties SET status='rejected' WHERE id=?", treaty_id)
        return False
    _, _, dur_, infl_cost = TREATY_KINDS[t["kind"]]
    p = need_player(c, uid)
    if p["influence"] < infl_cost:
        raise GameError("نفوذ کافی برای پذیرش ندارید.")
    ex(c, "UPDATE players SET influence=influence-? WHERE user_id=?", infl_cost, uid)
    ex(c, "UPDATE players SET influence=influence-? WHERE user_id=?", infl_cost, t["a_user"])
    ex(c, "UPDATE treaties SET status='active', until=? WHERE id=?", now() + dur_, treaty_id)
    add_relation_players(c, uid, t["a_user"], 15)
    bump(c, uid, "treaties_signed", 1)
    bump(c, t["a_user"], "treaties_signed", 1)
    notify(c, t["a_user"], "diplomacy", f"✅ معاهدهٔ {TREATY_KINDS[t['kind']][1]} پذیرفته شد.")
    return True


def treaty_active_between(c, a, b, kind=None):
    q = "SELECT * FROM treaties WHERE status='active' AND until>? AND ((a_user=? AND b_user=?) OR (a_user=? AND b_user=?))"
    args = [now(), a, b, b, a]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    return q1(c, q, *args)


def treaties_expire(c):
    ex(c, "UPDATE treaties SET status='expired' WHERE status='active' AND until<=?", now())


# ============================================================================
# INTELLIGENCE ENGINE
# ============================================================================
def intel_op(c, uid, ttype, target_id, op_kind, source="sigint"):
    if op_kind not in INTEL_OPS:
        raise GameError("نوع عملیات اطلاعاتی نامعتبر.")
    if source not in INTEL_SOURCES:
        source = "sigint"
    p = need_player(c, uid)
    _, _, cost, _ = INTEL_OPS[op_kind]
    src_acc, src_cost_mult = INTEL_SOURCES[source][2], INTEL_SOURCES[source][3]
    cost = int(cost * src_cost_mult)
    cooldown(c, uid, f"intel_{op_kind}", 90)
    spend(c, uid, {"money": cost})
    fx = effects(c, uid, p)
    my_intel = compute_powers(c, uid, p)["intel"]
    target_counter = 0.0
    data = {}
    if ttype == "player":
        tp = get_player(c, int(target_id))
        if not tp:
            raise GameError("هدف پیدا نشد.")
        tfx = effects(c, tp["user_id"], tp)
        target_counter = tfx.get("counter_intel", 0)
        if op_kind == "military":
            data = dict(mil_power=tp["mil_power"], level=tp["level"], victories=tp["victories"], defeats=tp["defeats"],
                        units=[dict(name=item_name(r["item_key"]), qty=r["qty"], deployed=r["deployed"]) for r in inv_stacks(c, tp["user_id"], True)][:8])
        elif op_kind == "economic":
            data = dict(gdp=tp["gdp"], money=get_res(c, tp["user_id"], "money"), stability=tp["stability"], tax_rate=tp["tax_rate"])
        elif op_kind == "alliance":
            data = dict(alliance_id=tp["alliance_id"])
        else:
            data = dict(city=None)
    elif ttype == "city":
        city = city_row(c, int(target_id))
        if not city:
            raise GameError("شهر پیدا نشد.")
        data = dict(name=city["name"], garrison=city["garrison"], fortification=city["fortification"], terrain=city["terrain"], population=city["population"],
                    facilities=[f["ftype"] for f in city_facilities(c, city["id"])])
    elif ttype == "alliance":
        al = q1(c, "SELECT * FROM alliances WHERE id=?", int(target_id))
        if not al:
            raise GameError("اتحاد پیدا نشد.")
        data = dict(name=al["name"], level=al["level"], power=al["power"], members=qv(c, "SELECT COUNT(*) FROM alliance_members WHERE alliance_id=?", al["id"], default=0))
    accuracy = clamp(0.5 + (my_intel / 3000.0) - target_counter + fx.get("recon_acc", 0) + temp_effect(c, uid, "recon_boost") + (src_acc - 1.0), 0.3, 0.97)
    confidence = round(accuracy * random.uniform(0.9, 1.0), 2)
    ex(c, "INSERT INTO intel_reports(user_id,ttype,tid,kind,data,confidence,accuracy,source,created) VALUES(?,?,?,?,?,?,?,?,?)",
       uid, ttype, str(target_id), op_kind, jd(data), confidence, round(accuracy, 2), source, now())
    bump(c, uid, "intel_ops", 1)
    audit(c, uid, "intel_op", f"{ttype}:{target_id}", op_kind)
    finish(c, uid)
    return dict(data=data, confidence=confidence, accuracy=accuracy)


def intel_reports_list(c, uid, ttype=None, limit=15):
    q = "SELECT * FROM intel_reports WHERE user_id=?"
    args = [uid]
    if ttype:
        q += " AND ttype=?"
        args.append(ttype)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return qa(c, q, *args)


def intel_report_text(r):
    age = dur(now() - r["created"])
    d = jl(r["data"])
    lines = [f"🕵️ گزارش {INTEL_OPS.get(r['kind'], (('', r['kind']),))[1] if r['kind'] in INTEL_OPS else r['kind']} — دقت {pct(r['accuracy'])} — سن {age}"]
    for k, v in d.items():
        if isinstance(v, list):
            continue
        lines.append(f"  • {k}: {v}")
    if isinstance(d.get("units"), list):
        lines.append("  🪖 یگان‌های شناسایی‌شده:")
        for u in d["units"]:
            lines.append(f"    - {u['name']} × {u['qty']} (مستقر {u['deployed']})")
    return "\n".join(lines)


# ============================================================================
# DYNAMIC WORLD EVENTS
# ============================================================================
def spawn_world_event(c, force_kind=None):
    if qv(c, "SELECT COUNT(*) FROM world_events WHERE active=1", default=0) >= 4:
        return None
    kinds = list(EVENT_TYPES)
    weights = [EVENT_TYPES[k]["w"] for k in kinds]
    kind = force_kind or random.choices(kinds, weights=weights)[0]
    et = EVENT_TYPES[kind]
    dur_ = random.randint(*et["dur"])
    target = ""
    if et["scope"] == "region":
        target = random.choice(list(REGIONS))
    elif et["scope"] == "country":
        target = random.choice([x[0] for x in COUNTRIES])
    params = {"fx": et["fx"], "emergency": kind in ("regional_tension", "political_crisis", "resource_shortage")}
    eid = ex(c, "INSERT INTO world_events(kind,title,scope,target,started,ends_at,params,active,source) VALUES(?,?,?,?,?,?,?,1,'auto')",
             kind, et["title"], et["scope"], target, now(), now() + dur_, jd(params)).lastrowid
    if et["scope"] == "region":
        scope_txt = f"🗺️ منطقهٔ {REGIONS[target][0]}"
    elif et["scope"] == "country":
        scope_txt = f"🏳️ {next(x[2] for x in COUNTRIES if x[0] == target)}"
    else:
        scope_txt = "🌍 جهانی"
    notify_broadcast(c, "event", f"{et['title']} ({scope_txt})\n{et['desc']}\n⏳ مدت: {dur(dur_)}")
    return eid


def expire_world_events(c):
    ex(c, "UPDATE world_events SET active=0 WHERE active=1 AND ends_at<=?", now())


def world_events_tick(c):
    expire_world_events(c)
    last = int(meta_get(c, "last_event_spawn", 0))
    if now() - last > random.randint(1800, 3600):
        spawn_world_event(c)
        meta_set(c, "last_event_spawn", now())


def notify_broadcast(c, kind, text):
    for r in qa(c, "SELECT user_id FROM players WHERE banned=0"):
        notify(c, r["user_id"], kind, text)


# ============================================================================
# LEADERBOARDS
# ============================================================================
LB_DEFS = {
    "military": ("🪖 نظامی", "mil_power"), "economy": ("💰 اقتصاد", "gdp"), "territory": ("🗺️ قلمرو", None),
    "industry": ("🏭 صنعت", "ind_power"), "intelligence": ("🕵️ اطلاعات", "int_power"), "victories": ("🏆 پیروزی‌ها", "victories"),
    "trade": ("🛒 تجارت", None), "achievements": ("🏅 دستاوردها", None), "growth": ("⭐ سطح", "level"), "activity": ("📱 فعالیت", "activity"),
}


def leaderboard(c, kind, limit=10):
    if kind == "territory":
        return qa(c, "SELECT p.name, COUNT(ci.id) AS val FROM players p JOIN cities ci ON ci.governor_id=p.user_id GROUP BY p.user_id ORDER BY val DESC LIMIT ?", limit)
    if kind == "trade":
        return qa(c, "SELECT p.name, COALESCE(SUM(t.qty*t.price),0) AS val FROM players p LEFT JOIN market_trades t ON t.user_id=p.user_id GROUP BY p.user_id ORDER BY val DESC LIMIT ?", limit)
    if kind == "achievements":
        return qa(c, "SELECT p.name, COUNT(a.aid) AS val FROM players p LEFT JOIN achievements a ON a.user_id=p.user_id GROUP BY p.user_id ORDER BY val DESC LIMIT ?", limit)
    col = LB_DEFS[kind][1]
    return qa(c, f"SELECT name, {col} AS val FROM players ORDER BY {col} DESC LIMIT ?", limit)


def alliance_leaderboard(c, limit=10):
    return qa(c, "SELECT name, tag, power, level FROM alliances ORDER BY power DESC LIMIT ?", limit)


# ============================================================================
# DAILY REWARD
# ============================================================================
def daily_claim(c, uid):
    p = need_player(c, uid)
    today = game_day()
    if q1(c, "SELECT 1 FROM daily_claims WHERE user_id=? AND day=?", uid, today):
        raise GameError("🎁 پاداش امروز را قبلاً دریافت کرده‌اید.")
    yday = game_day(now() - 86400)
    streak = p["streak"] + 1 if p["last_daily"] == yday else 1
    day_no = ((streak - 1) % DAILY_CYCLE) + 1
    reward = daily_reward_for(day_no)
    ex(c, "INSERT INTO daily_claims(user_id,day,day_no,ts) VALUES(?,?,?,?)", uid, today, day_no, now())
    ex(c, "UPDATE players SET streak=?, last_daily=? WHERE user_id=?", streak, today, uid)
    grant(c, uid, reward)
    bump(c, uid, "daily_claims", 1)
    notify(c, uid, "reward", f"🎁 پاداش روز {day_no}/{DAILY_CYCLE} (رشتهٔ {streak} روزه): {reward_text(reward)}")
    finish(c, uid)
    return day_no, streak, reward


# ============================================================================
# BALE BOT API — کلاینت خام مبتنی بر urllib (بدون هیچ وابستگی خارجی)
# ============================================================================
class BaleAPI:
    def __init__(self, token):
        self.base = f"{API_BASE}{token}/"
        self.session_offset = 0

    def call(self, method, payload=None, timeout=25):
        url = self.base + method
        data = jd(payload or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_updates(self, timeout=20):
        r = self.call("getUpdates", {"offset": self.session_offset, "timeout": timeout, "limit": 100}, timeout=timeout + 10)
        if not r or not r.get("ok"):
            return []
        updates = r.get("result", [])
        if updates:
            self.session_offset = max(u.get("update_id", 0) for u in updates) + 1
        return updates

    def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        payload = {"chat_id": chat_id, "text": text[:4096]}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self.call("sendMessage", payload)

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:4096]}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        r = self.call("editMessageText", payload)
        if not r.get("ok"):
            return self.send_message(chat_id, text, reply_markup)
        return r

    def answer_callback(self, callback_id, text=None, alert=False):
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
            payload["show_alert"] = bool(alert)
        return self.call("answerCallbackQuery", payload)

    def delete_message(self, chat_id, message_id):
        return self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


api = None  # در main() مقداردهی می‌شود

_TL = threading.local()


def set_cur_cbid(cbid):
    _TL.cbid = cbid


def cur_cbid():
    return getattr(_TL, "cbid", None)


def answer_err(text):
    """پاسخ خطا به صورت هشدار روی همان callback جاری (در غیر این صورت پیام معمولی)."""
    cid = cur_cbid()
    if cid:
        try:
            api.answer_callback(cid, text[:190], True)
        except Exception:
            pass
    return cid


def safe_send(chat_id, text, kb=None):
    try:
        return api.send_message(chat_id, rtl(text), kb)
    except Exception as e:
        log_error("send_message", e)
        return None


def safe_edit(chat_id, mid, text, kb=None):
    try:
        return api.edit_message(chat_id, mid, rtl(text), kb)
    except Exception as e:
        log_error("edit_message", e)
        return safe_send(chat_id, text, kb)


# ============================================================================
# کیبوردها
# ============================================================================
def ikb(rows):
    return {"inline_keyboard": rows}


def btn(text, cb):
    return {"text": text, "callback_data": cb[:64]}


def rkb(rows, resize=True):
    return {"keyboard": rows, "resize_keyboard": resize}


MAIN_MENU_ROWS = [
    ["🌍 جهان", "👤 کشور من"],
    ["🏭 اقتصاد", "🪖 ارتش"],
    ["⚔️ جنگ", "🛒 بازار"],
    ["🎒 انبار", "🔬 فناوری"],
    ["🤝 دیپلماسی", "🕵️ اطلاعات"],
    ["🏆 رتبه‌بندی", "🎯 مأموریت‌ها"],
    ["🏅 دستاوردها", "🤝 اتحاد"],
    ["🌍 رویدادهای جهان", "⚙️ تنظیمات"],
]


def main_menu_kb():
    return rkb([[{"text": t} for t in row] for row in MAIN_MENU_ROWS])


def back_row(cb="nav.home"):
    return [btn("🔙 بازگشت", cb)]


def pager_row(prefix, page, has_more):
    row = []
    if page > 0:
        row.append(btn("◀️ قبلی", f"{prefix}.{page - 1}"))
    if has_more:
        row.append(btn("بعدی ▶️", f"{prefix}.{page + 1}"))
    return [row] if row else []


# ============================================================================
# روتر: هر تابع UI امضای (c, uid, chat_id, msg_id?) دارد و پیام می‌فرستد/ویرایش می‌کند
# ============================================================================
CB_ROUTES = {}
TXT_ROUTES = {}


def cb_route(prefix):
    def deco(fn):
        CB_ROUTES[prefix] = fn
        return fn
    return deco


def txt_route(label):
    def deco(fn):
        TXT_ROUTES[label] = fn
        return fn
    return deco


def send_view(chat_id, uid, text, kb=None):
    safe_send(chat_id, text, kb)


def edit_view(chat_id, mid, uid, text, kb=None):
    safe_edit(chat_id, mid, text, kb)


# ============================================================================
# شروع / ثبت‌نام — بدون انتخاب از لیست: بازیکن خودش اسم کشور را تایپ می‌کند
# و بازی به‌صورت خودکار یک شهر آزاد برایش تعیین می‌کند.
# ============================================================================
def view_start(c, uid, chat_id, username=""):
    p = get_player(c, uid)
    if p:
        return view_home(c, uid, chat_id)
    session_set(c, uid, "reg_empire", {})
    txt = (f"🌍🔥 به {APP_NAME} خوش آمدید، فرمانده!\n\n"
           "یک استراتژی جهانی واقعی: اقتصاد، ارتش، فناوری، دیپلماسی و جنگ در دستان شماست.\n\n"
           "🏰 ابتدا نام کشور/امپراتوری خود را تایپ کنید (۲ تا ۳۰ نویسه — هر اسمی که دوست دارید):")
    send_view(chat_id, uid, txt)


def handle_registration_text(c, uid, chat_id, username, text):
    state, data = session_get(c, uid)
    if state == "reg_empire":
        try:
            empire_name = clean_name(text, 2, 30)
        except GameError as e:
            safe_send(chat_id, str(e))
            return True
        if qv(c, "SELECT COUNT(*) FROM players WHERE lower(empire_name)=lower(?)", empire_name, default=0):
            safe_send(chat_id, "❌ این نام قبلاً استفاده شده؛ نام دیگری تایپ کنید.")
            return True
        session_set(c, uid, "reg_name", {"empire_name": empire_name})
        safe_send(chat_id, f"🏰 کشور «{empire_name}» ثبت شد!\n\n✍️ حالا نام فرماندهی خودتان را تایپ کنید (۲ تا ۲۴ نویسه):")
        return True
    if state == "reg_name":
        try:
            city = pick_auto_city(c)
            p = create_player(c, uid, username, text, city["owner_country"], city["id"], data.get("empire_name", ""))
        except GameError as e:
            safe_send(chat_id, str(e))
            return True
        session_clear(c, uid)
        disp = empire_label(c, p)
        cap = city_row(c, p["city_id"])
        safe_send(chat_id, f"✅ فرماندهی {p['name']} ثبت شد!\n{disp}\n🏙️ پایتخت: {cap['name']}", main_menu_kb())
        view_home(c, uid, chat_id)
        return True
    return False


# ============================================================================
# خانه / داشبورد کشور من
# ============================================================================
def res_line(res_d):
    return "\n".join(f"{RES[k][0]} {RES[k][1]}: {fmt(res_d.get(k, 0))}" for k in RES_ORDER)


def view_home(c, uid, chat_id, mid=None):
    p = player_home(c, uid)
    if not p:
        return view_start(c, uid, chat_id)
    res = all_res(c, uid)
    ncities = qv(c, "SELECT COUNT(*) FROM cities WHERE governor_id=?", uid, default=0)
    shield = "🛡️ فعال" if p["shield_until"] > now() else "❌"
    txt = (f"👤 {p['name']} — {rank_title(p['level'])} (سطح {p['level']})\n"
           f"{empire_label(c, p)}   🏙️ شهرها: {ncities}\n\n"
           f"⭐ XP: {fmt(p['xp'])} ({bar((p['xp'] - xp_for_level(p['level'])) / max(1, xp_for_level(p['level'] + 1) - xp_for_level(p['level'])))})\n"
           f"🏛️ ثبات: {p['stability']:.0f}٪   😊 روحیه: {p['morale']:.0f}٪   📈 GDP/h: {fmt(p['gdp'])}\n\n"
           f"{res_line(res)}\n\n"
           f"💪 نظامی: {short(p['mil_power'])}  🏭 صنعت: {short(p['ind_power'])}  🕵️ اطلاعات: {short(p['int_power'])}\n"
           f"🏆 پیروزی/شکست: {p['victories']}/{p['defeats']}   🛡️ سپر: {shield}")
    rows = [
        [btn("🏙️ شهرهای من", "home.cities"), btn("🎁 پاداش روزانه", "home.daily")],
        [btn("🏗️ ساختمان‌ها", "eco.build"), btn("📊 صف‌ها", "home.queues")],
        [btn("⚙️ نرخ مالیات", "home.tax")],
    ]
    if mid:
        edit_view(chat_id, mid, uid, txt, ikb(rows))
    else:
        send_view(chat_id, uid, txt, ikb(rows))


@cb_route("nav")
def cb_nav(c, uid, chat_id, mid, args):
    target = args[0]
    if target == "home":
        view_home(c, uid, chat_id, mid)
    elif target == "start":
        view_start(c, uid, chat_id)


@cb_route("home")
def cb_home(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "cities":
        view_my_cities(c, uid, chat_id, mid)
    elif sub == "daily":
        try:
            day_no, streak, reward = daily_claim(c, uid)
            edit_view(chat_id, mid, uid, f"🎁 پاداش روز {day_no}/{DAILY_CYCLE} (رشتهٔ {streak} روزه) دریافت شد!\n{reward_text(reward)}", ikb([back_row()]))
        except GameError as e:
            answer_err(str(e))
    elif sub == "queues":
        view_queues(c, uid, chat_id, mid)
    elif sub == "tax":
        view_tax(c, uid, chat_id, mid)
    elif sub.startswith("taxset"):
        rate = int(args[1])
        ex(c, "UPDATE players SET tax_rate=? WHERE user_id=?", rate, uid)
        view_tax(c, uid, chat_id, mid)


def view_tax(c, uid, chat_id, mid):
    p = get_player(c, uid)
    rows = [[btn(f"{'✅ ' if p['tax_rate'] == r else ''}{r}٪", f"home.taxset.{r}") for r in (5, 10, 15, 20, 25, 30)][i:i + 3] for i in range(0, 6, 3)]
    rows.append(back_row())
    edit_view(chat_id, mid, uid, f"⚙️ نرخ مالیات فعلی: {p['tax_rate']}٪\nمالیات بالاتر = درآمد بیشتر اما ثبات کمتر.", ikb(rows))


def view_my_cities(c, uid, chat_id, mid):
    cities = governed_cities(c, uid)
    if not cities:
        return edit_view(chat_id, mid, uid, "شهری تحت حکمرانی ندارید.", ikb([back_row()]))
    lines = ["🏙️ شهرهای تحت حکمرانی شما:\n"]
    for ci in cities:
        facs = city_facilities(c, ci["id"])
        fnames = "، ".join(f"{FACILITIES[f['ftype']][0]}L{f['level']}" for f in facs)
        lines.append(f"• {ci['name']} — جمعیت {short(ci['population'])} — پادگان {fmt(ci['garrison'])} — {fnames}")
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb([back_row()]))


def view_queues(c, uid, chat_id, mid):
    b = qa(c, "SELECT * FROM build_queue WHERE user_id=?", uid)
    r = q1(c, "SELECT * FROM research_queue WHERE user_id=?", uid)
    pr = qa(c, "SELECT * FROM prod_queue WHERE user_id=?", uid)
    lines = ["📊 صف‌های جاری:\n"]
    if b:
        lines.append("🏗️ ساخت‌وساز:")
        for x in b:
            lines.append(f"  {BUILDINGS[x['btype']]['emoji']} {BUILDINGS[x['btype']]['name']} → سطح {x['to_level']} — {dur(max(0, x['ends_at'] - now()))} مانده")
    if r:
        lines.append("🔬 تحقیق:")
        lines.append(f"  {TECHS[r['tech_id']]['name']} — {dur(max(0, r['ends_at'] - now()))} مانده")
    if pr:
        lines.append("🪖 تولید:")
        for x in pr:
            lines.append(f"  {item_name(x['item_key'])} × {x['qty']} — {dur(max(0, x['ends_at'] - now()))} مانده")
    if not (b or r or pr):
        lines.append("چیزی در صف نیست.")
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb([back_row()]))


# ============================================================================
# جهان — لیست کشورها / شهرها / حمله به شهر
# ============================================================================
@txt_route("🌍 جهان")
def txt_world(c, uid, chat_id, username, text):
    view_world(c, uid, chat_id)


def view_world(c, uid, chat_id, mid=None, page=0):
    countries = qa(c, "SELECT * FROM countries ORDER BY (military+defense+tech) DESC")
    p = get_player(c, uid)
    page_size = 8
    chunk = countries[page * page_size:(page + 1) * page_size]
    rows = [[btn(f"{cr['flag']} {cr['name']}" + (" (شما)" if cr["code"] == p["country"] else ""), f"world.country.{cr['code']}")] for cr in chunk]
    rows += pager_row("world.page", page, len(countries) > (page + 1) * page_size)
    rows.append(back_row())
    edit_view(chat_id, mid, uid, "🌍 کشورهای جهان — برای مشاهدهٔ شهرها لمس کنید:", ikb(rows)) if mid else send_view(chat_id, uid, "🌍 کشورهای جهان:", ikb(rows))


@cb_route("world")
def cb_world(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "page":
        view_world(c, uid, chat_id, mid, int(args[1]))
    elif sub == "country":
        view_country(c, uid, chat_id, mid, args[1])
    elif sub == "city":
        view_city(c, uid, chat_id, mid, int(args[1]))
    elif sub == "attack":
        try:
            res, rep, captured = attack_city(c, uid, int(args[1]))
            edit_view(chat_id, mid, uid, rep + ("\n\n🏙️ شهر فتح شد!" if captured else ""), ikb([back_row(f"world.city.{args[1]}")]))
        except GameError as e:
            answer_err(str(e))


def view_country(c, uid, chat_id, mid, code):
    cr = country_row(c, code)
    cities = qa(c, "SELECT * FROM cities WHERE country=? ORDER BY population DESC", code)
    lines = [f"{cr['flag']} {cr['name']} ({REGIONS[cr['region']][0]})",
             f"👥 جمعیت: {short(cr['population'])}  💰 GDP: {short(cr['gdp'])}B",
             f"🏭 صنعت {cr['industry']:.0f}  ⚡ انرژی {cr['energy']:.0f}  🧪 منابع {cr['resources']:.0f}",
             f"🪖 نظامی {cr['military']:.0f}  🛡️ دفاع {cr['defense']:.0f}  🕵️ اطلاعات {cr['intel']:.0f}  🔬 فناوری {cr['tech']:.0f}",
             f"🏳️ ویژگی‌ها: {'، '.join(TRAIT_NAMES.get(t, t) for t in (cr['traits'] or '').split(','))}", ""]
    rows = []
    for ci in cities[:10]:
        tag = "👑" if ci["owner_country"] != code else ("👤" if ci["governor_id"] else "")
        rows.append([btn(f"{tag}{ci['name']} ({short(ci['population'])})", f"world.city.{ci['id']}")])
    rows.append(back_row("world.page.0"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def view_city(c, uid, chat_id, mid, cid):
    city = city_row(c, cid)
    if not city:
        return edit_view(chat_id, mid, uid, "شهر پیدا نشد.", ikb([back_row()]))
    facs = city_facilities(c, cid)
    gov = get_player(c, city["governor_id"]) if city["governor_id"] else None
    lines = [f"🏙️ {city['name']} — استان {city['pname']}",
             f"🗺️ زمین: {TERRAINS[city['terrain']][0]} {TERRAINS[city['terrain']][1]}   {'🌊 ساحلی' if city['coastal'] else ''}",
             f"👥 جمعیت: {short(city['population'])}   📈 توسعه: {city['development']:.2f}",
             f"🛡️ پادگان: {fmt(city['garrison'])}/{fmt(city['garrison_max'])}   🏰 استحکامات: {city['fortification']}",
             f"👤 حاکم: {(gov['name'] + ' — ' + empire_label(c, gov)) if gov else '— (کنترل دولت)'}",
             "🏗️ تأسیسات: " + "، ".join(f"{FACILITIES[f['ftype']][0]}{FACILITIES[f['ftype']][1]}L{f['level']}" for f in facs)]
    rows = []
    p = get_player(c, uid)
    if city["owner_country"] != p["country"] or city["governor_id"]:
        if city["governor_id"] != uid:
            rows.append([btn("⚔️ حمله به این شهر", f"world.attack.{cid}")])
    rows.append(back_row(f"world.country.{city['country']}"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


# ============================================================================
# اقتصاد — ساختمان‌ها
# ============================================================================
@txt_route("🏭 اقتصاد")
def txt_economy(c, uid, chat_id, username, text):
    view_economy_home(c, uid, chat_id)


def view_economy_home(c, uid, chat_id, mid=None):
    p = player_home(c, uid)
    info = settle_player(c, uid, dry=True)
    lines = [f"🏭 اقتصاد {p['name']}", "",
             f"📈 GDP/ساعت: {fmt(info['gdp_rate'])}   💰 مالیات/ساعت: {fmt(info['tax_h'])}",
             f"⚡ نسبت انرژی: {pct(info['energy_ratio'])}   🪖 هزینهٔ ارتش/ساعت: {fmt(info['army_money_h'])}",
             f"🥀 قحطی: {pct(info['starve'])}   💸 بدهی نگهداری: {pct(info['unpaid'])}"]
    rows = [[btn("🏗️ ساختمان‌ها", "eco.build"), btn("🏦 سرمایه‌گذاری", "eco.invest")],
            [btn("🛒 بازار سریع", "market.home")], back_row()]
    (edit_view if mid else send_view)(chat_id, mid, uid, "\n".join(lines), ikb(rows)) if mid else send_view(chat_id, uid, "\n".join(lines), ikb(rows))


@cb_route("eco")
def cb_eco(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "build":
        view_buildings(c, uid, chat_id, mid)
    elif sub == "bdetail":
        view_building_detail(c, uid, chat_id, mid, args[1])
    elif sub == "bupgrade":
        try:
            r = start_upgrade(c, uid, args[1])
            edit_view(chat_id, mid, uid, f"🏗️ ارتقا به سطح {r['to_level']} آغاز شد.\n💰 هزینه: {cost_text(r['cost'])}\n⏳ زمان: {dur(r['secs'])}", ikb([back_row("eco.build")]))
        except GameError as e:
            answer_err(str(e))
    elif sub == "invest":
        view_invest(c, uid, chat_id, mid)
    elif sub == "investdo":
        try:
            kind = args[1]
            amt = {"s": 5000, "m": 20000, "l": 60000}[args[2]]
            r = invest_start(c, uid, kind, amt)
            edit_view(chat_id, mid, uid, f"🏦 سرمایه‌گذاری {fmt(amt)} پول ثبت شد.\n⏳ {dur(r['secs'])}  💰 بازده: {fmt(r['payout'])}", ikb([back_row("eco.invest")]))
        except GameError as e:
            answer_err(str(e))
    elif sub == "home":
        view_economy_home(c, uid, chat_id, mid)


def view_buildings(c, uid, chat_id, mid):
    bl = blevels(c, uid)
    rows = []
    for bt in BUILD_ORDER:
        lv = bl.get(bt, 0)
        b = BUILDINGS[bt]
        rows.append([btn(f"{b['emoji']} {b['name']} L{lv}/{b['max']}", f"eco.bdetail.{bt}")])
    rows.append(back_row("eco.home"))
    edit_view(chat_id, mid, uid, f"🏗️ ساختمان‌ها (ظرفیت صف: {build_slots(c, uid)})", ikb(rows))


def view_building_detail(c, uid, chat_id, mid, bt):
    b = BUILDINGS[bt]
    bl = blevels(c, uid)
    lv = bl.get(bt, 0)
    p = get_player(c, uid)
    fx = effects(c, uid, p)
    lines = [f"{b['emoji']} {b['name']} — سطح فعلی {lv}/{b['max']}", b["desc"], ""]
    if lv < b["max"]:
        to = lv + 1
        cost = building_cost(c, uid, bt, to, fx, p)
        secs = building_time(c, uid, bt, to, fx)
        miss = upgrade_requirements(c, uid, bt, to)
        lines.append(f"⬆️ سطح بعدی ({to}): {cost_text(cost)} — ⏳ {dur(secs)}")
        if miss:
            lines.append("🔒 " + "، ".join(miss))
    else:
        lines.append("✅ به حداکثر سطح رسیده است.")
    io = b.get("io", {})
    if io:
        lines.append("📊 تولید/مصرف پایه هر سطح: " + "، ".join(f"{rn(k)} {v:+g}" for k, v in io.items()))
    rows = []
    if lv < b["max"] and not q1(c, "SELECT 1 FROM build_queue WHERE user_id=? AND btype=?", uid, bt) and not upgrade_requirements(c, uid, bt, lv + 1):
        rows.append([btn("⬆️ ارتقا", f"eco.bupgrade.{bt}")])
    rows.append(back_row("eco.build"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def view_invest(c, uid, chat_id, mid):
    active = qa(c, "SELECT * FROM investments WHERE user_id=? AND status='active'", uid)
    lines = ["🏦 سرمایه‌گذاری (بانک):", ""]
    for k, (em, nm, secs, rate) in INVEST_KINDS.items():
        lines.append(f"{em} {nm}: {dur(secs)} — سود {pct(rate)}")
    if active:
        lines.append("\n📋 سپرده‌های فعال:")
        for a in active:
            lines.append(f"  {INVEST_KINDS[a['kind']][0]} {fmt(a['principal'])} — سررسید {dur(max(0, a['ends_at'] - now()))}")
    rows = [[btn("⏱️ کوتاه (۵K)", "eco.investdo.short.s"), btn("📆 میان (۲۰K)", "eco.investdo.mid.m"), btn("🏦 بلند (۶۰K)", "eco.investdo.long.l")], back_row("eco.home")]
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


# ============================================================================
# ارتش — تولید یگان، مدیریت استقرار
# ============================================================================
@txt_route("🪖 ارتش")
def txt_military(c, uid, chat_id, username, text):
    view_military_home(c, uid, chat_id)


def view_military_home(c, uid, chat_id, mid=None):
    p = player_home(c, uid)
    mil = calc_military_power(c, uid)
    n_deployed = qv(c, "SELECT COALESCE(SUM(deployed),0) FROM inventory i WHERE i.user_id=?", uid, default=0)
    lines = [f"🪖 ارتش {p['name']}", f"💪 قدرت نظامی: {short(mil)}   🎖️ سطح فرمانده: {commander_level(p)}", f"👥 یگان‌های مستقر: {fmt(n_deployed)}"]
    rows = [[btn("⚙️ تولید یگان", "mil.categories"), btn("🎒 انبار یگان‌ها", "inv.home")],
            back_row()]
    (send_view if not mid else edit_view)(chat_id, mid if mid else uid, uid, "\n".join(lines), ikb(rows)) if mid else send_view(chat_id, uid, "\n".join(lines), ikb(rows))


@cb_route("mil")
def cb_mil(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "home":
        view_military_home(c, uid, chat_id, mid)
    elif sub == "categories":
        view_unit_categories(c, uid, chat_id, mid)
    elif sub == "catlist":
        view_units_in_cat(c, uid, chat_id, mid, args[1])
    elif sub == "unit":
        view_unit_detail(c, uid, chat_id, mid, args[1])
    elif sub == "buildunit":
        key, qty = args[1], int(args[2])
        try:
            r = start_production(c, uid, key, qty)
            edit_view(chat_id, mid, uid, f"⚙️ تولید {item_name(key)} × {qty} آغاز شد.\n💰 {cost_text(r['cost'])}\n⏳ {dur(r['secs'])}", ikb([back_row(f"mil.unit.{key}")]))
        except GameError as e:
            answer_err(str(e))


def view_unit_categories(c, uid, chat_id, mid):
    rows = []
    line = []
    for k, (em, nm, bt, kind) in CATEGORIES.items():
        line.append(btn(f"{em} {nm}", f"mil.catlist.{k}"))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append(back_row("mil.home"))
    edit_view(chat_id, mid, uid, "⚙️ دستهٔ یگان را انتخاب کنید:", ikb(rows))


def view_units_in_cat(c, uid, chat_id, mid, cat):
    bl = blevels(c, uid)
    bt = CATEGORIES[cat][2]
    rows = []
    for key, u in UNITS.items():
        if u["cat"] != cat:
            continue
        lock = "🔒" if (not has_tech(c, uid, u["tech"]) or bl.get(bt, 0) < u["bl"] or not u["craft"]) else ""
        rows.append([btn(f"{RARITY[u['rarity']][0]} {u['name']} {lock}", f"mil.unit.{key}")])
    rows.append(back_row("mil.categories"))
    edit_view(chat_id, mid, uid, f"{CATEGORIES[cat][0]} {CATEGORIES[cat][1]}:", ikb(rows))


def view_unit_detail(c, uid, chat_id, mid, key):
    u = UNITS[key]
    bt = CATEGORIES[u["cat"]][2]
    bl = blevels(c, uid).get(bt, 0)
    have_tech = has_tech(c, uid, u["tech"])
    owned = inv_count(c, uid, key)
    m = unit_cost_mult(c, uid)
    lines = [f"{RARITY[u['rarity']][0]} {u['name']} ({RARITY[u['rarity']][1]})",
              f"⚔️ حمله {u['atk']}  🛡️ دفاع {u['df']}  🏃 تحرک {u['mob']}  🎯 دقت {u['acc']}٪  📏 برد {u['rng']}",
              f"❤️ دوام پایه: {u['dur']}", f"💰 هزینه: {cost_text({k: v * m for k, v in u['cost'].items()})}  ⏳ زمان: {dur(u['time'])}",
              f"🏗️ نیازمند: {BUILDINGS[bt]['name']} سطح {u['bl']} (فعلی {bl})", f"🔬 نیازمند فناوری: {TECHS[u['tech']]['name'] if u['tech'] else '—'} {'✅' if have_tech else '❌'}",
              f"📦 موجودی شما: {fmt(owned)}"]
    rows = []
    if u["craft"] and have_tech and bl >= u["bl"]:
        rows.append([btn("×1", f"mil.buildunit.{key}.1"), btn("×5", f"mil.buildunit.{key}.5"), btn("×10", f"mil.buildunit.{key}.10")])
    rows.append(back_row(f"mil.catlist.{u['cat']}"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


# ============================================================================
# انبار (Inventory) — مدیریت استقرار، ارتقا، تعمیر، آیتم‌ها
# ============================================================================
@txt_route("🎒 انبار")
def txt_inventory(c, uid, chat_id, username, text):
    view_inventory(c, uid, chat_id)


def view_inventory(c, uid, chat_id, mid=None, page=0):
    units = inv_stacks(c, uid, True)
    items = [r for r in inv_stacks(c, uid) if r["item_key"] in ITEMS]
    page_size = 8
    chunk = units[page * page_size:(page + 1) * page_size]
    rows = [[btn(f"{item_name(r['item_key'])} L{r['level']} ×{fmt(r['qty'])} (مستقر {fmt(r['deployed'])})", f"inv.stack.{r['id']}")] for r in chunk]
    rows += pager_row("inv.page", page, len(units) > (page + 1) * page_size)
    if items:
        rows.append([btn("🧰 آیتم‌های مصرفی", "inv.items")])
    rows.append([btn("🛒 خرید آیتم", "inv.shop")])
    rows.append(back_row())
    txt = "🎒 یگان‌های شما:" if units else "🎒 هنوز یگانی ندارید — از منوی ارتش تولید کنید."
    (edit_view(chat_id, mid, uid, txt, ikb(rows)) if mid else send_view(chat_id, uid, txt, ikb(rows)))


@cb_route("inv")
def cb_inv(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "home":
        view_inventory(c, uid, chat_id, mid)
    elif sub == "page":
        view_inventory(c, uid, chat_id, mid, int(args[1]))
    elif sub == "stack":
        view_stack_detail(c, uid, chat_id, mid, int(args[1]))
    elif sub == "deploy":
        sid, n = int(args[1]), args[2]
        r = q1(c, "SELECT * FROM inventory WHERE id=?", sid)
        target = r["qty"] if n == "all" else (0 if n == "none" else int(n))
        try:
            set_deployed(c, uid, sid, target)
            view_stack_detail(c, uid, chat_id, mid, sid)
        except GameError as e:
            answer_err(str(e))
    elif sub == "upgrade":
        try:
            lv, cost = upgrade_unit(c, uid, int(args[1]))
            edit_view(chat_id, mid, uid, f"⬆️ ارتقا به سطح {lv} انجام شد.\n💰 {cost_text(cost)}", ikb([back_row("inv.home")]))
        except GameError as e:
            answer_err(str(e))
    elif sub == "repair":
        try:
            cost = repair_stack(c, uid, int(args[1]))
            edit_view(chat_id, mid, uid, f"🧰 تعمیر شد.\n💰 {cost_text(cost)}", ikb([back_row("inv.home")]))
        except GameError as e:
            answer_err(str(e))
    elif sub == "items":
        view_items(c, uid, chat_id, mid)
    elif sub == "use":
        try:
            msg = use_item(c, uid, args[1])
            edit_view(chat_id, mid, uid, msg, ikb([back_row("inv.items")]))
        except GameError as e:
            answer_err(str(e))
    elif sub == "shop":
        view_item_shop(c, uid, chat_id, mid)
    elif sub == "buy":
        try:
            cost = buy_item(c, uid, args[1], int(args[2]))
            edit_view(chat_id, mid, uid, f"🛒 خریداری شد.\n💰 {cost_text(cost)}", ikb([back_row("inv.shop")]))
        except GameError as e:
            answer_err(str(e))


def view_stack_detail(c, uid, chat_id, mid, sid):
    r = q1(c, "SELECT * FROM inventory WHERE id=? AND user_id=?", sid, uid)
    if not r:
        return edit_view(chat_id, mid, uid, "این دسته یافت نشد.", ikb([back_row("inv.home")]))
    u = UNITS[r["item_key"]]
    lines = [f"{RARITY[u['rarity']][0]} {u['name']} — سطح {r['level']}", f"📦 تعداد: {fmt(r['qty'])}   🪖 مستقر: {fmt(r['deployed'])}   ❤️ دوام: {r['durability']:.0f}٪",
              f"⚔️ قدرت مؤثر واحد: {unit_power(u, r['level'], r['durability']):.1f}"]
    rows = [[btn("مستقر همه", f"inv.deploy.{sid}.all"), btn("خارج از خدمت", f"inv.deploy.{sid}.none")]]
    if r["level"] < UPGRADE_MAX:
        rows.append([btn("⬆️ ارتقای سطح", f"inv.upgrade.{sid}")])
    if r["durability"] < 99.5:
        rows.append([btn("🧰 تعمیر", f"inv.repair.{sid}")])
    rows.append(back_row("inv.home"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def view_items(c, uid, chat_id, mid):
    items = [r for r in inv_stacks(c, uid) if r["item_key"] in ITEMS]
    if not items:
        return edit_view(chat_id, mid, uid, "آیتمی ندارید.", ikb([back_row("inv.home")]))
    rows = [[btn(f"{item_name(r['item_key'])} ×{fmt(r['qty'])}", f"inv.use.{r['item_key']}")] for r in items]
    rows.append(back_row("inv.home"))
    edit_view(chat_id, mid, uid, "🧰 آیتم‌های شما (لمس = استفاده):", ikb(rows))


def view_item_shop(c, uid, chat_id, mid):
    rows = []
    lines = ["🛒 فروشگاه آیتم:"]
    for k, it in ITEMS.items():
        if not it["price"]:
            continue
        lines.append(f"{it['emoji']} {it['name']}: {cost_text(it['price'])} — {it['desc']}")
        rows.append([btn(f"خرید {it['emoji']} {it['name']}", f"inv.buy.{k}.1")])
    rows.append(back_row("inv.home"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


# ============================================================================
# فناوری
# ============================================================================
@txt_route("🔬 فناوری")
def txt_tech(c, uid, chat_id, username, text):
    view_tech_home(c, uid, chat_id)


def view_tech_home(c, uid, chat_id, mid=None):
    active = q1(c, "SELECT * FROM research_queue WHERE user_id=?", uid)
    lines = ["🔬 درخت فناوری — شاخه را انتخاب کنید:"]
    if active:
        lines.append(f"⏳ در حال تحقیق: {TECHS[active['tech_id']]['name']} — {dur(max(0, active['ends_at'] - now()))} مانده")
    rows = [[btn(f"{em} {nm}", f"tech.branch.{k}") for k, (em, nm) in list(TECH_BRANCHES.items())[i:i + 2]] for i in range(0, 8, 2)]
    rows.append(back_row())
    (edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows)) if mid else send_view(chat_id, uid, "\n".join(lines), ikb(rows)))


@cb_route("tech")
def cb_tech(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "home":
        view_tech_home(c, uid, chat_id, mid)
    elif sub == "branch":
        view_tech_branch(c, uid, chat_id, mid, args[1])
    elif sub == "detail":
        view_tech_detail(c, uid, chat_id, mid, args[1])
    elif sub == "start":
        try:
            r = start_research(c, uid, args[1])
            edit_view(chat_id, mid, uid, f"🔬 تحقیق «{TECHS[args[1]]['name']}» آغاز شد.\n💰 {cost_text(r['cost'])}\n⏳ {dur(r['secs'])}", ikb([back_row("tech.home")]))
        except GameError as e:
            answer_err(str(e))


def view_tech_branch(c, uid, chat_id, mid, br):
    rows = []
    for tid, t in sorted(TECHS.items(), key=lambda x: (x[1]["tier"], x[1]["name"])):
        if t["branch"] != br:
            continue
        st = tech_status(c, uid, tid)
        mark = {"done": "✅", "active": "⏳", "available": "🔓", "locked": "🔒"}[st]
        rows.append([btn(f"{mark} T{t['tier']} {t['name']}", f"tech.detail.{tid}")])
    rows.append(back_row("tech.home"))
    edit_view(chat_id, mid, uid, f"{TECH_BRANCHES[br][0]} شاخهٔ {TECH_BRANCHES[br][1]}:", ikb(rows))


def view_tech_detail(c, uid, chat_id, mid, tid):
    t = TECHS[tid]
    st = tech_status(c, uid, tid)
    lines = [f"🔬 {t['name']} (ردیف {t['tier']})", t["desc"], f"✨ اثر: {fx_text(t['fx'])}"]
    if t["pre"]:
        lines.append("🔒 پیش‌نیاز: " + "، ".join(TECHS[p]["name"] for p in t["pre"]))
    if st == "available":
        lines.append(f"💰 هزینه: {cost_text(t['cost'])}   ⏳ زمان: {dur(research_time(c, uid, tid))}")
    rows = []
    if st == "available":
        rows.append([btn("🔬 شروع تحقیق", f"tech.start.{tid}")])
    rows.append(back_row(f"tech.branch.{t['branch']}"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


# ============================================================================
# بازار
# ============================================================================
@txt_route("🛒 بازار")
def txt_market(c, uid, chat_id, username, text):
    view_market_home(c, uid, chat_id)


def view_market_home(c, uid, chat_id, mid=None):
    with_tx(lambda c2: market_tick(c2))
    rows = []
    lines = ["🛒 بازار جهانی:\n"]
    prices = market_all_readonly()
    for com in TRADABLE:
        m = prices[com]
        lines.append(f"{RES[com][0]} {RES[com][1]}: {fmt(m['price'])} 💰")
        rows.append([btn(f"{RES[com][0]} {RES[com][1]}", f"market.com.{com}")])
    rows.append([btn("🏷️ آگهی‌های بازیکنان", "market.offers")])
    rows.append(back_row())
    (edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows)) if mid else send_view(chat_id, uid, "\n".join(lines), ikb(rows)))


def market_all_readonly():
    c = rd()
    return market_all(c)


def with_tx(fn):
    with db.tx() as c:
        return fn(c)


@cb_route("market")
def cb_market(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "home":
        view_market_home(c, uid, chat_id, mid)
    elif sub == "com":
        view_commodity(c, uid, chat_id, mid, args[1])
    elif sub == "buy":
        com, qty = args[1], float(args[2])
        try:
            r = market_buy(c, uid, com, qty)
            edit_view(chat_id, mid, uid, f"✅ خرید شد: {fmt(qty)} {rn(com)} — {fmt(r['total'])} 💰", ikb([back_row(f"market.com.{com}")]))
        except GameError as e:
            answer_err(str(e))
    elif sub == "sell":
        com, qty = args[1], float(args[2])
        try:
            r = market_sell(c, uid, com, qty)
            edit_view(chat_id, mid, uid, f"✅ فروخته شد: {fmt(qty)} {rn(com)} — {fmt(r['total'])} 💰", ikb([back_row(f"market.com.{com}")]))
        except GameError as e:
            answer_err(str(e))
    elif sub == "offers":
        view_offers(c, uid, chat_id, mid)
    elif sub == "offerbuy":
        try:
            total = offer_buy(c, uid, int(args[1]))
            edit_view(chat_id, mid, uid, f"✅ خریداری شد ({fmt(total)} 💰)", ikb([back_row("market.offers")]))
        except GameError as e:
            answer_err(str(e))


def view_commodity(c, uid, chat_id, mid, com):
    m = market_row(c, com)
    fee = market_fee_rate(c, uid)
    lines = [f"{RES[com][0]} {RES[com][1]}", f"💰 قیمت: {fmt(m['price'])}  📦 موجودی بازار: {short(m['stock'])}", f"💸 کارمزد معامله: {pct(fee)}", "", f"موجودی شما: {fmt(get_res(c, uid, com))}"]
    rows = [[btn("خرید ۱۰۰", f"market.buy.{com}.100"), btn("خرید ۱۰۰۰", f"market.buy.{com}.1000")],
            [btn("فروش ۱۰۰", f"market.sell.{com}.100"), btn("فروش ۱۰۰۰", f"market.sell.{com}.1000")],
            back_row("market.home")]
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def view_offers(c, uid, chat_id, mid, page=0):
    offers = qa(c, "SELECT o.*, p.name AS seller_name FROM offers o JOIN players p ON p.user_id=o.seller_id WHERE o.status='open' ORDER BY o.id DESC LIMIT 10")
    if not offers:
        return edit_view(chat_id, mid, uid, "آگهی بازی باز نیست.", ikb([back_row("market.home")]))
    rows = [[btn(f"{rn(o['res'])} ×{fmt(o['qty'])} @ {fmt(o['unit_price'])} — {o['seller_name']}", f"market.offerbuy.{o['id']}")] for o in offers]
    rows.append(back_row("market.home"))
    edit_view(chat_id, mid, uid, "🏷️ آگهی‌های باز:", ikb(rows))


# ============================================================================
# جنگ — حمله به بازیکن، اتاق جنگ، جنگ گروهی
# ============================================================================
@txt_route("⚔️ جنگ")
def txt_war(c, uid, chat_id, username, text):
    view_war_home(c, uid, chat_id)


def view_war_home(c, uid, chat_id, mid=None):
    active_wr = q1(c, "SELECT * FROM war_rooms WHERE (a_id=? OR b_id=?) AND status='active'", uid, uid)
    lines = ["⚔️ مرکز جنگ", ""]
    rows = [[btn("🎯 حمله به بازیکن", "war.targets"), btn("🎮 اتاق جنگ", "war.wrnew")],
            [btn("📜 تاریخچهٔ نبردها", "war.history")]]
    if active_wr:
        rows.insert(0, [btn("⚔️ ادامهٔ اتاق جنگ فعال", f"war.wrview.{active_wr['id']}")])
    p = get_player(c, uid)
    if p["alliance_id"] and alliance_has_perm(c, uid, "groupwar"):
        rows.append([btn("👥 جنگ گروهی اتحاد", "gw.home")])
    rows.append(back_row())
    (edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows)) if mid else send_view(chat_id, uid, "\n".join(lines), ikb(rows)))


@cb_route("war")
def cb_war(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "home":
        view_war_home(c, uid, chat_id, mid)
    elif sub == "targets":
        view_war_targets(c, uid, chat_id, mid)
    elif sub == "target":
        view_target_detail(c, uid, chat_id, mid, int(args[1]))
    elif sub == "attack":
        target = int(args[1])
        try:
            res, rep, loot = attack_player(c, uid, target)
            edit_view(chat_id, mid, uid, rep + (f"\n\n🎁 غنیمت: {reward_text(loot)}" if loot else ""), ikb([back_row("war.targets")]))
        except GameError as e:
            answer_err(str(e))
    elif sub == "wrnew":
        view_war_targets(c, uid, chat_id, mid, wr=True)
    elif sub == "wrcreate":
        target = int(args[1])
        try:
            wid = war_room_create(c, uid, target)
            view_war_room(c, uid, chat_id, mid, wid)
        except GameError as e:
            answer_err(str(e))
    elif sub == "wrview":
        view_war_room(c, uid, chat_id, mid, int(args[1]))
    elif sub == "wract":
        wid, action = int(args[1]), args[2]
        try:
            res = war_room_act(c, wid, uid, action)
            view_war_room(c, uid, chat_id, mid, wid)
        except GameError as e:
            answer_err(str(e))
    elif sub == "history":
        view_battle_history(c, uid, chat_id, mid)


def view_war_targets(c, uid, chat_id, mid, wr=False, page=0):
    p = get_player(c, uid)
    rows_q = qa(c, "SELECT * FROM players WHERE user_id!=? AND banned=0 ORDER BY last_seen DESC LIMIT 200", uid)
    targets = [r for r in rows_q if r["shield_until"] <= now()][page * 8:(page + 1) * 8]
    pfx = "war.wrcreate" if wr else "war.target"
    rows = [[btn(f"{r['name']} (Lv{r['level']}, 💪{short(r['mil_power'])})", f"{pfx if wr else 'war.target'}.{r['user_id']}")] for r in targets]
    rows.append(back_row("war.home"))
    edit_view(chat_id, mid, uid, "🎯 هدف را انتخاب کنید (سپردار حذف شده):", ikb(rows))


def view_target_detail(c, uid, chat_id, mid, target):
    tp = get_player(c, target)
    if not tp:
        return edit_view(chat_id, mid, uid, "یافت نشد.", ikb([back_row("war.targets")]))
    lines = [f"🎯 {tp['name']} — سطح {tp['level']}", f"💪 نظامی: {short(tp['mil_power'])}   🏆 {tp['victories']}/{tp['defeats']}"]
    rows = [[btn("⚔️ حملهٔ مستقیم", f"war.attack.{target}"), btn("🎮 اتاق جنگ", f"war.wrcreate.{target}")], back_row("war.targets")]
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def war_room_kb(wid, wr, uid):
    side = "a" if uid == wr["a_id"] else "b"
    my_action = wr[f"{side}_action"]
    if my_action:
        return ikb([back_row("war.home")])
    rows = [[btn(f"{em}{nm}", f"war.wract.{wid}.{k}") for k, (em, nm, _) in list(WR_ACTIONS.items())[i:i + 2]] for i in range(0, 8, 2)]
    rows.append(back_row("war.home"))
    return ikb(rows)


def view_war_room(c, uid, chat_id, mid, wid):
    wr = q1(c, "SELECT * FROM war_rooms WHERE id=?", wid)
    if not wr:
        return edit_view(chat_id, mid, uid, "این اتاق جنگ یافت نشد.", ikb([back_row("war.home")]))
    ap = get_player(c, wr["a_id"])
    bp = get_player(c, wr["b_id"])
    lines = [f"🎮 اتاق جنگ — دور {wr['round']}/{wr['max_rounds']} — زمین: {TERRAINS[wr['terrain']][0]} {TERRAINS[wr['terrain']][1]}",
             f"{ap['name']}: {bar(wr['a_hp'] / max(1, wr['a_hp0']))} {fmt(wr['a_hp'])}/{fmt(wr['a_hp0'])}",
             f"{bp['name']}: {bar(wr['b_hp'] / max(1, wr['b_hp0']))} {fmt(wr['b_hp'])}/{fmt(wr['b_hp0'])}", ""]
    if wr["log"]:
        lines.append(wr["log"][-600:])
    if wr["status"] == "done":
        lines.append(f"\n🏁 پایان — برنده: {'مساوی' if wr['winner'] == 'draw' else (ap['name'] if wr['winner'] == 'a' else bp['name'])}")
        rows = [back_row("war.home")]
    else:
        side = "a" if uid == wr["a_id"] else "b"
        if wr[f"{side}_action"]:
            lines.append("⏳ منتظر انتخاب حریف...")
        rows_kb = war_room_kb(wid, wr, uid)
        return edit_view(chat_id, mid, uid, "\n".join(lines), rows_kb)
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def view_battle_history(c, uid, chat_id, mid):
    rows_ = qa(c, "SELECT * FROM battles WHERE attacker_id=? OR defender_id=? ORDER BY id DESC LIMIT 8", uid, uid)
    if not rows_:
        return edit_view(chat_id, mid, uid, "تاریخچه‌ای وجود ندارد.", ikb([back_row("war.home")]))
    lines = ["📜 آخرین نبردها:\n"]
    for b in rows_:
        me = "مهاجم" if b["attacker_id"] == uid else "مدافع"
        res_txt = "برد" if (b["winner"] == "att") == (b["attacker_id"] == uid) else "باخت"
        lines.append(f"{ts_str(b['ts'])} — {me} — {res_txt} ({b['margin'] * 100:+.0f}٪)")
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb([back_row("war.home")]))


# ============================================================================
# جنگ گروهی اتحاد
# ============================================================================
@cb_route("gw")
def cb_gw(c, uid, chat_id, mid, args):
    sub = args[0]
    p = get_player(c, uid)
    if sub == "home":
        active = q1(c, "SELECT * FROM group_wars WHERE alliance_id=? AND status='forming'", p["alliance_id"])
        rows = [[btn("➕ جنگ گروهی جدید", "gw.new")]]
        if active:
            rows.insert(0, [btn("👥 مشاهدهٔ جنگ گروهی فعال", f"gw.view.{active['id']}")])
        rows.append(back_row("war.home"))
        edit_view(chat_id, mid, uid, "👥 جنگ گروهی اتحاد:", ikb(rows))
    elif sub == "new":
        rows_q = qa(c, "SELECT * FROM players WHERE user_id!=? AND alliance_id!=? ORDER BY mil_power DESC LIMIT 8", uid, p["alliance_id"])
        rows = [[btn(f"{r['name']} (💪{short(r['mil_power'])})", f"gw.create.{r['user_id']}")] for r in rows_q]
        rows.append(back_row("gw.home"))
        edit_view(chat_id, mid, uid, "🎯 هدف جنگ گروهی را انتخاب کنید:", ikb(rows))
    elif sub == "create":
        gwid = group_war_create(c, p["alliance_id"], uid, "player", int(args[1]))
        view_gw(c, uid, chat_id, mid, gwid)
    elif sub == "view":
        view_gw(c, uid, chat_id, mid, int(args[1]))
    elif sub == "join":
        gwid, role = int(args[1]), args[2]
        try:
            group_war_join(c, gwid, uid, role)
            view_gw(c, uid, chat_id, mid, gwid)
        except GameError as e:
            answer_err(str(e))
    elif sub == "launch":
        gwid = int(args[1])
        try:
            res, captured = group_war_launch(c, gwid, uid)
            edit_view(chat_id, mid, uid, f"👥 نتیجهٔ عملیات: {'✅ پیروزی' if res['win'] else '❌ شکست'} (حاشیه {res['margin'] * 100:+.1f}٪)" + ("\n🏙️ شهر فتح شد!" if captured else ""), ikb([back_row("gw.home")]))
        except GameError as e:
            answer_err(str(e))


def view_gw(c, uid, chat_id, mid, gwid):
    gw = q1(c, "SELECT * FROM group_wars WHERE id=?", gwid)
    members = qa(c, "SELECT gm.*, p.name FROM group_war_members gm JOIN players p ON p.user_id=gm.user_id WHERE gm.war_id=?", gwid)
    lines = [f"👥 جنگ گروهی #{gwid} — استراتژی: {STRATEGIES[gw['strategy']][0]} {STRATEGIES[gw['strategy']][1]}", f"وضعیت: {gw['status']}", "", "اعضا:"]
    for m in members:
        lines.append(f"  {GW_ROLES[m['role']][0]} {m['name']} — {GW_ROLES[m['role']][1]}")
    rows = []
    if gw["status"] == "forming":
        taken = {m["role"] for m in members}
        my_role = next((m["role"] for m in members if m["user_id"] == uid), None)
        if not my_role:
            for rk in GW_ROLE_ORDER[1:]:
                rows.append([btn(f"{GW_ROLES[rk][0]} پیوستن به‌عنوان {GW_ROLES[rk][1]}", f"gw.join.{gwid}.{rk}")])
        if gw["leader_id"] == uid and len(members) >= 2:
            rows.append([btn("🚀 آغاز عملیات", f"gw.launch.{gwid}")])
    rows.append(back_row("gw.home"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


# ============================================================================
# اتحاد
# ============================================================================
@txt_route("🤝 اتحاد")
def txt_alliance(c, uid, chat_id, username, text):
    view_alliance_home(c, uid, chat_id)


def view_alliance_home(c, uid, chat_id, mid=None):
    p = get_player(c, uid)
    if not p["alliance_id"]:
        rows = [[btn("➕ ساخت اتحاد (۵۰K)", "al.create"), btn("🔎 مشاهدهٔ اتحادها", "al.list")], back_row()]
        txt = "🤝 شما عضو هیچ اتحادی نیستید."
        return (edit_view(chat_id, mid, uid, txt, ikb(rows)) if mid else send_view(chat_id, uid, txt, ikb(rows)))
    al = q1(c, "SELECT * FROM alliances WHERE id=?", p["alliance_id"])
    role = alliance_member_row(c, al["id"], uid)["role"]
    n_members = qv(c, "SELECT COUNT(*) FROM alliance_members WHERE alliance_id=?", al["id"], default=0)
    treas = alliance_treasury(c, al["id"])
    lines = [f"🤝 {al['name']} [{al['tag']}] — سطح {al['level']} (XP {fmt(al['xp'])})", f"🎖️ نقش شما: {ALLIANCE_ROLES[role][1]}   👥 اعضا: {n_members}",
             f"💰 خزانه: {fmt(treas.get('money', 0))}", ""]
    rows = [[btn("👥 اعضا", "al.members"), btn("💰 خزانه/کمک", "al.treasury")],
            [btn("🔬 فناوری اتحاد", "al.techs"), btn("🏗️ پروژه‌ها", "al.projects")],
            [btn("🗳️ رأی‌گیری‌ها", "al.votes"), btn("📜 گزارش‌ها", "al.logs")]]
    if role != "leader":
        rows.append([btn("🚪 ترک اتحاد", "al.leave")])
    rows.append(back_row())
    (edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows)) if mid else send_view(chat_id, uid, "\n".join(lines), ikb(rows)))


@cb_route("al")
def cb_al(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "home":
        view_alliance_home(c, uid, chat_id, mid)
    elif sub == "create":
        session_set(c, uid, "al_create_name", {})
        edit_view(chat_id, mid, uid, "✍️ نام اتحاد را ارسال کنید (۳ تا ۳۰ نویسه):")
    elif sub == "list":
        view_alliance_list(c, uid, chat_id, mid)
    elif sub == "join":
        try:
            r = alliance_join_request(c, uid, int(args[1]))
            edit_view(chat_id, mid, uid, "✅ عضو شدید!" if r == "joined" else "📨 درخواست ارسال شد.", ikb([back_row("al.home")]))
        except GameError as e:
            answer_err(str(e))
    elif sub == "members":
        view_alliance_members(c, uid, chat_id, mid)
    elif sub == "treasury":
        view_alliance_treasury(c, uid, chat_id, mid)
    elif sub == "contribute":
        res, amt = args[1], float(args[2])
        try:
            alliance_contribute(c, uid, res, amt)
            view_alliance_treasury(c, uid, chat_id, mid)
        except GameError as e:
            answer_err(str(e))
    elif sub == "techs":
        view_alliance_techs(c, uid, chat_id, mid)
    elif sub == "techdo":
        try:
            alliance_research_start(c, uid, args[1])
            view_alliance_techs(c, uid, chat_id, mid)
        except GameError as e:
            answer_err(str(e))
    elif sub == "projects":
        view_alliance_projects(c, uid, chat_id, mid)
    elif sub == "projstart":
        try:
            alliance_project_start(c, uid, args[1])
            view_alliance_projects(c, uid, chat_id, mid)
        except GameError as e:
            answer_err(str(e))
    elif sub == "projcontrib":
        pid, res, amt = int(args[1]), args[2], 1000.0
        try:
            alliance_project_contribute(c, uid, pid, res, amt)
            view_alliance_projects(c, uid, chat_id, mid)
        except GameError as e:
            answer_err(str(e))
    elif sub == "votes":
        view_alliance_votes(c, uid, chat_id, mid)
    elif sub == "voteyes":
        alliance_vote_cast(c, uid, int(args[1]), 1)
        view_alliance_votes(c, uid, chat_id, mid)
    elif sub == "voteno":
        alliance_vote_cast(c, uid, int(args[1]), 0)
        view_alliance_votes(c, uid, chat_id, mid)
    elif sub == "logs":
        view_alliance_logs(c, uid, chat_id, mid)
    elif sub == "leave":
        try:
            alliance_leave(c, uid)
            view_alliance_home(c, uid, chat_id, mid)
        except GameError as e:
            answer_err(str(e))
    elif sub == "kick":
        try:
            alliance_kick(c, uid, int(args[1]))
            view_alliance_members(c, uid, chat_id, mid)
        except GameError as e:
            answer_err(str(e))
    elif sub == "promote":
        try:
            alliance_promote(c, uid, int(args[1]), args[2])
            view_alliance_members(c, uid, chat_id, mid)
        except GameError as e:
            answer_err(str(e))


def view_alliance_list(c, uid, chat_id, mid):
    rows_q = qa(c, "SELECT * FROM alliances ORDER BY power DESC LIMIT 15")
    if not rows_q:
        return edit_view(chat_id, mid, uid, "هنوز اتحادی ساخته نشده.", ikb([back_row("al.home")]))
    rows = [[btn(f"{a['name']} [{a['tag']}] Lv{a['level']}", f"al.join.{a['id']}")] for a in rows_q]
    rows.append(back_row("al.home"))
    edit_view(chat_id, mid, uid, "🔎 اتحادهای موجود:", ikb(rows))


def view_alliance_members(c, uid, chat_id, mid):
    p = get_player(c, uid)
    members = qa(c, "SELECT am.*, pl.name, pl.level FROM alliance_members am JOIN players pl ON pl.user_id=am.user_id WHERE am.alliance_id=? ORDER BY am.contribution DESC", p["alliance_id"])
    my_role = alliance_member_row(c, p["alliance_id"], uid)["role"]
    lines = ["👥 اعضای اتحاد:\n"]
    rows = []
    for m in members:
        lines.append(f"{ALLIANCE_ROLES[m['role']][0]} {m['name']} (Lv{m['level']}) — کمک: {fmt(m['contribution'])}")
        if m["user_id"] != uid and ROLE_RANK.get(my_role, 0) > ROLE_RANK.get(m["role"], 0) and alliance_has_perm(c, uid, "kick"):
            rows.append([btn(f"🚪 اخراج {m['name']}", f"al.kick.{m['user_id']}")])
    rows.append(back_row("al.home"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def view_alliance_treasury(c, uid, chat_id, mid):
    p = get_player(c, uid)
    treas = alliance_treasury(c, p["alliance_id"])
    lines = ["💰 خزانهٔ اتحاد:\n"] + [f"{RES[k][0]} {RES[k][1]}: {fmt(v)}" for k, v in treas.items() if v]
    rows = [[btn("💰 کمک ۵۰۰۰ پول", "al.contribute.money.5000"), btn("⛏️ کمک ۵۰۰ آهن", "al.contribute.iron.500")], back_row("al.home")]
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def view_alliance_techs(c, uid, chat_id, mid):
    p = get_player(c, uid)
    done = {r["tech_id"] for r in qa(c, "SELECT tech_id FROM alliance_techs WHERE alliance_id=?", p["alliance_id"])}
    rows = []
    lines = ["🔬 فناوری‌های اتحاد:\n"]
    for k, t in ALLIANCE_TECHS.items():
        mark = "✅" if k in done else ""
        lines.append(f"{mark} {t['name']} (Lv{t['lvl']}): {t['desc']} — {cost_text(t['cost'])}")
        if k not in done:
            rows.append([btn(f"تحقیق {t['name']}", f"al.techdo.{k}")])
    rows.append(back_row("al.home"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def view_alliance_projects(c, uid, chat_id, mid):
    p = get_player(c, uid)
    active = qa(c, "SELECT * FROM alliance_projects WHERE alliance_id=? AND status='active'", p["alliance_id"])
    lines = ["🏗️ پروژه‌های اتحاد:\n"]
    rows = []
    active_kinds = {a["kind"] for a in active}
    for a in active:
        pk = ALLIANCE_PROJECTS[a["kind"]]
        prog = jl(a["progress"])
        goal = jl(a["goal"])
        lines.append(f"⏳ {pk['name']}: " + "، ".join(f"{rn(k)} {fmt(prog.get(k, 0))}/{fmt(v)}" for k, v in goal.items()))
        for k in goal:
            rows.append([btn(f"کمک {rn(k)} به {pk['name']}", f"al.projcontrib.{a['id']}.{k}")])
    for k, pk in ALLIANCE_PROJECTS.items():
        if k not in active_kinds:
            lines.append(f"🔓 {pk['name']}: {pk['desc']} — هدف: {cost_text(pk['goal'])}")
            rows.append([btn(f"شروع {pk['name']}", f"al.projstart.{k}")])
    rows.append(back_row("al.home"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def view_alliance_votes(c, uid, chat_id, mid):
    p = get_player(c, uid)
    votes = qa(c, "SELECT * FROM votes WHERE alliance_id=? AND status='open' ORDER BY id DESC LIMIT 5", p["alliance_id"])
    if not votes:
        return edit_view(chat_id, mid, uid, "رأی‌گیری باز وجود ندارد.", ikb([back_row("al.home")]))
    lines = ["🗳️ رأی‌گیری‌های باز:\n"]
    rows = []
    for v in votes:
        yes, no = alliance_vote_tally(c, v["id"])
        lines.append(f"#{v['id']} {VOTE_KINDS.get(v['kind'], v['kind'])} — رأی: {yes}✅ / {no}❌ — پایان {ts_str(v['ends_at'])}")
        rows.append([btn(f"✅ موافق #{v['id']}", f"al.voteyes.{v['id']}"), btn(f"❌ مخالف #{v['id']}", f"al.voteno.{v['id']}")])
    rows.append(back_row("al.home"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def view_alliance_logs(c, uid, chat_id, mid):
    p = get_player(c, uid)
    logs = qa(c, "SELECT * FROM alliance_logs WHERE alliance_id=? ORDER BY id DESC LIMIT 15", p["alliance_id"])
    lines = ["📜 گزارش‌های اخیر اتحاد:\n"] + [f"{ts_str(l['ts'])} — {l['text']}" for l in logs]
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb([back_row("al.home")]))


def handle_alliance_create_text(c, uid, chat_id, username, text):
    state, data = session_get(c, uid)
    if state != "al_create_name":
        return False
    try:
        session_set(c, uid, "al_create_tag", {"name": text})
        safe_send(chat_id, "✍️ حالا تگ کوتاه اتحاد را ارسال کنید (۲ تا ۶ نویسه):")
    except GameError as e:
        safe_send(chat_id, str(e))
    return True


def handle_alliance_create_tag_text(c, uid, chat_id, username, text):
    state, data = session_get(c, uid)
    if state != "al_create_tag":
        return False
    try:
        aid = alliance_create(c, uid, data["name"], text)
        session_clear(c, uid)
        safe_send(chat_id, "✅ اتحاد ساخته شد!")
        view_alliance_home(c, uid, chat_id)
    except GameError as e:
        safe_send(chat_id, str(e))
    return True


# ============================================================================
# دیپلماسی
# ============================================================================
@txt_route("🤝 دیپلماسی")
def txt_dip(c, uid, chat_id, username, text):
    view_dip_home(c, uid, chat_id)


def view_dip_home(c, uid, chat_id, mid=None):
    treaties = qa(c, "SELECT * FROM treaties WHERE (a_user=? OR b_user=?) AND status='active'", uid, uid)
    pending = qa(c, "SELECT * FROM treaties WHERE b_user=? AND status='pending'", uid)
    lines = ["🕊️ دیپلماسی", ""]
    if treaties:
        lines.append("📜 معاهدات فعال:")
        for t in treaties:
            other = t["b_user"] if t["a_user"] == uid else t["a_user"]
            op = get_player(c, other)
            lines.append(f"  {TREATY_KINDS[t['kind']][0]} با {op['name'] if op else '?'} تا {ts_str(t['until'])}")
    rows = [[btn("🎯 اقدام دیپلماتیک", "dip.targets"), btn("📜 پیشنهاد معاهده", "dip.treaty")]]
    if pending:
        rows.append([btn(f"📨 {len(pending)} پیشنهاد در انتظار", "dip.pending")])
    rows.append(back_row())
    (edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows)) if mid else send_view(chat_id, uid, "\n".join(lines), ikb(rows)))


@cb_route("dip")
def cb_dip(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "home":
        view_dip_home(c, uid, chat_id, mid)
    elif sub == "targets":
        view_dip_targets(c, uid, chat_id, mid, "act")
    elif sub == "treaty":
        view_dip_targets(c, uid, chat_id, mid, "treaty")
    elif sub == "acttarget":
        view_dip_actions(c, uid, chat_id, mid, int(args[1]))
    elif sub == "treatytarget":
        view_treaty_kinds(c, uid, chat_id, mid, int(args[1]))
    elif sub == "do":
        target, action = int(args[1]), args[2]
        try:
            newrel = dip_action(c, uid, target, action)
            edit_view(chat_id, mid, uid, f"✅ انجام شد. روابط جدید: {fmt(newrel)}", ikb([back_row("dip.home")]))
        except GameError as e:
            answer_err(str(e))
    elif sub == "treatydo":
        target, kind = int(args[1]), args[2]
        treaty_propose(c, uid, target, kind)
        edit_view(chat_id, mid, uid, "📜 پیشنهاد ارسال شد.", ikb([back_row("dip.home")]))
    elif sub == "pending":
        view_dip_pending(c, uid, chat_id, mid)
    elif sub == "accept":
        try:
            treaty_respond(c, uid, int(args[1]), True)
            view_dip_pending(c, uid, chat_id, mid)
        except GameError as e:
            answer_err(str(e))
    elif sub == "reject":
        treaty_respond(c, uid, int(args[1]), False)
        view_dip_pending(c, uid, chat_id, mid)


def view_dip_targets(c, uid, chat_id, mid, mode):
    rows_q = qa(c, "SELECT * FROM players WHERE user_id!=? ORDER BY last_seen DESC LIMIT 10", uid)
    pfx = "dip.acttarget" if mode == "act" else "dip.treatytarget"
    rows = [[btn(r["name"], f"{pfx}.{r['user_id']}")] for r in rows_q]
    rows.append(back_row("dip.home"))
    edit_view(chat_id, mid, uid, "🎯 طرف مقابل را انتخاب کنید:", ikb(rows))


def view_dip_actions(c, uid, chat_id, mid, target):
    rel = get_relation_players(c, uid, target)
    lines = [f"روابط فعلی: {fmt(rel)}", ""]
    rows = [[btn(f"{em} {nm}", f"dip.do.{target}.{k}")] for k, (em, nm, desc, cost) in DIP_ACTIONS.items()]
    rows.append(back_row("dip.home"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def view_treaty_kinds(c, uid, chat_id, mid, target):
    rows = [[btn(f"{em} {nm}", f"dip.treatydo.{target}.{k}")] for k, (em, nm, dur_, cost) in TREATY_KINDS.items()]
    rows.append(back_row("dip.home"))
    edit_view(chat_id, mid, uid, "📜 نوع معاهده را انتخاب کنید:", ikb(rows))


def view_dip_pending(c, uid, chat_id, mid):
    pend = qa(c, "SELECT * FROM treaties WHERE b_user=? AND status='pending'", uid)
    if not pend:
        return edit_view(chat_id, mid, uid, "پیشنهادی در انتظار نیست.", ikb([back_row("dip.home")]))
    rows = []
    lines = ["📨 پیشنهادهای در انتظار:\n"]
    for t in pend:
        ap = get_player(c, t["a_user"])
        lines.append(f"#{t['id']} {TREATY_KINDS[t['kind']][1]} از {ap['name']}")
        rows.append([btn(f"✅ پذیرش #{t['id']}", f"dip.accept.{t['id']}"), btn(f"❌ رد #{t['id']}", f"dip.reject.{t['id']}")])
    rows.append(back_row("dip.home"))
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


# ============================================================================
# اطلاعات
# ============================================================================
@txt_route("🕵️ اطلاعات")
def txt_intel(c, uid, chat_id, username, text):
    view_intel_home(c, uid, chat_id)


def view_intel_home(c, uid, chat_id, mid=None):
    p = get_player(c, uid)
    pw = compute_powers(c, uid, p)
    lines = [f"🕵️ اطلاعات — قدرت اطلاعاتی: {short(pw['intel'])}", ""]
    rows = [[btn("🎯 عملیات جدید", "intel.new"), btn("📁 گزارش‌های من", "intel.reports")], back_row()]
    (edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows)) if mid else send_view(chat_id, uid, "\n".join(lines), ikb(rows)))


@cb_route("intel")
def cb_intel(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "home":
        view_intel_home(c, uid, chat_id, mid)
    elif sub == "new":
        rows_q = qa(c, "SELECT * FROM players WHERE user_id!=? ORDER BY last_seen DESC LIMIT 10", uid)
        rows = [[btn(r["name"], f"intel.target.{r['user_id']}")] for r in rows_q]
        rows.append(back_row("intel.home"))
        edit_view(chat_id, mid, uid, "🎯 هدف را انتخاب کنید:", ikb(rows))
    elif sub == "target":
        target = int(args[1])
        rows = [[btn(f"{em} {nm}", f"intel.do.player.{target}.{k}")] for k, (em, nm, cost, _) in INTEL_OPS.items()]
        rows.append(back_row("intel.home"))
        edit_view(chat_id, mid, uid, "🔍 نوع عملیات:", ikb(rows))
    elif sub == "do":
        ttype, target, kind = args[1], args[2], args[3]
        try:
            r = intel_op(c, uid, ttype, target, kind)
            edit_view(chat_id, mid, uid, f"🕵️ عملیات انجام شد (دقت {pct(r['accuracy'])}).", ikb([back_row("intel.reports")]))
        except GameError as e:
            answer_err(str(e))
    elif sub == "reports":
        view_intel_reports(c, uid, chat_id, mid)
    elif sub == "report":
        r = q1(c, "SELECT * FROM intel_reports WHERE id=? AND user_id=?", int(args[1]), uid)
        if r:
            edit_view(chat_id, mid, uid, intel_report_text(r), ikb([back_row("intel.reports")]))


def view_intel_reports(c, uid, chat_id, mid):
    reps = intel_reports_list(c, uid)
    if not reps:
        return edit_view(chat_id, mid, uid, "گزارشی ثبت نشده.", ikb([back_row("intel.home")]))
    rows = [[btn(f"#{r['id']} {r['ttype']}/{r['kind']} — {ts_str(r['created'])}", f"intel.report.{r['id']}")] for r in reps]
    rows.append(back_row("intel.home"))
    edit_view(chat_id, mid, uid, "📁 گزارش‌های اطلاعاتی:", ikb(rows))


# ============================================================================
# رتبه‌بندی
# ============================================================================
@txt_route("🏆 رتبه‌بندی")
def txt_lb(c, uid, chat_id, username, text):
    view_lb_menu(c, uid, chat_id)


def view_lb_menu(c, uid, chat_id, mid=None):
    rows = [[btn(f"{em}", f"lb.show.{k}") for k, (em, _) in list(LB_DEFS.items())[i:i + 3]] for i in range(0, len(LB_DEFS), 3)]
    rows.append([btn("🤝 برترین اتحادها", "lb.alliance")])
    rows.append(back_row())
    (edit_view(chat_id, mid, uid, "🏆 رتبه‌بندی — دسته را انتخاب کنید:", ikb(rows)) if mid else send_view(chat_id, uid, "🏆 رتبه‌بندی:", ikb(rows)))


@cb_route("lb")
def cb_lb(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "home":
        view_lb_menu(c, uid, chat_id, mid)
    elif sub == "show":
        rows_ = leaderboard(c, args[1])
        lines = [f"{LB_DEFS[args[1]][0]} برترین‌ها:\n"]
        for i, r in enumerate(rows_, 1):
            medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
            lines.append(f"{medal} {r['name']} — {short(r['val'])}")
        edit_view(chat_id, mid, uid, "\n".join(lines), ikb([back_row("lb.home")]))
    elif sub == "alliance":
        rows_ = alliance_leaderboard(c)
        lines = ["🤝 برترین اتحادها:\n"]
        for i, r in enumerate(rows_, 1):
            medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
            lines.append(f"{medal} {r['name']} [{r['tag']}] Lv{r['level']} — قدرت {short(r['power'])}")
        edit_view(chat_id, mid, uid, "\n".join(lines), ikb([back_row("lb.home")]))


# ============================================================================
# مأموریت‌ها / دستاوردها
# ============================================================================
@txt_route("🎯 مأموریت‌ها")
def txt_missions(c, uid, chat_id, username, text):
    ensure_missions(c, uid)
    view_missions(c, uid, chat_id)


def view_missions(c, uid, chat_id, mid=None, period="daily"):
    p = get_player(c, uid)
    day, week = game_day(), game_week()
    per_key = {"daily": day, "weekly": week, "perm": "perm"}[period]
    rows_q = qa(c, "SELECT * FROM missions WHERE user_id=? AND period=? ORDER BY claimed, mid", uid, per_key)
    lines = [f"🎯 مأموریت‌های {('روزانه' if period == 'daily' else 'هفتگی' if period == 'weekly' else 'دائمی')}:\n"]
    rows = []
    for m in rows_q:
        mm = MISSIONS[m["mid"]]
        pct_ = min(1.0, m["progress"] / mm["target"])
        status = "✅" if m["claimed"] else ("🎁" if pct_ >= 1 else f"{bar(pct_, 6)}")
        lines.append(f"{status} {mm['title']} ({fmt(min(m['progress'], mm['target']))}/{fmt(mm['target'])}) — {reward_text(mm['reward'])}")
        if not m["claimed"] and pct_ >= 1:
            rows.append([btn(f"🎁 دریافت: {mm['title'][:20]}", f"msn.claim.{m['mid']}.{per_key}")])
    rows.append([btn("📅 روزانه", "msn.period.daily"), btn("🗓️ هفتگی", "msn.period.weekly"), btn("♾️ دائمی", "msn.period.perm")])
    rows.append(back_row())
    (edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows)) if mid else send_view(chat_id, uid, "\n".join(lines), ikb(rows)))


@cb_route("msn")
def cb_msn(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "period":
        view_missions(c, uid, chat_id, mid, args[1])
    elif sub == "claim":
        mid_, period = args[1], args[2]
        try:
            reward = claim_mission(c, uid, mid_, period)
            api.answer_callback(cur_cbid(), "🎁 " + reward_text(reward), False)
            view_missions(c, uid, chat_id, mid, MISSIONS[mid_]["period"] if MISSIONS[mid_]["period"] in ("daily", "weekly", "perm") else "daily")
        except GameError as e:
            answer_err(str(e))


@txt_route("🏅 دستاوردها")
def txt_achievements(c, uid, chat_id, username, text):
    view_achievements(c, uid, chat_id)


def view_achievements(c, uid, chat_id, mid=None, page=0):
    earned = {r["aid"] for r in qa(c, "SELECT aid FROM achievements WHERE user_id=?", uid)}
    stats = all_stats(c, uid)
    cats = list(dict.fromkeys(a["cat"] for a in ACHIEVEMENTS.values()))
    lines = [f"🏅 دستاوردها ({len(earned)}/{len(ACHIEVEMENTS)})\n"]
    items = list(ACHIEVEMENTS.values())[page * 10:(page + 1) * 10]
    for a in items:
        got = a["id"] in earned
        cur = stats.get(a["stat"], 0)
        lines.append(f"{'✅' if got else '🔒'} {a['title']} — {fmt(min(cur, a['thr']))}/{fmt(a['thr'])}")
    rows = pager_row("ach.page", page, len(ACHIEVEMENTS) > (page + 1) * 10)
    rows.append(back_row())
    (edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows)) if mid else send_view(chat_id, uid, "\n".join(lines), ikb(rows)))


@cb_route("ach")
def cb_ach(c, uid, chat_id, mid, args):
    if args[0] == "page":
        view_achievements(c, uid, chat_id, mid, int(args[1]))


# ============================================================================
# رویدادهای جهانی
# ============================================================================
@txt_route("🌍 رویدادهای جهان")
def txt_events(c, uid, chat_id, username, text):
    view_events(c, uid, chat_id)


def view_events(c, uid, chat_id, mid=None):
    evs = active_events(c)
    if not evs:
        lines = ["🌍 هیچ رویداد فعالی در جهان نیست."]
    else:
        lines = ["🌍 رویدادهای فعال جهان:\n"]
        for e in evs:
            scope = {"global": "جهانی", "region": REGIONS.get(e["target"], ("", ""))[0], "country": e["target"]}[e["scope"]]
            lines.append(f"{e['title']} ({scope}) — {dur(max(0, e['ends_at'] - now()))} مانده\n  {EVENT_TYPES[e['kind']]['desc']}")
    (edit_view(chat_id, mid, uid, "\n".join(lines), ikb([back_row()])) if mid else send_view(chat_id, uid, "\n".join(lines), ikb([back_row()])))


# ============================================================================
# تنظیمات
# ============================================================================
@txt_route("⚙️ تنظیمات")
def txt_settings(c, uid, chat_id, username, text):
    view_settings(c, uid, chat_id)


def view_settings(c, uid, chat_id, mid=None):
    p = get_player(c, uid)
    lines = [f"⚙️ تنظیمات", f"👤 نام: {p['name']}", f"🆔 شناسه: {uid}", f"📅 عضویت: {ts_str(p['created'])}"]
    rows = [[btn("📊 آمار کامل من", "set.stats")], [btn("🐞 گزارش باگ", "set.bug")], back_row()]
    (edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows)) if mid else send_view(chat_id, uid, "\n".join(lines), ikb(rows)))


@cb_route("set")
def cb_set(c, uid, chat_id, mid, args):
    sub = args[0]
    if sub == "home":
        view_settings(c, uid, chat_id, mid)
    elif sub == "stats":
        stats = all_stats(c, uid)
        lines = ["📊 آمار کامل:\n"] + [f"{k}: {fmt(v)}" for k, v in sorted(stats.items())]
        edit_view(chat_id, mid, uid, "\n".join(lines)[:3900], ikb([back_row("set.home")]))
    elif sub == "bug":
        session_set(c, uid, "bug_report", {})
        edit_view(chat_id, mid, uid, "🐞 توضیح باگ را در یک پیام متنی ارسال کنید:")


def handle_bug_report_text(c, uid, chat_id, username, text):
    state, data = session_get(c, uid)
    if state != "bug_report":
        return False
    session_clear(c, uid)
    ex(c, "INSERT INTO error_log(ts,ctx,message,trace) VALUES(?,?,?,?)", now(), f"bugreport:{uid}", text[:400], "")
    if ADMIN_ID:
        notify(c, ADMIN_ID, "system", f"🐞 گزارش باگ از {uid}:\n{text[:300]}")
    safe_send(chat_id, "🙏 ممنون! گزارش شما ثبت شد.")
    return True


# ============================================================================
# DISPATCHER — مسیریابی پیام‌ها و کال‌بک‌ها
# ============================================================================
TEXT_HANDLERS_ORDERED = []  # (state_matcher, fn) برای جریان‌های ورودی متنی چندمرحله‌ای


def register_text_flow(fn):
    TEXT_HANDLERS_ORDERED.append(fn)
    return fn


register_text_flow(handle_registration_text)
register_text_flow(handle_alliance_create_text)
register_text_flow(handle_alliance_create_tag_text)
register_text_flow(handle_bug_report_text)


def dispatch_text(uid, chat_id, username, text):
    text = (text or "").strip()
    if not text:
        return
    if text in ("/start", "start", "شروع"):
        with db.tx() as c:
            view_start(c, uid, chat_id, username)
        return
    if text == "/admin" and uid == ADMIN_ID:
        with db.tx() as c:
            view_admin_home(c, uid, chat_id)
        return
    with db.tx() as c:
        if q1(c, "SELECT 1 FROM players WHERE user_id=? AND banned=1", uid):
            return
        for flow in TEXT_HANDLERS_ORDERED:
            try:
                if flow(c, uid, chat_id, username, text):
                    return
            except GameError as e:
                safe_send(chat_id, str(e))
                return
        if text in TXT_ROUTES:
            p = get_player(c, uid)
            if not p:
                view_start(c, uid, chat_id, username)
                return
            player_home(c, uid)
            try:
                TXT_ROUTES[text](c, uid, chat_id, username, text)
            except GameError as e:
                safe_send(chat_id, str(e))
            return
        p = get_player(c, uid)
        if not p:
            view_start(c, uid, chat_id, username)
        else:
            safe_send(chat_id, "از منوی زیر یک گزینه انتخاب کنید 👇", main_menu_kb())


def dispatch_callback(uid, chat_id, mid, cb_id, data, username=""):
    set_cur_cbid(cb_id)
    try:
        parts = data.split(".")
        prefix = parts[0]
        fn = CB_ROUTES.get(prefix)
        if not fn:
            api.answer_callback(cb_id)
            return
        with db.tx() as c:
            if q1(c, "SELECT 1 FROM players WHERE user_id=? AND banned=1", uid):
                api.answer_callback(cb_id, "⛔ حساب شما مسدود است.", True)
                return
            p = get_player(c, uid)
            if not p and prefix not in ("reg", "nav"):
                api.answer_callback(cb_id, "ابتدا ثبت‌نام کنید: /start", True)
                return
            if p:
                process_due(c, uid)
                settle_player(c, uid)
            fn(c, uid, chat_id, mid, parts[1:])
        api.answer_callback(cb_id)
    except GameError as e:
        answer_err(str(e))
    except Exception as e:
        log_error("dispatch_callback:" + data, e)
        api.answer_callback(cb_id, "⚠️ خطای داخلی — دوباره تلاش کنید.", True)
    finally:
        set_cur_cbid(None)


def process_update(u):
    try:
        if "message" in u:
            msg = u["message"]
            chat = msg.get("chat", {})
            frm = msg.get("from", {})
            uid = frm.get("id") or chat.get("id")
            chat_id = chat.get("id")
            if uid is None or chat_id is None:
                return
            if not RATE.allow(uid):
                return
            with USER_LOCKS[uid]:
                text = msg.get("text", "")
                username = frm.get("username", "") or frm.get("first_name", "")
                dispatch_text(uid, chat_id, username, text)
        elif "callback_query" in u:
            cq = u["callback_query"]
            frm = cq.get("from", {})
            uid = frm.get("id")
            msg = cq.get("message", {}) or {}
            chat = msg.get("chat", {}) or {}
            chat_id = chat.get("id") or uid
            mid = msg.get("message_id")
            data = cq.get("data", "") or ""
            cb_id = cq.get("id")
            if uid is None:
                return
            if cb_duplicate(uid, cb_id, mid, data):
                if cb_id:
                    api.answer_callback(cb_id)
                return
            if not RATE.allow(uid):
                if cb_id:
                    api.answer_callback(cb_id, "⏳ کمی آهسته‌تر", True)
                return
            with USER_LOCKS[uid]:
                username = frm.get("username", "") or frm.get("first_name", "")
                dispatch_callback(uid, chat_id, mid, cb_id, data, username)
    except Exception as e:
        log_error("process_update", e)
        SEC["errors"] += 1


# ============================================================================
# NOTIFICATION WORKER — ارسال واقعی اعلان‌های ثبت‌شده
# ============================================================================
def notification_worker_tick(limit=25):
    with db.tx() as c:
        rows = qa(c, "SELECT * FROM notifications WHERE sent=0 AND tries<5 ORDER BY id LIMIT ?", limit)
        ids = [r["id"] for r in rows]
        if ids:
            ex(c, f"UPDATE notifications SET tries=tries+1 WHERE id IN ({','.join('?' * len(ids))})", *ids)
    sent_ids, failed_ids = [], []
    for r in rows:
        res = safe_send(r["user_id"], f"{NOTIF_KINDS.get(r['kind'], '🔔')} {r['text']}")
        if res and res.get("ok"):
            sent_ids.append(r["id"])
        else:
            failed_ids.append(r["id"])
    if sent_ids:
        with db.tx() as c:
            ex(c, f"UPDATE notifications SET sent=1 WHERE id IN ({','.join('?' * len(sent_ids))})", *sent_ids)
    return len(sent_ids), len(failed_ids)


def broadcast_worker_tick():
    with db.tx() as c:
        rows = qa(c, "SELECT * FROM broadcasts WHERE sent < total")
    for b in rows:
        targets = qa(rd(), "SELECT user_id FROM players WHERE banned=0 ORDER BY user_id LIMIT 30 OFFSET ?", b["sent"])
        cnt = 0
        for t in targets:
            if safe_send(t["user_id"], b["text"]):
                cnt += 1
        if cnt:
            with db.tx() as c:
                ex(c, "UPDATE broadcasts SET sent=sent+? WHERE id=?", cnt, b["id"])


# ============================================================================
# BACKGROUND SCHEDULER — تیک‌های دوره‌ای بازی (بدون نیاز به تعامل کاربر)
# ============================================================================
STOP_FLAG = {"stop": False}


def background_loop():
    last_backup = time.time()
    last_market = 0
    last_investments = 0
    last_orders = 0
    last_treaties = 0
    last_votes = 0
    last_wr_expire = 0
    while not STOP_FLAG["stop"]:
        try:
            t = time.time()
            if t - last_market > 60:
                with db.tx() as c:
                    market_tick(c)
                last_market = t
            if t - last_investments > 45:
                with db.tx() as c:
                    invest_collect(c)
                last_investments = t
            if t - last_orders > 30:
                with db.tx() as c:
                    orders_match(c)
                last_orders = t
            if t - last_treaties > 300:
                with db.tx() as c:
                    treaties_expire(c)
                last_treaties = t
            if t - last_votes > 120:
                with db.tx() as c:
                    alliance_votes_close_due(c)
                last_votes = t
            if t - last_wr_expire > 30:
                with db.tx() as c:
                    war_room_expire_check(c)
                last_wr_expire = t
            with db.tx() as c:
                world_events_tick(c)
            notification_worker_tick()
            broadcast_worker_tick()
            if t - last_backup > 3600:
                backup_database("hourly")
                last_backup = t
        except Exception as e:
            log_error("background_loop", e)
        time.sleep(3)


# ============================================================================
# ADMIN PANEL
# ============================================================================
def is_admin(uid):
    return ADMIN_ID and uid == ADMIN_ID


def view_admin_home(c, uid, chat_id, mid=None):
    if not is_admin(uid):
        return
    n_players = qv(c, "SELECT COUNT(*) FROM players", default=0)
    n_online = qv(c, "SELECT COUNT(*) FROM players WHERE last_seen>?", now() - 3600, default=0)
    n_alliances = qv(c, "SELECT COUNT(*) FROM alliances", default=0)
    errs = qv(c, "SELECT COUNT(*) FROM error_log WHERE ts>?", now() - 86400, default=0)
    maint = meta_get(c, "maintenance", "0")
    lines = [f"🛡️ پنل مدیریت {APP_NAME} v{APP_VERSION}", f"👥 بازیکنان: {n_players}   🟢 فعال (۱ساعت): {n_online}",
             f"🤝 اتحادها: {n_alliances}   🐞 خطاهای ۲۴ساعت: {errs}", f"🔧 حالت تعمیر: {'فعال ⛔' if maint == '1' else 'غیرفعال ✅'}"]
    rows = [[btn("🔎 جستجوی بازیکن", "adm.search"), btn("📊 آمار سرور", "adm.stats")],
            [btn("💰 کنترل اقتصاد", "adm.eco"), btn("🌍 رویداد جهانی", "adm.event")],
            [btn("📢 پیام همگانی", "adm.broadcast"), btn("🐞 لاگ خطاها", "adm.errors")],
            [btn("🚫 مسدودها", "adm.banned"), btn("🔧 حالت تعمیر", "adm.maint")],
            [btn("💾 پشتیبان‌گیری فوری", "adm.backup"), btn("🧪 اجرای Self-Test", "adm.selftest")]]
    (edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows)) if mid else send_view(chat_id, uid, "\n".join(lines), ikb(rows)))


@cb_route("adm")
def cb_adm(c, uid, chat_id, mid, args):
    if not is_admin(uid):
        return
    sub = args[0]
    if sub == "home":
        view_admin_home(c, uid, chat_id, mid)
    elif sub == "search":
        session_set(c, uid, "adm_search", {})
        edit_view(chat_id, mid, uid, "🔎 شناسهٔ عددی یا نام بازیکن را ارسال کنید:")
    elif sub == "player":
        view_admin_player(c, uid, chat_id, mid, int(args[1]))
    elif sub == "ban":
        target = int(args[1])
        ex(c, "UPDATE players SET banned=1, ban_reason='admin action' WHERE user_id=?", target)
        audit(c, uid, "admin_ban", str(target))
        view_admin_player(c, uid, chat_id, mid, target)
    elif sub == "unban":
        target = int(args[1])
        ex(c, "UPDATE players SET banned=0, ban_reason='' WHERE user_id=?", target)
        audit(c, uid, "admin_unban", str(target))
        view_admin_player(c, uid, chat_id, mid, target)
    elif sub == "give":
        target, res, amt = int(args[1]), args[2], float(args[3])
        add_res(c, target, res, amt)
        audit(c, uid, "admin_give", str(target), f"{res}+{amt}")
        view_admin_player(c, uid, chat_id, mid, target)
    elif sub == "take":
        target, res, amt = int(args[1]), args[2], float(args[3])
        cur = get_res(c, target, res)
        add_res(c, target, res, -min(cur, amt))
        audit(c, uid, "admin_take", str(target), f"{res}-{amt}")
        view_admin_player(c, uid, chat_id, mid, target)
    elif sub == "stats":
        view_admin_stats(c, uid, chat_id, mid)
    elif sub == "eco":
        view_admin_eco(c, uid, chat_id, mid)
    elif sub == "inflset":
        meta_set(c, "inflation", float(args[1]))
        view_admin_eco(c, uid, chat_id, mid)
    elif sub == "event":
        rows = [[btn(et["title"], f"adm.eventdo.{k}")] for k, et in EVENT_TYPES.items()]
        rows.append(back_row("adm.home"))
        edit_view(chat_id, mid, uid, "🌍 رویداد را برای فعال‌سازی دستی انتخاب کنید:", ikb(rows))
    elif sub == "eventdo":
        eid = spawn_world_event(c, args[1])
        edit_view(chat_id, mid, uid, "✅ رویداد فعال شد." if eid else "❌ ظرفیت رویدادهای فعال پر است.", ikb([back_row("adm.home")]))
    elif sub == "broadcast":
        session_set(c, uid, "adm_broadcast", {})
        edit_view(chat_id, mid, uid, "📢 متن پیام همگانی را ارسال کنید:")
    elif sub == "errors":
        view_admin_errors(c, uid, chat_id, mid)
    elif sub == "banned":
        view_admin_banned(c, uid, chat_id, mid)
    elif sub == "maint":
        cur = meta_get(c, "maintenance", "0")
        meta_set(c, "maintenance", "0" if cur == "1" else "1")
        view_admin_home(c, uid, chat_id, mid)
    elif sub == "backup":
        p = backup_database("manual")
        edit_view(chat_id, mid, uid, f"💾 پشتیبان ساخته شد: {os.path.basename(p) if p else 'خطا'}", ikb([back_row("adm.home")]))
    elif sub == "selftest":
        ok, total, fails = run_self_tests()
        txt = f"🧪 نتیجهٔ Self-Test: {ok}/{total} موفق"
        if fails:
            txt += "\n❌ " + "\n❌ ".join(fails[:10])
        edit_view(chat_id, mid, uid, txt, ikb([back_row("adm.home")]))


def view_admin_player(c, uid, chat_id, mid, target):
    tp = get_player(c, target)
    if not tp:
        return edit_view(chat_id, mid, uid, "بازیکن پیدا نشد.", ikb([back_row("adm.home")]))
    res = all_res(c, target)
    lines = [f"👤 {tp['name']} (ID {target})", f"سطح {tp['level']}  کشور {tp['country']}  اتحاد {tp['alliance_id'] or '—'}",
             f"وضعیت: {'🚫 مسدود' if tp['banned'] else '✅ فعال'}", res_line(res)]
    rows = [[btn("🚫 مسدود کردن" if not tp["banned"] else "✅ رفع مسدودیت", f"adm.{'ban' if not tp['banned'] else 'unban'}.{target}")],
            [btn("💰 +۱۰۰K پول", f"adm.give.{target}.money.100000"), btn("💠 +۵۰ جم", f"adm.give.{target}.gems.50")],
            back_row("adm.home")]
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def view_admin_stats(c, uid, chat_id, mid):
    n_players = qv(c, "SELECT COUNT(*) FROM players", default=0)
    n_cities_taken = qv(c, "SELECT COUNT(*) FROM cities WHERE governor_id!=0", default=0)
    n_battles = qv(c, "SELECT COUNT(*) FROM battles", default=0)
    n_alliances = qv(c, "SELECT COUNT(*) FROM alliances", default=0)
    tot_money = qv(c, "SELECT COALESCE(SUM(amount),0) FROM resources WHERE res='money'", default=0)
    db_size = os.path.getsize(db.path) if os.path.exists(db.path) else 0
    lines = [f"📊 آمار سرور {APP_NAME}", f"⏱️ آپ‌تایم: {dur(time.time() - BOOT_TIME)}", f"👥 بازیکنان: {n_players}   🏙️ شهرهای اشغال‌شده: {n_cities_taken}",
             f"⚔️ نبردهای ثبت‌شده: {n_battles}   🤝 اتحادها: {n_alliances}", f"💰 کل پول در گردش: {short(tot_money)}", f"💾 حجم دیتابیس: {db_size / 1024 / 1024:.2f} MB",
             f"🌐 تورم: {infl(c):.3f}", f"🛡️ امنیت — محدودیت نرخ: {SEC['rate_limited']}  تکراری: {SEC['duplicates']}  خطاها: {SEC['errors']}"]
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb([back_row("adm.home")]))


def view_admin_eco(c, uid, chat_id, mid):
    lines = [f"💰 کنترل اقتصاد", f"🌐 تورم فعلی: {infl(c):.3f}"]
    rows = [[btn("−", "adm.inflset.0.9"), btn("ریست به ۱.۰", "adm.inflset.1.0"), btn("+", "adm.inflset.1.1")], back_row("adm.home")]
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb(rows))


def view_admin_errors(c, uid, chat_id, mid):
    errs = qa(c, "SELECT * FROM error_log ORDER BY id DESC LIMIT 10")
    lines = ["🐞 آخرین خطاها:\n"] + [f"{ts_str(e['ts'])} [{e['ctx']}] {e['message'][:80]}" for e in errs]
    edit_view(chat_id, mid, uid, "\n".join(lines)[:3900], ikb([back_row("adm.home")]))


def view_admin_banned(c, uid, chat_id, mid):
    rows_q = qa(c, "SELECT * FROM players WHERE banned=1 LIMIT 20")
    lines = ["🚫 بازیکنان مسدود:\n"] + [f"{r['name']} (ID {r['user_id']})" for r in rows_q]
    if not rows_q:
        lines = ["هیچ بازیکن مسدودی نیست."]
    edit_view(chat_id, mid, uid, "\n".join(lines), ikb([back_row("adm.home")]))


def handle_admin_search_text(c, uid, chat_id, username, text):
    state, data = session_get(c, uid)
    if state != "adm_search" or not is_admin(uid):
        return False
    session_clear(c, uid)
    target = None
    if text.strip().lstrip("-").isdigit():
        target = int(text.strip())
    else:
        r = q1(c, "SELECT user_id FROM players WHERE lower(name) LIKE lower(?)", f"%{text.strip()}%")
        if r:
            target = r["user_id"]
    if not target or not get_player(c, target):
        safe_send(chat_id, "پیدا نشد.")
        return True
    view_admin_player(c, uid, chat_id, target)
    return True


def handle_admin_broadcast_text(c, uid, chat_id, username, text):
    state, data = session_get(c, uid)
    if state != "adm_broadcast" or not is_admin(uid):
        return False
    session_clear(c, uid)
    total = qv(c, "SELECT COUNT(*) FROM players WHERE banned=0", default=0)
    ex(c, "INSERT INTO broadcasts(ts,admin_id,text,total,sent) VALUES(?,?,?,?,0)", now(), uid, text, total)
    safe_send(chat_id, f"📢 پیام همگانی برای {total} بازیکن صف شد.")
    return True


register_text_flow(handle_admin_search_text)
register_text_flow(handle_admin_broadcast_text)


# ============================================================================
# SELF-TEST SUITE — اجرای واقعی روی یک دیتابیس موقت مجزا
# ============================================================================
def run_self_tests(verbose=False):
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        if verbose:
            print(("✅" if cond else "❌"), name)

    tmp_path = os.path.join(BASE_DIR, f"_selftest_{int(time.time())}.db")
    real_db = globals()["db"]
    test_db = DB(tmp_path)
    globals()["db"] = test_db
    try:
        init_database()
        with test_db.tx() as c:
            check("seed_world creates countries", seed_world(c) and qv(c, "SELECT COUNT(*) FROM countries") == len(COUNTRIES))
            check("cities generated", qv(c, "SELECT COUNT(*) FROM cities") > 50)
            c1 = q1(c, "SELECT * FROM cities WHERE owner_country='IR' AND governor_id=0 LIMIT 1")
            c2 = q1(c, "SELECT * FROM cities WHERE owner_country='IR' AND governor_id=0 AND id!=?", c1["id"])
            p1 = create_player(c, 900001, "t", "تستر یک", "IR", c1["id"], "امپراتوری آزمایشی")
            p2 = create_player(c, 900002, "t2", "تستر دو", "IR", c2["id"])
            check("player creation", p1 is not None and p1["level"] == 1)
            check("custom empire_name stored and shown", p1["empire_name"] == "امپراتوری آزمایشی" and empire_label(c, p1) == "🏰 امپراتوری آزمایشی")
            check("empire_label falls back to real country when no custom name", empire_label(c, p2).startswith(q1(c, "SELECT flag FROM countries WHERE code='IR'")["flag"]))
            auto_city = pick_auto_city(c)
            check("pick_auto_city returns a free city", auto_city is not None and auto_city["governor_id"] == 0)
            check("starting resources granted", get_res(c, 900001, "money") > 0)
            try:
                create_player(c, 900001, "t", "دوباره", "IR", c1["id"])
                check("duplicate registration blocked", False)
            except GameError:
                check("duplicate registration blocked", True)
            add_res(c, 900001, "money", 1_000_000)
            add_res(c, 900001, "iron", 50_000)
            add_res(c, 900001, "goods", 10_000)
            add_res(c, 900001, "food", 20_000)
            add_res(c, 900001, "fuel", 20_000)
            r = start_upgrade(c, 900001, "farm")
            check("building upgrade queued", q1(c, "SELECT 1 FROM build_queue WHERE user_id=?", 900001) is not None)
            ex(c, "UPDATE build_queue SET ends_at=0")
            n = complete_builds(c, 900001)
            check("building upgrade completes", n >= 1 and blevels(c, 900001)["farm"] == 2)
            try:
                spend(c, 900001, {"money": 10 ** 12})
                check("overspend blocked", False)
            except GameError:
                check("overspend blocked", True)
            before = get_res(c, 900001, "money")
            try:
                spend(c, 900001, {"money": before + 1000, "iron": 1})
            except GameError:
                pass
            check("atomic spend (partial failure doesn't deduct)", get_res(c, 900001, "money") == before)
            rifle_before = inv_count(c, 900001, "rifleman")
            start_production(c, 900001, "rifleman", 20)
            ex(c, "UPDATE prod_queue SET ends_at=0")
            complete_production(c, 900001)
            check("unit production completes", inv_count(c, 900001, "rifleman") == rifle_before + 20)
            r_before = get_res(c, 900001, "iron")
            mb = market_buy(c, 900001, "iron", 50)
            check("market buy increases stock", get_res(c, 900001, "iron") > r_before)
            ms = market_sell(c, 900001, "iron", 20)
            check("market sell returns money", ms["total"] > 0)
            offer_create(c, 900001, "food", 100, 2)
            off = q1(c, "SELECT * FROM offers WHERE seller_id=? AND status='open'", 900001)
            offer_buy(c, 900002, off["id"])
            check("player market offer trade works", q1(c, "SELECT * FROM offers WHERE id=?", off["id"])["status"] == "sold")
            start_upgrade(c, 900001, "command_center")
            ex(c, "UPDATE build_queue SET ends_at=0")
            complete_builds(c, 900001)
            start_upgrade(c, 900001, "research_center")
            ex(c, "UPDATE build_queue SET ends_at=0")
            complete_builds(c, 900001)
            check("research_center built (prereq chain)", blevels(c, 900001).get("research_center", 0) >= 1)
            start_research(c, 900001, "eco_taxation")
            ex(c, "UPDATE research_queue SET ends_at=0")
            complete_research(c, 900001)
            check("research completes and grants tech", has_tech(c, 900001, "eco_taxation"))
            for r_ in inv_stacks(c, 900001, True):
                set_deployed(c, 900001, r_["id"], r_["qty"])
            for r_ in inv_stacks(c, 900002, True):
                set_deployed(c, 900002, r_["id"], r_["qty"])
            ex(c, "UPDATE players SET shield_until=0")
            mp1 = get_res(c, 900002, "money")
            res, rep, loot = attack_player(c, 900001, 900002)
            check("battle resolves with report", isinstance(res["margin"], float) and len(rep) > 10)
            check("battle applies losses/effects consistently", get_res(c, 900002, "money") <= mp1)
            ex(c, "UPDATE players SET shield_until=0")
            wid = war_room_create(c, 900001, 900002)
            war_room_act(c, wid, 900001, "attack")
            r2 = war_room_act(c, wid, 900002, "defend")
            check("war room round resolves", r2 is not None and r2["round"] == 1)
            aid = alliance_create(c, 900001, "تست اتحاد", "TST")
            check("alliance creation", aid is not None)
            alliance_invite(c, 900001, 900002)
            alliance_invite_accept(c, 900002, aid)
            check("alliance invite/join flow", get_player(c, 900002)["alliance_id"] == aid)
            add_res(c, 900002, "money", 5000)
            alliance_contribute(c, 900002, "money", 3000)
            check("alliance treasury contribution", alliance_treasury(c, aid)["money"] == 3000)
            vid = alliance_vote_create(c, 900001, "policy", {})
            alliance_vote_cast(c, 900001, vid, 1)
            check("alliance vote cast", alliance_vote_tally(c, vid)[0] == 1)
            newrel = dip_action(c, 900001, 900002, "talks")
            check("diplomacy action changes relation", newrel != 0)
            tid = treaty_propose(c, 900001, 900002, "nap")
            treaty_respond(c, 900002, tid, True)
            check("treaty signing works", treaty_active_between(c, 900001, 900002) is not None)
            irep = intel_op(c, 900001, "player", 900002, "military")
            check("intel operation produces report", 0 < irep["accuracy"] <= 1)
            eid = spawn_world_event(c, "resource_boom")
            check("world event spawns and is active", eid is not None and len(active_events(c)) >= 1)
            check("achievements award correctly", len(check_achievements(c, 900001)) >= 0)
            ensure_missions(c, 900001)
            check("missions assigned", qv(c, "SELECT COUNT(*) FROM missions WHERE user_id=?", 900001) > 0)
            d1, s1, rw1 = daily_claim(c, 900001)
            check("daily reward claim works", d1 == 1 and s1 == 1)
            try:
                daily_claim(c, 900001)
                check("daily reward idempotency", False)
            except GameError:
                check("daily reward idempotency", True)
            check("idempotency key blocks repeat", idem(c, "selftest_key_1") and not idem(c, "selftest_key_1"))
            u1, u2 = os.urandom(4).hex(), os.urandom(4).hex()
            tok = make_token(c, 900001, "test", {"x": 1})
            take_token(c, 900001, tok, "test")
            try:
                take_token(c, 900001, tok, "test")
                check("action token single-use", False)
            except GameError:
                check("action token single-use", True)
        check("PRAGMA integrity_check passes", integrity_ok())
        bpath = backup_database("selftest")
        check("backup creation works", bpath is not None and os.path.exists(bpath))
        with test_db.tx() as c:
            with test_db.tx() as c2:
                ex(c2, "UPDATE players SET level=99 WHERE user_id=900001")
                raise GameError("force-rollback-check")
    except GameError as e:
        if "force-rollback-check" not in str(e):
            results.append(("nested transaction test", False))
    try:
        with test_db.tx() as c:
            lv = q1(c, "SELECT level FROM players WHERE user_id=900001")["level"]
            results.append(("nested transaction rollback (SAVEPOINT)", lv != 99))
    except Exception:
        results.append(("nested transaction rollback (SAVEPOINT)", False))
    finally:
        globals()["db"] = real_db
        test_db.close_all()
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(tmp_path + ext)
            except OSError:
                pass
        for f in os.listdir(BACKUP_DIR) if os.path.isdir(BACKUP_DIR) else []:
            if f.startswith("ww4_selftest_") or f.startswith("ww4_premigrate_"):
                try:
                    os.remove(os.path.join(BACKUP_DIR, f))
                except OSError:
                    pass
    ok = sum(1 for _, v in results if v)
    fails = [n for n, v in results if not v]
    if verbose:
        print(f"\n{ok}/{len(results)} tests passed.")
        if fails:
            print("FAILED:", fails)
    return ok, len(results), fails


# ============================================================================
# MAIN
# ============================================================================
def graceful_shutdown(*_a):
    log("🛑 در حال خاموش‌شدن امن...")
    STOP_FLAG["stop"] = True
    try:
        backup_database("shutdown")
    except Exception:
        pass
    try:
        db.close_all()
    except Exception:
        pass
    sys.exit(0)


def main():
    global api
    if "--selftest" in sys.argv:
        log(f"🧪 اجرای Self-Test برای {APP_NAME} v{APP_VERSION} ...")
        ok, total, fails = run_self_tests(verbose=True)
        sys.exit(0 if ok == total else 1)

    if BALE_BOT_TOKEN == "PUT_YOUR_BALE_TOKEN_HERE" or not BALE_BOT_TOKEN:
        log("❌ لطفاً ابتدا مقدار BALE_BOT_TOKEN را در بالای فایل تنظیم کنید.")
        sys.exit(1)

    log(f"🚀 {APP_NAME} v{APP_VERSION} در حال راه‌اندازی...")
    ver = init_database()
    log(f"✅ دیتابیس آماده (schema v{ver})")
    with db.tx() as c:
        if seed_world(c):
            log("🌍 جهان بازی ساخته شد (کشورها/استان‌ها/شهرها/بازار).")
        else:
            log("🌍 جهان بازی از قبل موجود است.")

    try:
        signal.signal(signal.SIGINT, graceful_shutdown)
        signal.signal(signal.SIGTERM, graceful_shutdown)
    except Exception:
        pass

    api = BaleAPI(BALE_BOT_TOKEN)
    bg = threading.Thread(target=background_loop, daemon=True)
    bg.start()
    log("🔄 حلقهٔ پس‌زمینه (بازار/سرمایه‌گذاری/رویدادها/اعلان‌ها) فعال شد.")
    log("📡 شروع دریافت پیام‌ها (long polling)...")

    executor_threads = []
    MAX_WORKERS = 8
    work_q = queue.Queue()

    def worker():
        while not STOP_FLAG["stop"]:
            try:
                u = work_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                process_update(u)
            except Exception as e:
                log_error("worker", e)
            finally:
                work_q.task_done()

    for _ in range(MAX_WORKERS):
        th = threading.Thread(target=worker, daemon=True)
        th.start()
        executor_threads.append(th)

    fail_streak = 0
    while not STOP_FLAG["stop"]:
        try:
            updates = api.get_updates(timeout=20)
            fail_streak = 0
            for u in updates:
                work_q.put(u)
        except Exception as e:
            fail_streak += 1
            log_error("poll_loop", e)
            time.sleep(min(30, 2 ** fail_streak))


if __name__ == "__main__":
    main()
