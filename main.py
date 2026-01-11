# bot.py
# POWER POINT BREAK — Official Giveaway System (Single-file, Full A-to-Z)
# Compatible with: python-telegram-bot==21.6 (async)
# Storage: SQLite (aiosqlite) — persists across restarts

import os
import re
import json
import time
import random
import asyncio
from dataclasses import dataclass
from typing import Optional, Any

import aiosqlite
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# =========================================================
# ENV / CONFIG
# =========================================================
load_dotenv()

@dataclass(frozen=True)
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
    MAIN_CHANNEL_ID: int = int(os.getenv("MAIN_CHANNEL_ID", "0"))
    GROUP_USERNAME: str = os.getenv("GROUP_USERNAME", "@PowerPointBreak").strip()
    OWNER_USERNAME: str = os.getenv("OWNER_USERNAME", "@MinexxProo").strip()
    OWNER_USER_ID: int = int(os.getenv("OWNER_USER_ID", "0"))
    ADMIN_IDS: tuple[int, ...] = tuple(
        int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
    )
    OFFICIAL_CHANNEL_USERNAME: str = os.getenv("OFFICIAL_CHANNEL_USERNAME", "@PowerPointBreak").strip()
    DB_PATH: str = os.getenv("DB_PATH", "giveaway.db").strip()

CFG = Config()
if not CFG.BOT_TOKEN:
    raise SystemExit("Missing BOT_TOKEN in environment.")
if CFG.MAIN_CHANNEL_ID == 0:
    raise SystemExit("Missing MAIN_CHANNEL_ID in environment.")
if not CFG.ADMIN_IDS and CFG.OWNER_USER_ID > 0:
    # if ADMIN_IDS not set, fallback to owner as admin
    CFG = Config(ADMIN_IDS=(CFG.OWNER_USER_ID,))


# =========================================================
# DATABASE
# =========================================================
def now_ts() -> int:
    return int(time.time())

class DB:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bans (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    reason TEXT,
                    ts INTEGER
                );

                CREATE TABLE IF NOT EXISTS giveaways (
                    giveaway_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    prize TEXT NOT NULL,
                    total_winners INTEGER NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    hosted_by TEXT NOT NULL,
                    rules TEXT NOT NULL,
                    created_ts INTEGER NOT NULL,
                    ends_ts INTEGER NOT NULL,
                    status TEXT NOT NULL,          -- DRAFT / ACTIVE / CLOSED / SELECTING / ANNOUNCED / COMPLETED
                    autodraw INTEGER NOT NULL,     -- 0/1
                    old_winner_mode TEXT NOT NULL, -- BLOCK / SKIP
                    channel_post_msg_id INTEGER,
                    close_post_msg_id INTEGER,
                    selection_post_msg_id INTEGER,
                    winners_post_msg_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS participants (
                    giveaway_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    joined_ts INTEGER NOT NULL,
                    is_first_join INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (giveaway_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS winners (
                    giveaway_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    rank INTEGER NOT NULL,         -- 0 = first join champion, 1..N others
                    delivered INTEGER NOT NULL DEFAULT 0,
                    delivered_ts INTEGER,
                    claimed_ts INTEGER,
                    PRIMARY KEY (giveaway_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS winner_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    giveaway_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    prize TEXT NOT NULL,
                    ts INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lucky_draw (
                    giveaway_id TEXT PRIMARY KEY,
                    winner_user_id INTEGER,
                    winner_username TEXT,
                    winner_ts INTEGER,
                    locked INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            await db.commit()

    async def set_setting(self, key: str, value: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await db.commit()

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cur.fetchone()
            return row[0] if row else default

    async def reset_all(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                DELETE FROM settings;
                DELETE FROM bans;
                DELETE FROM giveaways;
                DELETE FROM participants;
                DELETE FROM winners;
                DELETE FROM lucky_draw;
                """
            )
            await db.commit()

    # ---- bans ----
    async def add_ban(self, user_id: int, username: Optional[str], reason: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO bans(user_id,username,reason,ts) VALUES(?,?,?,?)",
                (user_id, username, reason, now_ts()),
            )
            await db.commit()

    async def remove_ban(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("DELETE FROM bans WHERE user_id=?", (user_id,))
            await db.commit()
            return cur.rowcount > 0

    async def is_banned(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT 1 FROM bans WHERE user_id=?", (user_id,))
            row = await cur.fetchone()
            return bool(row)

    async def list_bans(self) -> list[tuple[int, Optional[str], str, int]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT user_id,username,reason,ts FROM bans ORDER BY ts DESC")
            return await cur.fetchall()

    # ---- giveaways ----
    async def create_giveaway(self, data: dict[str, Any]):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO giveaways(
                    giveaway_id,title,prize,total_winners,duration_seconds,hosted_by,rules,
                    created_ts,ends_ts,status,autodraw,old_winner_mode,
                    channel_post_msg_id,close_post_msg_id,selection_post_msg_id,winners_post_msg_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    data["giveaway_id"],
                    data["title"],
                    data["prize"],
                    data["total_winners"],
                    data["duration_seconds"],
                    data["hosted_by"],
                    data["rules"],
                    data["created_ts"],
                    data["ends_ts"],
                    data["status"],
                    data["autodraw"],
                    data["old_winner_mode"],
                    data.get("channel_post_msg_id"),
                    data.get("close_post_msg_id"),
                    data.get("selection_post_msg_id"),
                    data.get("winners_post_msg_id"),
                ),
            )
            await db.commit()

    async def update_giveaway_fields(self, giveaway_id: str, **fields):
        if not fields:
            return
        keys = list(fields.keys())
        vals = [fields[k] for k in keys]
        sets = ", ".join([f"{k}=?" for k in keys])
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"UPDATE giveaways SET {sets} WHERE giveaway_id=?", (*vals, giveaway_id))
            await db.commit()

    async def get_giveaway(self, giveaway_id: str) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (giveaway_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_latest_giveaway(self) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM giveaways ORDER BY created_ts DESC LIMIT 1")
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_active_giveaways(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM giveaways WHERE status='ACTIVE' ORDER BY created_ts DESC")
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ---- participants ----
    async def count_participants(self, giveaway_id: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM participants WHERE giveaway_id=?", (giveaway_id,))
            (n,) = await cur.fetchone()
            return int(n)

    async def add_participant(self, giveaway_id: str, user_id: int, username: Optional[str], is_first: bool) -> bool:
        async with aiosqlite.connect(self.path) as db:
            try:
                await db.execute(
                    "INSERT INTO participants(giveaway_id,user_id,username,joined_ts,is_first_join) VALUES(?,?,?,?,?)",
                    (giveaway_id, user_id, username, now_ts(), 1 if is_first else 0),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def get_participant(self, giveaway_id: str, user_id: int) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM participants WHERE giveaway_id=? AND user_id=?",
                (giveaway_id, user_id),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_participants(self, giveaway_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM participants WHERE giveaway_id=? ORDER BY joined_ts ASC",
                (giveaway_id,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ---- winners ----
    async def add_winner(self, giveaway_id: str, user_id: int, username: Optional[str], rank: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO winners(giveaway_id,user_id,username,rank,delivered,delivered_ts,claimed_ts)
                VALUES(?,?,?,?,COALESCE((SELECT delivered FROM winners WHERE giveaway_id=? AND user_id=?),0),
                       (SELECT delivered_ts FROM winners WHERE giveaway_id=? AND user_id=?),
                       (SELECT claimed_ts FROM winners WHERE giveaway_id=? AND user_id=?))
                """,
                (giveaway_id, user_id, username, rank,
                 giveaway_id, user_id, giveaway_id, user_id, giveaway_id, user_id),
            )
            await db.commit()

    async def list_winners(self, giveaway_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM winners WHERE giveaway_id=? ORDER BY rank ASC",
                (giveaway_id,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def set_delivered(self, giveaway_id: str, user_id: int, delivered: bool):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE winners SET delivered=?, delivered_ts=? WHERE giveaway_id=? AND user_id=?",
                (1 if delivered else 0, now_ts() if delivered else None, giveaway_id, user_id),
            )
            await db.commit()

    async def set_claimed_ts(self, giveaway_id: str, user_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE winners SET claimed_ts=? WHERE giveaway_id=? AND user_id=?",
                (now_ts(), giveaway_id, user_id),
            )
            await db.commit()

    # ---- history ----
    async def insert_winner_history(self, giveaway_id: str, user_id: int, username: Optional[str], prize: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO winner_history(giveaway_id,user_id,username,prize,ts) VALUES(?,?,?,?,?)",
                (giveaway_id, user_id, username, prize, now_ts()),
            )
            await db.commit()

    async def list_winner_history(self, limit: int = 50) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM winner_history ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ---- lucky draw ----
    async def lucky_init(self, giveaway_id: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO lucky_draw(giveaway_id,locked) VALUES(?,0)", (giveaway_id,))
            await db.commit()

    async def lucky_get(self, giveaway_id: str) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM lucky_draw WHERE giveaway_id=?", (giveaway_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def lucky_set_winner(self, giveaway_id: str, user_id: int, username: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                UPDATE lucky_draw
                SET winner_user_id=?, winner_username=?, winner_ts=?, locked=1
                WHERE giveaway_id=? AND (winner_user_id IS NULL) AND locked=0
                """,
                (user_id, username, now_ts(), giveaway_id),
            )
            await db.commit()
            return cur.rowcount > 0


db = DB(CFG.DB_PATH)

# =========================================================
# TEXT TEMPLATES (FULL)
# =========================================================
def fmt_mmss(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    m = seconds // 60
    s = seconds % 60
    return f"{m:02d}:{s:02d}"

def progress_bar(pct: int, blocks: int = 10) -> str:
    pct = max(0, min(100, pct))
    filled = round((pct / 100) * blocks)
    filled = max(0, min(blocks, filled))
    return "▰" * filled + "▱" * (blocks - filled)

def giveaway_post(
    title: str,
    prize: str,
    participants: int,
    winners: int,
    time_remaining: str,
    progress: str,
    rules: str,
    hosted_by: str,
) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{title}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"🏆 {prize}\n\n"
        f"👥 Total Participants: {participants}  \n"
        f"🏆 Total Winners: {winners}  \n\n"
        "🎯 Winner Selection\n"
        "• 100% Random & Fair  \n"
        "• Auto System  \n\n"
        f"⏱️ Time Remaining: {time_remaining}  \n"
        "📊 Live Progress\n"
        f"{progress}  \n\n"
        "📜 Official Rules  \n"
        f"{rules}\n\n"
        f"📢 Hosted by: {hosted_by}  \n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Tap below to join the giveaway 👇  \n"
    )

def giveaway_closed_post(prize: str, participants: int, winners: int) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway has officially ended.  \n"
        "🔒 All entries are now locked.\n\n"
        "📊 Giveaway Summary  \n"
        f"🎁 Prize: {prize}  \n\n"
        f"👥 Total Participants: {participants}  \n"
        f"🏆 Total Winners: {winners}  \n\n"
        "🎯 Winners will be announced very soon.  \n"
        "Please stay tuned for the final results.\n\n"
        "✨ Best of luck to everyone!\n\n"
        "— POWER POINT BREAK ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

def selection_post(
    hosted_title: str,
    prize: str,
    winners_selected: int,
    total_winners: int,
    pct: int,
    bar: str,
    time_remaining: str,
    show_lines: list[str],
) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 LIVE RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ {hosted_title} ⚡\n\n"
        "🎁 GIVEAWAY SUMMARY  \n"
        f"🏆 Prize: {prize}  \n"
        f"✅ Winners Selected: {winners_selected}/{total_winners}\n\n"
        "📌 Important Rule  \n"
        "Users without a valid @username  \n"
        "are automatically excluded.\n\n"
        f"⏳ Selection Progress: {pct}%  \n"
        f"📊 Progress Bar: {bar}  \n\n"
        f"🕒 Time Remaining: {time_remaining}  \n"
        "🔐 System Mode: 100% Random • Fair • Auto  \n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👥 LIVE ENTRIES SHOWCASE\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(show_lines)
        + "\n━━━━━━━━━━━━━━━━━━━━"
    )

def entry_rule_popup() -> str:
    return (
        "📌 ENTRY RULE\n"
        "• Tap 🍀 Try Your Luck at the right moment\n"
        "• First click wins instantly (Lucky Draw)\n"
        "• Must have a valid @username\n"
        "• Winner is added live to the selection post\n"
        "• 100% fair: first-come-first-win"
    )

def try_luck_not_time() -> str:
    return (
        "⏳ NOT YET\n\n"
        "The Lucky Draw window has not started yet.\n"
        "Please wait until the exact time.\n\n"
        "Keep watching the live selection."
    )

def lucky_no_participants() -> str:
    return (
        "⚠️ NO ENTRIES FOUND\n\n"
        "No one joined this giveaway yet.\n"
        "Lucky Draw cannot be used right now.\n\n"
        "Please wait for the final winners announcement."
    )

def too_late_popup(winner_username: str, winner_id: int) -> str:
    return (
        "⚠️ TOO LATE\n\n"
        "Someone already won the Lucky Draw slot.\n"
        "Please continue watching the live selection.\n\n"
        f"👤 {winner_username}\n"
        f"🆔 {winner_id}"
    )

def lucky_winner_popup(username: str, user_id: int) -> str:
    return (
        "🌟 CONGRATULATIONS!\n"
        "You won the 🍀 Lucky Draw Winner slot ✅\n\n"
        f"👤 {username}\n"
        f"🆔 {user_id}\n"
        "📸 Please take a screenshot\n"
        "and post it in the group\n"
        "to confirm 👈\n\n"
        "🏆 Added to winners list LIVE!"
    )

def popup_already_joined() -> str:
    return (
        "✅ ALREADY JOINED\n\n"
        "You have already joined this giveaway.\n"
        "Please wait for the final results."
    )

def popup_join_success() -> str:
    return (
        "✅ JOINED SUCCESSFULLY\n\n"
        "Your entry has been recorded.\n"
        "Good luck!"
    )

def popup_first_join(username: str, user_id: int, group_username: str) -> str:
    return (
        "🥇 FIRST JOIN CHAMPION 🌟\n"
        "Congratulations! You joined\n"
        "the giveaway FIRST and secured\n"
        f"👤 {username}\n"
        f"🆔 {user_id}\n"
        "📸 Please take a screenshot\n"
        "and post it in the group\n"
        f"to confirm 👈 ({group_username})"
    )

def popup_not_winner() -> str:
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry! Your User ID is not\n"
        "in the winners list.\n\n"
        "Please wait for the next\n"
        "giveaway ❤️‍🩹"
    )

def popup_prize_delivered(username: str, user_id: int, owner: str) -> str:
    return (
        "📦 PRIZE ALREADY DELIVERED\n"
        "Your prize has already been\n"
        "successfully delivered ✅\n"
        f"👤 {username}\n"
        f"🆔 {user_id}\n"
        "If you face any issue,\n"
        f"contact admin 👉 {owner}"
    )

def popup_claim_ok(username: str, user_id: int, owner: str) -> str:
    return (
        "🎉 PRIZE CLAIM REQUEST RECEIVED\n\n"
        "Your claim has been recorded.\n"
        "Please contact admin to receive your prize:\n"
        f"👉 {owner}\n\n"
        f"👤 {username}\n"
        f"🆔 {user_id}"
    )

def popup_expired(owner: str) -> str:
    return (
        "⌛ PRIZE EXPIRED\n\n"
        "The 24-hour claim window has ended.\n"
        "This prize slot is now expired.\n\n"
        f"If you think this is a mistake,\ncontact admin 👉 {owner}"
    )

def popup_giveaway_completed(owner: str) -> str:
    return (
        "✅ GIVEAWAY COMPLETED\n\n"
        "This giveaway has been completed.\n"
        f"If you have any issues, please contact admin 👉 {owner}"
    )

def admin_welcome() -> str:
    return (
        "👋 Welcome, Admin!\n\n"
        "You have successfully started the Giveaway Management Bot.\n\n"
        "From here, you can:\n"
        "• Create and manage giveaways\n"
        "• Control auto / manual winner selection\n"
        "• Review winners and delivery status\n"
        "• Access advanced admin commands\n\n"
        "Use the admin panel to get started.\n"
        "If you need help at any time, use /panel\n\n"
        "🚀 Let’s run a perfect giveaway!"
    )

def unauthorized_notice(username: str, user_id: int, official_channel: str, owner: str) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ UNAUTHORIZED NOTICE\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Hi there!  \n"
        f"Username: {username}\n"
        f"UserID: {user_id}\n\n"
        "It looks like you tried to start the giveaway,\n"
        "but this action is available for admins only.\n\n"
        "😊 No worries — this is just a friendly heads-up.\n\n"
        "🎁 This is an official Giveaway Bot.  \n"
        "For exciting giveaway updates,  \n"
        "join our official channel now:  \n"
        f"👉 {official_channel}\n\n"
        "🤖 Powered by:\n"
        "Power Point Break — Official Giveaway System\n\n"
        "👤 Bot Owner:\n"
        f"{owner}\n\n"
        "If you think this was a mistake,\n"
        "please feel free to contact an admin anytime.\n"
        "We’re always happy to help!\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

def winner_header_line() -> str:
    return "🏆 GIVEAWAY WINNER ANNOUNCEMENT 🏆"

def winners_post(
    hosted_by: str,
    giveaway_id: str,
    prize: str,
    delivery_done: int,
    delivery_total: int,
    first_username: str,
    first_id: int,
    other_lines: list[str],
) -> str:
    return (
        f"{winner_header_line()}\n\n"
        f"{hosted_by}\n\n"
        f"🆔 Giveaway ID: {giveaway_id}\n\n"
        f"🎁 PRIZE:\n{prize}\n\n"
        f"📦 Prize Delivery: {delivery_done}/{delivery_total}\n\n"
        "🥇 ⭐ FIRST JOIN CHAMPION ⭐\n"
        f"👑 {first_username}\n"
        f"🆔 {first_id}\n\n"
        "👑 OTHER WINNERS\n"
        + "\n".join(other_lines)
        + "\n\n👇 Click the button below to claim your prize\n\n"
        "⏳ Rule: Claim within 24 hours — after that, prize expires."
    )


# =========================================================
# UTILS
# =========================================================
GIVEAWAY_ID_RE = re.compile(r"^P\d{3}-P\d{3}-B\d{4}$", re.IGNORECASE)

def gen_giveaway_id() -> str:
    p1 = random.randint(100, 999)
    p2 = random.randint(100, 999)
    b = random.randint(1000, 9999)
    return f"P{p1}-P{p2}-B{b}"

def parse_duration(text: str) -> Optional[int]:
    t = text.strip().lower()
    m = re.match(r"^(\d+)\s*(second|seconds|minute|minutes|hour|hours)$", t)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2)
    if "second" in unit:
        return val
    if "minute" in unit:
        return val * 60
    if "hour" in unit:
        return val * 3600
    return None

def parse_user_lines(text: str) -> list[tuple[Optional[str], int]]:
    # Accept:
    # 123456789
    # @name | 123456789
    out: list[tuple[Optional[str], int]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "|" in line:
            left, right = [x.strip() for x in line.split("|", 1)]
            if not right.isdigit():
                continue
            if left and not left.startswith("@"):
                left = "@" + left
            out.append((left or None, int(right)))
        else:
            if line.isdigit():
                out.append((None, int(line)))
    return out

def parse_delivered_lines(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "|" not in line:
            raise ValueError(f"Invalid line (missing '|'): {line}")
        left, right = [x.strip() for x in line.split("|", 1)]
        if not left.startswith("@"):
            raise ValueError(f"Invalid username (must start with @): {line}")
        if not right.isdigit():
            raise ValueError(f"Invalid user_id (must be digits): {line}")
        out.append((left, int(right)))
    return out

def pick_three_distinct_colors() -> list[str]:
    colors = ["🟡", "🟠", "⚫", "🟣", "🔵", "🟢", "🟤", "⚪", "🔴"]
    random.shuffle(colors)
    return colors[:3]

# =========================================================
# KEYBOARDS
# =========================================================
def is_admin(user_id: int) -> bool:
    return user_id in CFG.ADMIN_IDS

def kb_admin_panel():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ New Giveaway", callback_data="ADMIN_NEWGIVEAWAY")],
            [InlineKeyboardButton("🎲 Manual Draw", callback_data="ADMIN_DRAW")],
            [InlineKeyboardButton("⚙️ AutoDraw ON/OFF", callback_data="ADMIN_AUTODRAW")],
            [InlineKeyboardButton("📦 Prize Delivery", callback_data="ADMIN_PRIZEDELIVERY")],
            [InlineKeyboardButton("📜 Winner List", callback_data="ADMIN_WINNERLIST")],
            [InlineKeyboardButton("🧹 Reset Bot", callback_data="ADMIN_RESET")],
        ]
    )

def kb_autodraw_toggle(is_on: bool):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Auto Draw ON" if not is_on else "✅ Auto Draw ON (Active)", callback_data="AUTODRAW_ON"),
                InlineKeyboardButton("⛔ Auto Draw OFF" if is_on else "⛔ Auto Draw OFF (Active)", callback_data="AUTODRAW_OFF"),
            ]
        ]
    )

def kb_join(giveaway_id: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data=f"JOIN|{giveaway_id}")]]
    )

def kb_claim(giveaway_id: str, claim_post_slot: int = 1):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data=f"CLAIM|{giveaway_id}|{claim_post_slot}")]]
    )

def kb_selection_buttons(giveaway_id: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🍀 Try Your Luck", callback_data=f"TRYLUCK|{giveaway_id}"),
                InlineKeyboardButton("📌 Entry Rule", callback_data=f"ENTRYRULE|{giveaway_id}"),
            ]
        ]
    )

# =========================================================
# SAFE EDIT
# =========================================================
async def safe_edit_message(app: Application, chat_id: int, message_id: int, text: str, reply_markup=None):
    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        raise

# =========================================================
# CLAIM POSTS (KEEP LAST 5)
# =========================================================
CLAIM_SLOTS_KEY = "claim_slots_v1"

async def load_claim_slots() -> list[dict]:
    raw = await db.get_setting(CLAIM_SLOTS_KEY, "[]")
    try:
        arr = json.loads(raw)
        if isinstance(arr, list):
            return [x for x in arr if isinstance(x, dict)]
    except Exception:
        pass
    return []

async def save_claim_slots(arr: list[dict]):
    await db.set_setting(CLAIM_SLOTS_KEY, json.dumps(arr, ensure_ascii=False))

def build_claim_post_text(hosted_by: str, giveaway_id: str, prize: str) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🏆 CLAIM PRIZE CENTER\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📢 Hosted By: {hosted_by}\n"
        f"🆔 Giveaway ID: {giveaway_id}\n\n"
        "🎁 Prize:\n"
        f"{prize}\n\n"
        "👇 Tap the button below to claim your prize\n"
        "⏳ Claim within 24 hours.\n\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

async def create_claim_post_and_keep_last_5(context: ContextTypes.DEFAULT_TYPE, giveaway_id: str):
    g = await db.get_giveaway(giveaway_id)
    if not g:
        return

    txt = build_claim_post_text(g["hosted_by"], giveaway_id, g["prize"])
    msg = await context.application.bot.send_message(
        chat_id=CFG.MAIN_CHANNEL_ID,
        text=txt,
        reply_markup=kb_claim(giveaway_id, claim_post_slot=1),
        disable_web_page_preview=True,
    )

    slots = await load_claim_slots()
    slots.insert(0, {"giveaway_id": giveaway_id, "message_id": msg.message_id, "ts": now_ts()})

    # remove duplicates by giveaway_id (keep newest)
    seen = set()
    cleaned = []
    for it in slots:
        gid = it.get("giveaway_id")
        if not gid or gid in seen:
            continue
        seen.add(gid)
        cleaned.append(it)
    slots = cleaned

    # keep only 5
    to_delete = slots[5:]
    slots = slots[:5]

    for it in to_delete:
        mid = it.get("message_id")
        if mid:
            try:
                await context.application.bot.delete_message(chat_id=CFG.MAIN_CHANNEL_ID, message_id=int(mid))
            except Exception:
                pass

    await save_claim_slots(slots)

# =========================================================
# STATE (ADMIN FLOWS)
# =========================================================
STATE_NEW: dict[int, dict[str, Any]] = {}
STATE_DELIVERY: dict[int, dict[str, Any]] = {}
STATE_RESET: dict[int, dict[str, Any]] = {}
STATE_BLOCKWAIT: dict[int, bool] = {}

# =========================================================
# OWNER UNAUTHORIZED NOTIFY
# =========================================================
async def owner_notify_unauthorized(user, app: Application):
    if CFG.OWNER_USER_ID <= 0:
        return
    username = f"@{user.username}" if user.username else "(no username)"
    msg = (
        "⚠️ Unauthorized bot start attempt detected.\n\n"
        f"Username: {username}\n"
        f"UserID: {user.id}"
    )
    try:
        await app.bot.send_message(chat_id=CFG.OWNER_USER_ID, text=msg)
    except Exception:
        pass

# =========================================================
# JOBS (LIVE COUNTDOWN + CLOSE)
# =========================================================
def job_name_tick(gid: str) -> str:
    return f"GW_TICK|{gid}"

def job_name_close(gid: str) -> str:
    return f"GW_CLOSE|{gid}"

async def schedule_giveaway_jobs(app: Application, giveaway_id: str):
    g = await db.get_giveaway(giveaway_id)
    if not g or g["status"] != "ACTIVE":
        return

    # Remove old jobs with same names (best effort)
    for j in list(app.job_queue.jobs()):
        if j.name in (job_name_tick(giveaway_id), job_name_close(giveaway_id)):
            try:
                j.schedule_removal()
            except Exception:
                pass

    # Tick update every 5 seconds
    app.job_queue.run_repeating(
        giveaway_tick_job,
        interval=5,
        first=0,
        name=job_name_tick(giveaway_id),
        data={"giveaway_id": giveaway_id},
    )

    # Close at remaining time
    remaining = int(g["ends_ts"]) - now_ts()
    if remaining < 0:
        remaining = 0
    app.job_queue.run_once(
        giveaway_close_job,
        when=remaining,
        name=job_name_close(giveaway_id),
        data={"giveaway_id": giveaway_id},
    )

async def giveaway_tick_job(context: ContextTypes.DEFAULT_TYPE):
    gid = context.job.data["giveaway_id"]
    g = await db.get_giveaway(gid)
    if not g or g["status"] != "ACTIVE":
        try:
            context.job.schedule_removal()
        except Exception:
            pass
        return
    await refresh_join_post(context, gid)

async def giveaway_close_job(context: ContextTypes.DEFAULT_TYPE):
    gid = context.job.data["giveaway_id"]
    await close_giveaway_and_maybe_start_selection(context, gid, forced=False)

# =========================================================
# REFRESH JOIN POST (LIVE)
# =========================================================
async def refresh_join_post(context: ContextTypes.DEFAULT_TYPE, giveaway_id: str):
    g = await db.get_giveaway(giveaway_id)
    if not g or not g.get("channel_post_msg_id"):
        return

    participants = await db.count_participants(giveaway_id)
    now = now_ts()
    remaining = int(g["ends_ts"]) - now
    total = int(g["duration_seconds"]) if int(g["duration_seconds"]) > 0 else 1
    elapsed = total - max(0, remaining)
    pct = int((elapsed / total) * 100)
    bar = progress_bar(pct, 10)

    rules_lines = [x.strip() for x in (g["rules"] or "").splitlines() if x.strip()]
    rules = "• " + "\n• ".join(rules_lines) if rules_lines else "• Admin decision is final & binding"

    text = giveaway_post(
        title=g["title"],
        prize=g["prize"],
        participants=participants,
        winners=int(g["total_winners"]),
        time_remaining=fmt_mmss(remaining),
        progress=bar,
        rules=rules,
        hosted_by=g["hosted_by"],
    )

    await safe_edit_message(
        context.application,
        chat_id=CFG.MAIN_CHANNEL_ID,
        message_id=int(g["channel_post_msg_id"]),
        text=text,
        reply_markup=kb_join(giveaway_id),
    )

# =========================================================
# SELECTION ENGINE (10 MINUTES, 5/7/9 SHOW, RANDOM WINNERS)
# =========================================================
async def start_selection(context: ContextTypes.DEFAULT_TYPE, giveaway_id: str, manual_flow: bool):
    g = await db.get_giveaway(giveaway_id)
    if not g:
        return

    await db.update_giveaway_fields(giveaway_id, status="SELECTING")

    participants = await db.list_participants(giveaway_id)

    # Username required
    eligible = [p for p in participants if p.get("username")]

    # Old winner SKIP mode
    if g["old_winner_mode"] == "SKIP":
        hist = await db.list_winner_history(limit=2000)
        old_ids = {h["user_id"] for h in hist}
        eligible = [p for p in eligible if p["user_id"] not in old_ids]

    # Strict cycle list (one-by-one)
    cycle = [{"user_id": p["user_id"], "username": p["username"]} for p in eligible]

    duration = 10 * 60
    sel_end_ts = now_ts() + duration
    await db.set_setting(f"sel_end:{giveaway_id}", str(sel_end_ts))
    await db.set_setting(f"manual_flow:{giveaway_id}", "1" if manual_flow else "0")
    await db.lucky_init(giveaway_id)

    # initial selection post
    show_lines = [
        "🟡 Now Showing → @username | 🆔 0000000000  ",
        "🟠 Now Showing → @username | 🆔 0000000000  ",
        "⚫ Now Showing → @username | 🆔 0000000000  ",
    ]
    text = selection_post(
        hosted_title=g["hosted_by"],
        prize=g["prize"],
        winners_selected=0,
        total_winners=int(g["total_winners"]),
        pct=0,
        bar=progress_bar(0, 10),
        time_remaining=fmt_mmss(duration),
        show_lines=show_lines,
    )
    msg = await context.application.bot.send_message(
        chat_id=CFG.MAIN_CHANNEL_ID,
        text=text,
        reply_markup=kb_selection_buttons(giveaway_id),
        disable_web_page_preview=True,
    )
    try:
        await context.application.bot.pin_chat_message(
            chat_id=CFG.MAIN_CHANNEL_ID,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except Exception:
        pass

    await db.update_giveaway_fields(giveaway_id, selection_post_msg_id=msg.message_id)

    # run loop as background task
    context.application.create_task(selection_loop(context, giveaway_id, cycle, sel_end_ts))

async def selection_loop(context: ContextTypes.DEFAULT_TYPE, giveaway_id: str, cycle: list[dict], sel_end_ts: int):
    idx = 0

    last1 = 0
    last2 = 0
    last3 = 0
    row1 = None
    row2 = None
    row3 = None

    total = 10 * 60

    while True:
        now = now_ts()
        remaining = sel_end_ts - now
        if remaining <= 0:
            break

        elapsed = total - remaining
        pct = int((elapsed / total) * 100)
        bar = progress_bar(pct, 10)

        # rotate rows
        if now - last1 >= 5:
            row1, idx = next_cycle_item(cycle, idx)
            last1 = now
        if now - last2 >= 7:
            row2, idx = next_cycle_item(cycle, idx)
            last2 = now
        if now - last3 >= 9:
            row3, idx = next_cycle_item(cycle, idx)
            last3 = now

        colors = pick_three_distinct_colors()
        show_lines = []
        if row1:
            show_lines.append(f"{colors[0]} Now Showing → {row1[0]} | 🆔 {row1[1]}  ")
        if row2:
            show_lines.append(f"{colors[1]} Now Showing → {row2[0]} | 🆔 {row2[1]}  ")
        if row3:
            show_lines.append(f"{colors[2]} Now Showing → {row3[0]} | 🆔 {row3[1]}  ")

        # keep exactly 3 lines in display (if cycle empty, show placeholders)
        while len(show_lines) < 3:
            pad = pick_three_distinct_colors()[0]
            show_lines.append(f"{pad} Now Showing → @username | 🆔 0000000000  ")

        # pick winners progressively with real random timing
        await maybe_pick_next_winner(context, giveaway_id)

        await refresh_selection_post(
            context,
            giveaway_id,
            pct=pct,
            bar=bar,
            remaining=remaining,
            show_lines=show_lines,
        )

        await asyncio.sleep(1)

    await finish_selection(context, giveaway_id)

def next_cycle_item(cycle: list[dict], idx: int):
    if not cycle:
        return None, idx
    if idx >= len(cycle):
        idx = 0
    item = cycle[idx]
    idx += 1
    return (item["username"], int(item["user_id"])), idx

async def maybe_pick_next_winner(context: ContextTypes.DEFAULT_TYPE, giveaway_id: str):
    g = await db.get_giveaway(giveaway_id)
    if not g:
        return

    winners = await db.list_winners(giveaway_id)

    # ensure first join champion (rank 0) if exists AND has username
    participants = await db.list_participants(giveaway_id)
    first = next((p for p in participants if int(p.get("is_first_join", 0)) == 1 and p.get("username")), None)
    if first:
        exists = any(w["rank"] == 0 and w["user_id"] == first["user_id"] for w in winners)
        if not exists:
            await db.add_winner(giveaway_id, int(first["user_id"]), first["username"], rank=0)
            await db.insert_winner_history(giveaway_id, int(first["user_id"]), first["username"], g["prize"])

    winners = await db.list_winners(giveaway_id)

    # total winners for OTHER winners = total_winners
    other = [w for w in winners if int(w["rank"]) >= 1]
    if len(other) >= int(g["total_winners"]):
        return

    # random timing probability per second (finishes naturally within 10 minutes)
    target = max(1, int(g["total_winners"]))
    p = min(0.30, max(0.03, target / 600))

    if random.random() > p:
        return

    participants = await db.list_participants(giveaway_id)

    eligible = [p for p in participants if p.get("username")]  # username required
    if g["old_winner_mode"] == "SKIP":
        hist = await db.list_winner_history(limit=5000)
        old_ids = {h["user_id"] for h in hist}
        eligible = [p for p in eligible if int(p["user_id"]) not in old_ids]

    w_ids = {int(w["user_id"]) for w in winners}
    eligible = [p for p in eligible if int(p["user_id"]) not in w_ids]
    if not eligible:
        return

    pick = random.choice(eligible)
    next_rank = max([int(w["rank"]) for w in winners], default=0) + 1
    await db.add_winner(giveaway_id, int(pick["user_id"]), pick["username"], rank=next_rank)
    await db.insert_winner_history(giveaway_id, int(pick["user_id"]), pick["username"], g["prize"])

async def refresh_selection_post(
    context: ContextTypes.DEFAULT_TYPE,
    giveaway_id: str,
    pct: int,
    bar: str,
    remaining: int,
    show_lines: list[str],
):
    g = await db.get_giveaway(giveaway_id)
    if not g or not g.get("selection_post_msg_id"):
        return

    winners = await db.list_winners(giveaway_id)
    winners_selected = len([w for w in winners if int(w["rank"]) >= 1])
    total_winners = int(g["total_winners"])

    text = selection_post(
        hosted_title=g["hosted_by"],
        prize=g["prize"],
        winners_selected=winners_selected,
        total_winners=total_winners,
        pct=pct,
        bar=bar,
        time_remaining=fmt_mmss(max(0, remaining)),
        show_lines=show_lines,
    )

    await safe_edit_message(
        context.application,
        chat_id=CFG.MAIN_CHANNEL_ID,
        message_id=int(g["selection_post_msg_id"]),
        text=text,
        reply_markup=kb_selection_buttons(giveaway_id),
    )

async def finish_selection(context: ContextTypes.DEFAULT_TYPE, giveaway_id: str):
    manual_flow = (await db.get_setting(f"manual_flow:{giveaway_id}", "0")) == "1"

    if manual_flow:
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ APPROVE & POST WINNERS", callback_data=f"MANUALAPPROVE|{giveaway_id}"),
                    InlineKeyboardButton("❌ REJECT", callback_data=f"MANUALREJECT|{giveaway_id}"),
                ]
            ]
        )
        admin_id = CFG.ADMIN_IDS[0]
        await context.application.bot.send_message(
            chat_id=admin_id,
            text=f"Manual selection finished for Giveaway ID: {giveaway_id}\n\nChoose what to do:",
            reply_markup=kb,
        )
        return

    await post_winners_and_cleanup(context, giveaway_id)

# =========================================================
# WINNERS POST + CLEANUP + CLAIM POSTS
# =========================================================
async def build_winners_post_text_and_kb(giveaway_id: str):
    g = await db.get_giveaway(giveaway_id)
    winners = await db.list_winners(giveaway_id)

    first = next((w for w in winners if int(w["rank"]) == 0), None)
    if first:
        first_username = first["username"] or "@firstuser"
        first_id = int(first["user_id"])
    else:
        # fallback: show first eligible or 0
        first_username = "@firstuser"
        first_id = 0

    others = [w for w in winners if int(w["rank"]) >= 1]
    others.sort(key=lambda x: int(x["rank"]))

    delivery_done = len([w for w in others if int(w.get("delivered", 0)) == 1])
    delivery_total = len(others)

    other_lines = []
    for i, w in enumerate(others, start=1):
        uname = w["username"] or "@user"
        status = "✅ Delivered" if int(w.get("delivered", 0)) == 1 else "⏳ Pending"
        other_lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {int(w['user_id'])} | {status}")

    text = winners_post(
        hosted_by=g["hosted_by"],
        giveaway_id=giveaway_id,
        prize=g["prize"],
        delivery_done=delivery_done,
        delivery_total=delivery_total if delivery_total > 0 else 0,
        first_username=first_username,
        first_id=first_id,
        other_lines=other_lines if other_lines else ["1️⃣ 👤 @user | 🆔 0000000000 | ⏳ Pending"],
    )
    return text, kb_claim(giveaway_id, 1)

async def post_winners_and_cleanup(context: ContextTypes.DEFAULT_TYPE, giveaway_id: str):
    g = await db.get_giveaway(giveaway_id)
    if not g:
        return

    # delete close + selection posts
    if g.get("close_post_msg_id"):
        try:
            await context.application.bot.delete_message(chat_id=CFG.MAIN_CHANNEL_ID, message_id=int(g["close_post_msg_id"]))
        except Exception:
            pass
    if g.get("selection_post_msg_id"):
        try:
            await context.application.bot.delete_message(chat_id=CFG.MAIN_CHANNEL_ID, message_id=int(g["selection_post_msg_id"]))
        except Exception:
            pass

    txt, kb = await build_winners_post_text_and_kb(giveaway_id)
    msg = await context.application.bot.send_message(
        chat_id=CFG.MAIN_CHANNEL_ID,
        text=txt,
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    try:
        await context.application.bot.pin_chat_message(chat_id=CFG.MAIN_CHANNEL_ID, message_id=msg.message_id, disable_notification=True)
    except Exception:
        pass

    await db.update_giveaway_fields(giveaway_id, winners_post_msg_id=msg.message_id, status="ANNOUNCED")

    # create separate claim post and keep only last 5
    await create_claim_post_and_keep_last_5(context, giveaway_id)

# =========================================================
# CLOSE GIVEAWAY
# =========================================================
async def close_giveaway_and_maybe_start_selection(context: ContextTypes.DEFAULT_TYPE, giveaway_id: str, forced: bool):
    g = await db.get_giveaway(giveaway_id)
    if not g:
        return
    if g["status"] != "ACTIVE":
        return

    # mark closed
    await db.update_giveaway_fields(giveaway_id, status="CLOSED")

    # post close summary
    participants_count = await db.count_participants(giveaway_id)
    close_text = giveaway_closed_post(g["prize"], participants_count, int(g["total_winners"]))
    msg = await context.application.bot.send_message(
        chat_id=CFG.MAIN_CHANNEL_ID,
        text=close_text,
        disable_web_page_preview=True,
    )
    await db.update_giveaway_fields(giveaway_id, close_post_msg_id=msg.message_id)

    # If AutoDraw ON -> start selection automatically
    g2 = await db.get_giveaway(giveaway_id)
    if g2 and int(g2["autodraw"]) == 1:
        await start_selection(context, giveaway_id, manual_flow=False)

# =========================================================
# COMMANDS
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_admin(user.id):
        username = f"@{user.username}" if user.username else "(no username)"
        await update.message.reply_text(
            unauthorized_notice(username=username, user_id=user.id, official_channel=CFG.OFFICIAL_CHANNEL_USERNAME, owner=CFG.OWNER_USERNAME)
        )
        await owner_notify_unauthorized(user, context.application)
        return

    await update.message.reply_text(admin_welcome(), reply_markup=kb_admin_panel())

async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_admin(user.id):
        return
    await update.message.reply_text("✅ Admin Panel opened.", reply_markup=kb_admin_panel())

async def cmd_newgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_admin(user.id):
        return
    STATE_NEW[user.id] = {"step": 1}
    await update.message.reply_text(
        "Send Giveaway Title (single line).\n"
        "Example:\n"
        "⚡ POWER POINT BREAK GIVEAWAY ⚡"
    )

async def cmd_autodraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_admin(user.id):
        return
    g = await db.get_latest_giveaway()
    if not g:
        await update.message.reply_text("No giveaways found.")
        return
    is_on = bool(int(g["autodraw"]))
    await update.message.reply_text("⚙️ AUTO DRAW", reply_markup=kb_autodraw_toggle(is_on))

async def cmd_draw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_admin(user.id):
        return
    g = await db.get_latest_giveaway()
    if not g:
        await update.message.reply_text("No giveaways found.")
        return
    if g["status"] not in ("CLOSED", "ACTIVE"):
        await update.message.reply_text(f"Cannot draw now. Status: {g['status']}")
        return

    await start_selection(context, g["giveaway_id"], manual_flow=True)
    await update.message.reply_text("✅ Manual selection started. At the end you will Approve/Reject winners post.")

async def cmd_endgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_admin(user.id):
        return
    g = await db.get_latest_giveaway()
    if not g:
        await update.message.reply_text("No giveaways found.")
        return
    if g["status"] != "ACTIVE":
        await update.message.reply_text("No active giveaway is running right now.")
        return
    await close_giveaway_and_maybe_start_selection(context, g["giveaway_id"], forced=True)
    await update.message.reply_text("✅ Giveaway ended.")

async def cmd_participants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_admin(user.id):
        return
    g = await db.get_latest_giveaway()
    if not g:
        await update.message.reply_text("No giveaways found.")
        return
    n = await db.count_participants(g["giveaway_id"])
    await update.message.reply_text(f"👥 Total Participants: {n}\nGiveaway ID: {g['giveaway_id']}")

# ---- block system ----
async def cmd_blockpermanent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_admin(user.id):
        return
    STATE_BLOCKWAIT[user.id] = True
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 PERMANENT BLOCK\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send list (one per line):\n"
        "User ID only OR username + id\n\n"
        "Examples:\n"
        "7297292\n"
        "@MinexxProo | 7297292"
    )

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_admin(user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    uid = int(context.args[0])
    ok = await db.remove_ban(uid)
    await update.message.reply_text("✅ Unbanned." if ok else "⚠️ User was not banned.")

async def cmd_blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_admin(user.id):
        return
    bans = await db.list_bans()
    if not bans:
        await update.message.reply_text("✅ Block list is empty.")
        return
    lines = []
    for uid, uname, reason, ts in bans[:80]:
        uname = uname or "(no username)"
        lines.append(f"• {uname} | {uid} | {reason}")
    await update.message.reply_text("━━━━━━━━━━━━━━━━━━━━\n🔒 BLOCK LIST\n━━━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines))

async def cmd_removeban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_unban(update, context)

# ---- prize delivery ----
async def cmd_prizedelivered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_admin(user.id):
        return
    STATE_DELIVERY[user.id] = {"step": 1}
    await update.message.reply_text(
        "📦 PRIZE DELIVERY UPDATE\n\n"
        "Step 1/2 — Send Giveaway ID (example:\n"
        "P857-P583-B6714)\n"
        "OR send: latest\n\n"
        "After that, I will ask for delivered users list."
    )

# ---- winner list ----
async def cmd_winnerlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_admin(user.id):
        return
    hist = await db.list_winner_history(limit=50)
    if not hist:
        await update.message.reply_text("✅ Winner history is empty.")
        return

    def fmt_date(ts: int) -> str:
        t = time.localtime(ts)
        return f"{t.tm_mday:02d}-{t.tm_mon:02d}-{t.tm_year}"

    lines = []
    for r in hist:
        uname = r["username"] or "(no username)"
        lines.append(
            f"• Giveaway: {r['giveaway_id']}\n"
            f"  Prize: {r['prize']}\n"
            f"  Winner: {uname} | {r['user_id']}\n"
            f"  Date: {fmt_date(r['ts'])}\n"
        )
    await update.message.reply_text("━━━━━━━━━━━━━━━━━━━━\n📜 WINNER LIST (LAST 50)\n━━━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines))

# ---- reset ----
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_admin(user.id):
        return
    STATE_RESET[user.id] = {"await": True}
    await update.message.reply_text(
        "⚠️ RESET WARNING\n\n"
        "This will delete:\n"
        "• All giveaways\n"
        "• All participants\n"
        "• All winners\n"
        "• All settings\n"
        "• All lucky draw data\n\n"
        "Type: CONFIRM RESET\n"
        "to proceed.\n\n"
        "Type anything else to cancel."
    )

# =========================================================
# TEXT HANDLER (ADMIN FLOWS)
# =========================================================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    user = update.effective_user
    text = (update.message.text or "").strip()

    # Permanent block input
    if STATE_BLOCKWAIT.get(user.id):
        entries = parse_user_lines(text)
        added = 0
        for uname, uid in entries:
            await db.add_ban(uid, uname, "Permanent block")
            added += 1
        STATE_BLOCKWAIT.pop(user.id, None)
        await update.message.reply_text(f"✅ Added to permanent block list: {added}")
        return

    # Reset confirm
    if user.id in STATE_RESET:
        if text.upper() == "CONFIRM RESET":
            msg = await update.message.reply_text("🧹 Resetting: 0%\n📊 Progress: ▱▱▱▱▱▱▱▱▱▱")
            for i in range(1, 41):
                pct = int((i / 40) * 100)
                bar = progress_bar(pct, 10)
                await safe_edit_message(context.application, msg.chat_id, msg.message_id, f"🧹 Resetting: {pct}%\n📊 Progress: {bar}")
                await asyncio.sleep(1)
            await db.reset_all()
            STATE_RESET.pop(user.id, None)
            await update.message.reply_text("✅ Reset completed successfully. Bot is now fully clean.")
        else:
            STATE_RESET.pop(user.id, None)
            await update.message.reply_text("✅ Reset cancelled.")
        return

    # Prize delivery flow
    if user.id in STATE_DELIVERY:
        st = STATE_DELIVERY[user.id]
        if st["step"] == 1:
            if text.lower() == "latest":
                g = await db.get_latest_giveaway()
                if not g:
                    await update.message.reply_text("No giveaways found.")
                    STATE_DELIVERY.pop(user.id, None)
                    return
                st["giveaway_id"] = g["giveaway_id"]
            else:
                st["giveaway_id"] = text

            g = await db.get_giveaway(st["giveaway_id"])
            if not g:
                await update.message.reply_text("❌ Invalid Giveaway ID. Try again.")
                return

            st["step"] = 2
            await update.message.reply_text(
                f"✅ Giveaway selected: {st['giveaway_id']}\n\n"
                "Step 2/2 — Send delivered users list (one per line):\n"
                "@username | user_id\n\n"
                "Example:\n"
                "@MinexxProo | 5692210187"
            )
            return

        if st["step"] == 2:
            gid = st["giveaway_id"]
            g = await db.get_giveaway(gid)
            if not g:
                await update.message.reply_text("❌ Giveaway not found. Cancelled.")
                STATE_DELIVERY.pop(user.id, None)
                return

            winners = await db.list_winners(gid)
            w_by_id = {int(w["user_id"]): w for w in winners}
            participants = await db.list_participants(gid)
            p_by_id = {int(p["user_id"]): p for p in participants}

            try:
                pairs = parse_delivered_lines(text)
            except Exception as e:
                await update.message.reply_text(f"❌ Invalid list format.\nReason: {e}\n\nPlease resend correct list.")
                return

            invalid_lines = []
            updated = 0

            for uname, uid in pairs:
                w = w_by_id.get(uid)
                if not w:
                    invalid_lines.append(f"• {uname} | {uid} → Not in winners list")
                    continue

                stored_uname = (w.get("username") or p_by_id.get(uid, {}).get("username") or "").strip()
                if stored_uname and uname.lower() != stored_uname.lower():
                    invalid_lines.append(f"• {uname} | {uid} → Username mismatch (expected {stored_uname})")
                    continue

                await db.set_delivered(gid, uid, True)
                updated += 1

            # refresh winners post if exists
            try:
                await refresh_winners_post(context, gid)
            except Exception:
                pass

            if invalid_lines:
                await update.message.reply_text(
                    "⚠️ Some entries were rejected:\n\n"
                    + "\n".join(invalid_lines)
                    + "\n\n✅ You can resend ONLY the corrected lines."
                )

            await update.message.reply_text(
                f"✅ Prize delivery updated successfully.\n"
                f"Giveaway ID: {gid}\n"
                f"Updated: {updated} winner(s)"
            )
            STATE_DELIVERY.pop(user.id, None)
            return

    # New giveaway flow
    if user.id in STATE_NEW:
        st = STATE_NEW[user.id]
        step = int(st.get("step", 0))

        if step == 1:
            st["title"] = text
            st["step"] = 2
            await update.message.reply_text("Send Prize text (multi-line allowed). Example:\n10× ChatGPT PREMIUM")
            return

        if step == 2:
            st["prize"] = text
            st["step"] = 3
            await update.message.reply_text("Send Total Winners (number). Example: 10")
            return

        if step == 3:
            if not text.isdigit() or int(text) <= 0:
                await update.message.reply_text("❌ Please send a valid number for total winners.")
                return
            st["total_winners"] = int(text)
            st["step"] = 4
            await update.message.reply_text("Send Giveaway Duration.\nExample:\n30 Second\n5 Minute\n1 Hour")
            return

        if step == 4:
            sec = parse_duration(text)
            if sec is None or sec < 10:
                await update.message.reply_text("❌ Invalid duration. Example: 30 Second / 5 Minute / 1 Hour")
                return
            st["duration_seconds"] = sec
            st["step"] = 5
            await update.message.reply_text(
                "🔐 OLD WINNER PROTECTION MODE\n\n"
                "1 → BLOCK OLD WINNERS\n"
                "2 → SKIP OLD WINNERS\n\n"
                "Reply with:\n1 or 2"
            )
            return

        if step == 5:
            if text not in ("1", "2"):
                await update.message.reply_text("Reply with 1 or 2 only.")
                return
            st["old_winner_mode"] = "BLOCK" if text == "1" else "SKIP"
            st["step"] = 6
            await update.message.reply_text("Send Giveaway Rules (multi-line):")
            return

        if step == 6:
            st["rules"] = text
            st["step"] = 7

            rules_lines = [x.strip() for x in st["rules"].splitlines() if x.strip()]
            rules = "• " + "\n• ".join(rules_lines) if rules_lines else "• Admin decision is final & binding"

            preview = giveaway_post(
                title=st["title"],
                prize=st["prize"],
                participants=0,
                winners=int(st["total_winners"]),
                time_remaining=fmt_mmss(int(st["duration_seconds"])),
                progress=progress_bar(0, 10),
                rules=rules,
                hosted_by="POWER POINT BREAK",
            )

            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ APPROVE & POST", callback_data="GW_APPROVE"),
                        InlineKeyboardButton("❌ REJECT", callback_data="GW_REJECT"),
                    ]
                ]
            )
            st["preview_text"] = preview
            await update.message.reply_text("✅ Rules saved!\nShowing preview...")
            await update.message.reply_text(preview, reply_markup=kb)
            return

# =========================================================
# CALLBACK HANDLER
# =========================================================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return
    data = q.data or ""
    user = q.from_user

    # Default answer
    try:
        await q.answer()
    except Exception:
        pass

    # Admin panel buttons
    if data == "ADMIN_NEWGIVEAWAY":
        if not is_admin(user.id):
            return
        STATE_NEW[user.id] = {"step": 1}
        await q.message.reply_text(
            "Send Giveaway Title (single line).\n"
            "Example:\n"
            "⚡ POWER POINT BREAK GIVEAWAY ⚡"
        )
        return

    if data == "ADMIN_DRAW":
        if not is_admin(user.id):
            return
        # manual draw
        g = await db.get_latest_giveaway()
        if not g:
            await q.message.reply_text("No giveaways found.")
            return
        await start_selection(context, g["giveaway_id"], manual_flow=True)
        await q.message.reply_text("✅ Manual selection started. At the end you will Approve/Reject winners post.")
        return

    if data == "ADMIN_AUTODRAW":
        if not is_admin(user.id):
            return
        g = await db.get_latest_giveaway()
        if not g:
            await q.message.reply_text("No giveaways found.")
            return
        is_on = bool(int(g["autodraw"]))
        await q.message.reply_text("⚙️ AUTO DRAW", reply_markup=kb_autodraw_toggle(is_on))
        return

    if data == "ADMIN_PRIZEDELIVERY":
        if not is_admin(user.id):
            return
        STATE_DELIVERY[user.id] = {"step": 1}
        await q.message.reply_text(
            "📦 PRIZE DELIVERY UPDATE\n\n"
            "Step 1/2 — Send Giveaway ID (example:\n"
            "P857-P583-B6714)\n"
            "OR send: latest\n\n"
            "After that, I will ask for delivered users list."
        )
        return

    if data == "ADMIN_WINNERLIST":
        if not is_admin(user.id):
            return
        fake_update = Update(update.update_id, message=q.message)
        fake_update._effective_user = user
        await cmd_winnerlist(fake_update, context)
        return

    if data == "ADMIN_RESET":
        if not is_admin(user.id):
            return
        fake_update = Update(update.update_id, message=q.message)
        fake_update._effective_user = user
        await cmd_reset(fake_update, context)
        return

    # Giveaway approve/reject
    if data in ("GW_APPROVE", "GW_REJECT"):
        if not is_admin(user.id):
            return
        st = STATE_NEW.get(user.id)
        if not st or "preview_text" not in st:
            await q.message.reply_text("⚠️ No pending giveaway preview found.")
            return

        if data == "GW_REJECT":
            STATE_NEW.pop(user.id, None)
            await q.message.reply_text("❌ Giveaway rejected. Nothing was posted.")
            return

        # APPROVE
        gid = gen_giveaway_id()
        created = now_ts()
        ends = created + int(st["duration_seconds"])

        gdata = dict(
            giveaway_id=gid,
            title=st["title"],
            prize=st["prize"],
            total_winners=int(st["total_winners"]),
            duration_seconds=int(st["duration_seconds"]),
            hosted_by="POWER POINT BREAK",
            rules=st["rules"],
            created_ts=created,
            ends_ts=ends,
            status="ACTIVE",
            autodraw=0,  # default OFF
            old_winner_mode=st["old_winner_mode"],
        )
        await db.create_giveaway(gdata)
        await db.lucky_init(gid)

        # post join message to main channel
        rules_lines = [x.strip() for x in st["rules"].splitlines() if x.strip()]
        rules = "• " + "\n• ".join(rules_lines) if rules_lines else "• Admin decision is final & binding"

        join_text = giveaway_post(
            title=st["title"],
            prize=st["prize"],
            participants=0,
            winners=int(st["total_winners"]),
            time_remaining=fmt_mmss(int(st["duration_seconds"])),
            progress=progress_bar(0, 10),
            rules=rules,
            hosted_by="POWER POINT BREAK",
        )

        m = await context.application.bot.send_message(
            chat_id=CFG.MAIN_CHANNEL_ID,
            text=join_text,
            reply_markup=kb_join(gid),
            disable_web_page_preview=True,
        )

        await db.update_giveaway_fields(gid, channel_post_msg_id=m.message_id)

        # schedule jobs (tick + close)
        await schedule_giveaway_jobs(context.application, gid)

        STATE_NEW.pop(user.id, None)
        await q.message.reply_text("✅ Giveaway approved and posted to channel!")
        return

    # AutoDraw ON/OFF
    if data in ("AUTODRAW_ON", "AUTODRAW_OFF"):
        if not is_admin(user.id):
            return
        g = await db.get_latest_giveaway()
        if not g:
            await q.message.reply_text("No giveaways found.")
            return
        new_val = 1 if data == "AUTODRAW_ON" else 0
        await db.update_giveaway_fields(g["giveaway_id"], autodraw=new_val)
        await q.message.reply_text("✅ Auto Draw is now ON." if new_val else "✅ Auto Draw is now OFF.")
        return

    # Manual approve/reject
    if data.startswith("MANUALAPPROVE|") or data.startswith("MANUALREJECT|"):
        if not is_admin(user.id):
            return
        gid = data.split("|", 1)[1]
        if data.startswith("MANUALAPPROVE|"):
            await q.message.reply_text("✅ Approved! Winners list will be posted to channel.")
            await post_winners_and_cleanup(context, gid)
            return
        else:
            await q.message.reply_text("❌ Rejected. No winners post was made.")
            await db.update_giveaway_fields(gid, status="CLOSED")
            return

    # Join
    if data.startswith("JOIN|"):
        gid = data.split("|", 1)[1]
        g = await db.get_giveaway(gid)
        if not g or g["status"] != "ACTIVE":
            await q.answer("This giveaway is not active.", show_alert=True)
            return

        # Ban check
        if await db.is_banned(user.id):
            await q.answer("⛔ You are permanently blocked from this system.", show_alert=True)
            return

        # Old winner BLOCK mode (by history)
        if g["old_winner_mode"] == "BLOCK":
            hist = await db.list_winner_history(limit=5000)
            if any(int(h["user_id"]) == user.id for h in hist):
                await q.answer(
                    "🚫You have already won a previous giveaway.\n"
                    "To keep the giveaway fair for everyone,\n"
                    "repeat winners are restricted from participating.\n"
                    "🙏Please wait for the next Giveaway",
                    show_alert=True
                )
                return

        already = await db.get_participant(gid, user.id)
        if already:
            # if user is first join champion, show first join pop-up again
            if int(already.get("is_first_join", 0)) == 1:
                uname = f"@{user.username}" if user.username else "User"
                await q.answer(popup_first_join(uname, user.id, CFG.GROUP_USERNAME), show_alert=True)
            else:
                await q.answer(popup_already_joined(), show_alert=True)
            return

        uname = f"@{user.username}" if user.username else None
        # allow join even without username (but they will be excluded from selection as rule says)
        pcount = await db.count_participants(gid)
        is_first = (pcount == 0)

        ok = await db.add_participant(gid, user.id, uname, is_first=is_first)
        if not ok:
            await q.answer(popup_already_joined(), show_alert=True)
            return

        # refresh join post right now
        await refresh_join_post(context, gid)

        if is_first:
            await q.answer(popup_first_join(uname or "User", user.id, CFG.GROUP_USERNAME), show_alert=True)
        else:
            await q.answer(popup_join_success(), show_alert=True)
        return

    # Claim
    if data.startswith("CLAIM|"):
        _, gid, slot = data.split("|", 2)
        g = await db.get_giveaway(gid)
        if not g:
            await q.answer("Invalid giveaway.", show_alert=True)
            return

        now = now_ts()
        expired_24h = (now > int(g["ends_ts"]) + 24 * 3600)

        winners = await db.list_winners(gid)
        w_by_id = {int(w["user_id"]): w for w in winners}
        w = w_by_id.get(user.id)

        if expired_24h:
            if w:
                await q.answer(popup_expired(CFG.OWNER_USERNAME), show_alert=True)
            else:
                await q.answer(popup_giveaway_completed(CFG.OWNER_USERNAME), show_alert=True)
            return

        if not w:
            await q.answer(popup_not_winner(), show_alert=True)
            return

        uname = w.get("username") or (f"@{user.username}" if user.username else "User")

        if int(w.get("delivered", 0)) == 1:
            await q.answer(popup_prize_delivered(uname, user.id, CFG.OWNER_USERNAME), show_alert=True)
            return

        await db.set_claimed_ts(gid, user.id)
        await q.answer(popup_claim_ok(uname, user.id, CFG.OWNER_USERNAME), show_alert=True)
        return

    # Entry Rule
    if data.startswith("ENTRYRULE|"):
        await q.answer(entry_rule_popup(), show_alert=True)
        return

    # Try Your Luck
    if data.startswith("TRYLUCK|"):
        gid = data.split("|", 1)[1]
        g = await db.get_giveaway(gid)
        if not g:
            await q.answer("Invalid giveaway.", show_alert=True)
            return

        participants = await db.list_participants(gid)
        if not participants:
            await q.answer(lucky_no_participants(), show_alert=True)
            return

        uname = f"@{user.username}" if user.username else None
        if not uname:
            await q.answer("A valid @username is required for Lucky Draw.", show_alert=True)
            return

        sel_end = await db.get_setting(f"sel_end:{gid}", None)
        if not sel_end:
            await q.answer("Lucky Draw is not available right now.", show_alert=True)
            return

        # Lucky Draw must be at Time Remaining 05:55 (355 seconds)
        remaining = int(sel_end) - now_ts()

        # Robust: allow remaining 355 or 354 (network delays) while still being "05:55"
        allowed_window = remaining in (355, 354)

        lucky = await db.lucky_get(gid)
        if not allowed_window:
            # if already winner exists, show TOO LATE with winner
            if lucky and lucky.get("winner_user_id"):
                await q.answer(too_late_popup(lucky["winner_username"], int(lucky["winner_user_id"])), show_alert=True)
            else:
                await q.answer(try_luck_not_time(), show_alert=True)
            return

        ok = await db.lucky_set_winner(gid, user.id, uname)
        lucky = await db.lucky_get(gid)

        if ok:
            # add as extra winner (last rank)
            winners = await db.list_winners(gid)
            max_rank = max([int(w["rank"]) for w in winners], default=0)
            await db.add_winner(gid, user.id, uname, rank=max_rank + 1)
            await db.insert_winner_history(gid, user.id, uname, g["prize"])

            # refresh selection post instantly
            try:
                await force_refresh_selection_display(context, gid)
            except Exception:
                pass

            await q.answer(lucky_winner_popup(uname, user.id), show_alert=True)
            return

        # too late
        if lucky and lucky.get("winner_user_id"):
            await q.answer(too_late_popup(lucky["winner_username"], int(lucky["winner_user_id"])), show_alert=True)
        else:
            await q.answer("⚠️ TOO LATE\n\nSomeone already won the Lucky Draw slot.", show_alert=True)
        return

# =========================================================
# FORCE REFRESH SELECTION DISPLAY (WHEN LUCKY WINNER ADDED)
# =========================================================
async def force_refresh_selection_display(context: ContextTypes.DEFAULT_TYPE, giveaway_id: str):
    g = await db.get_giveaway(giveaway_id)
    if not g or not g.get("selection_post_msg_id"):
        return
    sel_end = await db.get_setting(f"sel_end:{giveaway_id}", None)
    if not sel_end:
        return
    remaining = int(sel_end) - now_ts()
    total = 10 * 60
    elapsed = total - max(0, remaining)
    pct = int((elapsed / total) * 100)
    bar = progress_bar(pct, 10)

    colors = pick_three_distinct_colors()
    show_lines = [
        f"{colors[0]} Now Showing → @username | 🆔 0000000000  ",
        f"{colors[1]} Now Showing → @username | 🆔 0000000000  ",
        f"{colors[2]} Now Showing → @username | 🆔 0000000000  ",
    ]
    await refresh_selection_post(context, giveaway_id, pct, bar, remaining, show_lines)

# =========================================================
# REFRESH WINNERS POST AFTER DELIVERY UPDATES
# =========================================================
async def refresh_winners_post(context: ContextTypes.DEFAULT_TYPE, giveaway_id: str):
    g = await db.get_giveaway(giveaway_id)
    if not g or not g.get("winners_post_msg_id"):
        return
    txt, kb = await build_winners_post_text_and_kb(giveaway_id)
    await safe_edit_message(
        context.application,
        chat_id=CFG.MAIN_CHANNEL_ID,
        message_id=int(g["winners_post_msg_id"]),
        text=txt,
        reply_markup=kb,
    )

# =========================================================
# STARTUP RESUME
# =========================================================
async def resume_active_giveaways(app: Application):
    actives = await db.list_active_giveaways()
    for g in actives:
        gid = g["giveaway_id"]
        await schedule_giveaway_jobs(app, gid)

# =========================================================
# MAIN
# =========================================================
async def main():
    await db.init()

    app = Application.builder().token(CFG.BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))

    app.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    app.add_handler(CommandHandler("participants", cmd_participants))
    app.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    app.add_handler(CommandHandler("draw", cmd_draw))
    app.add_handler(CommandHandler("autodraw", cmd_autodraw))

    app.add_handler(CommandHandler("prizedelivered", cmd_prizedelivered))
    app.add_handler(CommandHandler("winnerlist", cmd_winnerlist))

    app.add_handler(CommandHandler("reset", cmd_reset))

    # Block system
    app.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("blocklist", cmd_blocklist))
    app.add_handler(CommandHandler("removeban", cmd_removeban))

    # Callbacks + Text
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Resume active giveaways on startup
    await resume_active_giveaways(app)

    await app.run_polling(close_loop=False)

if __name__ == "__main__":
    asyncio.run(main())
