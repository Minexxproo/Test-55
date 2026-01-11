import asyncio
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple, Dict

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0").strip() or "0")
ADMIN_IDS = set(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit())
OFFICIAL_CHANNEL = os.getenv("OFFICIAL_CHANNEL", "@PowerPointBreak").strip()
DEFAULT_CHANNEL_ID = int(os.getenv("DEFAULT_CHANNEL_ID", "0").strip() or "0")
DEFAULT_HOSTED_BY = os.getenv("DEFAULT_HOSTED_BY", "POWER POINT BREAK").strip()
BOT_OWNER_USERNAME = os.getenv("BOT_OWNER_USERNAME", "@MinexxProo").strip()

DB_PATH = "data.db"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in .env")

UTC = timezone.utc

# =========================
# Utility
# =========================

def now_utc() -> datetime:
    return datetime.now(tz=UTC)

def fmt_hms(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"00:{m:02d}:{s:02d}"

def safe_username(u) -> Optional[str]:
    # Telegram username (without @) or None
    if u and isinstance(u, str):
        u = u.strip()
        if u:
            return u
    return None

def with_at(username: Optional[str]) -> Optional[str]:
    if not username:
        return None
    if username.startswith("@"):
        return username
    return "@" + username

def border_line(short: bool = True) -> str:
    # Short border to avoid Telegram line break issues
    return "━━━━━━━━━━━━━━━━━━━━"

def pick_three_distinct_colors() -> List[str]:
    # Ensure 3 different colors always
    colors = ["🟡", "🟠", "🟣", "🔵", "⚫", "🟢", "🔴", "⚪"]
    return random.sample(colors, 3)

def progress_bar(pct: int, blocks: int = 10) -> str:
    pct = max(0, min(100, pct))
    filled = int(round((pct / 100) * blocks))
    if filled > blocks:
        filled = blocks
    return "▰" * filled + "▱" * (blocks - filled)

def normalize_pair_line(line: str) -> Optional[Tuple[Optional[str], int]]:
    """
    Accepts:
      @username | 123
      username | 123
      123
    Returns: (username_with_at_or_None, user_id_int)
    """
    line = line.strip()
    if not line:
        return None
    # If only digits
    if re.fullmatch(r"\d{5,20}", line):
        return (None, int(line))
    m = re.match(r"^@?([A-Za-z0-9_]{3,32})\s*\|\s*(\d{5,20})$", line)
    if not m:
        return None
    uname = with_at(m.group(1))
    uid = int(m.group(2))
    return (uname, uid)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# =========================
# Database
# =========================

CREATE_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS settings(
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS giveaways(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  prize TEXT NOT NULL,
  winners_total INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  hosted_by TEXT NOT NULL,
  rules TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  ends_at INTEGER NOT NULL,
  status TEXT NOT NULL,          -- ACTIVE / CLOSED / SELECTING / ANNOUNCED
  auto_draw INTEGER NOT NULL,    -- 0/1
  old_winner_mode TEXT NOT NULL, -- BLOCK / SKIP
  msg_giveaway INTEGER,          -- giveaway post message_id
  msg_close INTEGER,             -- close post message_id
  msg_select INTEGER,            -- selection post message_id
  msg_winners INTEGER,           -- winners post message_id
  claim_deadline INTEGER,        -- unix time
  lucky_user_id INTEGER,         -- lucky draw winner user_id
  lucky_username TEXT            -- lucky draw winner username (@..)
);

CREATE TABLE IF NOT EXISTS participants(
  giveaway_id TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  username TEXT,
  joined_at INTEGER NOT NULL,
  is_first INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(giveaway_id, user_id)
);

CREATE TABLE IF NOT EXISTS winners(
  giveaway_id TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  username TEXT,
  kind TEXT NOT NULL,         -- FIRST / LUCKY / RANDOM
  rank INTEGER NOT NULL,      -- 1..n
  created_at INTEGER NOT NULL,
  delivered_at INTEGER,       -- unix time
  PRIMARY KEY(giveaway_id, user_id)
);

CREATE TABLE IF NOT EXISTS winner_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  giveaway_id TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  username TEXT,
  prize TEXT NOT NULL,
  won_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks(
  user_id INTEGER PRIMARY KEY,
  username TEXT,
  reason TEXT,
  blocked_at INTEGER NOT NULL
);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        for stmt in CREATE_SQL.strip().split(";"):
            s = stmt.strip()
            if s:
                await db.execute(s + ";")
        await db.commit()

async def db_set_setting(k: str, v: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        await db.commit()

async def db_get_setting(k: str, default: Optional[str] = None) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT v FROM settings WHERE k=?", (k,))
        row = await cur.fetchone()
        return row[0] if row else default

# =========================
# Giveaway ID generator (P###-P###-B####)
# =========================

def gen_giveaway_id() -> str:
    a = random.randint(100, 999)
    b = random.randint(100, 999)
    c = random.randint(1000, 9999)
    return f"P{a}-P{b}-B{c}"

# =========================
# FSM
# =========================

class NewGiveawayFlow(StatesGroup):
    title = State()
    prize = State()
    winners_total = State()
    duration = State()
    old_winner_mode = State()
    rules = State()
    approve = State()

class PrizeDeliveryFlow(StatesGroup):
    ask_gid = State()
    ask_list = State()

class ResetFlow(StatesGroup):
    confirm = State()

class BlockFlow(StatesGroup):
    ask_list = State()

# =========================
# Bot + Dispatcher
# =========================

bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())

# =========================
# Text Templates (Short Borders)
# =========================

def admin_welcome_text() -> str:
    return (
        "👋 <b>Welcome, Admin!</b>\n\n"
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

def unauthorized_notice_text(u_name: Optional[str], u_id: int) -> str:
    uname_line = f"Usenam: {with_at(u_name) if u_name else 'N/A'}"
    return (
        f"{border_line()}\n"
        "⚠️ <b>UNAUTHORIZED NOTICE</b>\n"
        f"{border_line()}\n\n"
        "Hi there!\n"
        f"{uname_line}\n"
        f"Useid: <code>{u_id}</code>\n\n"
        "It looks like you tried to start the giveaway,\n"
        "but this action is available for admins only.\n\n"
        "😊 No worries — this is just a friendly heads-up.\n\n"
        "🎁 This is an official Giveaway Bot.\n"
        "For exciting giveaway updates,\n"
        "join our official channel now:\n"
        f"👉 {OFFICIAL_CHANNEL}\n\n"
        "🤖 Powered by:\n"
        f"{DEFAULT_HOSTED_BY} — Official Giveaway System\n\n"
        "👤 Bot Owner:\n"
        f"{BOT_OWNER_USERNAME}\n\n"
        "If you think this was a mistake,\n"
        "please feel free to contact an admin anytime.\n"
        "We’re always happy to help!\n"
        f"{border_line()}"
    )

def giveaway_post_text(title: str, prize: str, participants: int, winners_total: int, seconds_left: int, rules: str, hosted_by: str) -> str:
    return (
        f"{border_line()}\n"
        f"⚡ <b>{title}</b> ⚡\n"
        f"{border_line()}\n\n"
        f"🎁 <b>PRIZE POOL</b> 🌟\n"
        f"🏆 <b>{prize}</b>\n\n"
        f"👥 Total Participants: <b>{participants}</b>\n"
        f"🏆 Total Winners: <b>{winners_total}</b>\n\n"
        f"🎯 <b>Winner Selection</b>\n"
        f"• 100% Random & Fair\n"
        f"• Auto System\n\n"
        f"⏱️ Time Remaining: <b>{fmt_hms(seconds_left)}</b>\n"
        f"📊 Live Progress\n"
        f"{progress_bar(0, 10)}\n\n"
        f"📜 <b>Official Rules</b>\n"
        f"{rules}\n\n"
        f"📢 Hosted by: <b>{hosted_by}</b>\n\n"
        f"{border_line()}\n"
        f"👇 Tap below to join the giveaway 👇"
    )

def giveaway_closed_text(prize: str, participants: int, winners_total: int) -> str:
    return (
        f"{border_line()}\n"
        "🚫 <b>GIVEAWAY CLOSED</b> 🚫\n"
        f"{border_line()}\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now locked.\n\n"
        "📊 <b>Giveaway Summary</b>\n"
        f"🎁 Prize: <b>{prize}</b>\n\n"
        f"👥 Total Participants: <b>{participants}</b>\n"
        f"🏆 Total Winners: <b>{winners_total}</b>\n\n"
        "🎯 Winners will be announced very soon.\n"
        "Please stay tuned for the final results.\n\n"
        "✨ Best of luck to everyone!\n\n"
        f"— <b>{DEFAULT_HOSTED_BY}</b> ⚡\n"
        f"{border_line()}"
    )

def selection_post_text(title: str, prize: str, winners_selected: int, winners_total: int, pct: int, time_left: int, showcase_lines: List[str]) -> str:
    # Important rule fixed line (no dot spinner)
    return (
        f"{border_line()}\n"
        "🎲 <b>LIVE RANDOM WINNER SELECTION</b>\n"
        f"{border_line()}\n\n"
        f"⚡ <b>{title}</b> ⚡\n\n"
        "🎁 <b>GIVEAWAY SUMMARY</b>\n"
        f"🏆 Prize: <b>{prize}</b>\n"
        f"✅ Winners Selected: <b>{winners_selected}/{winners_total}</b>\n\n"
        "📌 <b>Important Rule</b>\n"
        "Users without a valid @username\n"
        "are automatically excluded.\n\n"
        f"⏳ Selection Progress: <b>{pct}%</b>\n"
        f"📊 Progress Bar: {progress_bar(pct, 10)}\n\n"
        f"🕒 Time Remaining: <b>{fmt_hms(time_left)}</b>\n"
        "🔐 System Mode: 100% Random • Fair • Auto\n\n"
        f"{border_line()}\n"
        "👥 <b>LIVE ENTRIES SHOWCASE</b>\n"
        f"{border_line()}\n"
        + "\n".join(showcase_lines) +
        f"\n{border_line()}"
    )

def entry_rule_popup_text(current_remaining: str) -> str:
    return (
        "📌 ENTRY RULE\n"
        "• Tap 🍀 Try Your Luck exactly at ⏱️ 05:55\n"
        "• First click wins instantly (Lucky Draw)\n"
        "• Must have a valid @username\n"
        "• Winner is added live to the selection post\n"
        "• 100% fair: first-come-first-win\n\n"
        f"⏱️ Current Time Remaining: {current_remaining}"
    )

def not_yet_popup_text(current_remaining: str) -> str:
    return (
        "⏳ NOT YET\n\n"
        "The Lucky Draw is not open right now.\n"
        "Please wait until the timer reaches ⏱️ 05:55.\n\n"
        f"Current Time Remaining: {current_remaining}"
    )

def lucky_win_popup(username: str, user_id: int) -> str:
    return (
        "🌟 CONGRATULATIONS!\n"
        "You won the 🍀 Lucky Draw Winner slot ✅\n\n"
        f"👤 {username}\n"
        f"🆔 {user_id}\n"
        "📸 Take a screenshot and send it in the group to confirm 👈\n\n"
        "🏆 Added to winners list LIVE!"
    )

def too_late_popup(winner_username: str, winner_user_id: int) -> str:
    return (
        "⚠️ TOO LATE\n\n"
        "Someone already won the Lucky Draw slot.\n"
        "Please continue watching the live selection.\n\n"
        f"👤 {winner_username}\n"
        f"🆔 {winner_user_id}"
    )

def no_username_popup() -> str:
    return "⚠️ You need a valid @username to use Lucky Draw."

def not_winner_popup() -> str:
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry! Your User ID is not\n"
        "in the winners list.\n\n"
        "Please wait for the next\n"
        "giveaway ❤️‍🩹"
    )

def already_delivered_popup(username: str, user_id: int) -> str:
    return (
        "📦 PRIZE ALREADY DELIVERED\n"
        "Your prize has already been\n"
        "successfully delivered ✅\n\n"
        f"👤 {username}\n"
        f"🆔 {user_id}\n"
        "If you face any issue,\n"
        f"contact admin 👉 {BOT_OWNER_USERNAME}"
    )

def expired_popup() -> str:
    return (
        "⛔ PRIZE EXPIRED\n\n"
        "Your claim window has expired (24 hours).\n"
        "Please contact admin if you think this is a mistake."
    )

def giveaway_completed_popup() -> str:
    return (
        "✅ GIVEAWAY COMPLETED\n\n"
        "This giveaway has been completed.\n"
        f"If you have any issues, please contact admin 👉 {BOT_OWNER_USERNAME}"
    )

# =========================
# Keyboards
# =========================

def kb_join(gid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data=f"join:{gid}")]
    ])

def kb_admin_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 New Giveaway", callback_data="ap:new"),
         InlineKeyboardButton(text="⚙️ Auto Draw", callback_data="ap:autodraw")],
        [InlineKeyboardButton(text="🎲 Manual Draw", callback_data="ap:draw"),
         InlineKeyboardButton(text="📦 Prize Delivered", callback_data="ap:delivery")],
        [InlineKeyboardButton(text="🏆 Winner List", callback_data="ap:winnerlist"),
         InlineKeyboardButton(text="♻️ Reset", callback_data="ap:reset")]
    ])

def kb_approve() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ APPROVE & POST", callback_data="ng:approve"),
         InlineKeyboardButton(text="❌ CANCEL", callback_data="ng:cancel")]
    ])

def kb_autodraw_toggle(is_on: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Auto Draw ON" if is_on else "✅ Auto Draw ON", callback_data="ad:on"),
         InlineKeyboardButton(text="⛔ Auto Draw OFF" if not is_on else "⛔ Auto Draw OFF", callback_data="ad:off")]
    ])

def kb_claim(gid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data=f"claim:{gid}")]
    ])

def kb_selection_buttons(gid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍀 Try Your Luck", callback_data=f"luck:{gid}"),
         InlineKeyboardButton(text="📌 Entry Rule", callback_data=f"rule:{gid}")]
    ])

def kb_reset_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ CONFIRM RESET", callback_data="rs:yes"),
         InlineKeyboardButton(text="❌ CANCEL", callback_data="rs:no")]
    ])

# =========================
# Core DB helpers
# =========================

async def get_autodraw_flag() -> bool:
    v = await db_get_setting("autodraw", "0")
    return v == "1"

async def set_autodraw_flag(flag: bool):
    await db_set_setting("autodraw", "1" if flag else "0")

async def get_latest_giveaway_id() -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM giveaways ORDER BY created_at DESC LIMIT 1")
        row = await cur.fetchone()
        return row[0] if row else None

async def get_giveaway(gid: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM giveaways WHERE id=?", (gid,))
        row = await cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

async def update_giveaway_fields(gid: str, **kwargs):
    if not kwargs:
        return
    keys = list(kwargs.keys())
    vals = [kwargs[k] for k in keys]
    set_clause = ", ".join([f"{k}=?" for k in keys])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE giveaways SET {set_clause} WHERE id=?", (*vals, gid))
        await db.commit()

async def count_participants(gid: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM participants WHERE giveaway_id=?", (gid,))
        row = await cur.fetchone()
        return int(row[0]) if row else 0

async def get_participants_usernames(gid: str) -> List[Tuple[int, str]]:
    # return list of (user_id, @username) only valid usernames
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, username FROM participants WHERE giveaway_id=? AND username IS NOT NULL AND username != ''",
            (gid,)
        )
        rows = await cur.fetchall()
        out = []
        for uid, uname in rows:
            uname = uname.strip() if uname else ""
            if uname and uname.startswith("@"):
                out.append((int(uid), uname))
        return out

async def was_winner_before(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM winners WHERE user_id=? LIMIT 1", (user_id,))
        row = await cur.fetchone()
        return bool(row)

async def is_blocked(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM blocks WHERE user_id=? LIMIT 1", (user_id,))
        row = await cur.fetchone()
        return bool(row)

async def add_block(user_id: int, username: Optional[str], reason: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO blocks(user_id, username, reason, blocked_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, reason=excluded.reason, blocked_at=excluded.blocked_at",
            (user_id, username or "", reason, int(time.time()))
        )
        await db.commit()

async def remove_block(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM blocks WHERE user_id=?", (user_id,))
        await db.commit()

async def add_participant(gid: str, user_id: int, username: Optional[str], is_first: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO participants(giveaway_id,user_id,username,joined_at,is_first) VALUES(?,?,?,?,?)",
            (gid, user_id, username or "", int(time.time()), is_first)
        )
        await db.execute(
            "UPDATE participants SET username=? WHERE giveaway_id=? AND user_id=?",
            (username or "", gid, user_id)
        )
        if is_first == 1:
            await db.execute(
                "UPDATE participants SET is_first=1 WHERE giveaway_id=? AND user_id=?",
                (gid, user_id)
            )
        await db.commit()

async def participant_exists(gid: str, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM participants WHERE giveaway_id=? AND user_id=? LIMIT 1", (gid, user_id))
        return bool(await cur.fetchone())

async def set_first_join(gid: str, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE participants SET is_first=0 WHERE giveaway_id=?", (gid,))
        await db.execute("UPDATE participants SET is_first=1 WHERE giveaway_id=? AND user_id=?", (gid, user_id))
        await db.commit()

async def get_first_join(gid: str) -> Optional[Tuple[int, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, username FROM participants WHERE giveaway_id=? AND is_first=1 LIMIT 1",
            (gid,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        uid, uname = row
        return int(uid), (uname or "")

async def add_winner(gid: str, user_id: int, username: str, kind: str, rank: int, prize: str):
    ts = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO winners(giveaway_id,user_id,username,kind,rank,created_at) VALUES(?,?,?,?,?,?)",
            (gid, user_id, username, kind, rank, ts)
        )
        await db.execute(
            "INSERT INTO winner_history(giveaway_id,user_id,username,prize,won_at) VALUES(?,?,?,?,?)",
            (gid, user_id, username, prize, ts)
        )
        await db.commit()

async def get_winners(gid: str) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT giveaway_id,user_id,username,kind,rank,created_at,delivered_at FROM winners WHERE giveaway_id=? ORDER BY kind='FIRST' DESC, kind='LUCKY' DESC, rank ASC",
            (gid,)
        )
        rows = await cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "giveaway_id": r[0],
                "user_id": int(r[1]),
                "username": r[2] or "",
                "kind": r[3],
                "rank": int(r[4]),
                "created_at": int(r[5]),
                "delivered_at": int(r[6]) if r[6] else None
            })
        return out

async def mark_delivered(gid: str, user_id: int):
    ts = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE winners SET delivered_at=? WHERE giveaway_id=? AND user_id=?", (ts, gid, user_id))
        await db.commit()

async def delivery_count(gid: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM winners WHERE giveaway_id=? AND delivered_at IS NOT NULL", (gid,))
        row = await cur.fetchone()
        return int(row[0]) if row else 0

async def winner_count(gid: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM winners WHERE giveaway_id=?", (gid,))
        row = await cur.fetchone()
        return int(row[0]) if row else 0

async def get_winner_by_user(gid: str, user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT giveaway_id,user_id,username,kind,rank,created_at,delivered_at FROM winners WHERE giveaway_id=? AND user_id=? LIMIT 1",
            (gid, user_id)
        )
        r = await cur.fetchone()
        if not r:
            return None
        return {
            "giveaway_id": r[0],
            "user_id": int(r[1]),
            "username": r[2] or "",
            "kind": r[3],
            "rank": int(r[4]),
            "created_at": int(r[5]),
            "delivered_at": int(r[6]) if r[6] else None
        }

# =========================
# Admin panel
# =========================

@dp.message(Command("panel"))
async def cmd_panel(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    is_on = await get_autodraw_flag()
    await m.answer("🛠 <b>Admin Panel</b>", reply_markup=kb_admin_panel())
    await m.answer("⚙️ Auto Draw Toggle:", reply_markup=kb_autodraw_toggle(is_on))

@dp.callback_query(F.data == "ap:new")
async def ap_new(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Admins only.", show_alert=True)
    await state.set_state(NewGiveawayFlow.title)
    await c.message.answer("Send Giveaway Title (exact):")
    await c.answer()

@dp.callback_query(F.data == "ap:autodraw")
async def ap_autodraw(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Admins only.", show_alert=True)
    is_on = await get_autodraw_flag()
    await c.message.answer("⚙️ Auto Draw Toggle:", reply_markup=kb_autodraw_toggle(is_on))
    await c.answer()

@dp.callback_query(F.data == "ap:draw")
async def ap_draw(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Admins only.", show_alert=True)
    await c.answer()
    await start_manual_draw_latest(c.message)

@dp.callback_query(F.data == "ap:delivery")
async def ap_delivery(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Admins only.", show_alert=True)
    await c.answer()
    await state.set_state(PrizeDeliveryFlow.ask_gid)
    await c.message.answer(
        "📦 <b>PRIZE DELIVERY UPDATE</b>\n\n"
        "Step 1/2 — Send Giveaway ID (example: P857-P583-B6714)\n"
        "OR send: <b>latest</b>\n\n"
        "After that, I will ask for delivered users list."
    )

@dp.callback_query(F.data == "ap:winnerlist")
async def ap_winnerlist(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Admins only.", show_alert=True)
    await c.answer()
    await send_winnerlist(c.message)

@dp.callback_query(F.data == "ap:reset")
async def ap_reset(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Admins only.", show_alert=True)
    await c.answer()
    await state.set_state(ResetFlow.confirm)
    await c.message.answer(
        "♻️ <b>RESET WARNING</b>\n\n"
        "This will erase all giveaways, participants, winners, and settings.\n"
        "Do you want to continue?",
        reply_markup=kb_reset_confirm()
    )

# =========================
# /start behavior
# =========================

@dp.message(Command("start"))
async def cmd_start(m: Message):
    u = m.from_user
    if is_admin(u.id):
        return await m.answer(admin_welcome_text())
    # unauthorized
    await m.answer(unauthorized_notice_text(u.username, u.id))
    # notify owner in private
    if OWNER_ID:
        try:
            await bot.send_message(
                OWNER_ID,
                "🔔 <b>Unauthorized Start Attempt</b>\n\n"
                f"👤 Username: {with_at(u.username) if u.username else 'N/A'}\n"
                f"🆔 User ID: <code>{u.id}</code>"
            )
        except Exception:
            pass

# =========================
# AutoDraw Toggle
# =========================

@dp.message(Command("autodraw"))
async def cmd_autodraw(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    is_on = await get_autodraw_flag()
    await m.answer("⚙️ Auto Draw Toggle:", reply_markup=kb_autodraw_toggle(is_on))

@dp.callback_query(F.data.in_({"ad:on", "ad:off"}))
async def cb_autodraw(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Admins only.", show_alert=True)
    flag = (c.data == "ad:on")
    await set_autodraw_flag(flag)
    await c.message.answer("✅ Auto Draw is now ON." if flag else "⛔ Auto Draw is now OFF.")
    await c.answer()

# =========================
# New Giveaway Wizard
# =========================

@dp.message(NewGiveawayFlow.title)
async def ng_title(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await state.clear()
    title = (m.text or "").strip()
    if not title:
        return await m.answer("Send a valid title.")
    await state.update_data(title=title)
    await state.set_state(NewGiveawayFlow.prize)
    await m.answer("Send Prize (example: 10× ChatGPT PREMIUM):")

@dp.message(NewGiveawayFlow.prize)
async def ng_prize(m: Message, state: FSMContext):
    prize = (m.text or "").strip()
    if not prize:
        return await m.answer("Send a valid prize.")
    await state.update_data(prize=prize)
    await state.set_state(NewGiveawayFlow.winners_total)
    await m.answer("Send Total Winners number (example: 10):")

@dp.message(NewGiveawayFlow.winners_total)
async def ng_winners(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    if not t.isdigit():
        return await m.answer("Send a number only.")
    n = int(t)
    if n <= 0 or n > 500:
        return await m.answer("Winners must be 1..500.")
    await state.update_data(winners_total=n)
    await state.set_state(NewGiveawayFlow.duration)
    await m.answer(
        "Send Giveaway Duration\n"
        "Example:\n"
        "30 Second\n"
        "5 Minute\n"
        "1 Hour"
    )

def parse_duration(text: str) -> Optional[int]:
    s = text.strip().lower()
    m = re.match(r"^(\d+)\s*(second|seconds|sec|minute|minutes|min|hour|hours|hr|day|days)$", s)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2)
    if "sec" in unit or "second" in unit:
        return val
    if "min" in unit or "minute" in unit:
        return val * 60
    if "hr" in unit or "hour" in unit:
        return val * 3600
    if "day" in unit:
        return val * 86400
    return None

@dp.message(NewGiveawayFlow.duration)
async def ng_duration(m: Message, state: FSMContext):
    seconds = parse_duration(m.text or "")
    if not seconds or seconds < 10:
        return await m.answer("Invalid duration. Example: 1 Minute / 30 Second / 1 Hour")
    await state.update_data(duration=seconds)
    await state.set_state(NewGiveawayFlow.old_winner_mode)
    await m.answer(
        "🔐 <b>OLD WINNER PROTECTION MODE</b>\n\n"
        "1) BLOCK OLD WINNERS\n"
        "2) SKIP OLD WINNERS\n\n"
        "Reply with:\n"
        "1 → BLOCK\n"
        "2 → SKIP"
    )

@dp.message(NewGiveawayFlow.old_winner_mode)
async def ng_old_mode(m: Message, state: FSMContext):
    v = (m.text or "").strip()
    if v not in ("1", "2"):
        return await m.answer("Reply with 1 or 2.")
    mode = "BLOCK" if v == "1" else "SKIP"
    await state.update_data(old_winner_mode=mode)
    await state.set_state(NewGiveawayFlow.rules)
    await m.answer("Now send Giveaway Rules (multi-line):")

@dp.message(NewGiveawayFlow.rules)
async def ng_rules(m: Message, state: FSMContext):
    rules = (m.text or "").strip()
    if not rules:
        return await m.answer("Rules cannot be empty.")
    await state.update_data(rules=rules)
    data = await state.get_data()

    gid = gen_giveaway_id()
    created = int(time.time())
    ends = created + int(data["duration"])
    auto = 1 if (await get_autodraw_flag()) else 0

    # Save draft in state (not DB yet)
    await state.update_data(gid=gid, created_at=created, ends_at=ends, auto_draw=auto)

    preview = giveaway_post_text(
        title=data["title"],
        prize=data["prize"],
        participants=0,
        winners_total=data["winners_total"],
        seconds_left=int(data["duration"]),
        rules=data["rules"],
        hosted_by=DEFAULT_HOSTED_BY
    )
    await state.set_state(NewGiveawayFlow.approve)
    await m.answer("✅ Rules saved!\nShowing preview…")
    await m.answer(preview, reply_markup=kb_approve())

@dp.callback_query(F.data.in_({"ng:approve", "ng:cancel"}))
async def ng_approve(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Admins only.", show_alert=True)

    if c.data == "ng:cancel":
        await state.clear()
        await c.message.answer("❌ Cancelled.")
        return await c.answer()

    data = await state.get_data()
    gid = data["gid"]

    channel_id = DEFAULT_CHANNEL_ID
    if channel_id == 0:
        await c.message.answer("❌ DEFAULT_CHANNEL_ID is not set in .env")
        await state.clear()
        return await c.answer()

    # Insert giveaway into DB
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO giveaways(id,title,prize,winners_total,channel_id,hosted_by,rules,created_at,ends_at,status,auto_draw,old_winner_mode) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                gid, data["title"], data["prize"], int(data["winners_total"]),
                channel_id, DEFAULT_HOSTED_BY, data["rules"],
                int(data["created_at"]), int(data["ends_at"]),
                "ACTIVE", int(data["auto_draw"]), data["old_winner_mode"]
            )
        )
        await db.commit()

    # Post to channel
    seconds_left = int(data["duration"])
    text = giveaway_post_text(
        title=data["title"], prize=data["prize"], participants=0,
        winners_total=int(data["winners_total"]),
        seconds_left=seconds_left, rules=data["rules"], hosted_by=DEFAULT_HOSTED_BY
    )
    try:
        msg = await bot.send_message(channel_id, text, reply_markup=kb_join(gid))
        await update_giveaway_fields(gid, msg_giveaway=msg.message_id)
    except Exception as e:
        await c.message.answer(f"❌ Could not post to channel. Error: {e}")
        await state.clear()
        return await c.answer()

    await c.message.answer("✅ Giveaway approved and posted to channel!")
    await state.clear()
    await c.answer()

# =========================
# Join Giveaway Callback
# =========================

@dp.callback_query(F.data.startswith("join:"))
async def cb_join(c: CallbackQuery):
    gid = c.data.split(":", 1)[1]
    g = await get_giveaway(gid)
    if not g:
        return await c.answer("Giveaway not found.", show_alert=True)

    if g["status"] != "ACTIVE":
        return await c.answer("This giveaway is closed.", show_alert=True)

    user = c.from_user
    uid = user.id

    if await is_blocked(uid):
        return await c.answer("You are blocked.", show_alert=True)

    # Old winner protection at entry time:
    if g["old_winner_mode"] == "BLOCK" and await was_winner_before(uid):
        # Add to blocks automatically
        await add_block(uid, with_at(user.username) or "", "Old winner blocked by mode")
        return await c.answer("You are not allowed (old winner).", show_alert=True)

    # Already joined
    if await participant_exists(gid, uid):
        # If first join champion, show same popup always
        first = await get_first_join(gid)
        if first and first[0] == uid:
            uname = with_at(user.username) or "N/A"
            return await c.answer(
                "🥇 FIRST JOIN CHAMPION 🌟\n"
                "Congratulations! You joined\n"
                "the giveaway FIRST and secured\n"
                f"👤 {uname}\n"
                f"🆔 {uid}\n"
                "📸 Please take a screenshot\n"
                "and post it in the group\n"
                "to confirm 👈",
                show_alert=True
            )
        return await c.answer("✅ You already joined.", show_alert=True)

    # Set first join if none
    first = await get_first_join(gid)
    is_first = 0
    if not first:
        is_first = 1

    uname = with_at(user.username)  # may be None
    await add_participant(gid, uid, uname or "", is_first)
    if is_first == 1:
        await set_first_join(gid, uid)
        await c.answer(
            "🥇 FIRST JOIN CHAMPION 🌟\n"
            "Congratulations! You joined\n"
            "the giveaway FIRST and secured\n"
            f"👤 {uname if uname else 'N/A'}\n"
            f"🆔 {uid}\n"
            "📸 Please take a screenshot\n"
            "and post it in the group\n"
            "to confirm 👈",
            show_alert=True
        )
    else:
        await c.answer("✅ Entry confirmed!", show_alert=True)

    # Update giveaway post participants count (safe edit)
    try:
        part = await count_participants(gid)
        ends_at = int(g["ends_at"])
        left = max(0, ends_at - int(time.time()))
        text = giveaway_post_text(
            title=g["title"], prize=g["prize"], participants=part,
            winners_total=int(g["winners_total"]),
            seconds_left=left, rules=g["rules"], hosted_by=g["hosted_by"]
        )
        await bot.edit_message_text(
            chat_id=int(g["channel_id"]),
            message_id=int(g["msg_giveaway"]),
            text=text,
            reply_markup=kb_join(gid)
        )
    except Exception:
        pass

# =========================
# Background watcher: Close + Auto Select Start (FIXED)
# =========================

async def close_giveaway_if_needed(gid: str):
    g = await get_giveaway(gid)
    if not g:
        return
    if g["status"] != "ACTIVE":
        return
    if int(time.time()) < int(g["ends_at"]):
        return

    # Close giveaway
    part = await count_participants(gid)
    close_text = giveaway_closed_text(g["prize"], part, int(g["winners_total"]))
    try:
        msg = await bot.send_message(int(g["channel_id"]), close_text)
        await update_giveaway_fields(gid, status="CLOSED", msg_close=msg.message_id)
    except Exception:
        await update_giveaway_fields(gid, status="CLOSED")

    # If AutoDraw ON at giveaway creation time OR global ON now => start selection
    auto_now = await get_autodraw_flag()
    if int(g["auto_draw"]) == 1 or auto_now:
        await start_selection_flow(gid, auto=True)

async def watcher_loop():
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT id FROM giveaways WHERE status='ACTIVE'")
                rows = await cur.fetchall()
            for (gid,) in rows:
                await close_giveaway_if_needed(gid)
        except Exception:
            pass
        await asyncio.sleep(2)

# =========================
# Manual draw
# =========================

@dp.message(Command("draw"))
async def cmd_draw(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    await start_manual_draw_latest(m)

async def start_manual_draw_latest(m: Message):
    gid = await get_latest_giveaway_id()
    if not gid:
        return await m.answer("No giveaways found.")
    g = await get_giveaway(gid)
    if not g:
        return await m.answer("No giveaways found.")

    # Must be closed or ended
    if g["status"] == "ACTIVE":
        return await m.answer("Giveaway is still active. Use /endgiveaway or wait until it ends.")
    if g["status"] in ("SELECTING", "ANNOUNCED"):
        return await m.answer("Winner selection already started.")

    await start_selection_flow(gid, auto=False, require_approval=True, admin_chat_id=m.chat.id)

# =========================
# Selection Flow (10 minutes, random timings, fixed edits, Lucky Draw at 05:55)
# =========================

SELECTION_LOCKS: Dict[str, asyncio.Lock] = {}

async def start_selection_flow(gid: str, auto: bool, require_approval: bool = False, admin_chat_id: Optional[int] = None):
    g = await get_giveaway(gid)
    if not g:
        return

    # Set status to SELECTING
    await update_giveaway_fields(gid, status="SELECTING")

    # Post selection message and pin
    participants = await get_participants_usernames(gid)

    # If no valid username participants, still show selection, but winners might be 0
    winners_total = int(g["winners_total"])
    duration_seconds = 10 * 60  # 10 minutes fixed
    start_ts = int(time.time())
    end_ts = start_ts + duration_seconds

    # Initial showcase lines
    colors = pick_three_distinct_colors()
    demo = participants[:3] if participants else []
    def mk_line(color, idx):
        if idx < len(demo):
            uid, uname = demo[idx]
            return f"{color} Now Showing → {uname} | 🆔 {uid}"
        return f"{color} Now Showing → N/A | 🆔 0"

    showcase = [mk_line(colors[0], 0), mk_line(colors[1], 1), mk_line(colors[2], 2)]
    text = selection_post_text(g["title"], g["prize"], winners_selected=0, winners_total=winners_total, pct=1, time_left=duration_seconds, showcase_lines=showcase)

    try:
        msg = await bot.send_message(int(g["channel_id"]), text, reply_markup=kb_selection_buttons(gid))
        await update_giveaway_fields(gid, msg_select=msg.message_id)
        try:
            await bot.pin_chat_message(int(g["channel_id"]), msg.message_id, disable_notification=True)
        except Exception:
            pass
    except Exception:
        return

    # If require approval (manual draw), ask admin approve/reject
    if require_approval and admin_chat_id:
        await bot.send_message(
            admin_chat_id,
            "✅ Selection started.\n"
            "Winners post will be created automatically when selection completes."
        )

    # Start background task for live updates and winner picking
    if gid not in SELECTION_LOCKS:
        SELECTION_LOCKS[gid] = asyncio.Lock()
    asyncio.create_task(selection_loop(gid, start_ts, end_ts))

async def selection_loop(gid: str, start_ts: int, end_ts: int):
    async with SELECTION_LOCKS[gid]:
        g = await get_giveaway(gid)
        if not g:
            return

        channel_id = int(g["channel_id"])
        msg_id = g["msg_select"]
        if not msg_id:
            return

        winners_total = int(g["winners_total"])

        # Prepare valid pool: only participants with @username
        pool = await get_participants_usernames(gid)
        random.shuffle(pool)

        # Old winner protection at selection time:
        # SKIP => remove old winners from pool
        if g["old_winner_mode"] == "SKIP":
            filtered = []
            for uid, uname in pool:
                if not await was_winner_before(uid):
                    filtered.append((uid, uname))
            pool = filtered

        # If BLOCK old winners, they were blocked at join; still safe to filter
        filtered2 = []
        for uid, uname in pool:
            if not await is_blocked(uid):
                filtered2.append((uid, uname))
        pool = filtered2

        # Winner schedule: random times inside 10 minutes, one by one (not fixed looking)
        # If pool smaller than winners_total, will select as many as possible.
        max_pick = min(winners_total, len(pool))
        if max_pick <= 0:
            # still do live update, then finalize with 0 winners
            pass

        # Choose random pick moments
        pick_times = []
        for _ in range(max_pick):
            # random within 10 minutes, but not too early
            pick_times.append(random.randint(20, max(25, (end_ts - start_ts) - 20)))
        pick_times.sort()

        next_pick_index = 0
        winners_selected = 0

        # Showcase rotations:
        # 1st line changes every 5 sec, 2nd every 7 sec, 3rd every 9 sec
        idx1 = idx2 = idx3 = 0
        used_in_cycle = set()  # ensure each user shown once per cycle of all users
        all_users = pool.copy()
        if not all_users:
            all_users = [(0, "@username")]

        def next_unique_user():
            nonlocal used_in_cycle, all_users
            # show all users once, then restart cycle
            if len(used_in_cycle) >= len(all_users):
                used_in_cycle = set()
            # pick next unused sequentially for stability
            for uid, uname in all_users:
                if uid not in used_in_cycle:
                    used_in_cycle.add(uid)
                    return uid, uname
            # fallback
            uid, uname = random.choice(all_users)
            return uid, uname

        last_edit_text = None

        while True:
            now = int(time.time())
            if now >= end_ts:
                break

            elapsed = now - start_ts
            remaining = end_ts - now

            # Progress % smoothly
            pct = int((elapsed / max(1, (end_ts - start_ts))) * 100)
            pct = max(1, min(99, pct))

            # Winner picks
            while next_pick_index < len(pick_times) and elapsed >= pick_times[next_pick_index]:
                next_pick_index += 1
                if winners_selected < max_pick:
                    uid, uname = pool[winners_selected]
                    # Add as RANDOM winner
                    await add_winner(gid, uid, uname, "RANDOM", rank=winners_selected + 1, prize=g["prize"])
                    winners_selected += 1

            # Ensure FIRST JOIN champion added (but only if has username)
            first = await get_first_join(gid)
            if first:
                f_uid, f_uname = first
                f_uname = f_uname.strip()
                if f_uname and f_uname.startswith("@"):
                    # Add FIRST if not exists
                    existing = await get_winner_by_user(gid, f_uid)
                    if not existing:
                        await add_winner(gid, f_uid, f_uname, "FIRST", rank=0, prize=g["prize"])

            # Lucky draw open exactly at Time Remaining 05:55
            # We only allow win if remaining == 355 (with small tolerance)
            # handled in callback; here just show live.

            # Showcase changes:
            # We update text every second for smooth time/progress (no stuck),
            # but swap showcase lines at 5/7/9 second intervals.
            if elapsed % 5 == 0:
                idx1 += 1
            if elapsed % 7 == 0:
                idx2 += 1
            if elapsed % 9 == 0:
                idx3 += 1

            # Build showcase lines each tick, but users rotate at their intervals
            c1, c2, c3 = pick_three_distinct_colors()

            # Choose users (unique cycle)
            u1 = next_unique_user() if elapsed % 5 == 0 else u1 if 'u1' in locals() else next_unique_user()
            u2 = next_unique_user() if elapsed % 7 == 0 else u2 if 'u2' in locals() else next_unique_user()
            u3 = next_unique_user() if elapsed % 9 == 0 else u3 if 'u3' in locals() else next_unique_user()

            line1 = f"{c1} Now Showing → {u1[1]} | 🆔 {u1[0]}"
            line2 = f"{c2} Now Showing → {u2[1]} | 🆔 {u2[0]}"
            line3 = f"{c3} Now Showing → {u3[1]} | 🆔 {u3[0]}"

            # Winners selected count displayed includes FIRST + LUCKY + RANDOM rows
            total_selected = await winner_count(gid)

            text = selection_post_text(
                g["title"], g["prize"],
                winners_selected=total_selected,
                winners_total=winners_total,
                pct=pct,
                time_left=remaining,
                showcase_lines=[line1, line2, line3]
            )

            # Edit only if changed to avoid "message is not modified"
            if text != last_edit_text:
                try:
                    await bot.edit_message_text(
                        chat_id=channel_id,
                        message_id=int(msg_id),
                        text=text,
                        reply_markup=kb_selection_buttons(gid)
                    )
                    last_edit_text = text
                except Exception:
                    pass

            await asyncio.sleep(1)

        # finalize
        await finalize_winners_post(gid)

async def finalize_winners_post(gid: str):
    g = await get_giveaway(gid)
    if not g:
        return

    channel_id = int(g["channel_id"])
    winners_total = int(g["winners_total"])

    # Remove close + selection post when announcing (as you wanted)
    # We remove by deleting messages (if bot has rights)
    for k in ("msg_close", "msg_select"):
        mid = g.get(k)
        if mid:
            try:
                await bot.delete_message(channel_id, int(mid))
            except Exception:
                pass

    # Build winners post
    winners = await get_winners(gid)
    dcount = await delivery_count(gid)

    # Find first join champion (display in a separate block)
    first_user = None
    others = []
    for w in winners:
        if w["kind"] == "FIRST":
            first_user = w
        else:
            others.append(w)

    # Sort others by rank
    others.sort(key=lambda x: x["rank"])

    # Header must be single-line (your request)
    header = "🏆 GIVEAWAY WINNER ANNOUNCEMENT 🏆"

    lines = [
        header,
        "",
        f"{DEFAULT_HOSTED_BY}",
        "",
        f"🆔 Giveaway ID: <b>{gid}</b>",
        "",
        f"🎁 PRIZE:",
        f"<b>{g['prize']}</b>",
        "",
        f"📦 Prize Delivery: <b>{dcount}/{winners_total}</b>",
        "",
    ]

    if first_user:
        lines += [
            "🥇 ⭐ <b>FIRST JOIN CHAMPION</b> ⭐",
            f"👑 {first_user['username']}",
            f"🆔 <code>{first_user['user_id']}</code>",
            ""
        ]

    lines += ["👑 <b>OTHER WINNERS</b>"]

    idx = 1
    for w in others:
        status = "✅ Delivered" if w["delivered_at"] else "⏳ Pending"
        lines.append(f"{idx}️⃣ 👤 {w['username']} | 🆔 <code>{w['user_id']}</code> | {status}")
        idx += 1

    lines += [
        "",
        "👇 Click the button below to claim your prize",
        "",
        "⏳ Rule: Claim within 24 hours — after that, prize expires."
    ]

    text = "\n".join(lines)

    # Post winners + pin
    msg = await bot.send_message(channel_id, text, reply_markup=kb_claim(gid))
    try:
        await bot.pin_chat_message(channel_id, msg.message_id, disable_notification=True)
    except Exception:
        pass

    # Claim deadline 24h
    deadline = int(time.time()) + 24 * 3600
    await update_giveaway_fields(gid, status="ANNOUNCED", msg_winners=msg.message_id, claim_deadline=deadline)

# =========================
# Try Your Luck + Entry Rule
# =========================

LUCKY_LOCKS: Dict[str, asyncio.Lock] = {}

@dp.callback_query(F.data.startswith("rule:"))
async def cb_rule(c: CallbackQuery):
    gid = c.data.split(":", 1)[1]
    g = await get_giveaway(gid)
    if not g or not g.get("msg_select"):
        return await c.answer("Not available.", show_alert=True)

    # Compute remaining based on selection loop target (10 minutes)
    # We store selection started time? For simplicity, infer from msg edit timestamp isn't accessible.
    # So we approximate: if selecting, show "unknown" safe.
    current_remaining = "Unknown"
    await c.answer(entry_rule_popup_text(current_remaining), show_alert=True)

@dp.callback_query(F.data.startswith("luck:"))
async def cb_luck(c: CallbackQuery):
    gid = c.data.split(":", 1)[1]
    g = await get_giveaway(gid)
    if not g or g["status"] != "SELECTING":
        return await c.answer("Lucky Draw is not available.", show_alert=True)

    user = c.from_user
    uid = user.id
    uname = with_at(user.username)

    if not uname:
        return await c.answer(no_username_popup(), show_alert=True)

    # Use a lock to guarantee "first click wins" under high load
    if gid not in LUCKY_LOCKS:
        LUCKY_LOCKS[gid] = asyncio.Lock()

    async with LUCKY_LOCKS[gid]:
        g = await get_giveaway(gid)
        # If already has lucky winner -> too late
        if g.get("lucky_user_id"):
            return await c.answer(too_late_popup(g.get("lucky_username") or "@unknown", int(g["lucky_user_id"])), show_alert=True)

        # We need exact time remaining 05:55
        # We approximate by reading selection message text and parsing remaining from it
        # (This is robust and works with your live edited post)
        try:
            sel_mid = int(g["msg_select"])
            sel_msg = await bot.get_message(chat_id=int(g["channel_id"]), message_id=sel_mid)  # may fail on some bots
            txt = sel_msg.text or ""
        except Exception:
            txt = ""

        m = re.search(r"Time Remaining:\s*([0-9:]{5,8})", txt)
        current = m.group(1) if m else None

        if current != "00:05:55":
            return await c.answer(not_yet_popup_text(current or "Unknown"), show_alert=True)

        # Winner wins now
        # Add lucky to DB + to winners list
        await update_giveaway_fields(gid, lucky_user_id=uid, lucky_username=uname)
        # Determine next rank after existing RANDOM (but rank not used for display order except sorting)
        existing_total = await winner_count(gid)
        await add_winner(gid, uid, uname, "LUCKY", rank=existing_total + 1, prize=g["prize"])

        # Update selection post immediately (optional)
        await c.answer(lucky_win_popup(uname, uid), show_alert=True)

# =========================
# Claim button (with 24h logic + delivered logic + completed)
# =========================

@dp.callback_query(F.data.startswith("claim:"))
async def cb_claim(c: CallbackQuery):
    gid = c.data.split(":", 1)[1]
    g = await get_giveaway(gid)
    if not g or not g.get("msg_winners"):
        return await c.answer("Not available.", show_alert=True)

    uid = c.from_user.id
    uname = with_at(c.from_user.username) or "N/A"

    deadline = int(g.get("claim_deadline") or 0)
    now = int(time.time())

    # After 24h: everyone sees completed
    if deadline and now > deadline:
        return await c.answer(giveaway_completed_popup(), show_alert=True)

    w = await get_winner_by_user(gid, uid)
    if not w:
        return await c.answer(not_winner_popup(), show_alert=True)

    # Winner but expired
    if deadline and now > deadline:
        return await c.answer(expired_popup(), show_alert=True)

    # Delivered
    if w["delivered_at"]:
        return await c.answer(already_delivered_popup(w["username"] or uname, uid), show_alert=True)

    # Pending winner claim
    return await c.answer(
        "🌟 Congratulations!\n"
        "You’ve won this giveaway.\n\n"
        f"👤 {w['username']}\n"
        f"🆔 {uid}\n"
        f"📩 Please contact admin to claim your prize:\n"
        f"👉 {BOT_OWNER_USERNAME}",
        show_alert=True
    )

# =========================
# Prize Delivered flow (FIXED with validation + proper editing)
# =========================

@dp.message(Command("prizedelivered"))
async def cmd_prizedelivered(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    await state.set_state(PrizeDeliveryFlow.ask_gid)
    await m.answer(
        "📦 <b>PRIZE DELIVERY UPDATE</b>\n\n"
        "Step 1/2 — Send Giveaway ID (example: P857-P583-B6714)\n"
        "OR send: <b>latest</b>\n\n"
        "After that, I will ask for delivered users list."
    )

@dp.message(PrizeDeliveryFlow.ask_gid)
async def pd_ask_gid(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await state.clear()

    t = (m.text or "").strip()
    if t.lower() == "latest":
        gid = await get_latest_giveaway_id()
        if not gid:
            return await m.answer("No giveaways found.")
    else:
        gid = t

    g = await get_giveaway(gid)
    if not g:
        return await m.answer("❌ Giveaway not found. Send a valid Giveaway ID or 'latest'.")

    await state.update_data(gid=gid)
    await state.set_state(PrizeDeliveryFlow.ask_list)
    await m.answer(f"✅ Giveaway selected: <b>{gid}</b>\n\n"
                   "Step 2/2 — Send delivered users list (one per line):\n"
                   "<code>@username | user_id</code>\n\n"
                   "Example:\n"
                   "<code>@MinexxProo | 5692210187</code>")

@dp.message(PrizeDeliveryFlow.ask_list)
async def pd_ask_list(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await state.clear()

    data = await state.get_data()
    gid = data["gid"]
    g = await get_giveaway(gid)
    if not g or not g.get("msg_winners"):
        await state.clear()
        return await m.answer("❌ Winners post not found for this giveaway.")

    raw = (m.text or "").strip().splitlines()
    parsed = []
    invalid_lines = []
    for line in raw:
        p = normalize_pair_line(line)
        if not p:
            invalid_lines.append(line)
        else:
            parsed.append(p)

    if invalid_lines:
        return await m.answer(
            "⚠️ Invalid lines found. Please resend only valid format:\n"
            "<code>@username | user_id</code>\n\n"
            "Invalid:\n" + "\n".join(f"• {x}" for x in invalid_lines[:10])
        )

    # Validate against winners list
    winners = await get_winners(gid)
    winners_map = {w["user_id"]: w for w in winners}

    updated = 0
    errors = []
    for uname, uid in parsed:
        if uid not in winners_map:
            errors.append(f"❌ Not a winner: {uname or 'User'} | {uid}")
            continue
        w = winners_map[uid]
        # username check if provided
        if uname and w["username"] and uname.lower() != w["username"].lower():
            errors.append(f"⚠️ Username mismatch for {uid}\nExpected: {w['username']}\nGot: {uname}")
            continue
        if w["delivered_at"]:
            errors.append(f"ℹ️ Already delivered: {w['username']} | {uid}")
            continue
        await mark_delivered(gid, uid)
        updated += 1

    # Edit winners post with updated delivery + ✅ Delivered
    try:
        await edit_winners_post(gid)
    except Exception as e:
        errors.append(f"⚠️ Could not edit winners post: {e}")

    msg = (
        "✅ Prize delivery updated successfully.\n"
        f"Giveaway ID: <b>{gid}</b>\n"
        f"Updated: <b>{updated}</b> winner(s)"
    )
    if errors:
        msg += "\n\n" + "\n\n".join(errors[:10])

    await m.answer(msg)
    await state.clear()

async def edit_winners_post(gid: str):
    g = await get_giveaway(gid)
    if not g or not g.get("msg_winners"):
        return
    channel_id = int(g["channel_id"])
    msg_id = int(g["msg_winners"])
    winners_total = int(g["winners_total"])
    dcount = await delivery_count(gid)

    winners = await get_winners(gid)

    first_user = None
    others = []
    for w in winners:
        if w["kind"] == "FIRST":
            first_user = w
        else:
            others.append(w)
    others.sort(key=lambda x: x["rank"])

    header = "🏆 GIVEAWAY WINNER ANNOUNCEMENT 🏆"

    lines = [
        header,
        "",
        f"{DEFAULT_HOSTED_BY}",
        "",
        f"🆔 Giveaway ID: <b>{gid}</b>",
        "",
        f"🎁 PRIZE:",
        f"<b>{g['prize']}</b>",
        "",
        f"📦 Prize Delivery: <b>{dcount}/{winners_total}</b>",
        "",
    ]
    if first_user:
        lines += [
            "🥇 ⭐ <b>FIRST JOIN CHAMPION</b> ⭐",
            f"👑 {first_user['username']}",
            f"🆔 <code>{first_user['user_id']}</code>",
            ""
        ]

    lines += ["👑 <b>OTHER WINNERS</b>"]
    idx = 1
    for w in others:
        status = "✅ Delivered" if w["delivered_at"] else "⏳ Pending"
        lines.append(f"{idx}️⃣ 👤 {w['username']} | 🆔 <code>{w['user_id']}</code> | {status}")
        idx += 1

    lines += [
        "",
        "👇 Click the button below to claim your prize",
        "",
        "⏳ Rule: Claim within 24 hours — after that, prize expires."
    ]
    new_text = "\n".join(lines)

    # To avoid "message is not modified", fetch current text by editing with compare is not possible;
    # We just try and ignore if same.
    try:
        await bot.edit_message_text(
            chat_id=channel_id,
            message_id=msg_id,
            text=new_text,
            reply_markup=kb_claim(gid)
        )
    except Exception:
        pass

# =========================
# Winner list history
# =========================

@dp.message(Command("winnerlist"))
async def cmd_winnerlist(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    await send_winnerlist(m)

async def send_winnerlist(m: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT giveaway_id,user_id,username,prize,won_at FROM winner_history ORDER BY won_at DESC LIMIT 50"
        )
        rows = await cur.fetchall()

    if not rows:
        return await m.answer("No winners yet.")

    out = ["🏆 <b>WINNER LIST (History)</b>\n"]
    for gid, uid, uname, prize, won_at in rows:
        dt = datetime.fromtimestamp(int(won_at), tz=UTC).strftime("%d-%m-%Y")
        out.append(
            f"• Giveaway: <b>{gid}</b>\n"
            f"  👤 {uname or 'N/A'} | 🆔 <code>{int(uid)}</code>\n"
            f"  🎁 {prize}\n"
            f"  📅 {dt}\n"
        )
    await m.answer("\n".join(out))

# =========================
# Block system
# =========================

@dp.message(Command("blockpermanent"))
async def cmd_blockpermanent(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    await state.set_state(BlockFlow.ask_list)
    await m.answer(
        "🔒 <b>PERMANENT BLOCK</b>\n\n"
        "Send list (one per line):\n"
        "User ID only OR username + id\n\n"
        "Examples:\n"
        "<code>7297292</code>\n"
        "<code>@MinexxProo | 7297292</code>"
    )

@dp.message(BlockFlow.ask_list)
async def block_list(m: Message, state: FSMContext):
    raw = (m.text or "").splitlines()
    added = 0
    bad = 0
    for line in raw:
        p = normalize_pair_line(line)
        if not p:
            bad += 1
            continue
        uname, uid = p
        await add_block(uid, uname or "", "Permanent block")
        added += 1
    await m.answer(f"✅ Block list updated.\nAdded: {added}\nInvalid: {bad}")
    await state.clear()

@dp.message(Command("unban"))
async def cmd_unban(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    t = (m.text or "").split()
    if len(t) < 2 or not t[1].isdigit():
        return await m.answer("Usage: /unban 123456789")
    uid = int(t[1])
    await remove_block(uid)
    await m.answer("✅ Unbanned.")

@dp.message(Command("blocklist"))
async def cmd_blocklist(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, username, reason, blocked_at FROM blocks ORDER BY blocked_at DESC LIMIT 50")
        rows = await cur.fetchall()
    if not rows:
        return await m.answer("Blocklist is empty.")
    out = ["🔒 <b>BLOCKLIST</b>\n"]
    for uid, uname, reason, ts in rows:
        out.append(f"• 👤 {uname or 'N/A'} | 🆔 <code>{int(uid)}</code>\n  Reason: {reason}")
    await m.answer("\n".join(out))

@dp.message(Command("removeban"))
async def cmd_removeban(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    t = (m.text or "").split()
    if len(t) < 2 or not t[1].isdigit():
        return await m.answer("Usage: /removeban 123456789")
    uid = int(t[1])
    await remove_block(uid)
    await m.answer("✅ Removed from blocklist.")

# =========================
# Reset (confirm + 40s progress)
# =========================

@dp.message(Command("reset"))
async def cmd_reset(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    await state.set_state(ResetFlow.confirm)
    await m.answer(
        "♻️ <b>RESET WARNING</b>\n\n"
        "This will erase all giveaways, participants, winners, and settings.\n"
        "Do you want to continue?",
        reply_markup=kb_reset_confirm()
    )

@dp.callback_query(F.data.in_({"rs:yes", "rs:no"}))
async def cb_reset(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Admins only.", show_alert=True)
    if c.data == "rs:no":
        await state.clear()
        await c.message.answer("❌ Reset cancelled.")
        return await c.answer()

    # 40s progress
    msg = await c.message.answer("♻️ Resetting… 0%")
    start = time.time()
    duration = 40
    while True:
        elapsed = time.time() - start
        pct = int((elapsed / duration) * 100)
        pct = max(0, min(100, pct))
        bar = progress_bar(pct, 10)
        try:
            await bot.edit_message_text(
                chat_id=msg.chat.id,
                message_id=msg.message_id,
                text=f"♻️ Resetting… <b>{pct}%</b>\n{bar}"
            )
        except Exception:
            pass
        if pct >= 100:
            break
        await asyncio.sleep(1)

    # wipe tables
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM giveaways")
        await db.execute("DELETE FROM participants")
        await db.execute("DELETE FROM winners")
        await db.execute("DELETE FROM winner_history")
        await db.execute("DELETE FROM blocks")
        await db.execute("DELETE FROM settings")
        await db.commit()

    await state.clear()
    await c.message.answer("✅ Reset completed. Bot is now fresh.")

# =========================
# Optional: /endgiveaway (force end latest)
# =========================

@dp.message(Command("endgiveaway"))
async def cmd_endgiveaway(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    gid = await get_latest_giveaway_id()
    if not gid:
        return await m.answer("No giveaways found.")
    g = await get_giveaway(gid)
    if not g or g["status"] != "ACTIVE":
        return await m.answer("Latest giveaway is not active.")
    await update_giveaway_fields(gid, ends_at=int(time.time()))
    await m.answer("✅ Giveaway will close now (watcher will handle).")

# =========================
# Startup
# =========================

async def main():
    await db_init()
    # default autodraw = 0 if missing
    if await db_get_setting("autodraw") is None:
        await set_autodraw_flag(False)

    asyncio.create_task(watcher_loop())  # FIXED auto start
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
