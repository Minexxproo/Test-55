# main.py
# Telegram Giveaway Management Bot (Full A to Z) — GSM-friendly (SQLite, low RAM)
# Features added per your request:
# ✅ Block system commands: /blockpermanent /unban /blocklist /removeban
# ✅ 5 claim button posts in main channel + per-giveaway claim mapping
# ✅ Manual draw approval flow (approve/reject before posting winners)
# ✅ AutoDraw ON: giveaway end হলে auto selection start + auto winners post
# ✅ Username missing users cannot join (hard block at join)
# ✅ Selection showcase: full cycle one-by-one then repeat until time ends (3 lines rotating)
# ✅ PrizeDelivered: Step1 GiveawayID -> Step2 list, validates username+id exist in winners list
# ✅ Lucky Draw: Try Your Luck + Entry Rule buttons; opens exactly at Time Remaining 05:55 (1s window)
# ✅ /reset confirm + 40s progress
# ✅ /winnerlist history with date-month-year
#
# NOTE:
# - Bot can’t “fetch username from user_id” reliably via Telegram API.
#   So validation is: your input must match winner list stored by bot.
#
# python-telegram-bot v21+

import asyncio
import contextlib
import json
import os
import random
import re
import sqlite3
import string
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

# =========================
# CONFIG (EDIT THESE)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Admin user IDs (comma separated) e.g. "123,456"
ADMIN_IDS = set(
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
)

# Owner (receives unauthorized start notifications)
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")

# Main channel username (for display) e.g. "@PowerPointBreak"
OFFICIAL_CHANNEL = os.getenv("OFFICIAL_CHANNEL", "@PowerPointBreak").strip()

# Bot owner username display
BOT_OWNER_USERNAME = os.getenv("BOT_OWNER_USERNAME", "@MinexxProo").strip()

# Main channel id (must be numeric like -100xxxxxxxxxx)
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0") or "0")

# Hosted by label
HOST_LABEL = os.getenv("HOST_LABEL", "POWER POINT BREAK").strip()

# DB path
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()

# Timezone
TZ = timezone(timedelta(hours=6))  # +06:00


# =========================
# CONSTANTS / UI
# =========================
SEP = "━━━━━━━━━━━━━━━━━━━━"
SEP2 = "━━━━━━━━━━━━━━━━━━━━━━"
DOTS = ""  # User asked: no dot spinner texts

COLOR_EMOJIS = ["🟣", "🟠", "🟡", "⚫", "🔵", "🟢", "🟤", "🔴", "⚪"]

# Conversation states
(
    NEW_TITLE,
    NEW_PRIZE,
    NEW_WINNERS,
    NEW_DURATION,
    NEW_OLDWINNER_MODE,
    NEW_RULES,
    NEW_APPROVE,

    PRIZEDELIV_STEP1,
    PRIZEDELIV_STEP2,

    BLOCK_LIST_IN,
    UNBAN_LIST_IN,
    REMOVEBAN_LIST_IN,

    RESET_CONFIRM,
) = range(16)


# =========================
# DB LAYER
# =========================
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS giveaways (
        gid TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        prize TEXT NOT NULL,
        winners_total INTEGER NOT NULL,
        duration_sec INTEGER NOT NULL,
        rules TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        ends_at INTEGER NOT NULL,
        status TEXT NOT NULL, -- draft/active/closed/selecting/announced/completed
        autodraw INTEGER NOT NULL DEFAULT 0,
        old_winner_mode TEXT NOT NULL DEFAULT 'skip', -- skip/block/none
        giveaway_post_mid INTEGER,
        close_post_mid INTEGER,
        selection_post_mid INTEGER,
        winners_post_mid INTEGER,
        claim_mids TEXT, -- json list of 5 message_ids in channel
        lucky_user_id INTEGER,
        lucky_username TEXT,
        lucky_won_at INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        gid TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        joined_at INTEGER NOT NULL,
        is_first INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (gid, user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS winners (
        gid TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        rank INTEGER NOT NULL, -- 0 for first join champion, 1..N for other winners, 999 for lucky slot if needed
        is_first_join INTEGER NOT NULL DEFAULT 0,
        won_at INTEGER NOT NULL,
        delivered_at INTEGER,
        delivered_by INTEGER,
        PRIMARY KEY (gid, user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS blocks (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        reason TEXT,
        created_at INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL
    )
    """)

    con.commit()
    con.close()


def set_setting(k: str, v: str):
    con = db()
    con.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    con.commit()
    con.close()


def get_setting(k: str, default: str = "") -> str:
    con = db()
    row = con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    con.close()
    return row["v"] if row else default


def now_ts() -> int:
    return int(time.time())


def fmt_dt(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, TZ)
    return dt.strftime("%d-%m-%Y")


def new_gid() -> str:
    # P788-P686-B6548
    p1 = random.randint(100, 999)
    p2 = random.randint(100, 999)
    b = random.randint(1000, 9999)
    return f"P{p1}-P{p2}-B{b}"


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def username_of(update: Update) -> str:
    u = update.effective_user
    if not u:
        return ""
    return f"@{u.username}" if u.username else ""


def ensure_valid_username(u: str) -> bool:
    return bool(re.fullmatch(r"@[A-Za-z0-9_]{5,32}", u))


def is_blocked(uid: int) -> bool:
    con = db()
    row = con.execute("SELECT 1 FROM blocks WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return bool(row)


def add_block(uid: int, uname: str, reason: str):
    con = db()
    con.execute(
        "INSERT INTO blocks(user_id, username, reason, created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, reason=excluded.reason, created_at=excluded.created_at",
        (uid, uname, reason, now_ts())
    )
    con.commit()
    con.close()


def remove_block(uid: int):
    con = db()
    con.execute("DELETE FROM blocks WHERE user_id=?", (uid,))
    con.commit()
    con.close()


def list_blocks(limit: int = 50) -> List[sqlite3.Row]:
    con = db()
    rows = con.execute("SELECT * FROM blocks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return rows


def get_latest_gid() -> Optional[str]:
    con = db()
    row = con.execute("SELECT gid FROM giveaways ORDER BY created_at DESC LIMIT 1").fetchone()
    con.close()
    return row["gid"] if row else None


def get_giveaway(gid: str) -> Optional[sqlite3.Row]:
    con = db()
    row = con.execute("SELECT * FROM giveaways WHERE gid=?", (gid,)).fetchone()
    con.close()
    return row


def update_giveaway_fields(gid: str, **fields):
    if not fields:
        return
    con = db()
    keys = list(fields.keys())
    vals = [fields[k] for k in keys]
    sets = ", ".join(f"{k}=?" for k in keys)
    con.execute(f"UPDATE giveaways SET {sets} WHERE gid=?", (*vals, gid))
    con.commit()
    con.close()


def insert_giveaway(gid: str, title: str, prize: str, winners_total: int, duration_sec: int, rules: str, ends_at: int):
    con = db()
    con.execute("""
    INSERT INTO giveaways(gid, title, prize, winners_total, duration_sec, rules, created_at, ends_at, status)
    VALUES(?,?,?,?,?,?,?,?,?)
    """, (gid, title, prize, winners_total, duration_sec, rules, now_ts(), ends_at, "active"))
    con.commit()
    con.close()


def count_participants(gid: str) -> int:
    con = db()
    row = con.execute("SELECT COUNT(*) c FROM participants WHERE gid=?", (gid,)).fetchone()
    con.close()
    return int(row["c"]) if row else 0


def get_participants(gid: str) -> List[sqlite3.Row]:
    con = db()
    rows = con.execute("SELECT * FROM participants WHERE gid=? ORDER BY joined_at ASC", (gid,)).fetchall()
    con.close()
    return rows


def add_participant(gid: str, uid: int, uname: str) -> Tuple[bool, bool]:
    # returns (added, is_first)
    con = db()
    cur = con.cursor()
    exists = cur.execute("SELECT 1 FROM participants WHERE gid=? AND user_id=?", (gid, uid)).fetchone()
    if exists:
        con.close()
        return False, False

    first = cur.execute("SELECT 1 FROM participants WHERE gid=? LIMIT 1", (gid,)).fetchone() is None
    cur.execute(
        "INSERT INTO participants(gid, user_id, username, joined_at, is_first) VALUES(?,?,?,?,?)",
        (gid, uid, uname, now_ts(), 1 if first else 0)
    )
    con.commit()
    con.close()
    return True, first


def has_won_before(uid: int) -> bool:
    con = db()
    row = con.execute("SELECT 1 FROM winners WHERE user_id=? LIMIT 1", (uid,)).fetchone()
    con.close()
    return bool(row)


def set_winner(gid: str, uid: int, uname: str, rank: int, is_first_join: int = 0):
    con = db()
    con.execute("""
    INSERT INTO winners(gid, user_id, username, rank, is_first_join, won_at)
    VALUES(?,?,?,?,?,?)
    ON CONFLICT(gid, user_id) DO UPDATE SET
      username=excluded.username,
      rank=excluded.rank,
      is_first_join=excluded.is_first_join
    """, (gid, uid, uname, rank, is_first_join, now_ts()))
    con.commit()
    con.close()


def get_winners(gid: str) -> List[sqlite3.Row]:
    con = db()
    rows = con.execute("SELECT * FROM winners WHERE gid=? ORDER BY rank ASC, won_at ASC", (gid,)).fetchall()
    con.close()
    return rows


def mark_delivered(gid: str, uid: int, delivered_by: int):
    con = db()
    con.execute(
        "UPDATE winners SET delivered_at=?, delivered_by=? WHERE gid=? AND user_id=?",
        (now_ts(), delivered_by, gid, uid)
    )
    con.commit()
    con.close()


def count_delivered(gid: str) -> int:
    con = db()
    row = con.execute("SELECT COUNT(*) c FROM winners WHERE gid=? AND delivered_at IS NOT NULL", (gid,)).fetchone()
    con.close()
    return int(row["c"]) if row else 0


def winner_history(limit: int = 50) -> List[sqlite3.Row]:
    con = db()
    rows = con.execute("""
    SELECT w.gid, w.user_id, w.username, w.rank, w.is_first_join, w.won_at, g.prize
    FROM winners w
    JOIN giveaways g ON g.gid=w.gid
    ORDER BY w.won_at DESC
    LIMIT ?
    """, (limit,)).fetchall()
    con.close()
    return rows


# =========================
# MESSAGE BUILDERS
# =========================
def build_giveaway_post(g: sqlite3.Row) -> str:
    remaining = max(0, g["ends_at"] - now_ts())
    mm, ss = divmod(remaining, 60)
    hh, mm = divmod(mm, 60)
    time_str = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"00:{mm:02d}:{ss:02d}"

    pcount = count_participants(g["gid"])
    wtotal = g["winners_total"]

    return (
        f"{SEP}\n"
        f"⚡ {g['title']} ⚡\n"
        f"{SEP}\n\n"
        f"🎁 PRIZE POOL 🌟\n"
        f"🏆 {g['prize']}\n\n"
        f"👥 Total Participants: {pcount}\n"
        f"🏆 Total Winners: {wtotal}\n\n"
        f"🎯 Winner Selection\n"
        f"• 100% Random & Fair\n"
        f"• Auto System\n\n"
        f"⏱️ Time Remaining: {time_str}\n"
        f"📊 Live Progress\n"
        f"▱▱▱▱▱▱▱▱▱▱\n\n"
        f"📜 Official Rules\n{g['rules']}\n\n"
        f"📢 Hosted by: {HOST_LABEL}\n\n"
        f"{SEP}\n"
        f"👇 Tap below to join the giveaway 👇"
    )


def build_closed_post(g: sqlite3.Row) -> str:
    return (
        f"{SEP2}\n"
        f"🚫 GIVEAWAY CLOSED 🚫\n"
        f"{SEP2}\n\n"
        f"⏰ The giveaway has officially ended.\n"
        f"🔒 All entries are now locked.\n\n"
        f"📊 Giveaway Summary\n"
        f"🎁 Prize: {g['prize']}\n\n"
        f"👥 Total Participants: {count_participants(g['gid'])}\n"
        f"🏆 Total Winners: {g['winners_total']}\n\n"
        f"🎯 Winners will be announced very soon.\n"
        f"Please stay tuned for the final results.\n\n"
        f"✨ Best of luck to everyone!\n\n"
        f"— {HOST_LABEL} ⚡\n"
        f"{SEP2}"
    )


def progress_bar(pct: int, size: int = 10) -> str:
    filled = max(0, min(size, int(round(size * (pct / 100)))))
    return "▰" * filled + "▱" * (size - filled)


def build_selection_post(
    g: sqlite3.Row,
    pct: int,
    winners_selected: int,
    winners_total: int,
    time_remaining: int,
    showcase: List[Tuple[str, int, str]],
) -> str:
    mm, ss = divmod(time_remaining, 60)
    hh, mm = divmod(mm, 60)
    time_str = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"00:{mm:02d}:{ss:02d}"

    lines = []
    for emoji, uname, uid in showcase:
        lines.append(f"{emoji} Now Showing → {uname} | 🆔 {uid}")

    return (
        f"{SEP}\n"
        f"🎲 LIVE RANDOM WINNER SELECTION\n"
        f"{SEP}\n\n"
        f"⚡ {g['title']} ⚡\n\n"
        f"🎁 GIVEAWAY SUMMARY\n"
        f"🏆 Prize: {g['prize']}\n"
        f"✅ Winners Selected: {winners_selected}/{winners_total}\n\n"
        f"📌 Important Rule\n"
        f"Users without a valid @username\n"
        f"are automatically excluded.\n\n"
        f"⏳ Selection Progress: {pct}%\n"
        f"📊 Progress Bar: {progress_bar(pct)}\n\n"
        f"🕒 Time Remaining: {time_str}\n"
        f"🔐 System Mode: 100% Random • Fair • Auto\n\n"
        f"{SEP}\n"
        f"👥 LIVE ENTRIES SHOWCASE\n"
        f"{SEP}\n"
        f"{chr(10).join(lines)}\n"
        f"{SEP}"
    )


def build_winners_post(g: sqlite3.Row) -> str:
    winners = get_winners(g["gid"])
    delivered = count_delivered(g["gid"])
    total = g["winners_total"]

    # Identify first join champion
    first = next((w for w in winners if w["is_first_join"] == 1 or w["rank"] == 0), None)

    others = [w for w in winners if not (first and w["user_id"] == first["user_id"]) and w["rank"] != 0]
    # sort rank
    others.sort(key=lambda r: r["rank"])

    header = (
        f"🏆 GIVEAWAY WINNER ANNOUNCEMENT 🏆\n\n"
        f"{HOST_LABEL}\n\n"
        f"🆔 Giveaway ID: {g['gid']}\n\n"
        f"🎁 PRIZE:\n{g['prize']}\n\n"
        f"📦 Prize Delivery: {delivered}/{total}\n\n"
    )

    champ = ""
    if first:
        champ = (
            f"🥇 ⭐ FIRST JOIN CHAMPION ⭐\n"
            f"👑 {first['username']}\n"
            f"🆔 {first['user_id']}\n\n"
        )

    other_lines = ["👑 OTHER WINNERS"]
    idx = 1
    for w in others:
        status = "✅ Delivered" if w["delivered_at"] else "⏳ Pending"
        other_lines.append(f"{idx}️⃣ 👤 {w['username']} | 🆔 {w['user_id']} | {status}")
        idx += 1

    footer = (
        "\n\n👇 Click the button below to claim your prize\n\n"
        "⏳ Rule: Claim within 24 hours — after that, prize expires."
    )

    return header + champ + "\n".join(other_lines) + footer


def claim_popup_delivered(u: str, uid: int) -> str:
    return (
        "📦 PRIZE ALREADY DELIVERED\n"
        "Your prize has already been\n"
        "successfully delivered ✅\n"
        f"👤 {u}\n"
        f"🆔 {uid}\n"
        "If you face any issue,\n"
        f"contact admin 👉 {BOT_OWNER_USERNAME}"
    )


def claim_popup_not_winner() -> str:
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry! Your User ID is not\n"
        "in the winners list.\n\n"
        "Please wait for the next\n"
        "giveaway ❤️‍🩹"
    )


def claim_popup_expired_non_winner() -> str:
    return (
        "✅ GIVEAWAY COMPLETED\n\n"
        "This giveaway has been completed.\n"
        f"If you have any issues, please contact admin 👉 {BOT_OWNER_USERNAME}"
    )


def claim_popup_expired_winner(u: str, uid: int) -> str:
    return (
        "⛔ CLAIM EXPIRED\n\n"
        "Your claim window has expired (24 hours).\n"
        f"👤 {u}\n"
        f"🆔 {uid}\n\n"
        f"If you have any issue, contact admin 👉 {BOT_OWNER_USERNAME}"
    )


def lucky_entry_rule_popup() -> str:
    return (
        "📌 ENTRY RULE\n"
        "• Tap 🍀 Try Your Luck at the right moment\n"
        "• First click wins instantly (Lucky Draw)\n"
        "• Must have a valid @username\n"
        "• Winner is added live to the selection post\n"
        "• 100% fair: first-come-first-win"
    )


def lucky_not_yet_popup() -> str:
    return (
        "⏳ NOT YET\n\n"
        "The Lucky Draw is not open yet.\n"
        "Please watch the timer and try again at the right moment."
    )


def lucky_too_late_popup(wu: str, wuid: int) -> str:
    return (
        "⚠️ TOO LATE\n\n"
        "Someone already won the Lucky Draw slot.\n"
        "Winner:\n"
        f"👤 {wu}\n"
        f"🆔 {wuid}\n\n"
        "Please continue watching the live selection."
    )


def lucky_no_participants_popup() -> str:
    return (
        "⚠️ NO ENTRIES\n\n"
        "No eligible participants joined yet.\n"
        "This Lucky Draw slot cannot be claimed right now."
    )


def lucky_win_popup(u: str, uid: int) -> str:
    return (
        "🌟 CONGRATULATIONS!\n"
        "You won the 🍀 Lucky Draw Winner slot ✅\n\n"
        f"👤 {u}\n"
        f"🆔 {uid}\n"
        "Take screenshot and send in the group to confirm 👈\n\n"
        "🏆 Added to winners list LIVE!"
    )


# =========================
# BOT JOBS / RATE LIMIT
# =========================
class EditLimiter:
    def __init__(self):
        self._last = 0.0
        self.min_interval = 0.9  # safe for channel edits

    async def wait(self):
        now = time.time()
        delay = self.min_interval - (now - self._last)
        if delay > 0:
            await asyncio.sleep(delay)
        self._last = time.time()


EDIT_LIMITER = EditLimiter()


async def safe_edit_message(bot, chat_id: int, message_id: int, text: str, reply_markup=None):
    # Avoid "Message is not modified"
    try:
        await EDIT_LIMITER.wait()
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        return True, ""
    except BadRequest as e:
        msg = str(e)
        if "Message is not modified" in msg:
            return False, "not_modified"
        return False, msg
    except Forbidden as e:
        return False, f"forbidden:{e}"
    except Exception as e:
        return False, f"error:{e}"


async def safe_send_message(bot, chat_id: int, text: str, reply_markup=None, pin: bool = False):
    m = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )
    if pin:
        with contextlib.suppress(Exception):
            await bot.pin_chat_message(chat_id=chat_id, message_id=m.message_id, disable_notification=True)
    return m.message_id


async def safe_delete(bot, chat_id: int, message_id: int):
    with contextlib.suppress(Exception):
        await bot.delete_message(chat_id=chat_id, message_id=message_id)


# =========================
# CALLBACK DATA HELPERS
# =========================
def cb_join(gid: str) -> str:
    return f"JOIN|{gid}"


def cb_claim(gid: str) -> str:
    return f"CLAIM|{gid}"


def cb_lucky(gid: str) -> str:
    return f"LUCKY|{gid}"


def cb_entry_rule(gid: str) -> str:
    return f"RULE|{gid}"


# =========================
# ADMIN PANEL
# =========================
def panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 New Giveaway", callback_data="PANEL|NEW")],
        [InlineKeyboardButton("🎲 Draw (Manual)", callback_data="PANEL|DRAW")],
        [InlineKeyboardButton("⚙️ Auto Draw ON/OFF", callback_data="PANEL|AUTO")],
        [InlineKeyboardButton("📦 Prize Delivery", callback_data="PANEL|DELIV")],
        [InlineKeyboardButton("📜 Winner List", callback_data="PANEL|WLIST")],
        [InlineKeyboardButton("🔒 Block System", callback_data="PANEL|BLOCK")],
        [InlineKeyboardButton("♻️ Reset", callback_data="PANEL|RESET")],
    ])


async def cmd_panel(update: Update, context: CallbackContext):
    if not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ Admins only.")
        return
    await update.message.reply_text("Admin Panel:", reply_markup=panel_kb())


# =========================
# /start HANDLER
# =========================
async def cmd_start(update: Update, context: CallbackContext):
    if not update.effective_user or not update.message:
        return

    uid = update.effective_user.id
    uname = username_of(update) or "(no username)"
    if is_admin(uid):
        await update.message.reply_text(
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
        return

    # Non-admin message
    text = (
        f"{SEP}\n"
        f"⚠️ UNAUTHORIZED NOTICE\n"
        f"{SEP}\n\n"
        f"Hi there!\n"
        f"Username: {uname}\n"
        f"User ID: {uid}\n\n"
        "It looks like you tried to start the giveaway,\n"
        "but this action is available for admins only.\n\n"
        "😊 No worries — this is just a friendly heads-up.\n\n"
        "🎁 This is an official Giveaway Bot.\n"
        "For exciting giveaway updates,\n"
        "join our official channel now:\n"
        f"👉 {OFFICIAL_CHANNEL}\n\n"
        "🤖 Powered by:\n"
        f"{HOST_LABEL} — Official Giveaway System\n\n"
        "👤 Bot Owner:\n"
        f"{BOT_OWNER_USERNAME}\n\n"
        "If you think this was a mistake,\n"
        "please feel free to contact an admin anytime.\n"
        "We’re always happy to help!\n"
        f"{SEP}"
    )
    await update.message.reply_text(text)

    # Owner notification
    if OWNER_ID:
        with contextlib.suppress(Exception):
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"⚠️ Unauthorized /start\nUsername: {uname}\nUser ID: {uid}"
            )


# =========================
# NEW GIVEAWAY FLOW
# =========================
@dataclass
class Draft:
    title: str = ""
    prize: str = ""
    winners_total: int = 0
    duration_sec: int = 0
    old_winner_mode: str = "skip"
    rules: str = ""


def parse_duration_to_seconds(s: str) -> Optional[int]:
    # Accept: "30 Second", "5 Minute", "1 Hour", "10 Minute"
    s = s.strip().lower()
    m = re.match(r"^\s*(\d+)\s*(second|seconds|sec|minute|minutes|min|hour|hours|hr)\s*$", s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit.startswith("sec"):
        return n
    if unit.startswith("min"):
        return n * 60
    if unit.startswith("hour") or unit.startswith("hr"):
        return n * 3600
    return None


async def cmd_newgiveaway(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ Admins only.")
        return ConversationHandler.END
    context.user_data["draft"] = Draft()
    await update.message.reply_text("Send Giveaway Title (single line):")
    return NEW_TITLE


async def new_title(update: Update, context: CallbackContext):
    d: Draft = context.user_data["draft"]
    d.title = (update.message.text or "").strip()
    await update.message.reply_text("Send Prize (multi-line allowed):")
    return NEW_PRIZE


async def new_prize(update: Update, context: CallbackContext):
    d: Draft = context.user_data["draft"]
    d.prize = (update.message.text or "").strip()
    await update.message.reply_text("Send Total Winners (number):")
    return NEW_WINNERS


async def new_winners(update: Update, context: CallbackContext):
    d: Draft = context.user_data["draft"]
    txt = (update.message.text or "").strip()
    if not txt.isdigit() or int(txt) <= 0 or int(txt) > 500:
        await update.message.reply_text("Send a valid number (1-500).")
        return NEW_WINNERS
    d.winners_total = int(txt)
    await update.message.reply_text("Send Giveaway Duration (Example: 30 Second / 10 Minute / 1 Hour):")
    return NEW_DURATION


async def new_duration(update: Update, context: CallbackContext):
    d: Draft = context.user_data["draft"]
    sec = parse_duration_to_seconds(update.message.text or "")
    if not sec or sec < 10:
        await update.message.reply_text("Invalid duration. Example: 30 Second / 10 Minute / 1 Hour")
        return NEW_DURATION
    d.duration_sec = sec

    await update.message.reply_text(
        "🔐 OLD WINNER PROTECTION MODE\n\n"
        "1 → BLOCK OLD WINNERS\n"
        "2 → SKIP OLD WINNERS\n\n"
        "Reply with:\n"
        "1 → BLOCK\n"
        "2 → SKIP"
    )
    return NEW_OLDWINNER_MODE


async def new_oldwinner(update: Update, context: CallbackContext):
    d: Draft = context.user_data["draft"]
    choice = (update.message.text or "").strip()
    if choice == "1":
        d.old_winner_mode = "block"
    elif choice == "2":
        d.old_winner_mode = "skip"
    else:
        await update.message.reply_text("Reply 1 or 2.")
        return NEW_OLDWINNER_MODE

    await update.message.reply_text("Now send Giveaway Rules (multi-line):")
    return NEW_RULES


async def new_rules(update: Update, context: CallbackContext):
    d: Draft = context.user_data["draft"]
    # keep exact formatting user sends
    d.rules = (update.message.text or "").strip()

    # Preview
    gid = new_gid()
    ends_at = now_ts() + d.duration_sec

    # Save to DB as active immediately; approval controls channel posting only
    insert_giveaway(gid, d.title, d.prize, d.winners_total, d.duration_sec, d.rules, ends_at)
    update_giveaway_fields(gid, old_winner_mode=d.old_winner_mode, status="active")

    g = get_giveaway(gid)
    preview_text = build_giveaway_post(g)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ APPROVE & POST", callback_data=f"APPROVE|{gid}")],
        [InlineKeyboardButton("❌ REJECT", callback_data=f"REJECT|{gid}")],
    ])
    await update.message.reply_text("✅ Rules saved!\nShowing preview...", disable_web_page_preview=True)
    await update.message.reply_text(preview_text, reply_markup=kb, disable_web_page_preview=True)
    return NEW_APPROVE


async def approve_reject_cb(update: Update, context: CallbackContext):
    q = update.callback_query
    await q.answer()
    data = q.data
    if not update.effective_user or not is_admin(update.effective_user.id):
        await q.answer("Admins only.", show_alert=True)
        return

    parts = data.split("|", 1)
    action = parts[0]
    gid = parts[1]

    g = get_giveaway(gid)
    if not g:
        await q.answer("Giveaway not found.", show_alert=True)
        return

    if action == "REJECT":
        update_giveaway_fields(gid, status="draft")
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("❌ Rejected. Nothing posted to channel.")
        return

    # APPROVE: post giveaway message with join button
    join_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data=cb_join(gid))]
    ])

    try:
        mid = await safe_send_message(context.bot, CHANNEL_ID, build_giveaway_post(g), reply_markup=join_kb, pin=True)
    except Exception as e:
        await q.message.reply_text(f"❌ Could not post to channel.\nReason: {e}")
        return

    update_giveaway_fields(gid, giveaway_post_mid=mid)
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text("✅ Giveaway approved and posted to channel!")
    # schedule auto close job
    schedule_close_job(context.application, gid)
    return


# =========================
# JOIN + POPUPS
# =========================
async def join_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id

    # check blocked
    if is_blocked(uid):
        await q.answer("You are blocked from joining giveaways.", show_alert=True)
        return

    _, gid = q.data.split("|", 1)
    g = get_giveaway(gid)
    if not g or g["status"] not in ("active",):
        await q.answer("This giveaway is not active.", show_alert=True)
        return

    # must have username
    uname = username_of(update)
    if not ensure_valid_username(uname):
        await q.answer("You must have a valid @username to join.", show_alert=True)
        return

    # old winner protection
    if g["old_winner_mode"] in ("skip", "block") and has_won_before(uid):
        if g["old_winner_mode"] == "block":
            add_block(uid, uname, "Old winner blocked")
            await q.answer("Old winners are permanently blocked.", show_alert=True)
            return
        else:
            await q.answer("Old winners are not allowed in this giveaway.", show_alert=True)
            return

    added, is_first = add_participant(gid, uid, uname)
    if not added:
        await q.answer("You already joined.", show_alert=True)
        return

    # If first join champion, record winner rank 0
    if is_first:
        set_winner(gid, uid, uname, rank=0, is_first_join=1)

    # Update giveaway post participant count (best-effort)
    if g["giveaway_post_mid"]:
        g2 = get_giveaway(gid)
        join_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data=cb_join(gid))]])
        await safe_edit_message(context.bot, CHANNEL_ID, g["giveaway_post_mid"], build_giveaway_post(g2), reply_markup=join_kb)

    # First join popup required
    if is_first:
        await q.answer(
            "🥇 FIRST JOIN CHAMPION 🌟\n"
            "Congratulations! You joined the giveaway FIRST and secured\n"
            f"👤 {uname}\n"
            f"🆔 {uid}\n"
            "📸 Please take a screenshot and post it in the group to confirm 👈",
            show_alert=True
        )
    else:
        await q.answer("✅ You have joined the giveaway!", show_alert=True)


# =========================
# AUTO DRAW ON/OFF
# =========================
def get_autodraw() -> bool:
    return get_setting("autodraw", "0") == "1"


def set_autodraw(v: bool):
    set_setting("autodraw", "1" if v else "0")


async def cmd_autodraw(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ Admins only.")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Auto Draw ON", callback_data="AUTO|ON"),
         InlineKeyboardButton("⛔ Auto Draw OFF", callback_data="AUTO|OFF")]
    ])
    await update.message.reply_text("Select Auto Draw mode:", reply_markup=kb)


async def auto_cb(update: Update, context: CallbackContext):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("Admins only.", show_alert=True)
        return

    _, mode = q.data.split("|", 1)
    if mode == "ON":
        set_autodraw(True)
        await q.message.reply_text("✅ Auto Draw is now ON.")
    else:
        set_autodraw(False)
        await q.message.reply_text("⛔ Auto Draw is now OFF.")
    await q.edit_message_reply_markup(reply_markup=None)


# =========================
# CLOSE JOB + AUTO START SELECTION
# =========================
def schedule_close_job(app: Application, gid: str):
    g = get_giveaway(gid)
    if not g:
        return
    delay = max(1, g["ends_at"] - now_ts())
    # job name unique
    job_name = f"close:{gid}"
    # remove existing
    with contextlib.suppress(Exception):
        app.job_queue.scheduler.remove_job(job_name)
    app.job_queue.run_once(close_job, when=delay, data={"gid": gid}, name=job_name)


async def close_job(context: CallbackContext):
    gid = context.job.data["gid"]
    g = get_giveaway(gid)
    if not g or g["status"] != "active":
        return

    update_giveaway_fields(gid, status="closed")

    # post closed message
    close_mid = None
    try:
        close_mid = await safe_send_message(context.bot, CHANNEL_ID, build_closed_post(get_giveaway(gid)), pin=False)
    except Exception:
        close_mid = None

    if close_mid:
        update_giveaway_fields(gid, close_post_mid=close_mid)

    # If AutoDraw ON => start selection automatically
    if get_autodraw():
        await start_selection_auto(context, gid)
    # If OFF => wait for /draw manual + approval flow
    else:
        # notify admin chat? (best-effort: send to owner)
        if OWNER_ID:
            with contextlib.suppress(Exception):
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=f"ℹ️ Giveaway closed (AutoDraw OFF).\nGiveaway ID: {gid}\nUse /draw to start selection."
                )


# =========================
# MANUAL DRAW + APPROVAL
# =========================
async def cmd_draw(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ Admins only.")
        return

    gid = get_latest_gid()
    if not gid:
        await update.message.reply_text("No giveaway found.")
        return

    g = get_giveaway(gid)
    if not g or g["status"] not in ("closed", "selecting", "announced"):
        await update.message.reply_text("Latest giveaway is not closed yet.")
        return

    # Start selection (manual) in channel, BUT winners post requires approval
    await start_selection_manual(context, gid, admin_chat_id=update.effective_chat.id)
    await update.message.reply_text("🎲 Manual selection started.\nYou will get Approve/Reject when finished.")


# =========================
# SELECTION ENGINE (AUTO/MANUAL)
# =========================
def unique_three_colors() -> List[str]:
    return random.sample(COLOR_EMOJIS, 3)


def build_showcase_cycle(participants: List[sqlite3.Row]) -> List[Tuple[str, str, int]]:
    # full cycle of valid participants (username present)
    valid = [p for p in participants if ensure_valid_username(p["username"])]
    # return list of (username, user_id)
    return [(p["username"], p["user_id"]) for p in valid]


async def start_selection_auto(context: CallbackContext, gid: str):
    # Auto: starts in channel and posts winners automatically at end
    await _start_selection(context, gid, mode="auto", admin_chat_id=None)


async def start_selection_manual(context: CallbackContext, gid: str, admin_chat_id: int):
    # Manual: starts in channel and asks approve/reject at end
    await _start_selection(context, gid, mode="manual", admin_chat_id=admin_chat_id)


async def _start_selection(context: CallbackContext, gid: str, mode: str, admin_chat_id: Optional[int]):
    g = get_giveaway(gid)
    if not g:
        return

    # mark selecting
    update_giveaway_fields(gid, status="selecting")

    # selection duration fixed 10 minutes
    total_sec = 600
    start_ts = now_ts()
    end_ts = start_ts + total_sec

    participants = get_participants(gid)
    cycle = build_showcase_cycle(participants)

    # if no eligible participants, still run but winners will be limited
    winners_total = int(g["winners_total"])
    winners_selected = 0

    # Selection message + buttons (Try Your Luck + Entry Rule)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🍀 Try Your Luck", callback_data=cb_lucky(gid)),
         InlineKeyboardButton("📌 Entry Rule", callback_data=cb_entry_rule(gid))]
    ])

    # create initial showcase (3 lines)
    def pick_three(state_idx: int) -> List[Tuple[str, int, str]]:
        # returns list of (emoji, username, user_id)
        if not cycle:
            return []
        # full cycle: choose sequential by state idx
        n = len(cycle)
        # 3 lines based on independent counters, but full-cycle style:
        # line1 changes every 5s, line2 every 7s, line3 every 9s
        i1 = (state_idx // 5) % n
        i2 = (state_idx // 7) % n
        i3 = (state_idx // 9) % n
        colors = unique_three_colors()
        a1 = cycle[i1]
        a2 = cycle[i2]
        a3 = cycle[i3]
        # ensure not identical lines if possible
        if n >= 3:
            while a2 == a1:
                i2 = (i2 + 1) % n
                a2 = cycle[i2]
            while a3 in (a1, a2):
                i3 = (i3 + 1) % n
                a3 = cycle[i3]
        return [(colors[0], a1[0], a1[1]), (colors[1], a2[0], a2[1]), (colors[2], a3[0], a3[1])]

    # send selection message, pin it
    showcase0 = pick_three(0)
    text0 = build_selection_post(get_giveaway(gid), 1, winners_selected, winners_total, total_sec, showcase0 if showcase0 else [("⚫", "@username", 0), ("🟠", "@username", 0), ("🟣", "@username", 0)])
    sel_mid = await safe_send_message(context.bot, CHANNEL_ID, text0, reply_markup=kb, pin=True)
    update_giveaway_fields(gid, selection_post_mid=sel_mid)

    # Lucky draw window: opens when remaining == 05:55 (i.e. 355 sec remaining)
    lucky_open_at = end_ts - 355  # timestamp
    lucky_close_at = lucky_open_at + 1  # 1 second window

    # store lucky timing in memory (in app bot_data)
    context.application.bot_data.setdefault("lucky_window", {})
    context.application.bot_data["lucky_window"][gid] = {
        "open_at": lucky_open_at,
        "close_at": lucky_close_at,
        "start_ts": start_ts,
        "end_ts": end_ts
    }

    # Winner selection timing: random but 1-by-1 during full 10 minutes
    # We'll choose random moments for each winner after minute 1 to minute 9
    # and also ensure unique users.
    eligible_uids = [p["user_id"] for p in participants if ensure_valid_username(p["username"])]
    eligible = [(p["user_id"], p["username"]) for p in participants if ensure_valid_username(p["username"])]

    # Apply old winner mode (skip/block) during selection too
    if g["old_winner_mode"] in ("skip", "block"):
        filtered = []
        for uid, uname in eligible:
            if has_won_before(uid):
                if g["old_winner_mode"] == "block":
                    add_block(uid, uname, "Old winner blocked (selection)")
                continue
            filtered.append((uid, uname))
        eligible = filtered

    # unique selection list (exclude first join champion already stored rank=0)
    first_champ = None
    for p in participants:
        if p["is_first"] == 1:
            first_champ = (p["user_id"], p["username"])
            break

    already = set()
    if first_champ:
        already.add(first_champ[0])

    # random schedule times for each winner
    select_times = []
    for i in range(winners_total):
        # spread across total_sec; allow "real" randomness
        t = random.randint(30, total_sec - 20)
        select_times.append(t)
    select_times.sort()

    async def finalize_selection():
        nonlocal winners_selected
        # build winners
        # Fill winners ranks 1..N from eligible random order but respecting schedule
        pool = [x for x in eligible if x[0] not in already]
        random.shuffle(pool)

        # Select as many as possible
        chosen = []
        for uid, uname in pool:
            if len(chosen) >= winners_total:
                break
            chosen.append((uid, uname))

        # Save winners with ranks
        r = 1
        for uid, uname in chosen:
            set_winner(gid, uid, uname, rank=r, is_first_join=0)
            r += 1

        # Update winners_selected
        winners_selected = min(winners_total, len(chosen))

        # Remove closed post and selection post pin if needed when winners announced
        g2 = get_giveaway(gid)

        # If manual => ask approve/reject in admin chat
        if mode == "manual" and admin_chat_id:
            kb2 = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ APPROVE & POST", callback_data=f"POSTWIN|{gid}")],
                [InlineKeyboardButton("❌ REJECT", callback_data=f"REJWIN|{gid}")],
            ])
            await context.bot.send_message(
                chat_id=admin_chat_id,
                text=f"🏆 Winners are ready for Giveaway ID: {gid}\nApprove to post to channel?",
                reply_markup=kb2
            )
            update_giveaway_fields(gid, status="announced")  # ready
            return

        # Auto: post immediately
        await post_winners_to_channel(context, gid)

    # Loop ticker
    t0 = time.time()
    last_winner_tick = -999

    while True:
        now = now_ts()
        if now >= end_ts:
            break

        elapsed = now - start_ts
        remaining = max(0, end_ts - now)

        # progress updates
        pct = int((elapsed / total_sec) * 100)
        pct = max(1, min(99, pct))

        # winners selection: pick winner at scheduled times
        # Here: 1 by 1 updates: winners_selected increments as time hits select_times[k]
        while winners_selected < winners_total and winners_selected < len(select_times) and elapsed >= select_times[winners_selected]:
            # pick next winner from eligible not already chosen
            pool = [(uid, uname) for (uid, uname) in eligible if uid not in already]
            if not pool:
                break
            uid, uname = random.choice(pool)
            already.add(uid)
            set_winner(gid, uid, uname, rank=winners_selected + 1, is_first_join=0)
            winners_selected += 1

        # showcase (full cycle then repeat)
        show = pick_three(elapsed)
        if not show:
            show = [("⚫", "@username", 0), ("🟠", "@username", 0), ("🟣", "@username", 0)]

        # Edit selection post (throttled)
        # Update every ~2 seconds for stability
        if elapsed % 2 == 0:
            text = build_selection_post(get_giveaway(gid), pct, winners_selected, winners_total, remaining, show)
            await safe_edit_message(context.bot, CHANNEL_ID, sel_mid, text, reply_markup=kb)

        await asyncio.sleep(1)

    # force 100%
    text = build_selection_post(get_giveaway(gid), 100, winners_selected, winners_total, 0, pick_three(total_sec) or [("⚫", "@username", 0), ("🟠", "@username", 0), ("🟣", "@username", 0)])
    await safe_edit_message(context.bot, CHANNEL_ID, sel_mid, text, reply_markup=kb)

    # finalize
    await finalize_selection()


async def post_winners_to_channel(context: CallbackContext, gid: str):
    g = get_giveaway(gid)
    if not g:
        return

    # winners post
    winners_text = build_winners_post(g)
    claim_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data=cb_claim(gid))]])

    # post winners
    wmid = await safe_send_message(context.bot, CHANNEL_ID, winners_text, reply_markup=claim_btn, pin=True)
    update_giveaway_fields(gid, winners_post_mid=wmid, status="announced")

    # Create 5 claim button posts (per-giveaway mapping)
    claim_mids = []
    for _ in range(5):
        mid = await safe_send_message(
            context.bot,
            CHANNEL_ID,
            "🏆✨ CLAIM YOUR PRIZE NOW ✨🏆",
            reply_markup=claim_btn,
            pin=False
        )
        claim_mids.append(mid)
        await asyncio.sleep(0.2)
    update_giveaway_fields(gid, claim_mids=json.dumps(claim_mids))

    # Remove close post + selection post if exists (as you wanted)
    if g["close_post_mid"]:
        await safe_delete(context.bot, CHANNEL_ID, g["close_post_mid"])
    if g["selection_post_mid"]:
        # keep selection post or remove? user said close post remove; selection can stay pinned or not
        # We'll unpin but keep message for history.
        with contextlib.suppress(Exception):
            await context.bot.unpin_chat_message(CHANNEL_ID, g["selection_post_mid"])
    update_giveaway_fields(gid, status="completed")


async def postwin_cb(update: Update, context: CallbackContext):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("Admins only.", show_alert=True)
        return
    action, gid = q.data.split("|", 1)
    if action == "REJWIN":
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("❌ Rejected. Winners will NOT be posted to channel.")
        return
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text("✅ Approved. Posting winners to channel...")
    await post_winners_to_channel(context, gid)


# =========================
# CLAIM CALLBACK (per-giveaway mapping)
# =========================
async def claim_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    uid = update.effective_user.id
    await q.answer()

    _, gid = q.data.split("|", 1)
    g = get_giveaway(gid)
    if not g:
        await q.answer("Giveaway not found.", show_alert=True)
        return

    # find winner
    winners = get_winners(gid)
    w = next((x for x in winners if x["user_id"] == uid), None)

    # 24h expiry check based on winners_post time ~ stored as g created? We'll use winners_post_mid time not accessible.
    # Use giveaway end time as base.
    expiry = g["ends_at"] + 24 * 3600
    expired = now_ts() > expiry

    if not w:
        # not winner
        if expired:
            await q.answer(claim_popup_expired_non_winner(), show_alert=True)
        else:
            await q.answer(claim_popup_not_winner(), show_alert=True)
        return

    # winner
    if w["delivered_at"]:
        await q.answer(claim_popup_delivered(w["username"], w["user_id"]), show_alert=True)
        return

    if expired:
        await q.answer(claim_popup_expired_winner(w["username"], w["user_id"]), show_alert=True)
        return

    # claim accepted (pending)
    await q.answer(
        "✅ CLAIM RECEIVED\n\n"
        "Please contact admin to receive your prize:\n"
        f"👉 {BOT_OWNER_USERNAME}",
        show_alert=True
    )


# =========================
# LUCKY DRAW BUTTONS
# =========================
async def entry_rule_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    await q.answer(lucky_entry_rule_popup(), show_alert=True)


async def lucky_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    uid = update.effective_user.id
    uname = username_of(update)
    await q.answer()  # we will show alert below

    _, gid = q.data.split("|", 1)
    g = get_giveaway(gid)
    if not g:
        await q.answer("Giveaway not found.", show_alert=True)
        return

    # must have username
    if not ensure_valid_username(uname):
        await q.answer("You must have a valid @username to use Lucky Draw.", show_alert=True)
        return

    # must have participants
    if count_participants(gid) == 0:
        await q.answer(lucky_no_participants_popup(), show_alert=True)
        return

    # check lucky window data
    win = context.application.bot_data.get("lucky_window", {}).get(gid)
    if not win:
        await q.answer(lucky_not_yet_popup(), show_alert=True)
        return

    now = now_ts()
    open_at = win["open_at"]
    close_at = win["close_at"]

    # If lucky already won
    if g["lucky_user_id"]:
        await q.answer(lucky_too_late_popup(g["lucky_username"], g["lucky_user_id"]), show_alert=True)
        return

    if now < open_at:
        await q.answer(lucky_not_yet_popup(), show_alert=True)
        return

    if now > close_at:
        # After window, but no winner => keep it unclaimable; show too late without winner? user wants winner show.
        await q.answer("⚠️ TOO LATE\n\nLucky Draw moment is over.", show_alert=True)
        return

    # Winner: first click wins
    update_giveaway_fields(gid, lucky_user_id=uid, lucky_username=uname, lucky_won_at=now_ts())
    # Add to winners list LIVE as rank next available (after current winners)
    current = get_winners(gid)
    max_rank = 0
    for w in current:
        if w["rank"] > max_rank and w["rank"] < 900:
            max_rank = w["rank"]
    next_rank = max_rank + 1
    set_winner(gid, uid, uname, rank=next_rank, is_first_join=0)

    await q.answer(lucky_win_popup(uname, uid), show_alert=True)


# =========================
# PRIZE DELIVERY (2 steps) + VALIDATION
# =========================
async def cmd_prizedelivered(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ Admins only.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📦 PRIZE DELIVERY UPDATE\n\n"
        "Step 1/2 — Send Giveaway ID (example: P857-P583-B6714)\n"
        "OR send: latest\n\n"
        "After that, I will ask for delivered users list."
    )
    return PRIZEDELIV_STEP1


def parse_delivered_line(line: str) -> Optional[Tuple[str, int]]:
    # Format: @username | user_id
    line = line.strip()
    m = re.match(r"^(@[A-Za-z0-9_]{5,32})\s*\|\s*(\d{4,15})$", line)
    if not m:
        return None
    return m.group(1), int(m.group(2))


async def pr_deliv_step1(update: Update, context: CallbackContext):
    txt = (update.message.text or "").strip()
    if txt.lower() == "latest":
        gid = get_latest_gid()
        if not gid:
            await update.message.reply_text("No giveaway found.")
            return ConversationHandler.END
    else:
        gid = txt
        if not get_giveaway(gid):
            await update.message.reply_text("Invalid Giveaway ID. Send a valid one or 'latest'.")
            return PRIZEDELIV_STEP1

    context.user_data["deliv_gid"] = gid
    await update.message.reply_text(
        f"✅ Giveaway selected: {gid}\n\n"
        "Step 2/2 — Send delivered users list (one per line):\n"
        "@username | user_id\n\n"
        "Example:\n"
        "@MinexxProo | 5692210187"
    )
    return PRIZEDELIV_STEP2


async def pr_deliv_step2(update: Update, context: CallbackContext):
    gid = context.user_data.get("deliv_gid")
    if not gid:
        await update.message.reply_text("Session expired. Use /prizedelivered again.")
        return ConversationHandler.END

    g = get_giveaway(gid)
    if not g:
        await update.message.reply_text("Giveaway not found.")
        return ConversationHandler.END

    raw = (update.message.text or "").strip().splitlines()
    parsed = []
    bad_lines = []
    for line in raw:
        if not line.strip():
            continue
        x = parse_delivered_line(line)
        if not x:
            bad_lines.append(line.strip())
        else:
            parsed.append(x)

    if bad_lines:
        await update.message.reply_text(
            "❌ Invalid lines detected:\n" +
            "\n".join(f"• {b}" for b in bad_lines) +
            "\n\nCorrect format:\n@username | user_id"
        )
        return PRIZEDELIV_STEP2

    winners = get_winners(gid)
    winners_map = {(w["username"], int(w["user_id"])): w for w in winners}

    updated = 0
    not_found = []
    already_delivered = []
    for uname, uid in parsed:
        w = winners_map.get((uname, uid))
        if not w:
            not_found.append(f"{uname} | {uid}")
            continue
        if w["delivered_at"]:
            already_delivered.append(f"{uname} | {uid}")
            continue
        mark_delivered(gid, uid, update.effective_user.id)
        updated += 1

    # update winners post in channel (edit)
    g2 = get_giveaway(gid)
    if g2 and g2["winners_post_mid"]:
        text = build_winners_post(g2)
        claim_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data=cb_claim(gid))]])
        ok, reason = await safe_edit_message(context.bot, CHANNEL_ID, g2["winners_post_mid"], text, reply_markup=claim_btn)
        if not ok and reason != "not_modified":
            await update.message.reply_text(f"⚠️ Could not edit winners post.\nReason: {reason}")

    # response summary
    msg = (
        "✅ Prize delivery updated successfully.\n"
        f"Giveaway ID: {gid}\n"
        f"Updated: {updated} winner(s)\n"
    )
    if not_found:
        msg += "\n⚠️ Not found in winners list (check username/user_id):\n" + "\n".join(f"• {x}" for x in not_found) + "\n"
    if already_delivered:
        msg += "\nℹ️ Already delivered:\n" + "\n".join(f"• {x}" for x in already_delivered) + "\n"

    await update.message.reply_text(msg)
    return ConversationHandler.END


# =========================
# BLOCK SYSTEM COMMANDS
# =========================
async def cmd_blockpermanent(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ Admins only.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🔒 PERMANENT BLOCK\n\n"
        "Send list (one per line):\n"
        "User ID only OR username | id\n\n"
        "Examples:\n"
        "7297292\n"
        "@MinexxProo | 7297292"
    )
    return BLOCK_LIST_IN


def parse_block_line(line: str) -> Optional[Tuple[int, str]]:
    line = line.strip()
    if line.isdigit():
        return int(line), ""
    m = re.match(r"^(@[A-Za-z0-9_]{5,32})\s*\|\s*(\d{4,15})$", line)
    if m:
        return int(m.group(2)), m.group(1)
    return None


async def block_list_in(update: Update, context: CallbackContext):
    raw = (update.message.text or "").splitlines()
    bad = []
    added = 0
    for line in raw:
        if not line.strip():
            continue
        x = parse_block_line(line)
        if not x:
            bad.append(line.strip())
            continue
        uid, uname = x
        add_block(uid, uname, "Permanent block (admin)")
        added += 1

    out = f"✅ Permanent block updated.\nAdded/Updated: {added}\n"
    if bad:
        out += "\n❌ Invalid lines:\n" + "\n".join(f"• {b}" for b in bad)
    await update.message.reply_text(out)
    return ConversationHandler.END


async def cmd_unban(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ Admins only.")
        return ConversationHandler.END
    await update.message.reply_text(
        "🔓 UNBAN\n\nSend user IDs (one per line).\nExample:\n5692210187"
    )
    return UNBAN_LIST_IN


async def unban_list_in(update: Update, context: CallbackContext):
    raw = (update.message.text or "").splitlines()
    bad = []
    removed = 0
    for line in raw:
        line = line.strip()
        if not line:
            continue
        if not line.isdigit():
            bad.append(line)
            continue
        remove_block(int(line))
        removed += 1
    out = f"✅ Unban complete.\nRemoved: {removed}\n"
    if bad:
        out += "\n❌ Invalid lines:\n" + "\n".join(f"• {b}" for b in bad)
    await update.message.reply_text(out)
    return ConversationHandler.END


async def cmd_blocklist(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ Admins only.")
        return
    rows = list_blocks(50)
    if not rows:
        await update.message.reply_text("Blocklist is empty.")
        return
    lines = []
    for r in rows:
        uname = r["username"] or "(no username)"
        lines.append(f"• {uname} | {r['user_id']} | {r['reason']}")
    await update.message.reply_text("🔒 BLOCKLIST\n\n" + "\n".join(lines))


async def cmd_removeban(update: Update, context: CallbackContext):
    # alias for unban
    return await cmd_unban(update, context)


# =========================
# /winnerlist
# =========================
async def cmd_winnerlist(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ Admins only.")
        return
    rows = winner_history(50)
    if not rows:
        await update.message.reply_text("No winner history yet.")
        return
    out = ["🏆 WINNER LIST (Latest 50)\n"]
    for r in rows:
        out.append(
            f"🗓️ {fmt_dt(r['won_at'])}\n"
            f"🆔 Giveaway: {r['gid']}\n"
            f"🎁 Prize: {r['prize']}\n"
            f"👤 {r['username']} | 🆔 {r['user_id']}\n"
            f"🏅 Rank: {r['rank']}\n"
            f"{SEP}"
        )
    await update.message.reply_text("\n".join(out))


# =========================
# /reset with confirm + 40s progress
# =========================
async def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ Admins only.")
        return ConversationHandler.END
    await update.message.reply_text(
        "⚠️ RESET WARNING\n\n"
        "This will reset the bot database and remove:\n"
        "• Giveaways\n• Participants\n• Winners\n• Blocks\n\n"
        "Type: CONFIRM to proceed\n"
        "Type: CANCEL to abort"
    )
    return RESET_CONFIRM


async def reset_confirm(update: Update, context: CallbackContext):
    txt = (update.message.text or "").strip().upper()
    if txt == "CANCEL":
        await update.message.reply_text("✅ Reset canceled.")
        return ConversationHandler.END
    if txt != "CONFIRM":
        await update.message.reply_text("Type CONFIRM or CANCEL.")
        return RESET_CONFIRM

    # 40 second progress
    msg = await update.message.reply_text("♻️ Resetting... 0%")
    for i in range(1, 41):
        pct = int(i * 100 / 40)
        bar = progress_bar(pct, 10)
        await safe_edit_message(context.bot, update.effective_chat.id, msg.message_id, f"♻️ Resetting... {pct}%\n{bar}")
        await asyncio.sleep(1)

    # wipe db
    con = db()
    cur = con.cursor()
    cur.execute("DELETE FROM participants")
    cur.execute("DELETE FROM winners")
    cur.execute("DELETE FROM giveaways")
    cur.execute("DELETE FROM blocks")
    con.commit()
    con.close()

    await update.message.reply_text("✅ Reset complete. Bot is clean now.")
    return ConversationHandler.END


# =========================
# PANEL CALLBACK ROUTER
# =========================
async def panel_cb(update: Update, context: CallbackContext):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("Admins only.", show_alert=True)
        return

    _, act = q.data.split("|", 1)
    await q.edit_message_reply_markup(reply_markup=None)

    if act == "NEW":
        await q.message.reply_text("Starting /newgiveaway ...")
        # start via command flow
        context.user_data["draft"] = Draft()
        await q.message.reply_text("Send Giveaway Title (single line):")
        context.user_data["_conv_override"] = "newgiveaway"
        return

    if act == "DRAW":
        await q.message.reply_text("Use /draw")
        return

    if act == "AUTO":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Auto Draw ON", callback_data="AUTO|ON"),
             InlineKeyboardButton("⛔ Auto Draw OFF", callback_data="AUTO|OFF")]
        ])
        await q.message.reply_text("Select Auto Draw mode:", reply_markup=kb)
        return

    if act == "DELIV":
        await q.message.reply_text("Use /prizedelivered")
        return

    if act == "WLIST":
        await cmd_winnerlist(Update(update.update_id, message=q.message), context)
        return

    if act == "BLOCK":
        await q.message.reply_text("Block system:\n/blockpermanent\n/unban\n/blocklist\n/removeban")
        return

    if act == "RESET":
        await q.message.reply_text("Use /reset")
        return


# =========================
# HELPER: Continue conversation when panel started it
# =========================
async def intercept_newgiveaway(update: Update, context: CallbackContext):
    # If panel initiated newgiveaway without typing /newgiveaway
    if context.user_data.get("_conv_override") == "newgiveaway":
        # route into NEW_TITLE handler manually
        context.user_data["_conv_override"] = None
        return await new_title(update, context)
    return ConversationHandler.END


# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("autodraw", cmd_autodraw))
    app.add_handler(CommandHandler("draw", cmd_draw))
    app.add_handler(CommandHandler("winnerlist", cmd_winnerlist))
    app.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # Callbacks
    app.add_handler(CallbackQueryHandler(panel_cb, pattern=r"^PANEL\|"))
    app.add_handler(CallbackQueryHandler(auto_cb, pattern=r"^AUTO\|"))
    app.add_handler(CallbackQueryHandler(approve_reject_cb, pattern=r"^(APPROVE|REJECT)\|"))
    app.add_handler(CallbackQueryHandler(postwin_cb, pattern=r"^(POSTWIN|REJWIN)\|"))
    app.add_handler(CallbackQueryHandler(join_callback, pattern=r"^JOIN\|"))
    app.add_handler(CallbackQueryHandler(claim_callback, pattern=r"^CLAIM\|"))
    app.add_handler(CallbackQueryHandler(lucky_callback, pattern=r"^LUCKY\|"))
    app.add_handler(CallbackQueryHandler(entry_rule_callback, pattern=r"^RULE\|"))

    # Conversations
    newgiveaway_conv = ConversationHandler(
        entry_points=[CommandHandler("newgiveaway", cmd_newgiveaway)],
        states={
            NEW_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_title)],
            NEW_PRIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_prize)],
            NEW_WINNERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_winners)],
            NEW_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_duration)],
            NEW_OLDWINNER_MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_oldwinner)],
            NEW_RULES: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_rules)],
            NEW_APPROVE: [],
        },
        fallbacks=[],
        name="newgiveaway_conv",
        persistent=False,
    )
    app.add_handler(newgiveaway_conv)

    prizedeliv_conv = ConversationHandler(
        entry_points=[CommandHandler("prizedelivered", cmd_prizedelivered)],
        states={
            PRIZEDELIV_STEP1: [MessageHandler(filters.TEXT & ~filters.COMMAND, pr_deliv_step1)],
            PRIZEDELIV_STEP2: [MessageHandler(filters.TEXT & ~filters.COMMAND, pr_deliv_step2)],
        },
        fallbacks=[],
        name="prizedeliv_conv",
        persistent=False,
    )
    app.add_handler(prizedeliv_conv)

    block_conv = ConversationHandler(
        entry_points=[CommandHandler("blockpermanent", cmd_blockpermanent)],
        states={
            BLOCK_LIST_IN: [MessageHandler(filters.TEXT & ~filters.COMMAND, block_list_in)],
        },
        fallbacks=[],
        name="block_conv",
        persistent=False,
    )
    app.add_handler(block_conv)

    unban_conv = ConversationHandler(
        entry_points=[CommandHandler("unban", cmd_unban), CommandHandler("removeban", cmd_removeban)],
        states={
            UNBAN_LIST_IN: [MessageHandler(filters.TEXT & ~filters.COMMAND, unban_list_in)],
        },
        fallbacks=[],
        name="unban_conv",
        persistent=False,
    )
    app.add_handler(unban_conv)

    reset_conv = ConversationHandler(
        entry_points=[CommandHandler("reset", cmd_reset)],
        states={
            RESET_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, reset_confirm)],
        },
        fallbacks=[],
        name="reset_conv",
        persistent=False,
    )
    app.add_handler(reset_conv)

    # Panel-started newgiveaway interceptor
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, intercept_newgiveaway))

    print("Bot running...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
