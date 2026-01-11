# =========================================================
# POWER POINT BREAK — Giveaway Bot (PTB v13 / Non-Async)
# Full A to Z (AutoDraw + Manual Draw + Lucky Draw Buttons + Prize Delivery + Reset Progress)
# python-telegram-bot==13.*
# =========================================================

import os
import json
import random
import threading
from datetime import datetime

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import BadRequest
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)

# =========================================================
# LOAD ENV
# =========================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

HOST_NAME = os.getenv("HOST_NAME", "POWER POINT BREAK")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@PowerPointBreak")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/PowerPointBreak")

ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@MinexxProo")
DATA_FILE = os.getenv("DATA_FILE", "giveaway_data.json")

# =========================================================
# THREAD SAFE STORAGE
# =========================================================
lock = threading.RLock()

# =========================================================
# JOB HANDLES
# =========================================================
countdown_job = None
closed_msg_job = None

draw_progress_job = None
draw_finalize_job = None

autodraw_tick_job = None
autodraw_finalize_job = None

claim_expire_job = None

reset_progress_job = None

# =========================================================
# STATE
# =========================================================
data = {}
admin_state = None

# =========================================================
# DATA / STORAGE
# =========================================================
def fresh_default_data():
    return {
        # current giveaway (editable/setup/live)
        "active": False,
        "closed": False,

        "title": "",
        "prize": "",
        "winner_count": 0,
        "duration_seconds": 0,
        "rules": "",

        "start_time": None,
        "live_message_id": None,
        "closed_message_id": None,

        # participants: uid(str) -> {"username":"@x","name":""}
        "participants": {},

        # verify targets: list of {"ref":"-100.. or @..","display":"..."}
        "verify_targets": [],

        # permanent block: uid -> {"username":"@x"}
        "permanent_block": {},

        # old winner protection
        "old_winner_mode": "skip",   # "block" or "skip"
        "old_winners": {},           # uid -> {"username":"@x"} used only if mode=block

        # first join winner
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # manual draw preview
        "pending_winners_text": "",
        "winners_preview": {},  # uid -> {"username":"@x","first":bool}

        # claim window (for current posted winners when manual)
        "claim_start_ts": None,
        "claim_expires_ts": None,
        "winners_message_id": None,

        # Auto Draw master switch
        "autodraw_enabled": False,

        # Auto Draw running marker
        "autodraw_in_progress": False,
        "autodraw_gid": None,

        # Multiple giveaway history (each winners post has own gid)
        "history": {},     # gid -> snapshot
        "latest_gid": None,

        # prize delivery command target
        "_prize_target_gid": None,
    }


def load_data():
    base = fresh_default_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    for k, v in base.items():
        d.setdefault(k, v)
    # safety defaults for nested objects
    d.setdefault("participants", {})
    d.setdefault("verify_targets", [])
    d.setdefault("permanent_block", {})
    d.setdefault("old_winners", {})
    d.setdefault("history", {})
    return d


def save_data():
    with lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)


data = load_data()

# =========================================================
# HELPERS
# =========================================================
def now_ts() -> float:
    return datetime.utcnow().timestamp()


def utc_now() -> datetime:
    return datetime.utcnow()


def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == ADMIN_ID)


def user_tag(username: str) -> str:
    u = (username or "").strip()
    if not u:
        return ""
    return u if u.startswith("@") else "@" + u


def participants_count() -> int:
    return len(data.get("participants", {}) or {})


def format_hms(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_bar(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    blocks = 10
    filled = int(round(blocks * percent / 100.0))
    empty = blocks - filled
    return "▰" * filled + "▱" * empty


def parse_duration(text: str) -> int:
    t = (text or "").strip().lower()
    parts = t.split()
    if len(parts) == 1 and parts[0].isdigit():
        return int(parts[0])

    if not parts or not parts[0].isdigit():
        return 0

    num = int(parts[0])
    unit = "".join(parts[1:])
    if unit.startswith("sec"):
        return num
    if unit.startswith("min"):
        return num * 60
    if unit.startswith("hour") or unit.startswith("hr"):
        return num * 3600
    return num


def safe_edit_text(bot, chat_id, message_id, text, reply_markup=None):
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return True, None
    except BadRequest as e:
        msg = str(e)
        if "Message is not modified" in msg:
            return True, None
        return False, msg
    except Exception as e:
        return False, str(e)


def safe_edit_markup(bot, chat_id, message_id, reply_markup=None):
    try:
        bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )
        return True, None
    except BadRequest as e:
        msg = str(e)
        if "Message is not modified" in msg:
            return True, None
        return False, msg
    except Exception as e:
        return False, str(e)


def normalize_verify_ref(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    if s.startswith("-") and s[1:].isdigit():
        return s
    if s.startswith("@"):
        return s
    raw = s.replace(" ", "")
    if "t.me/" in raw:
        slug = raw.split("t.me/", 1)[1]
        slug = slug.split("?", 1)[0]
        slug = slug.split("/", 1)[0]
        if slug and not slug.startswith("+"):
            return user_tag(slug)
    return ""


def verify_user_join(bot, user_id: int) -> bool:
    targets = data.get("verify_targets", []) or []
    if not targets:
        return True
    for t in targets:
        ref = (t or {}).get("ref", "")
        if not ref:
            return False
        try:
            member = bot.get_chat_member(chat_id=ref, user_id=user_id)
            status = getattr(member, "status", None)
            if status not in ("member", "administrator", "creator"):
                return False
        except Exception:
            return False
    return True


def parse_user_lines(text: str):
    """
    Accept:
      123456789
      @name | 123456789
    Returns list[(uid, uname)]
    """
    out = []
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for line in lines:
        if "|" in line:
            left, right = line.split("|", 1)
            uname = user_tag(left.strip().lstrip("@"))
            uid = right.strip().replace(" ", "")
            if uid.isdigit():
                out.append((uid, uname))
        else:
            uid = line.strip().replace(" ", "")
            if uid.isdigit():
                out.append((uid, ""))
    return out


def gen_giveaway_id() -> str:
    # P788-P686-B6548 style
    return f"P{random.randint(100,999)}-P{random.randint(100,999)}-B{random.randint(1000,9999)}"


# =========================================================
# POPUPS (Final)
# =========================================================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n"
        "To join this giveaway, you must join the required channels/groups first ✅\n\n"
        "After joining all of them, click JOIN GIVEAWAY again."
    )


def popup_old_winner_blocked() -> str:
    return (
        "🚫 You have already won a previous giveaway.\n"
        "Repeat winners are restricted to keep it fair.\n\n"
        "Please wait for the next giveaway."
    )


def popup_first_join(username: str, uid: str) -> str:
    return (
        "🥇 FIRST JOIN CHAMPION 🌟\n"
        "Congratulations! You joined\n"
        "the giveaway FIRST and secured\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n"
        "📸 Please take a screenshot\n"
        "and post it in the group\n"
        "to confirm 👈"
    )


def popup_already_joined() -> str:
    return (
        "🚫 ENTRY UNSUCCESSFUL\n"
        "You’ve already joined this giveaway.\n\n"
        "Multiple entries aren’t allowed."
    )


def popup_join_success(username: str, uid: str) -> str:
    return (
        "✅ JOINED SUCCESSFULLY\n\n"
        "Your entry is confirmed.\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        f"— {HOST_NAME}"
    )


def popup_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n"
        "You are permanently blocked from joining giveaways.\n\n"
        f"If you believe this is a mistake, contact admin: {ADMIN_CONTACT}"
    )


def popup_not_winner() -> str:
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry! Your User ID is not\n"
        "in the winners list.\n\n"
        "Please wait for the next\n"
        "giveaway ❤️‍🩹"
    )


def popup_prize_expired() -> str:
    return (
        "⏳ PRIZE EXPIRED\n\n"
        "Your 24-hour claim time has ended.\n"
        "This prize is no longer available."
    )


def popup_giveaway_completed(admin_contact: str) -> str:
    return (
        "✅ GIVEAWAY COMPLETED\n\n"
        "This giveaway has been completed.\n"
        f"If you have any issues, please contact admin 👉 {admin_contact}"
    )


def popup_claim_winner(username: str, uid: str, admin_contact: str) -> str:
    return (
        "🌟 CONGRATULATIONS!\n"
        "You’ve won this giveaway ✅\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        "Please contact admin to claim:\n"
        f"👉 {admin_contact}"
    )


def popup_prize_already_delivered(username: str, uid: str, admin_contact: str) -> str:
    return (
        "📦 PRIZE ALREADY DELIVERED\n"
        "Your prize has already been\n"
        "successfully delivered ✅\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        "If you face any issue,\n"
        f"contact admin 👉 {admin_contact}"
    )


def popup_lucky_rule() -> str:
    return (
        "📌 ENTRY RULE\n"
        "• Tap 🍀 Try Your Luck at the right moment\n"
        "• First click wins instantly (Lucky Draw)\n"
        "• Must have a valid @username\n"
        "• Winner is added live to the selection post\n"
        "• 100% fair: first-come-first-win\n\n"
        "⏰ Lucky Window: 00:00:48 → 00:00:49"
    )


def popup_not_eligible_username() -> str:
    return (
        "⚠️ NOT ELIGIBLE\n\n"
        "You must have a valid @username to participate.\n"
        "Please set a Telegram username and try again."
    )


def popup_not_joined_tryluck() -> str:
    return (
        "❌ NOT JOINED\n\n"
        "You are not in this giveaway entries list.\n"
        "Please join the giveaway first, then try again."
    )


def popup_too_late(lucky_uname: str, lucky_uid: str) -> str:
    return (
        "⚠️ TOO LATE\n\n"
        "Someone already won the 🍀 Lucky Draw slot.\n\n"
        "🏆 Lucky Draw Winner:\n"
        f"👤 {lucky_uname}\n"
        f"🆔 {lucky_uid}\n\n"
        "Please continue watching\n"
        "the live winner selection."
    )


def popup_tryluck_no_entries() -> str:
    return (
        "⚠️ NO ENTRIES\n\n"
        "No eligible entries found yet.\n"
        "Please join the giveaway first."
    )


# =========================================================
# MARKUPS
# =========================================================
def join_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data="join_giveaway")]]
    )


def winners_approve_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data="winners_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="winners_reject"),
        ]]
    )


def preview_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✔️ Approve & Post", callback_data="preview_approve"),
                InlineKeyboardButton("❌ Reject Giveaway", callback_data="preview_reject"),
            ],
            [InlineKeyboardButton("✏️ Edit Again", callback_data="preview_edit")],
        ]
    )


def verify_add_more_done_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("➕ Add Another Link", callback_data="verify_add_more"),
            InlineKeyboardButton("✅ Done", callback_data="verify_add_done"),
        ]]
    )


def end_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm End", callback_data="end_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="end_cancel"),
        ]]
    )


def reset_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ CONFIRM RESET", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ CANCEL", callback_data="reset_cancel"),
        ]]
    )


def autodraw_toggle_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Auto Draw ON", callback_data="autodraw_on"),
            InlineKeyboardButton("❌ Auto Draw OFF", callback_data="autodraw_off"),
        ]]
    )


def claim_button_markup(gid: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data=f"claim:{gid}")]]
    )


def selection_buttons_markup(gid: str):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🍀 Try Your Luck", callback_data=f"luck:{gid}"),
            InlineKeyboardButton("📌 Entry Rule", callback_data=f"rule:{gid}"),
        ]]
    )


# =========================================================
# TEXT BUILDERS (Final Styles)
# =========================================================
def format_rules() -> str:
    rules = (data.get("rules") or "").strip()
    if not rules:
        return (
            "• Must join the official channel\n"
            "• One entry per user only\n"
            "• Stay active until result announcement\n"
            "• Admin decision is final & binding"
        )
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    return "\n".join("• " + l for l in lines)


def build_preview_text() -> str:
    remaining = int(data.get("duration_seconds", 0) or 0)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 GIVEAWAY PREVIEW (ADMIN ONLY)\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ {data.get('title','')} ⚡\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{data.get('prize','')}\n\n"
        f"👥 Total Participants: 0  \n"
        f"🏆 Total Winners: {int(data.get('winner_count',0) or 0)}  \n\n"
        "🎯 Winner Selection\n"
        "• 100% Random & Fair  \n"
        "• Auto System  \n\n"
        f"⏱️ Time Remaining: {format_hms(remaining)}  \n"
        "📊 Live Progress\n"
        f"{build_bar(0)}  \n\n"
        "📜 Official Rules  \n"
        f"{format_rules()}  \n\n"
        f"📢 Hosted by: {HOST_NAME}  \n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Tap below to join the giveaway 👇  \n"
    )


def build_live_text(remaining: int) -> str:
    duration = int(data.get("duration_seconds", 1) or 1)
    elapsed = max(0, duration - remaining)
    percent = int(round((elapsed / float(duration)) * 100)) if duration > 0 else 0

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ {data.get('title','')} ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{data.get('prize','')}\n\n"
        f"👥 Total Participants: {participants_count()}  \n"
        f"🏆 Total Winners: {int(data.get('winner_count',0) or 0)}  \n\n"
        "🎯 Winner Selection\n"
        "• 100% Random & Fair  \n"
        "• Auto System  \n\n"
        f"⏱️ Time Remaining: {format_hms(remaining)}  \n"
        "📊 Live Progress\n"
        f"{build_bar(percent)}  \n\n"
        "📜 Official Rules  \n"
        f"{format_rules()}  \n\n"
        f"📢 Hosted by: {HOST_NAME}  \n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Tap below to join the giveaway 👇  \n"
    )


def build_closed_post_text(prize: str, total_p: int, total_w: int) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway has officially ended.  \n"
        "🔒 All entries are now locked.\n\n"
        "📊 Giveaway Summary  \n"
        f"🎁 Prize: {prize}  \n\n"
        f"👥 Total Participants: {total_p}  \n"
        f"🏆 Total Winners: {total_w}  \n\n"
        "🎯 Winners will be announced very soon.  \n"
        "Please stay tuned for the final results.\n\n"
        "✨ Best of luck to everyone!\n\n"
        f"— {HOST_NAME} ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


def build_winners_post_text(gid: str) -> str:
    snap = (data.get("history", {}) or {}).get(gid, {}) or {}
    winners = snap.get("winners", {}) or {}
    delivered = snap.get("delivered", {}) or {}
    prize = snap.get("prize", "")
    title = snap.get("title", HOST_NAME)

    delivered_count = sum(1 for _, v in delivered.items() if v is True)
    total_winners = int(snap.get("winner_count", 0) or 0)

    lines = []
    lines.append("🏆 GIVEAWAY WINNER ANNOUNCEMENT 🏆")
    lines.append("")
    lines.append(title)
    lines.append("")
    lines.append(f"🆔 Giveaway ID: {gid}")
    lines.append("")
    lines.append(f"🎁 PRIZE: {prize}")
    lines.append(f"📦 Prize Delivery: {delivered_count}/{total_winners}")
    lines.append("")

    # first join
    first_uid = None
    for uid, info in winners.items():
        if (info or {}).get("first") is True:
            first_uid = uid
            break

    if first_uid:
        fu = (winners.get(first_uid, {}) or {}).get("username", "") or f"User ID: {first_uid}"
        lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
        lines.append(f"👑 {fu}")
        lines.append(f"🆔 {first_uid}")
        lines.append("")

    lines.append("👑 OTHER WINNERS")
    idx = 1
    for uid, info in winners.items():
        if uid == first_uid:
            continue
        uname = (info or {}).get("username", "") or f"User ID: {uid}"
        status = "Delivered ✅" if delivered.get(uid) is True else "Pending ⏳"
        lines.append(f"{idx}️⃣ 👤 {uname} | 🆔 {uid} | {status}")
        idx += 1

    lines.append("")
    lines.append("👇 Click the button below to claim your prize")
    lines.append("")
    lines.append("⏳ Rule: Claim within 24 hours — after that, prize expires.")
    return "\n".join(lines)


def build_selection_post_text(gid: str, percent: int, time_remain: int, show_items: list, winners_selected: int, total_winners: int) -> str:
    snap = (data.get("history", {}) or {}).get(gid, {}) or {}
    prize = snap.get("prize", "")
    title = snap.get("title", HOST_NAME)

    bar = build_bar(percent)
    show_lines = []
    for emoji, uname, uid in show_items:
        show_lines.append(f"{emoji} Now Showing → {uname} | 🆔 {uid}")

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 LIVE RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ {title} ⚡\n\n"
        "🎁 GIVEAWAY SUMMARY  \n"
        f"🏆 Prize: {prize}  \n"
        f"✅ Winners Selected: {winners_selected}/{total_winners}\n\n"
        "📌 Important Rule  \n"
        "Users without a valid @username  \n"
        "are automatically excluded.\n\n"
        f"⏳ Selection Progress: {percent}%  \n"
        f"📊 Progress Bar: {bar}  \n\n"
        f"🕒 Time Remaining: {format_hms(time_remain)}  \n"
        "🔐 System Mode: 100% Random • Fair • Auto  \n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👥 LIVE ENTRIES SHOWCASE\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        + ("\n".join(show_lines) if show_lines else "⚠️ No eligible entries found yet.")
        + "\n━━━━━━━━━━━━━━━━━━━━"
    )


# =========================================================
# JOB CONTROLS
# =========================================================
def stop_live_countdown():
    global countdown_job
    if countdown_job is not None:
        try:
            countdown_job.schedule_removal()
        except Exception:
            pass
    countdown_job = None


def start_live_countdown(job_queue):
    global countdown_job
    stop_live_countdown()
    countdown_job = job_queue.run_repeating(live_tick, interval=5, first=0, name="live_countdown")


def stop_draw_jobs():
    global draw_progress_job, draw_finalize_job
    if draw_progress_job is not None:
        try:
            draw_progress_job.schedule_removal()
        except Exception:
            pass
    if draw_finalize_job is not None:
        try:
            draw_finalize_job.schedule_removal()
        except Exception:
            pass
    draw_progress_job = None
    draw_finalize_job = None


def stop_autodraw_jobs():
    global autodraw_tick_job, autodraw_finalize_job
    if autodraw_tick_job is not None:
        try:
            autodraw_tick_job.schedule_removal()
        except Exception:
            pass
    if autodraw_finalize_job is not None:
        try:
            autodraw_finalize_job.schedule_removal()
        except Exception:
            pass
    autodraw_tick_job = None
    autodraw_finalize_job = None


def stop_claim_expire_job():
    global claim_expire_job
    if claim_expire_job is not None:
        try:
            claim_expire_job.schedule_removal()
        except Exception:
            pass
    claim_expire_job = None


def stop_reset_progress_job():
    global reset_progress_job
    if reset_progress_job is not None:
        try:
            reset_progress_job.schedule_removal()
        except Exception:
            pass
    reset_progress_job = None


# =========================================================
# LIVE COUNTDOWN TICK
# =========================================================
def live_tick(context: CallbackContext):
    with lock:
        if not data.get("active"):
            stop_live_countdown()
            return

        start_time = data.get("start_time")
        if start_time is None:
            data["start_time"] = now_ts()
            save_data()
            start_time = data["start_time"]

        start = datetime.utcfromtimestamp(start_time)
        duration = int(data.get("duration_seconds", 1) or 1)
        elapsed = int((utc_now() - start).total_seconds())
        remaining = duration - elapsed

        live_mid = data.get("live_message_id")

    if remaining <= 0:
        # close giveaway
        with lock:
            data["active"] = False
            data["closed"] = True
            save_data()

        # delete live message
        if live_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception:
                pass

        # closed post (simple)
        try:
            m = context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=build_closed_post_text(
                    prize=data.get("prize", ""),
                    total_p=participants_count(),
                    total_w=int(data.get("winner_count", 0) or 0),
                ),
                disable_web_page_preview=True,
            )
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
        except Exception:
            pass

        stop_live_countdown()

        # ✅ AUTO DRAW START (FIXED)
        if data.get("autodraw_enabled"):
            with lock:
                if not data.get("autodraw_in_progress"):
                    data["autodraw_in_progress"] = True
                    save_data()
            try:
                start_autodraw_channel_progress(context)
            except Exception as e:
                with lock:
                    data["autodraw_in_progress"] = False
                    save_data()
                try:
                    context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Auto Draw start failed: {e}")
                except Exception:
                    pass
        else:
            try:
                context.bot.send_message(chat_id=ADMIN_ID, text="✅ Giveaway closed. Auto Draw is OFF. Use /draw.")
            except Exception:
                pass
        return

    if not live_mid:
        return

    # update live post
    try:
        safe_edit_text(
            context.bot,
            CHANNEL_ID,
            live_mid,
            build_live_text(remaining),
            reply_markup=join_button_markup(),
        )
    except Exception:
        pass


# =========================================================
# MANUAL DRAW (Admin only)
# =========================================================
DRAW_SECONDS = 40

def build_manual_draw_progress(percent: int) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 Selecting winners: {percent}%\n"
        f"📊 Progress: {build_bar(percent)}\n\n"
        "🔄 Winner selection is in progress\n"
        "✅ This draw is 100% fair & random.\n"
        "🔐 User ID based selection only.\n\n"
        "Please wait"
    )


def start_manual_draw_progress(context: CallbackContext, admin_chat_id: int):
    global draw_progress_job, draw_finalize_job
    stop_draw_jobs()

    msg = context.bot.send_message(chat_id=admin_chat_id, text=build_manual_draw_progress(0))

    ctx = {
        "admin_chat_id": admin_chat_id,
        "admin_msg_id": msg.message_id,
        "start_ts": now_ts(),
        "tick": 0,
    }

    def tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(DRAW_SECONDS)) * 100)))
        safe_edit_text(job_ctx.bot, jd["admin_chat_id"], jd["admin_msg_id"], build_manual_draw_progress(percent))

    draw_progress_job = context.job_queue.run_repeating(tick, interval=1, first=0, context=ctx)
    draw_finalize_job = context.job_queue.run_once(manual_draw_finalize, when=DRAW_SECONDS, context=ctx)


def manual_draw_finalize(context: CallbackContext):
    stop_draw_jobs()
    jd = context.job.context
    admin_chat_id = jd["admin_chat_id"]
    admin_msg_id = jd["admin_msg_id"]

    with lock:
        parts = data.get("participants", {}) or {}
        if not parts:
            safe_edit_text(context.bot, admin_chat_id, admin_msg_id, "No participants to draw winners from.")
            return

        # eligible pool for winners selection (username required)
        eligible = []
        for uid, info in parts.items():
            uname = (info or {}).get("username", "")
            if uname and uname.startswith("@"):
                eligible.append(uid)

        if not eligible:
            safe_edit_text(context.bot, admin_chat_id, admin_msg_id, "No eligible entries (username required).")
            return

        total_winners = max(1, int(data.get("winner_count", 1) or 1))

        # first join winner (must have username to be eligible; otherwise skip)
        first_uid = data.get("first_winner_id")
        first_uname = data.get("first_winner_username", "")

        winners = {}
        if first_uid and str(first_uid) in parts:
            fu = first_uname or (parts.get(str(first_uid), {}) or {}).get("username", "")
            if fu and fu.startswith("@"):
                winners[str(first_uid)] = {"username": fu, "first": True}

        pool = [uid for uid in eligible if uid not in winners]
        need = max(0, total_winners - len(winners))
        need = min(need, len(pool))
        picked = random.sample(pool, need) if need > 0 else []

        for uid in picked:
            uname = (parts.get(uid, {}) or {}).get("username", "")
            winners[str(uid)] = {"username": uname, "first": False}

        # preview text (admin only) — will be posted to channel after approve
        lines = []
        lines.append("🏆 GIVEAWAY WINNER ANNOUNCEMENT 🏆")
        lines.append("")
        lines.append(HOST_NAME)
        lines.append("")
        lines.append(f"🎁 PRIZE: {data.get('prize','')}")
        lines.append(f"📦 Prize Delivery: 0/{total_winners}")
        lines.append("")

        if winners:
            first_block = [uid for uid, info in winners.items() if info.get("first") is True]
            if first_block:
                uid = first_block[0]
                lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
                lines.append(f"👑 {winners[uid]['username']}")
                lines.append(f"🆔 {uid}")
                lines.append("")

        lines.append("👑 OTHER WINNERS")
        i = 1
        for uid, info in winners.items():
            if info.get("first") is True:
                continue
            lines.append(f"{i}️⃣ 👤 {info.get('username','')} | 🆔 {uid} | Pending ⏳")
            i += 1

        lines.append("")
        lines.append("👇 Click the button below to claim your prize")
        lines.append("")
        lines.append("⏳ Rule: Claim within 24 hours — after that, prize expires.")

        data["pending_winners_text"] = "\n".join(lines)
        data["winners_preview"] = winners
        save_data()

    safe_edit_text(
        context.bot,
        admin_chat_id,
        admin_msg_id,
        data["pending_winners_text"],
        reply_markup=winners_approve_markup(),
    )


# =========================================================
# AUTO DRAW (Channel live selection + buttons)
# =========================================================
AUTO_SELECT_TOTAL_SECONDS = 10 * 60  # 10 minutes

COLOR_EMOJIS = ["🟡", "🟠", "⚫", "🟣", "🔵", "🟢", "🟤", "⚪", "🔴"]
SPIN = ["🔄", "🔃", "🔁", "🔂"]

def start_autodraw_channel_progress(context: CallbackContext):
    """
    Starts channel selection post; updates it live; finalizes winners post.
    """
    stop_autodraw_jobs()

    with lock:
        parts = data.get("participants", {}) or {}
        total_winners = max(1, int(data.get("winner_count", 1) or 1))
        title = data.get("title", HOST_NAME)
        prize = data.get("prize", "")

        gid = gen_giveaway_id()
        data["latest_gid"] = gid
        data["autodraw_gid"] = gid
        data["autodraw_in_progress"] = True

        # eligible pool: username required
        eligible = []
        for uid, info in parts.items():
            uname = (info or {}).get("username", "")
            if uname and uname.startswith("@"):
                eligible.append(uid)

        # first winner (if eligible)
        winners = {}
        first_uid = data.get("first_winner_id")
        first_uname = data.get("first_winner_username", "")
        if first_uid and str(first_uid) in parts:
            fu = first_uname or (parts.get(str(first_uid), {}) or {}).get("username", "")
            if fu and fu.startswith("@"):
                winners[str(first_uid)] = {"username": fu, "first": True, "lucky": False}

        # snapshot
        snap = {
            "gid": gid,
            "created_ts": now_ts(),
            "selection_start_ts": now_ts(),

            "title": title,
            "prize": prize,
            "winner_count": total_winners,

            "participants_total": len(parts),
            "eligible_total": len(eligible),

            "winners": winners,          # uid -> {"username","first","lucky"}
            "delivered": {},             # uid -> True
            "completed": False,

            "selection_message_id": None,
            "winners_message_id": None,

            "claim_start_ts": None,
            "claim_expires_ts": None,

            "lucky_won_by": None,        # uid if lucky clicked first
        }

        data["history"][gid] = snap
        save_data()

    # post selection message in channel
    show_items = _pick_showcase_items(gid, k=3, used=set())
    text = build_selection_post_text(
        gid=gid,
        percent=0,
        time_remain=AUTO_SELECT_TOTAL_SECONDS,
        show_items=show_items,
        winners_selected=len((data["history"][gid].get("winners", {}) or {})),
        total_winners=int(data["history"][gid].get("winner_count", 1) or 1),
    )

    m = context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        reply_markup=selection_buttons_markup(gid),
        disable_web_page_preview=True,
    )

    try:
        context.bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=m.message_id, disable_notification=True)
    except Exception:
        pass

    with lock:
        data["history"][gid]["selection_message_id"] = m.message_id
        save_data()

    # start tick updates
    ctx = {
        "gid": gid,
        "start_ts": now_ts(),
        "tick": 0,

        # showcase cycle controls
        "used_show_uids": [],
        "show_last_1": 0,
        "show_last_2": 0,
        "show_last_3": 0,
        "show1": None,
        "show2": None,
        "show3": None,
    }

    def tick(job_ctx: CallbackContext):
        _autodraw_tick(job_ctx)

    global autodraw_tick_job, autodraw_finalize_job
    autodraw_tick_job = context.job_queue.run_repeating(tick, interval=1, first=0, context=ctx, name="autodraw_tick")
    autodraw_finalize_job = context.job_queue.run_once(_autodraw_finalize, when=AUTO_SELECT_TOTAL_SECONDS, context=ctx, name="autodraw_finalize")


def _eligible_uids_for_gid(gid: str):
    snap = (data.get("history", {}) or {}).get(gid, {}) or {}
    parts = data.get("participants", {}) or {}
    eligible = []
    for uid, info in parts.items():
        uname = (info or {}).get("username", "")
        if uname and uname.startswith("@"):
            eligible.append(uid)
    # keep stable-ish order
    random.shuffle(eligible)
    return eligible


def _pick_showcase_items(gid: str, k=3, used=None):
    used = used or set()
    parts = data.get("participants", {}) or {}
    eligible = [uid for uid in _eligible_uids_for_gid(gid) if uid not in used]

    items = []
    for _ in range(k):
        if not eligible:
            break
        uid = eligible.pop(0)
        info = parts.get(uid, {}) or {}
        uname = info.get("username", "") or f"User ID: {uid}"
        emoji = random.choice(COLOR_EMOJIS)
        items.append((emoji, uname, uid))
        used.add(uid)

    return items


def _autodraw_tick(context: CallbackContext):
    jd = context.job.context
    gid = jd["gid"]

    with lock:
        snap = (data.get("history", {}) or {}).get(gid, {}) or {}
        if not snap or snap.get("completed") is True:
            stop_autodraw_jobs()
            return

        mid = snap.get("selection_message_id")
        if not mid:
            return

        start_sel = float(snap.get("selection_start_ts", snap.get("created_ts", 0)) or 0)
        elapsed = int(now_ts() - start_sel)
        remain = max(0, AUTO_SELECT_TOTAL_SECONDS - elapsed)

        percent = int(round(min(100, (elapsed / float(AUTO_SELECT_TOTAL_SECONDS)) * 100)))

        winners = snap.get("winners", {}) or {}
        total_winners = int(snap.get("winner_count", 1) or 1)

        # auto add winners gradually (real feel): random chance each tick
        # (never fixed interval; feels "real")
        if len(winners) < total_winners:
            # chance increases with time
            chance = 0.02 + (elapsed / float(AUTO_SELECT_TOTAL_SECONDS)) * 0.08  # up to ~10%
            if random.random() < chance:
                # pick next random eligible not already winner
                parts = data.get("participants", {}) or {}
                eligible = [uid for uid in parts.keys()
                            if (parts.get(uid, {}) or {}).get("username", "").startswith("@")
                            and uid not in winners]
                if eligible:
                    uid = random.choice(eligible)
                    winners[str(uid)] = {
                        "username": (parts.get(uid, {}) or {}).get("username", ""),
                        "first": False,
                        "lucky": False,
                    }
                    snap["winners"] = winners
                    save_data()

        # showcase timing: 1st changes every 5s; 2nd every 7s; 3rd every 9s
        # keep 3 different emojis each update
        used_set = set(jd.get("used_show_uids", []) or [])
        parts = data.get("participants", {}) or {}

        def pick_one(exclude_uid=None):
            eligible = [uid for uid, info in parts.items()
                        if (info or {}).get("username", "").startswith("@")]
            eligible = [u for u in eligible if u not in used_set and u != exclude_uid]
            if not eligible:
                eligible = [uid for uid, info in parts.items()
                            if (info or {}).get("username", "").startswith("@") and uid != exclude_uid]
            if not eligible:
                return None
            uid = random.choice(eligible)
            used_set.add(uid)
            return uid

        # init
        if jd.get("show1") is None:
            jd["show1"] = pick_one()
            jd["show_last_1"] = elapsed
        if jd.get("show2") is None:
            jd["show2"] = pick_one(exclude_uid=jd.get("show1"))
            jd["show_last_2"] = elapsed
        if jd.get("show3") is None:
            jd["show3"] = pick_one(exclude_uid=jd.get("show2"))
            jd["show_last_3"] = elapsed

        if elapsed - int(jd.get("show_last_1", 0)) >= 5:
            jd["show1"] = pick_one(exclude_uid=jd.get("show2"))
            jd["show_last_1"] = elapsed
        if elapsed - int(jd.get("show_last_2", 0)) >= 7:
            jd["show2"] = pick_one(exclude_uid=jd.get("show3"))
            jd["show_last_2"] = elapsed
        if elapsed - int(jd.get("show_last_3", 0)) >= 9:
            jd["show3"] = pick_one(exclude_uid=jd.get("show1"))
            jd["show_last_3"] = elapsed

        jd["used_show_uids"] = list(used_set)

        # build show items
        def uinfo(uid):
            if not uid:
                return None
            info = parts.get(uid, {}) or {}
            uname = info.get("username", "") or f"User ID: {uid}"
            return uname, str(uid)

        s1 = uinfo(jd.get("show1"))
        s2 = uinfo(jd.get("show2"))
        s3 = uinfo(jd.get("show3"))

        ems = random.sample(COLOR_EMOJIS, k=3) if len(COLOR_EMOJIS) >= 3 else ["🟡","🟠","⚫"]
        show_items = []
        if s1: show_items.append((ems[0], s1[0], s1[1]))
        if s2: show_items.append((ems[1], s2[0], s2[1]))
        if s3: show_items.append((ems[2], s3[0], s3[1]))

        text = build_selection_post_text(
            gid=gid,
            percent=percent,
            time_remain=remain,
            show_items=show_items,
            winners_selected=len(winners),
            total_winners=total_winners,
        )

    # edit in channel
    safe_edit_text(context.bot, CHANNEL_ID, mid, text, reply_markup=selection_buttons_markup(gid))


def _autodraw_finalize(context: CallbackContext):
    stop_autodraw_jobs()
    gid = context.job.context["gid"]

    with lock:
        snap = (data.get("history", {}) or {}).get(gid, {}) or {}
        if not snap:
            return

        # finalize winners list: ensure total_winners reached if possible
        parts = data.get("participants", {}) or {}
        winners = snap.get("winners", {}) or {}
        total_winners = int(snap.get("winner_count", 1) or 1)

        # fill remaining winners from eligible pool
        if len(winners) < total_winners:
            eligible = [uid for uid, info in parts.items()
                        if (info or {}).get("username", "").startswith("@")
                        and uid not in winners]
            random.shuffle(eligible)
            for uid in eligible:
                if len(winners) >= total_winners:
                    break
                winners[str(uid)] = {
                    "username": (parts.get(uid, {}) or {}).get("username", ""),
                    "first": False,
                    "lucky": False,
                }

        snap["winners"] = winners
        snap["completed"] = True

        # claim window start
        ts = now_ts()
        snap["claim_start_ts"] = ts
        snap["claim_expires_ts"] = ts + 24 * 3600

        # save
        data["history"][gid] = snap

        # stop global autodraw marker
        data["autodraw_in_progress"] = False
        data["autodraw_gid"] = None
        save_data()

    # delete closed post (requested)
    try:
        cmid = data.get("closed_message_id")
        if cmid:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=cmid)
    except Exception:
        pass
    with lock:
        data["closed_message_id"] = None
        save_data()

    # delete selection post
    try:
        smid = (data.get("history", {}) or {}).get(gid, {}).get("selection_message_id")
        if smid:
            context.bot.unpin_chat_message(chat_id=CHANNEL_ID, message_id=smid)
    except Exception:
        pass
    try:
        if smid:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=smid)
    except Exception:
        pass

    # post winners announcement
    text = build_winners_post_text(gid)
    m = context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        reply_markup=claim_button_markup(gid),
        disable_web_page_preview=True,
    )
    with lock:
        data["history"][gid]["winners_message_id"] = m.message_id
        save_data()

    # schedule claim expiry button removal (best effort)
    schedule_claim_expire(context.job_queue, gid)


# =========================================================
# CLAIM EXPIRY (remove claim button after 24h)
# =========================================================
def schedule_claim_expire(job_queue, gid: str):
    global claim_expire_job
    stop_claim_expire_job()

    with lock:
        snap = (data.get("history", {}) or {}).get(gid, {}) or {}
        exp = snap.get("claim_expires_ts")
        mid = snap.get("winners_message_id")

    if not exp or not mid:
        return

    remain = float(exp) - now_ts()
    if remain <= 0:
        return

    ctx = {"gid": gid}
    claim_expire_job = job_queue.run_once(_expire_claim_button, when=remain, context=ctx, name="claim_expire_job")


def _expire_claim_button(context: CallbackContext):
    gid = context.job.context["gid"]
    with lock:
        snap = (data.get("history", {}) or {}).get(gid, {}) or {}
        mid = snap.get("winners_message_id")
        if not mid:
            return
    safe_edit_markup(context.bot, CHANNEL_ID, mid, reply_markup=None)


# =========================================================
# AUTODRAW TOGGLE
# =========================================================
def cmd_autodraw(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text("Auto Draw Settings:", reply_markup=autodraw_toggle_markup())


# =========================================================
# PRIZE DELIVERY (admin) — validate username + uid against winners
# =========================================================
def norm_uname(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    return u if u.startswith("@") else "@" + u


def parse_delivered_lines(text: str):
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "|" in line:
            a, b = line.split("|", 1)
            uname = norm_uname(a.strip())
            uid = "".join(ch for ch in b.strip() if ch.isdigit())
        else:
            uname = ""
            uid = "".join(ch for ch in line if ch.isdigit())
        if uid:
            out.append({"username": uname, "uid": uid})
    return out


def validate_delivered_list(gid: str, delivered_items: list):
    hist = data.get("history", {}) or {}
    snap = hist.get(gid)
    if not snap:
        return [], ["❌ Giveaway ID not found."]

    winners = snap.get("winners", {}) or {}
    delivered = snap.get("delivered", {}) or {}

    ok = []
    errs = []

    winners_uid_set = set(winners.keys())

    for item in delivered_items:
        uid = str(item.get("uid") or "").strip()
        uname = norm_uname(item.get("username") or "")

        if uid not in winners_uid_set:
            errs.append(f"❌ Not a winner: {uname+' ' if uname else ''}(ID {uid})")
            continue

        w_uname = norm_uname((winners.get(uid) or {}).get("username", ""))

        if uname and w_uname and uname.lower() != w_uname.lower():
            errs.append(
                f"❌ Username mismatch for ID {uid}\n"
                f"   • You sent: {uname}\n"
                f"   • Winner is: {w_uname}"
            )
            continue

        if delivered.get(uid) is True:
            errs.append(f"⚠️ Already delivered: {w_uname or uname or ('ID '+uid)}")
            continue

        ok.append(uid)

    return ok, errs


def cmd_prizedelivered(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "prize_gid"
    context.user_data["_prize_target_gid"] = None
    update.message.reply_text(
        "📦 PRIZE DELIVERY\n\n"
        "Send Giveaway ID:\n"
        "• Send the Giveaway ID like: P788-P686-B6548\n"
        "• Or send: latest"
    )


def cmd_winnerlist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    hist = data.get("history", {}) or {}
    if not hist:
        update.message.reply_text("No winner history found.")
        return

    # newest first
    items = sorted(hist.items(), key=lambda kv: float((kv[1] or {}).get("created_ts", 0) or 0), reverse=True)

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📜 WINNER LIST (HISTORY)")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    for gid, snap in items[:20]:
        dt = datetime.utcfromtimestamp(float((snap or {}).get("created_ts", 0) or 0))
        dstr = dt.strftime("%d-%m-%Y")
        prize = (snap or {}).get("prize", "")
        lines.append(f"🆔 {gid} | 📅 {dstr}")
        lines.append(f"🎁 {prize}")
        winners = (snap or {}).get("winners", {}) or {}
        delivered = (snap or {}).get("delivered", {}) or {}
        dc = sum(1 for _, v in delivered.items() if v is True)
        lines.append(f"📦 Delivery: {dc}/{int((snap or {}).get('winner_count',0) or 0)}")
        # list winners
        for uid, info in winners.items():
            uname = (info or {}).get("username", "") or f"User ID: {uid}"
            st = "Delivered ✅" if delivered.get(uid) is True else "Pending ⏳"
            lines.append(f"• {uname} | {uid} | {st}")
        lines.append("")

    update.message.reply_text("\n".join(lines))


# =========================================================
# RESET (confirm + 40s progress)
# =========================================================
def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "⚠️ RESET WARNING\n\n"
        "This will erase:\n"
        "• Active giveaway\n"
        "• Participants list\n"
        "• Winners history\n"
        "• Delivery status\n"
        "• Auto/Manual states\n\n"
        "Do you want to continue?",
        reply_markup=reset_confirm_markup()
    )


# =========================================================
# COMMANDS (Admin panel + flow)
# =========================================================
def cmd_start(update: Update, context: CallbackContext):
    u = update.effective_user
    if u and u.id == ADMIN_ID:
        update.message.reply_text(
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
    else:
        # user notice + admin notification
        uname = user_tag((u.username if u else "") or "")
        uid = str(u.id) if u else ""
        txt = (
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ UNAUTHORIZED NOTICE\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Hi there!\n"
            f"Username: {uname or 'N/A'}\n"
            f"User ID: {uid or 'N/A'}\n\n"
            "It looks like you tried to start the giveaway,\n"
            "but this action is available for admins only.\n\n"
            "🎁 This is an official Giveaway Bot.\n"
            "For exciting giveaway updates,\n"
            "join our official channel now:\n"
            f"👉 {CHANNEL_USERNAME}\n\n"
            "🤖 Powered by:\n"
            "Power Point Break — Official Giveaway System\n\n"
            "👤 Bot Owner:\n"
            f"{ADMIN_CONTACT}\n\n"
            "If you think this was a mistake,\n"
            "please feel free to contact an admin anytime.\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        update.message.reply_text(txt, disable_web_page_preview=True)
        try:
            context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🔔 Bot start attempted by non-admin:\n👤 {uname or 'N/A'}\n🆔 {uid or 'N/A'}"
            )
        except Exception:
            pass


def cmd_panel(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "🛠 ADMIN CONTROL PANEL — POWER POINT BREAK\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/draw\n"
        "/endgiveaway\n"
        "/autodraw\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "🔒 BLOCK SYSTEM\n"
        "/blockpermanent\n"
        "/unban\n"
        "/blocklist\n\n"
        "📦 DELIVERY / HISTORY\n"
        "/prizedelivered\n"
        "/winnerlist\n\n"
        "♻️ RESET\n"
        "/reset"
    )


def cmd_newgiveaway(update: Update, context: CallbackContext):
    global admin_state, data
    if not is_admin(update):
        return

    stop_live_countdown()
    stop_draw_jobs()
    stop_autodraw_jobs()
    stop_claim_expire_job()

    with lock:
        keep_perma = data.get("permanent_block", {})
        keep_verify = data.get("verify_targets", [])
        keep_old_mode = data.get("old_winner_mode", "skip")
        keep_old_list = data.get("old_winners", {})

        # reset current giveaway only; keep history
        hist = data.get("history", {}) or {}
        latest = data.get("latest_gid")

        data.clear()
        data.update(fresh_default_data())

        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        data["old_winner_mode"] = keep_old_mode
        data["old_winners"] = keep_old_list
        data["history"] = hist
        data["latest_gid"] = latest

        save_data()

    admin_state = "title"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 NEW GIVEAWAY SETUP STARTED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "STEP 1 — GIVEAWAY TITLE\n\n"
        "Send Giveaway Title:"
    )


def cmd_participants(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    parts = data.get("participants", {}) or {}
    if not parts:
        update.message.reply_text("👥 Participants list is empty.")
        return
    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("👥 PARTICIPANTS LIST")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Total Participants: {len(parts)}")
    lines.append("")
    i = 1
    for uid, info in parts.items():
        uname = (info or {}).get("username", "")
        lines.append(f"{i}. {uname or 'NO_USERNAME'} | User ID: {uid}")
        i += 1
    update.message.reply_text("\n".join(lines))


def cmd_endgiveaway(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not data.get("active"):
        update.message.reply_text("No active giveaway is running right now.")
        return
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ END GIVEAWAY CONFIRMATION\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Are you sure you want to end this giveaway now?\n\n"
        "✅ Confirm End → Giveaway will close\n"
        "❌ Cancel → Giveaway will continue",
        reply_markup=end_confirm_markup()
    )


def cmd_draw(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not data.get("closed"):
        update.message.reply_text("Giveaway is not closed yet or no giveaway running.")
        return
    if not (data.get("participants", {}) or {}):
        update.message.reply_text("No participants to draw winners from.")
        return
    start_manual_draw_progress(context, update.effective_chat.id)


def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ ADD VERIFY (CHAT ID / @USERNAME)\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send Chat ID (recommended) OR @username:\n\n"
        "Examples:\n"
        "-1001234567890\n"
        "@PowerPointBreak"
    )


def cmd_removeverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    targets = data.get("verify_targets", []) or []
    if not targets:
        update.message.reply_text("No verify targets are set.")
        return
    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🗑 REMOVE VERIFY TARGET")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    for i, t in enumerate(targets, start=1):
        lines.append(f"{i}) {t.get('display','')}")
    lines.append("")
    lines.append("Send a number to remove.")
    lines.append("99) Remove ALL")
    admin_state = "remove_verify_pick"
    update.message.reply_text("\n".join(lines))


def cmd_blockpermanent(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "perma_block_list"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 PERMANENT BLOCK\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send list (one per line):\n"
        "User ID only OR username + id\n\n"
        "Examples:\n"
        "7297292\n"
        "@MinexxProo | 7297292"
    )


def cmd_unban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "unban_choose"
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Unban Permanent Block", callback_data="unban_permanent"),
            InlineKeyboardButton("Unban Old Winner Block", callback_data="unban_oldwinner"),
        ]]
    )
    update.message.reply_text("Choose Unban Type:", reply_markup=kb)


def cmd_blocklist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    perma = data.get("permanent_block", {}) or {}
    oldw = data.get("old_winners", {}) or {}
    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📌 BAN LISTS")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"OLD WINNER MODE: {data.get('old_winner_mode','skip').upper()}")
    lines.append("")
    lines.append("⛔ OLD WINNER BLOCK LIST")
    lines.append(f"Total: {len(oldw)}")
    if oldw:
        i = 1
        for uid, info in oldw.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {uname or 'NO_USERNAME'} | User ID: {uid}")
            i += 1
    else:
        lines.append("No old winner blocked users.")
    lines.append("")
    lines.append("🔒 PERMANENT BLOCK LIST")
    lines.append(f"Total: {len(perma)}")
    if perma:
        i = 1
        for uid, info in perma.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {uname or 'NO_USERNAME'} | User ID: {uid}")
            i += 1
    else:
        lines.append("No permanently blocked users.")
    update.message.reply_text("\n".join(lines))


# =========================================================
# ADMIN TEXT FLOW
# =========================================================
def admin_text_handler(update: Update, context: CallbackContext):
    global admin_state, data
    if not is_admin(update):
        return
    if admin_state is None:
        return

    msg = (update.message.text or "").strip()
    if not msg:
        return

    # verify add
    if admin_state == "add_verify":
        ref = normalize_verify_ref(msg)
        if not ref:
            update.message.reply_text("Invalid input.\nSend Chat ID like -100123... or @username.")
            return
        with lock:
            targets = data.get("verify_targets", []) or []
            targets.append({"ref": ref, "display": ref})
            data["verify_targets"] = targets
            save_data()
        update.message.reply_text(
            f"✅ Verify target added: {ref}\nTotal: {len(data.get('verify_targets',[]) or [])}",
            reply_markup=verify_add_more_done_markup()
        )
        return

    # verify remove pick
    if admin_state == "remove_verify_pick":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number.")
            return
        n = int(msg)
        with lock:
            targets = data.get("verify_targets", []) or []
            if n == 99:
                data["verify_targets"] = []
                save_data()
                admin_state = None
                update.message.reply_text("✅ All verify targets removed.")
                return
            if n < 1 or n > len(targets):
                update.message.reply_text("Invalid number.")
                return
            removed = targets.pop(n - 1)
            data["verify_targets"] = targets
            save_data()
        admin_state = None
        update.message.reply_text(f"✅ Removed: {removed.get('display','')}")
        return

    # giveaway setup flow
    if admin_state == "title":
        with lock:
            data["title"] = msg
            save_data()
        admin_state = "prize"
        update.message.reply_text("✅ Title saved.\n\nNow send Giveaway Prize (multi-line allowed):")
        return

    if admin_state == "prize":
        with lock:
            data["prize"] = msg
            save_data()
        admin_state = "winners"
        update.message.reply_text("✅ Prize saved.\n\nNow send Total Winner Count (1 - 1000000):")
        return

    if admin_state == "winners":
        if not msg.isdigit():
            update.message.reply_text("Please send a valid number for winner count.")
            return
        count = max(1, min(1000000, int(msg)))
        with lock:
            data["winner_count"] = count
            save_data()
        admin_state = "duration"
        update.message.reply_text(
            "✅ Winner count saved.\n\n"
            f"🏆 Total Winners: {count}\n\n"
            "⏱ Send Giveaway Duration\n"
            "Example:\n"
            "30 Second\n"
            "30 Minute\n"
            "11 Hour"
        )
        return

    if admin_state == "duration":
        seconds = parse_duration(msg)
        if seconds <= 0:
            update.message.reply_text("Invalid duration. Example: 30 Second / 30 Minute / 11 Hour")
            return
        with lock:
            data["duration_seconds"] = seconds
            save_data()
        admin_state = "old_winner_mode"
        update.message.reply_text(
            "🔐 OLD WINNER PROTECTION MODE\n\n"
            "1) BLOCK OLD WINNERS\n"
            "2) SKIP OLD WINNERS\n\n"
            "Reply with:\n"
            "1 → BLOCK\n"
            "2 → SKIP"
        )
        return

    if admin_state == "old_winner_mode":
        if msg not in ("1", "2"):
            update.message.reply_text("Reply with 1 or 2 only.")
            return
        if msg == "2":
            with lock:
                data["old_winner_mode"] = "skip"
                data["old_winners"] = {}
                save_data()
            admin_state = "rules"
            update.message.reply_text("✅ Old winner mode: SKIP\n\nNow send Giveaway Rules (multi-line):")
            return
        with lock:
            data["old_winner_mode"] = "block"
            data["old_winners"] = {}
            save_data()
        admin_state = "old_winner_block_list"
        update.message.reply_text(
            "⛔ OLD WINNER BLOCK LIST SETUP\n\n"
            "Send old winners list (one per line):\n"
            "@username | user_id\n\n"
            "Example:\n"
            "@minexxproo | 728272\n"
            "556677"
        )
        return

    if admin_state == "old_winner_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            ow = data.get("old_winners", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[uid] = {"username": uname}
            data["old_winners"] = ow
            save_data()
        admin_state = "rules"
        update.message.reply_text(
            f"✅ Old winner block list saved. Added: {len(data['old_winners']) - before}\n\n"
            "Now send Giveaway Rules (multi-line):"
        )
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        update.message.reply_text("✅ Rules saved.\nShowing preview…")
        update.message.reply_text(build_preview_text(), reply_markup=preview_markup(), disable_web_page_preview=True)
        return

    # permanent block list
    if admin_state == "perma_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            perma = data.get("permanent_block", {}) or {}
            before = len(perma)
            for uid, uname in entries:
                perma[uid] = {"username": uname}
            data["permanent_block"] = perma
            save_data()
        admin_state = None
        update.message.reply_text(
            f"✅ Permanent block saved.\nNew Added: {len(data['permanent_block']) - before}\nTotal: {len(data['permanent_block'])}"
        )
        return

    # unban inputs
    if admin_state == "unban_permanent_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        with lock:
            perma = data.get("permanent_block", {}) or {}
            if uid in perma:
                del perma[uid]
                data["permanent_block"] = perma
                save_data()
                update.message.reply_text("✅ Unbanned from Permanent Block successfully.")
            else:
                update.message.reply_text("This user id is not in Permanent Block list.")
        admin_state = None
        return

    if admin_state == "unban_oldwinner_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        with lock:
            ow = data.get("old_winners", {}) or {}
            if uid in ow:
                del ow[uid]
                data["old_winners"] = ow
                save_data()
                update.message.reply_text("✅ Unbanned from Old Winner Block successfully.")
            else:
                update.message.reply_text("This user id is not in Old Winner Block list.")
        admin_state = None
        return

    # prize delivered flow
    if admin_state == "prize_gid":
        gid = msg.strip()
        if gid.lower() == "latest":
            gid = data.get("latest_gid") or ""
        hist = data.get("history", {}) or {}
        if not gid or gid not in hist:
            update.message.reply_text("❌ Giveaway ID not found. Send correct ID or 'latest'.")
            return
        context.user_data["_prize_target_gid"] = gid
        admin_state = "prize_list"
        update.message.reply_text(
            f"✅ Giveaway selected: {gid}\n\n"
            "Now send delivered winners list (one per line):\n"
            "@username | user_id\n\n"
            "Example:\n"
            "@MinexxProo | 5692210187"
        )
        return

    if admin_state == "prize_list":
        gid = context.user_data.get("_prize_target_gid") or data.get("latest_gid")
        if not gid:
            update.message.reply_text("❌ No Giveaway ID selected. Run /prizedelivered again.")
            admin_state = None
            return

        items = parse_delivered_lines(msg)
        if not items:
            update.message.reply_text("⚠️ No valid lines found. Send: @username | user_id")
            return

        ok_uids, errors = validate_delivered_list(gid, items)
        if not ok_uids:
            text = "⚠️ Delivery list has problems:\n\n" + "\n\n".join(errors[:12])
            if len(errors) > 12:
                text += f"\n\n… and {len(errors)-12} more."
            text += "\n\n✅ Please resend only the correct delivered winners list."
            update.message.reply_text(text)
            return

        with lock:
            snap = data["history"][gid]
            delivered = snap.get("delivered", {}) or {}
            for uid in ok_uids:
                delivered[str(uid)] = True
            snap["delivered"] = delivered
            data["history"][gid] = snap
            save_data()

        # update channel winners post
        wmid = (data.get("history", {}) or {}).get(gid, {}).get("winners_message_id")
        if wmid:
            safe_edit_text(
                context.bot, CHANNEL_ID, wmid,
                build_winners_post_text(gid),
                reply_markup=claim_button_markup(gid)
            )

        success = (
            "✅ Prize delivery updated successfully.\n"
            f"Giveaway ID: {gid}\n"
            f"Updated: {len(ok_uids)} winner(s)"
        )
        if errors:
            warn = "\n\n⚠️ Skipped items:\n" + "\n".join(errors[:10])
            if len(errors) > 10:
                warn += f"\n… and {len(errors)-10} more."
            success += warn

        update.message.reply_text(success)
        context.user_data.pop("_prize_target_gid", None)
        admin_state = None
        return


# =========================================================
# CALLBACK HANDLER
# =========================================================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state, data, reset_progress_job

    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

    # verify buttons
    if qd == "verify_add_more":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        admin_state = "add_verify"
        try:
            query.edit_message_text("Send another Chat ID or @username:")
        except Exception:
            pass
        return

    if qd == "verify_add_done":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        admin_state = None
        try:
            query.edit_message_text(
                f"✅ Verify setup completed.\nTotal targets: {len(data.get('verify_targets',[]) or [])}"
            )
        except Exception:
            pass
        return

    # preview actions
    if qd.startswith("preview_"):
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()

        if qd == "preview_approve":
            try:
                duration = int(data.get("duration_seconds", 0) or 1)
                m = context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=build_live_text(duration),
                    reply_markup=join_button_markup(),
                    disable_web_page_preview=True,
                )
                with lock:
                    data["live_message_id"] = m.message_id
                    data["active"] = True
                    data["closed"] = False
                    data["start_time"] = now_ts()
                    data["closed_message_id"] = None

                    data["participants"] = {}
                    data["pending_winners_text"] = ""
                    data["winners_preview"] = {}
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""

                    save_data()

                start_live_countdown(context.job_queue)
                query.edit_message_text("✅ Giveaway approved and posted to channel.")
            except Exception as e:
                query.edit_message_text(f"Failed to post in channel. Ensure bot is admin.\nError: {e}")
            return

        if qd == "preview_reject":
            query.edit_message_text("❌ Giveaway rejected.")
            return

        if qd == "preview_edit":
            query.edit_message_text("✏️ Edit Mode\n\nStart again with /newgiveaway")
            return

    # end giveaway confirm/cancel
    if qd == "end_confirm":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()

        with lock:
            if not data.get("active"):
                try:
                    query.edit_message_text("No active giveaway is running right now.")
                except Exception:
                    pass
                return

            data["active"] = False
            data["closed"] = True
            save_data()

        live_mid = data.get("live_message_id")
        if live_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception:
                pass

        try:
            m = context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=build_closed_post_text(
                    prize=data.get("prize", ""),
                    total_p=participants_count(),
                    total_w=int(data.get("winner_count", 0) or 0),
                ),
                disable_web_page_preview=True,
            )
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
        except Exception:
            pass

        stop_live_countdown()

        # auto draw start (fixed)
        if data.get("autodraw_enabled"):
            with lock:
                if not data.get("autodraw_in_progress"):
                    data["autodraw_in_progress"] = True
                    save_data()
            try:
                start_autodraw_channel_progress(context)
            except Exception as e:
                with lock:
                    data["autodraw_in_progress"] = False
                    save_data()
                try:
                    context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Auto Draw start failed: {e}")
                except Exception:
                    pass

        try:
            query.edit_message_text("✅ Giveaway closed.")
        except Exception:
            pass
        return

    if qd == "end_cancel":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        try:
            query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        except Exception:
            pass
        return

    # reset confirm/cancel + 40s progress
    if qd == "reset_cancel":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        try:
            query.edit_message_text("✅ Reset cancelled.")
        except Exception:
            pass
        return

    if qd == "reset_confirm":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()

        stop_live_countdown()
        stop_draw_jobs()
        stop_autodraw_jobs()
        stop_claim_expire_job()
        stop_reset_progress_job()

        # start 40s progress (10 steps, every 4s)
        ctx = {"chat_id": query.message.chat_id, "message_id": query.message.message_id, "step": 0}

        def reset_tick(job_ctx: CallbackContext):
            jd = job_ctx.job.context
            step = int(jd.get("step", 0))
            if step >= 10:
                with lock:
                    keep_perma = data.get("permanent_block", {})
                    keep_verify = data.get("verify_targets", [])
                    data.clear()
                    data.update(fresh_default_data())
                    data["permanent_block"] = keep_perma
                    data["verify_targets"] = keep_verify
                    save_data()
                try:
                    job_ctx.bot.edit_message_text(
                        chat_id=jd["chat_id"],
                        message_id=jd["message_id"],
                        text="✅ Reset completed!\n\nBot has been fully reset A to Z."
                    )
                except Exception:
                    pass
                stop_reset_progress_job()
                return

            pct = step * 10
            bar = "▰" * step + "▱" * (10 - step)
            try:
                job_ctx.bot.edit_message_text(
                    chat_id=jd["chat_id"],
                    message_id=jd["message_id"],
                    text=f"🔄 Resetting bot... {pct}%\n📊 Progress: {bar}"
                )
            except Exception:
                pass

            jd["step"] = step + 1
            job_ctx.job.context = jd

        global reset_progress_job
        reset_progress_job = context.job_queue.run_repeating(reset_tick, interval=4, first=0, context=ctx, name="reset_progress")
        return

    # unban choose
    if qd == "unban_permanent":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        admin_state = "unban_permanent_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Permanent Block:")
        except Exception:
            pass
        return

    if qd == "unban_oldwinner":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        admin_state = "unban_oldwinner_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Old Winner Block:")
        except Exception:
            pass
        return

    # AutoDraw toggle
    if qd in ("autodraw_on", "autodraw_off"):
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        with lock:
            data["autodraw_enabled"] = (qd == "autodraw_on")
            save_data()
        try:
            query.edit_message_text(f"✅ Auto Draw is now {'ON' if data['autodraw_enabled'] else 'OFF'}.")
        except Exception:
            pass
        return

    # JOIN GIVEAWAY
    if qd == "join_giveaway":
        if not data.get("active"):
            query.answer("This giveaway is not active right now.", show_alert=True)
            return

        # verify
        if not verify_user_join(context.bot, int(uid)):
            query.answer(popup_verify_required(), show_alert=True)
            return

        # perma block
        if uid in (data.get("permanent_block", {}) or {}):
            query.answer(popup_permanent_blocked(), show_alert=True)
            return

        # old winner block
        if data.get("old_winner_mode") == "block":
            if uid in (data.get("old_winners", {}) or {}):
                query.answer(popup_old_winner_blocked(), show_alert=True)
                return

        # first winner repeat click
        with lock:
            first_uid = data.get("first_winner_id")
        if first_uid and uid == str(first_uid):
            tg_user = query.from_user
            uname = user_tag(tg_user.username or "") or data.get("first_winner_username", "") or "@username"
            query.answer(popup_first_join(uname, uid), show_alert=True)
            return

        # already joined
        if uid in (data.get("participants", {}) or {}):
            query.answer(popup_already_joined(), show_alert=True)
            return

        tg_user = query.from_user
        uname = user_tag(tg_user.username or "")
        full_name = (tg_user.full_name or "").strip()

        with lock:
            if not data.get("first_winner_id"):
                data["first_winner_id"] = uid
                data["first_winner_username"] = uname
                data["first_winner_name"] = full_name

            data["participants"][uid] = {"username": uname, "name": full_name}
            save_data()

        # update live post
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time")
            if live_mid and start_ts:
                start = datetime.utcfromtimestamp(start_ts)
                duration = int(data.get("duration_seconds", 1) or 1)
                elapsed = int((utc_now() - start).total_seconds())
                remaining = max(0, duration - elapsed)
                safe_edit_text(context.bot, CHANNEL_ID, live_mid, build_live_text(remaining), reply_markup=join_button_markup())
        except Exception:
            pass

        # popup
        with lock:
            if data.get("first_winner_id") == uid:
                query.answer(popup_first_join(uname or "@username", uid), show_alert=True)
            else:
                query.answer(popup_join_success(uname or "@Username", uid), show_alert=True)
        return

    # manual winners approve/reject (posts to channel as a new gid snapshot)
    if qd == "winners_approve":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()

        text = (data.get("pending_winners_text") or "").strip()
        winners = data.get("winners_preview", {}) or {}
        if not text or not winners:
            try:
                query.edit_message_text("No pending winners preview found.")
            except Exception:
                pass
            return

        # create snapshot in history
        gid = gen_giveaway_id()
        with lock:
            snap = {
                "gid": gid,
                "created_ts": now_ts(),
                "selection_start_ts": now_ts(),
                "title": HOST_NAME,
                "prize": data.get("prize", ""),
                "winner_count": int(data.get("winner_count", 1) or 1),
                "participants_total": participants_count(),
                "eligible_total": len([1 for _, i in (data.get("participants", {}) or {}).items() if (i or {}).get("username","").startswith("@")]),
                "winners": winners,
                "delivered": {},
                "completed": True,
                "selection_message_id": None,
                "winners_message_id": None,
                "claim_start_ts": now_ts(),
                "claim_expires_ts": now_ts() + 24 * 3600,
                "lucky_won_by": None,
            }
            data["history"][gid] = snap
            data["latest_gid"] = gid
            save_data()

        # delete closed post if exists
        try:
            cmid = data.get("closed_message_id")
            if cmid:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=cmid)
        except Exception:
            pass
        with lock:
            data["closed_message_id"] = None
            save_data()

        # post winners
        m = context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=build_winners_post_text(gid),
            reply_markup=claim_button_markup(gid),
            disable_web_page_preview=True,
        )
        with lock:
            data["history"][gid]["winners_message_id"] = m.message_id
            save_data()

        schedule_claim_expire(context.job_queue, gid)

        try:
            query.edit_message_text("✅ Approved! Winners list posted to channel.")
        except Exception:
            pass
        return

    if qd == "winners_reject":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        with lock:
            data["pending_winners_text"] = ""
            data["winners_preview"] = {}
            save_data()
        try:
            query.edit_message_text("❌ Rejected! Winners list will NOT be posted.")
        except Exception:
            pass
        return

    # claim prize (per giveaway)
    if qd.startswith("claim:"):
        gid = qd.split(":", 1)[1].strip()
        hist = data.get("history", {}) or {}
        snap = hist.get(gid, {}) or {}
        winners = snap.get("winners", {}) or {}
        delivered = snap.get("delivered", {}) or {}
        exp = snap.get("claim_expires_ts")

        # if giveaway completed long ago and claim expired -> completed message for everyone
        if exp and now_ts() > float(exp):
            query.answer(popup_giveaway_completed(ADMIN_CONTACT), show_alert=True)
            return

        # not winner
        if uid not in winners:
            query.answer(popup_not_winner(), show_alert=True)
            return

        # already delivered
        w_uname = (winners.get(uid, {}) or {}).get("username", "") or "@username"
        if delivered.get(uid) is True:
            query.answer(popup_prize_already_delivered(w_uname, uid, ADMIN_CONTACT), show_alert=True)
            return

        # winner
        query.answer(popup_claim_winner(w_uname, uid, ADMIN_CONTACT), show_alert=True)
        return

    # selection buttons: rule / luck
    if qd.startswith("rule:"):
        gid = qd.split(":", 1)[1].strip()
        # if delivered/completed for this user => completed popup
        snap = (data.get("history", {}) or {}).get(gid, {}) or {}
        delivered = (snap.get("delivered", {}) or {})
        if delivered.get(uid) is True:
            w_uname = ((snap.get("winners", {}) or {}).get(uid, {}) or {}).get("username", "") or "@username"
            query.answer(popup_giveaway_completed(ADMIN_CONTACT), show_alert=True)
            return
        query.answer(popup_lucky_rule(), show_alert=True)
        return

    if qd.startswith("luck:"):
        gid = qd.split(":", 1)[1].strip()
        snap = (data.get("history", {}) or {}).get(gid, {}) or {}
        if not snap:
            query.answer("This selection is not available.", show_alert=True)
            return

        winners = snap.get("winners", {}) or {}
        delivered = snap.get("delivered", {}) or {}
        parts = data.get("participants", {}) or {}

        # delivered/completed: always completed popup
        if delivered.get(uid) is True:
            w_uname = (winners.get(uid, {}) or {}).get("username", "") or "@username"
            query.answer(popup_giveaway_completed(ADMIN_CONTACT), show_alert=True)
            return

        # must have joined entries
        if uid not in (data.get("participants", {}) or {}):
            query.answer(popup_not_joined_tryluck(), show_alert=True)
            return

        # must have valid @username
        my_uname = (parts.get(uid, {}) or {}).get("username", "")
        if not my_uname or not my_uname.startswith("@"):
            query.answer(popup_not_eligible_username(), show_alert=True)
            return

        # must have entries exist
        eligible_exist = any((info or {}).get("username", "").startswith("@") for _, info in parts.items())
        if not eligible_exist:
            query.answer(popup_tryluck_no_entries(), show_alert=True)
            return

        # lucky timing (48-49 second window)
        start_sel = float(snap.get("selection_start_ts", snap.get("created_ts", 0)) or 0)
        elapsed = int(now_ts() - start_sel)
        sec = elapsed % 60

        with lock:
            snap = (data.get("history", {}) or {}).get(gid, {}) or {}
            # already won by someone
            lucky_uid = snap.get("lucky_won_by")

            # if already won -> TOO LATE with winner details
            if lucky_uid:
                lucky_info = (snap.get("winners", {}) or {}).get(str(lucky_uid), {}) or {}
                lucky_uname = lucky_info.get("username", "") or "@username"
                query.answer(popup_too_late(lucky_uname, str(lucky_uid)), show_alert=True)
                return

            # window open only at 48..49
            if sec not in (48, 49):
                # no "Lucky Draw Closed" popup anymore -> use TOO LATE style only if someone already won
                # otherwise: just show rule (simple)
                query.answer(popup_lucky_rule(), show_alert=True)
                return

            # WIN: first click in window
            snap["lucky_won_by"] = uid

            # add as winner (if not already)
            if uid not in (snap.get("winners", {}) or {}):
                snap["winners"][uid] = {"username": my_uname, "first": False, "lucky": True}

            data["history"][gid] = snap
            save_data()

        query.answer(
            "🌟 CONGRATULATIONS!\n"
            "You won the 🍀 Lucky Draw Winner slot ✅\n\n"
            f"👤 {my_uname}\n"
            f"🆔 {uid}\n"
            "Take screenshot and send in the group to confirm 👈\n\n"
            "🏆 Added to winners list LIVE!",
            show_alert=True
        )
        return

    try:
        query.answer()
    except Exception:
        pass


# =========================================================
# MAIN
# =========================================================
def main():
    global data
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing in .env")

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # basic
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("panel", cmd_panel))

    # giveaway
    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))
    dp.add_handler(CommandHandler("autodraw", cmd_autodraw))

    # verify
    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    # bans
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # delivery / history
    dp.add_handler(CommandHandler("prizedelivered", cmd_prizedelivered))
    dp.add_handler(CommandHandler("winnerlist", cmd_winnerlist))

    # reset
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # handlers
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # resume systems after restart
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    # ✅ resume autodraw if giveaway closed + autodraw enabled + in_progress true but selection not running
    try:
        if data.get("closed") and data.get("autodraw_enabled"):
            # if it says in progress but jobs lost -> start again safely
            if data.get("autodraw_in_progress"):
                updater.job_queue.run_once(lambda c: start_autodraw_channel_progress(c), when=2)
    except Exception:
        pass

    print("Bot is running (PTB v13, non-async) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
