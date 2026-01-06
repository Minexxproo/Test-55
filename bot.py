import os
import json
import random
import threading
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
# GLOBAL STATE
# =========================================================
data = {}
admin_state = None
countdown_job = None

draw_job = None
draw_finalize_job = None

# =========================================================
# CONSTANTS
# =========================================================
LIVE_UPDATE_INTERVAL = 5          # Giveaway post update every 5s
DRAW_DURATION_SECONDS = 40        # /draw progress duration
DRAW_UPDATE_INTERVAL = 1          # progress update every 1s (fast)
CLAIM_EXPIRE_SECONDS = 24 * 3600  # 24h

DOTS = [".", "..", "...", "....", ".....", "......", "......."]
SPIN = ["🔄", "🔁", "♻️", "🔃"]  # fake rotating icon (animation)

# =========================================================
# DATA / STORAGE
# =========================================================
def fresh_default_data():
    return {
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
        "winners_message_id": None,

        # participants
        "participants": {},  # uid(str) -> {"username": "@x" or "", "name": ""}

        # verify targets
        "verify_targets": [],  # [{"ref": "-100..." or "@xxx", "display": "..."}]

        # permanent block
        "permanent_block": {},  # uid -> {"username": "@x" or ""}

        # old winner protection mode
        # "skip"  => everyone can join, included in selection
        # "block" => old winners cannot join (uses old_winners dict)
        "old_winner_mode": "skip",
        "old_winners": {},  # uid -> {"username": "@x" or ""}

        # first join winner
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # winners final for current giveaway
        "winners": {},  # uid -> {"username": "@x"}

        # pending previews
        "pending_winners_text": "",
        "pending_complete_prize_text": "",

        # message times
        "winners_post_time": None,  # timestamp when winners posted (for claim expire)

        # winner history (all time)
        # list of dict: {"uid","username","prize","title","win_type","date","time","ts"}
        "winner_history": [],
        # message id of prize completed post (optional)
        "prize_completed_message_id": None,
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
    return d


def save_data():
    with lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)


data = load_data()

# =========================================================
# HELPERS
# =========================================================
def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == ADMIN_ID)


def user_tag(username: str) -> str:
    if not username:
        return ""
    u = username.strip()
    if not u:
        return ""
    return u if u.startswith("@") else "@" + u


def now_ts() -> float:
    return datetime.utcnow().timestamp()


def participants_count() -> int:
    return len(data.get("participants", {}) or {})


def format_hms(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_progress(percent: float) -> str:
    percent = max(0, min(100, percent))
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


def format_rules() -> str:
    rules = (data.get("rules") or "").strip()
    if not rules:
        return (
            "• Must join our official channel\n"
            "• Only real accounts are allowed\n"
            "• Multiple entries are not permitted\n"
            "• Stay in the channel until results are announced\n"
            "• Admin decision will be final"
        )
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    return "\n".join("• " + l for l in lines)


def parse_user_lines(text: str):
    """
    Accept:
    123456789
    @name | 123456789
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


# =========================================================
# MARKUPS
# =========================================================
def join_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data="join_giveaway")]]
    )


def claim_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data="claim_prize")]]
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


def complete_prize_approve_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data="complete_prize_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="complete_prize_reject"),
        ]]
    )


def unban_choose_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Unban Permanent Block", callback_data="unban_permanent"),
            InlineKeyboardButton("Unban Old Winner Block", callback_data="unban_oldwinner"),
        ]]
    )


def removeban_choose_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Reset Permanent Ban List", callback_data="reset_permanent_ban"),
            InlineKeyboardButton("Reset Old Winner Ban List", callback_data="reset_oldwinner_ban"),
        ]]
    )


def confirm_reset_ban_markup(confirm_cb: str):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm", callback_data=confirm_cb),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
        ]]
    )


# =========================================================
# VERIFY SYSTEM
# =========================================================
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


# =========================================================
# POPUP TEXTS (ONLY SPACING, NO EXTRA WORD/EMOJI ADDED)
# =========================================================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n"
        "To join this giveaway, you must join the required channels/groups first ✅\n"
        "👇 After joining all of them, click JOIN GIVEAWAY again."
    )


def popup_old_winner_blocked() -> str:
    return (
        "🚫You have already won a previous giveaway.\n"
        "To keep the giveaway fair for everyone,\n"
        "repeat winners are restricted from participating.\n"
        "🙏Please wait for the next Giveaway"
    )


def popup_first_winner(username: str, uid: str) -> str:
    return (
        "✨CONGRATULATIONS🌟\n"
        "You joined the giveaway FIRST and secured the🥇1st Winner spot!\n"
        f"👤{username}|🆔{uid}\n"
        "📸Screenshot & post in the group to confirm."
    )


def popup_already_joined() -> str:
    return (
        "🚫 ENTRY Unsuccessful\n"
        "You’ve already joined\n"
        "this giveaway 🎁\n\n"
        "Multiple entries aren’t allowed.\n"
        "Please wait for the final result ⏳"
    )


def popup_join_success(username: str, uid: str) -> str:
    return (
        "🌹 CONGRATULATIONS!\n"
        "You’ve successfully joined\n"
        "the giveaway ✅\n\n"
        "Your details:\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        f"— {HOST_NAME}"
    )


def popup_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n"
        "You are permanently blocked from joining giveaways.\n"
        f"If you believe this is a mistake, contact admin: {ADMIN_CONTACT}"
    )


def popup_claim_winner(username: str, uid: str) -> str:
    return (
        "🌟Congratulations✨\n"
        "You’ve won this giveaway.\n"
        f"👤 {username} | 🆔 {uid}\n"
        "📩   please  Contract admin Claim your prize now:\n"
        f"👉 {ADMIN_CONTACT}"
    )


def popup_claim_expired() -> str:
    return (
        "⛔ PRIZE EXPIRED\n"
        "Your 24-hour claim time is over.\n"
        "This prize is no longer available."
    )


def popup_claim_not_winner() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "❌ NOT A WINNER\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Sorry! Your User ID is not in the winners list.\n"
        "Please wait for the next giveaway 🤍"
    )


# =========================================================
# GIVEAWAY TEXT BUILDERS
# =========================================================
def build_preview_text() -> str:
    remaining = int(data.get("duration_seconds", 0) or 0)
    progress = build_progress(0)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 GIVEAWAY PREVIEW (ADMIN ONLY)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ {data.get('title','')} ⚡\n\n"
        "🎁 Prize:\n"
        f"{data.get('prize','')}\n\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n"
        "👥 Total Participants: 0\n"
        "🎯 Winner Type: Random\n\n"
        f"⏰ Time Remaining: {format_hms(remaining)}\n"
        f"📊 Progress: {progress}\n\n"
        "📜 Rules:\n"
        f"{format_rules()}\n\n"
        f"📢 Hosted By: {HOST_NAME}\n"
        f"🔗 Official Channel: {CHANNEL_USERNAME}\n\n"
        "👇 Please tap the button below to join the giveaway"
    )


def build_live_text(remaining: int) -> str:
    duration = int(data.get("duration_seconds", 1) or 1)
    elapsed = duration - remaining
    elapsed = max(0, min(duration, elapsed))
    percent = int(round((elapsed / float(duration)) * 100))
    progress = build_progress(percent)

    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ {data.get('title','')} ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 Prize:\n"
        f"{data.get('prize','')}\n\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n"
        f"👥 Total Participants: {participants_count()}\n"
        "🎯 Winner Type: Random\n\n"
        f"⏰ Time Remaining: {format_hms(remaining)}\n"
        f"📊 Progress: {progress}\n\n"
        "📜 Rules:\n"
        f"{format_rules()}\n\n"
        f"📢 Hosted By: {HOST_NAME}\n"
        f"🔗 Official Channel: {CHANNEL_USERNAME}\n\n"
        "👇 Please tap the button below to join the giveaway"
    )


def build_closed_post_text() -> str:
    # 2 border lines only (your request)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway duration has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        "🏆 Winner selection is in progress.\n"
        "Please wait for the announcement.\n\n"
        "🙏 Thank you to everyone who participated.\n"
        f"— {HOST_NAME}"
    )


def build_winners_post_text(first_uid: str, first_user: str, random_winners: list) -> str:
    # random_winners: list of tuples [(uid, username_or_empty), ...]
    lines = []
    lines.append("🏆 GIVEAWAY WINNERS ANNOUNCEMENT 🏆")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"⚡ {data.get('title','')} ⚡")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("╔══════════════════════╗")
    lines.append("║ 🥇 1ST WINNER • FIRST JOIN 🥇 ║")
    lines.append("╠══════════════════════╣")
    if first_user:
        lines.append(f"║ 👤 {first_user} | 🆔 {first_uid} ║")
    else:
        lines.append(f"║ 👤 User ID: {first_uid} ║")
    lines.append("╚══════════════════════╝")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("👑 OTHER WINNERS (SELECTED RANDOMLY):")

    i = 1
    for uid, uname in random_winners:
        if uname:
            lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}")
        else:
            lines.append(f"{i}️⃣ 👤 User ID: {uid}")
        i += 1

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🎉 Congratulations to all the winners!")
    lines.append("✅ This giveaway was completed using a")
    lines.append("100% fair & transparent random system.")
    lines.append("")
    lines.append("⏳ Prize must be claimed within 24 hours or it will expire.")
    lines.append("")
    lines.append(f"📢 Hosted By: {HOST_NAME}")
    lines.append("👇 Click the button below to claim your prize:")

    return "\n".join(lines)


def build_complete_prize_text() -> str:
    winners_map = data.get("winners", {}) or {}
    title = data.get("title", "") or ""
    prize = data.get("prize", "") or ""

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🎉🏆 PRIZE DELIVERY COMPLETED 🏆🎉")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("We are happy to announce that")
    lines.append("all giveaway prizes have been")
    lines.append("successfully delivered to the winners ✅")
    lines.append("")
    if title:
        lines.append(f"⚡ Giveaway: {title}")
        lines.append("")
    lines.append("👑 Confirmed Winners:")

    if not winners_map:
        lines.append("No winners found to confirm.")
    else:
        i = 1
        for uid, info in winners_map.items():
            uid = str(uid)
            uname = (info or {}).get("username", "") or ""
            if uname:
                lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}")
            else:
                lines.append(f"{i}️⃣ 👤 User ID: {uid}")
            i += 1

    lines.append("")
    if prize:
        lines.append("🎁 Prize Delivered:")
        lines.append(prize)
        lines.append("")
    lines.append("📦 Prize Status:")
    lines.append("All prizes have been delivered")
    lines.append("successfully without any issues 🎁✨")
    lines.append("")
    lines.append("📩 Need help or have questions?")
    lines.append("Please contact the admin:")
    lines.append(f"👉 {ADMIN_CONTACT}")
    lines.append("")
    lines.append(f"Thank you for being part of\n{HOST_NAME} 💙")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lines)


# =========================================================
# LIVE COUNTDOWN (CHANNEL POST UPDATE) - Every 5 seconds
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
    countdown_job = job_queue.run_repeating(live_tick, interval=LIVE_UPDATE_INTERVAL, first=0)


def live_tick(context: CallbackContext):
    global data
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
        now = datetime.utcnow()
        duration = int(data.get("duration_seconds", 1) or 1)
        elapsed = int((now - start).total_seconds())
        remaining = duration - elapsed

        live_mid = data.get("live_message_id")

        if remaining <= 0:
            # close giveaway
            data["active"] = False
            data["closed"] = True
            save_data()

            # delete live message
            if live_mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
                except Exception:
                    pass

            # post closed message and save id
            try:
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text())
                data["closed_message_id"] = m.message_id
                save_data()
            except Exception:
                pass

            # notify admin private
            try:
                context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "⏰ Giveaway Closed Automatically!\n\n"
                        f"Giveaway: {data.get('title','')}\n"
                        f"Total Participants: {participants_count()}\n\n"
                        "Now use /draw to select winners."
                    ),
                )
            except Exception:
                pass

            stop_live_countdown()
            return

        if not live_mid:
            return

        # edit live post
        try:
            context.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=live_mid,
                text=build_live_text(remaining),
                reply_markup=join_button_markup(),
            )
        except Exception:
            pass


# =========================================================
# DRAW PROGRESS (40s, 1s update) - NO NUMBER COUNTDOWN
# =========================================================
def stop_draw_jobs():
    global draw_job, draw_finalize_job
    if draw_job is not None:
        try:
            draw_job.schedule_removal()
        except Exception:
            pass
    draw_job = None

    if draw_finalize_job is not None:
        try:
            draw_finalize_job.schedule_removal()
        except Exception:
            pass
    draw_finalize_job = None


def build_draw_progress_text(percent: int, dots: str, spin: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 Selecting winners... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        f"{spin} Winner selection is in progress...\n\n"
        "✅ This draw is 100% fair & random.\n"
        "🔐 User ID based selection only.\n\n"
        f"Please wait{dots}"
    )


def start_draw_progress(context: CallbackContext, admin_chat_id: int):
    global draw_job, draw_finalize_job
    stop_draw_jobs()

    msg = context.bot.send_message(
        chat_id=admin_chat_id,
        text=build_draw_progress_text(0, ".", SPIN[0]),
    )

    ctx = {
        "admin_chat_id": admin_chat_id,
        "admin_msg_id": msg.message_id,
        "start_ts": now_ts(),
        "tick": 0,
    }

    def draw_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        tick = int(jd.get("tick", 0)) + 1
        jd["tick"] = tick

        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100.0, (elapsed / float(DRAW_DURATION_SECONDS)) * 100.0)))

        dots = DOTS[(tick - 1) % len(DOTS)]
        spin = SPIN[(tick - 1) % len(SPIN)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["admin_msg_id"],
                text=build_draw_progress_text(percent, dots, spin),
            )
        except Exception:
            pass

    draw_job = context.job_queue.run_repeating(
        draw_tick,
        interval=DRAW_UPDATE_INTERVAL,
        first=0,
        context=ctx,
        name="draw_progress_job",
    )

    draw_finalize_job = context.job_queue.run_once(
        draw_finalize,
        when=DRAW_DURATION_SECONDS,
        context=ctx,
        name="draw_finalize_job",
    )


def add_winner_history(uid: str, username: str, win_type: str):
    # win_type: "🥇 1st Winner (First Join)" or "👑 Random Winner"
    ts = now_ts()
    dt = datetime.utcfromtimestamp(ts)
    entry = {
        "uid": str(uid),
        "username": username or "",
        "prize": data.get("prize", "") or "",
        "title": data.get("title", "") or "",
        "win_type": win_type,
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M:%S"),
        "ts": ts,
    }
    hist = data.get("winner_history", []) or []
    hist.append(entry)
    data["winner_history"] = hist


def draw_finalize(context: CallbackContext):
    global data
    stop_draw_jobs()

    jd = context.job.context
    admin_chat_id = jd["admin_chat_id"]
    admin_msg_id = jd["admin_msg_id"]

    with lock:
        participants = data.get("participants", {}) or {}
        if not participants:
            try:
                context.bot.edit_message_text(
                    chat_id=admin_chat_id,
                    message_id=admin_msg_id,
                    text="No participants to draw winners from.",
                )
            except Exception:
                pass
            return

        winner_count = int(data.get("winner_count", 1) or 1)
        winner_count = max(1, winner_count)

        # first winner must exist
        first_uid = data.get("first_winner_id")
        if not first_uid:
            first_uid = next(iter(participants.keys()))
            info = participants.get(first_uid, {}) or {}
            data["first_winner_id"] = first_uid
            data["first_winner_username"] = info.get("username", "")
            data["first_winner_name"] = info.get("name", "")
            save_data()

        first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

        # pick remaining winners randomly excluding first winner
        pool = [uid for uid in participants.keys() if uid != first_uid]
        remaining_needed = max(0, winner_count - 1)
        if remaining_needed > len(pool):
            remaining_needed = len(pool)

        selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

        winners_map = {}
        winners_map[str(first_uid)] = {"username": first_uname}

        random_list = []
        for uid in selected:
            info = participants.get(uid, {}) or {}
            winners_map[str(uid)] = {"username": info.get("username", "")}
            random_list.append((str(uid), info.get("username", "")))

        data["winners"] = winners_map
        pending_text = build_winners_post_text(str(first_uid), first_uname, random_list)
        data["pending_winners_text"] = pending_text
        save_data()

    try:
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            text=pending_text,
            reply_markup=winners_approve_markup(),
        )
    except Exception:
        context.bot.send_message(
            chat_id=admin_chat_id,
            text=pending_text,
            reply_markup=winners_approve_markup(),
        )


# =========================================================
# COMMANDS
# =========================================================
def cmd_start(update: Update, context: CallbackContext):
    u = update.effective_user
    if u and u.id == ADMIN_ID:
        update.message.reply_text(
            "🛡️👑 WELCOME BACK, ADMIN 👑🛡️\n\n"
            "⚙️ System Status: ONLINE ✅\n"
            "🚀 Giveaway Engine: READY\n"
            "🔐 Security Level: MAXIMUM\n\n"
            "🧭 Open the Admin Control Panel:\n"
            "/panel\n\n"
            f"⚡ POWERED BY: {HOST_NAME}"
        )
    else:
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please join our official channel and wait for the giveaway post.\n\n"
            "🔗 Official Channel:\n"
            f"{CHANNEL_LINK}"
        )


def cmd_panel(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "🛠 ADMIN CONTROL PANEL – POWER POINT BREAK\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/draw\n"
        "/endgiveaway\n"
        "/completePrize\n"
        "/winnerlist\n\n"
        "🔒 BAN SYSTEM\n"
        "/blockpermanent\n"
        "/blockoldwinner\n"
        "/unban\n"
        "/removeban\n"
        "/blocklist\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "♻️ RESET\n"
        "/reset"
    )


def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ ADD VERIFY (CHAT ID / @USERNAME)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send Chat ID (recommended) OR @username:\n\n"
        "Examples:\n"
        "-1001234567890\n"
        "@PowerPointBreak\n\n"
        "After adding, users must join ALL verify targets to join giveaway."
    )


def cmd_removeverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    targets = data.get("verify_targets", []) or []
    if not targets:
        update.message.reply_text("No verify targets are set.")
        return

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "🗑 REMOVE VERIFY TARGET",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "Current Verify Targets:",
        "",
    ]
    for i, t in enumerate(targets, start=1):
        lines.append(f"{i}) {t.get('display','')}")
    lines += [
        "",
        "Send a number to remove that target.",
        "11) Remove ALL verify targets",
    ]
    admin_state = "remove_verify_pick"
    update.message.reply_text("\n".join(lines))


def cmd_newgiveaway(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return

    with lock:
        # keep verify + permanent + old winners history list
        keep_perma = data.get("permanent_block", {}) or {}
        keep_verify = data.get("verify_targets", []) or {}
        keep_oldw = data.get("old_winners", {}) or {}
        keep_hist = data.get("winner_history", []) or []

        data.clear()
        data.update(fresh_default_data())

        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        data["old_winners"] = keep_oldw
        data["winner_history"] = keep_hist

        save_data()

    admin_state = "title"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 NEW GIVEAWAY SETUP STARTED\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "STEP 1️⃣ — GIVEAWAY TITLE\n\n"
        "Send Giveaway Title:"
    )


def cmd_participants(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    parts = data.get("participants", {}) or {}
    if not parts:
        update.message.reply_text("👥 Participants List is empty.")
        return

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "👥 PARTICIPANTS LIST (ADMIN VIEW)",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Total Participants: {len(parts)}",
        "",
    ]
    i = 1
    for uid, info in parts.items():
        uname = (info or {}).get("username", "")
        if uname:
            lines.append(f"{i}. {uname} | User ID: {uid}")
        else:
            lines.append(f"{i}. User ID: {uid}")
        lines.append("")
        i += 1

    update.message.reply_text("\n".join(lines))


def cmd_endgiveaway(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not data.get("active"):
        update.message.reply_text("No active giveaway is running right now.")
        return

    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ END GIVEAWAY CONFIRMATION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
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

    start_draw_progress(context, update.effective_chat.id)


def cmd_blockpermanent(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "perma_block_list"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 PERMANENT BLOCK\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send list (one per line):\n"
        "User ID only OR username + id\n\n"
        "Examples:\n"
        "7297292\n"
        "@MinexxProo | 7297292"
    )


def cmd_blockoldwinner(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "oldwinner_manual_add"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⛔ OLD WINNER BLOCK (MANUAL ADD)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send old winners list (one per line):\n\n"
        "Format:\n"
        "@username | user_id\n"
        "or\n"
        "user_id\n\n"
        "Example:\n"
        "@minexxproo | 8392828\n"
        "@hsieuehej | 833828292\n"
        "@hdieyehhd | 839393"
    )


def cmd_unban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "unban_choose"
    update.message.reply_text("Choose Unban Type:", reply_markup=unban_choose_markup())


def cmd_removeban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "removeban_choose"
    update.message.reply_text("Choose which ban list to reset:", reply_markup=removeban_choose_markup())


def cmd_blocklist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    perma = data.get("permanent_block", {}) or {}
    oldw = data.get("old_winners", {}) or {}

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📌 BAN LISTS")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"OLD WINNER MODE: {str(data.get('old_winner_mode','skip')).upper()}")
    lines.append("")

    lines.append("⛔ OLD WINNER BLOCK LIST")
    lines.append(f"Total: {len(oldw)}")
    if oldw:
        i = 1
        for uid, info in oldw.items():
            uname = (info or {}).get("username", "")
            if uname:
                lines.append(f"{i}) {uname} | User ID: {uid}")
            else:
                lines.append(f"{i}) User ID: {uid}")
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
            if uname:
                lines.append(f"{i}) {uname} | User ID: {uid}")
            else:
                lines.append(f"{i}) User ID: {uid}")
            i += 1
    else:
        lines.append("No permanently blocked users.")

    update.message.reply_text("\n".join(lines))


def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    # immediate reset (keeps verify + permanent + old winners + history)
    with lock:
        keep_perma = data.get("permanent_block", {}) or {}
        keep_verify = data.get("verify_targets", []) or []
        keep_oldw = data.get("old_winners", {}) or {}
        keep_hist = data.get("winner_history", []) or []

        # delete channel messages if possible
        for key in ("live_message_id", "closed_message_id", "winners_message_id", "prize_completed_message_id"):
            mid = data.get(key)
            if mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
                except Exception:
                    pass

        data.clear()
        data.update(fresh_default_data())
        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        data["old_winners"] = keep_oldw
        data["winner_history"] = keep_hist
        save_data()

    stop_live_countdown()
    stop_draw_jobs()
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ RESET COMPLETED SUCCESSFULLY!\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Start again with:\n"
        "/newgiveaway"
    )


def cmd_completePrize(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    winners_map = data.get("winners", {}) or {}
    if not winners_map:
        update.message.reply_text(
            "No winners found.\n\n"
            "First complete winners process:\n"
            "1) /draw\n"
            "2) Approve & Post winners\n\n"
            "Then use /completePrize."
        )
        return

    text = build_complete_prize_text()
    with lock:
        data["pending_complete_prize_text"] = text
        save_data()

    update.message.reply_text(text, reply_markup=complete_prize_approve_markup())


def cmd_winnerlist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    hist = data.get("winner_history", []) or []
    if not hist:
        update.message.reply_text("No winner history found yet.")
        return

    # show latest first
    hist_sorted = sorted(hist, key=lambda x: float(x.get("ts", 0)), reverse=True)

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆 WINNER HISTORY LIST (ALL WINNERS)")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    idx = 1
    for w in hist_sorted:
        uid = str(w.get("uid", ""))
        uname = w.get("username", "") or ""
        date = w.get("date", "")
        time = w.get("time", "")
        win_type = w.get("win_type", "")
        prize = w.get("prize", "")
        title = w.get("title", "")

        if uname:
            lines.append(f"{idx}️⃣ 👤 {uname} | 🆔 {uid}")
        else:
            lines.append(f"{idx}️⃣ 👤 User ID: {uid}")
        if date or time:
            lines.append(f"📅 Win Date: {date}")
            lines.append(f"⏰ Win Time: {time}")
        if win_type:
            lines.append(f"🏅 Win Type: {win_type}")
        if prize:
            lines.append("")
            lines.append("🎁 Prize Won:")
            lines.append(prize)
        if title:
            lines.append("")
            lines.append("⚡ Giveaway:")
            lines.append(title)
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")
        idx += 1

        # avoid huge spam
        if idx > 50:
            lines.append("... (showing latest 50 only)")
            break

    update.message.reply_text("\n".join(lines).strip())


# =========================================================
# ADMIN TEXT FLOW
# =========================================================
def admin_text_handler(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    if admin_state is None:
        return

    msg = (update.message.text or "").strip()
    if not msg:
        return

    # ADD VERIFY
    if admin_state == "add_verify":
        ref = normalize_verify_ref(msg)
        if not ref:
            update.message.reply_text("Invalid input.\nSend Chat ID like -100123... or @username.")
            return

        with lock:
            targets = data.get("verify_targets", []) or []
            if len(targets) >= 100:
                update.message.reply_text("Max verify targets reached (100). Remove some first.")
                return

            targets.append({"ref": ref, "display": ref})
            data["verify_targets"] = targets
            save_data()

        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET ADDED SUCCESSFULLY!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Added: {ref}\n"
            f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}\n\n"
            "Add another or Done?",
            reply_markup=verify_add_more_done_markup()
        )
        return

    # REMOVE VERIFY PICK
    if admin_state == "remove_verify_pick":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number (1,2,3... or 11).")
            return
        n = int(msg)

        with lock:
            targets = data.get("verify_targets", []) or []
            if not targets:
                admin_state = None
                update.message.reply_text("No verify targets remain.")
                return

            if n == 11:
                data["verify_targets"] = []
                save_data()
                admin_state = None
                update.message.reply_text("✅ All verify targets removed successfully!")
                return

            if n < 1 or n > len(targets):
                update.message.reply_text("Invalid number. Try again.")
                return

            removed = targets.pop(n - 1)
            data["verify_targets"] = targets
            save_data()

        admin_state = None
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET REMOVED\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Removed: {removed.get('display','')}\n"
            f"Remaining: {len(data.get('verify_targets', []) or [])}"
        )
        return

    # GIVEAWAY SETUP FLOW
    if admin_state == "title":
        with lock:
            data["title"] = msg
            save_data()
        admin_state = "prize"
        update.message.reply_text("✅ Title saved!\n\nNow send Giveaway Prize (multi-line allowed):")
        return

    if admin_state == "prize":
        with lock:
            data["prize"] = msg
            save_data()
        admin_state = "winners"
        update.message.reply_text("✅ Prize saved!\n\nNow send Total Winner Count (1 - 1000000):")
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
            "✅ Winner count saved!\n\n"
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

        # OLD WINNER MODE selection step
        admin_state = "old_winner_mode"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 OLD WINNER PROTECTION MODE\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "1️⃣ BLOCK OLD WINNERS\n"
            "• Old winners cannot join this giveaway\n\n"
            "2️⃣ SKIP OLD WINNERS\n"
            "• Everyone can join\n"
            "• Old winners will ALSO be included in winner selection\n\n"
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
                save_data()
            admin_state = "rules"
            update.message.reply_text("Now send Giveaway Rules (multi-line):")
            return

        # block mode
        with lock:
            data["old_winner_mode"] = "block"
            save_data()

        admin_state = "old_winner_block_list"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⛔ OLD WINNER BLOCK LIST SETUP\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send old winners list (one per line):\n\n"
            "Format:\n"
            "@username | user_id\n\n"
            "Example:\n"
            "@minexxproo | 728272\n"
            "@user2 | 889900\n"
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
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ OLD WINNER BLOCK LIST SAVED!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 Total Added: {len(data.get('old_winners', {}) or {}) - before}\n"
            "Now send Giveaway Rules (multi-line):"
        )
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        update.message.reply_text("✅ Rules saved!\nShowing preview…")
        update.message.reply_text(build_preview_text(), reply_markup=preview_markup())
        return

    # PERMANENT BLOCK
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
            "✅ Permanent block saved!\n"
            f"New Added: {len(data.get('permanent_block', {}) or {}) - before}\n"
            f"Total Blocked: {len(data.get('permanent_block', {}) or {})}"
        )
        return

    # MANUAL OLD WINNER ADD
    if admin_state == "oldwinner_manual_add":
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

        admin_state = None
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ OLD WINNER BLOCK LIST UPDATED!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 New Added: {len(data.get('old_winners', {}) or {}) - before}\n"
            f"🔒 Total Old Winner Blocked: {len(data.get('old_winners', {}) or {})}\n\n"
            "Done ✅"
        )
        return

    # UNBAN INPUT
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
                update.message.reply_text("✅ Unbanned from Permanent Block successfully!")
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
                update.message.reply_text("✅ Unbanned from Old Winner Block successfully!")
            else:
                update.message.reply_text("This user id is not in Old Winner Block list.")
        admin_state = None
        return


# =========================================================
# CALLBACK HANDLER
# =========================================================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

    # Verify buttons
    if qd == "verify_add_more":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        admin_state = "add_verify"
        try:
            query.edit_message_text("Send another Chat ID or @username:")
        except Exception:
            pass
        return

    if qd == "verify_add_done":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        admin_state = None
        try:
            query.edit_message_text(
                "✅ Verify setup completed successfully!\n\n"
                f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}"
            )
        except Exception:
            pass
        return

    # Preview actions
    if qd.startswith("preview_"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return

        if qd == "preview_approve":
            try:
                query.answer()
            except Exception:
                pass

            try:
                duration = int(data.get("duration_seconds", 0) or 1)

                m = context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=build_live_text(duration),
                    reply_markup=join_button_markup(),
                )

                with lock:
                    data["live_message_id"] = m.message_id
                    data["active"] = True
                    data["closed"] = False
                    data["start_time"] = now_ts()

                    data["closed_message_id"] = None
                    data["winners_message_id"] = None
                    data["winners_post_time"] = None

                    # reset current giveaway runtime data
                    data["participants"] = {}
                    data["winners"] = {}
                    data["pending_winners_text"] = ""
                    data["pending_complete_prize_text"] = ""
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""

                    save_data()

                start_live_countdown(context.job_queue)

                query.edit_message_text("✅ Giveaway approved and posted to channel!")
            except Exception as e:
                try:
                    query.edit_message_text(f"Failed to post in channel. Make sure bot is admin.\nError: {e}")
                except Exception:
                    pass
            return

        if qd == "preview_reject":
            try:
                query.answer()
            except Exception:
                pass
            try:
                query.edit_message_text("❌ Giveaway rejected and cleared.")
            except Exception:
                pass
            return

        if qd == "preview_edit":
            try:
                query.answer()
            except Exception:
                pass
            try:
                query.edit_message_text("✏️ Edit Mode\n\nStart again with /newgiveaway")
            except Exception:
                pass
            return

    # End giveaway confirm/cancel
    if qd == "end_confirm":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return

        try:
            query.answer()
        except Exception:
            pass

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

        # delete live post
        live_mid = data.get("live_message_id")
        if live_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception:
                pass

        # post closed
        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text())
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
        except Exception:
            pass

        stop_live_countdown()
        try:
            query.edit_message_text("✅ Giveaway Closed Successfully! Now use /draw")
        except Exception:
            pass
        return

    if qd == "end_cancel":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        try:
            query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        except Exception:
            pass
        return

    # Unban choose
    if qd == "unban_permanent":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        admin_state = "unban_permanent_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Permanent Block:")
        except Exception:
            pass
        return

    if qd == "unban_oldwinner":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        admin_state = "unban_oldwinner_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Old Winner Block:")
        except Exception:
            pass
        return

    # removeban choose confirm
    if qd == "reset_permanent_ban":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        try:
            query.edit_message_text("Confirm reset Permanent Ban List?", reply_markup=confirm_reset_ban_markup("confirm_reset_permanent"))
        except Exception:
            pass
        return

    if qd == "reset_oldwinner_ban":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        try:
            query.edit_message_text("Confirm reset Old Winner Ban List?", reply_markup=confirm_reset_ban_markup("confirm_reset_oldwinner"))
        except Exception:
            pass
        return

    if qd == "cancel_reset_ban":
        try:
            query.answer()
        except Exception:
            pass
        admin_state = None
        try:
            query.edit_message_text("Cancelled.")
        except Exception:
            pass
        return

    if qd == "confirm_reset_permanent":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        with lock:
            data["permanent_block"] = {}
            save_data()
        try:
            query.edit_message_text("✅ Permanent Ban List has been reset.")
        except Exception:
            pass
        return

    if qd == "confirm_reset_oldwinner":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        with lock:
            data["old_winners"] = {}
            save_data()
        try:
            query.edit_message_text("✅ Old Winner Ban List has been reset.")
        except Exception:
            pass
        return

    # Winners Approve/Reject
    if qd == "winners_approve":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass

        text = (data.get("pending_winners_text") or "").strip()
        if not text:
            try:
                query.edit_message_text("No pending winners preview found.")
            except Exception:
                pass
            return

        # delete CLOSED post before posting winners (your rule)
        closed_mid = data.get("closed_message_id")
        if closed_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception:
                pass

        try:
            m = context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                reply_markup=claim_button_markup(),
            )

            with lock:
                data["winners_message_id"] = m.message_id
                data["closed_message_id"] = None
                data["winners_post_time"] = now_ts()

                # auto-add winners into old_winners (history-based protection)
                winners_map = data.get("winners", {}) or {}
                ow = data.get("old_winners", {}) or {}
                for wuid, info in winners_map.items():
                    wuid = str(wuid)
                    ow[wuid] = {"username": (info or {}).get("username", "") or ""}

                data["old_winners"] = ow

                # save winner history (all time)
                first_uid = str(data.get("first_winner_id") or "")
                for wuid, info in winners_map.items():
                    wuid = str(wuid)
                    uname = (info or {}).get("username", "") or ""
                    if wuid == first_uid:
                        add_winner_history(wuid, uname, "🥇 1st Winner (First Join)")
                    else:
                        add_winner_history(wuid, uname, "👑 Random Winner")

                data["pending_winners_text"] = ""
                save_data()

            try:
                query.edit_message_text("✅ Approved! Winners list posted to channel (with Claim button).")
            except Exception:
                pass

            # admin confirm message
            try:
                context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "✅ Winners post has been published successfully!\n\n"
                        f"Giveaway: {data.get('title','')}\n"
                        f"Winners saved to history: {len(data.get('winners', {}) or {})}\n\n"
                        "You can check all winners with:\n"
                        "/winnerlist"
                    )
                )
            except Exception:
                pass

        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post winners in channel: {e}")
            except Exception:
                pass
        return

    if qd == "winners_reject":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        with lock:
            data["pending_winners_text"] = ""
            save_data()
        try:
            query.edit_message_text("❌ Rejected! Winners list will NOT be posted.")
        except Exception:
            pass
        return

    # Complete Prize Approve/Reject
    if qd == "complete_prize_approve":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass

        text = (data.get("pending_complete_prize_text") or "").strip()
        if not text:
            try:
                query.edit_message_text("No pending /completePrize preview found.")
            except Exception:
                pass
            return

        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=text)
            with lock:
                data["prize_completed_message_id"] = m.message_id
                data["pending_complete_prize_text"] = ""
                save_data()

            try:
                query.edit_message_text("✅ Approved! Prize completed post sent to channel.")
            except Exception:
                pass

            try:
                context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "✅ Prize delivery post has been published successfully!\n\n"
                        f"Giveaway: {data.get('title','')}\n"
                        f"Winners confirmed: {len(data.get('winners', {}) or {})}\n\n"
                        "Command used:\n"
                        "/completePrize"
                    )
                )
            except Exception:
                pass

        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post in channel: {e}")
            except Exception:
                pass
        return

    if qd == "complete_prize_reject":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass

        with lock:
            data["pending_complete_prize_text"] = ""
            save_data()

        try:
            query.edit_message_text("❌ Rejected! /completePrize will NOT be posted.")
        except Exception:
            pass
        return

    # Join giveaway
    if qd == "join_giveaway":
        if not data.get("active"):
            try:
                query.answer("This giveaway is not active right now.", show_alert=True)
            except Exception:
                pass
            return

        # verify required
        if not verify_user_join(context.bot, int(uid)):
            try:
                query.answer(popup_verify_required(), show_alert=True)
            except Exception:
                pass
            return

        # permanent block
        if uid in (data.get("permanent_block", {}) or {}):
            try:
                query.answer(popup_permanent_blocked(), show_alert=True)
            except Exception:
                pass
            return

        # old winner blocked only if mode=block
        if data.get("old_winner_mode") == "block":
            if uid in (data.get("old_winners", {}) or {}):
                try:
                    query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception:
                    pass
                return

        # first winner repeated click
        first_uid = str(data.get("first_winner_id") or "")
        if first_uid and uid == first_uid:
            tg_user = query.from_user
            uname = user_tag(tg_user.username or "") or data.get("first_winner_username", "") or "@username"
            try:
                query.answer(popup_first_winner(uname, uid), show_alert=True)
            except Exception:
                pass
            return

        # already joined (normal users)
        if uid in (data.get("participants", {}) or {}):
            try:
                query.answer(popup_already_joined(), show_alert=True)
            except Exception:
                pass
            return

        # success join
        tg_user = query.from_user
        uname = user_tag(tg_user.username or "")
        full_name = (tg_user.full_name or "").strip()

        with lock:
            # first join => 1st winner
            if not data.get("first_winner_id"):
                data["first_winner_id"] = uid
                data["first_winner_username"] = uname
                data["first_winner_name"] = full_name

            data["participants"][uid] = {"username": uname, "name": full_name}
            save_data()

        # instant update live post (participants count)
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time")
            if live_mid and start_ts:
                start = datetime.utcfromtimestamp(start_ts)
                now = datetime.utcnow()
                duration = int(data.get("duration_seconds", 1) or 1)
                elapsed = int((now - start).total_seconds())
                remaining = duration - elapsed
                if remaining < 0:
                    remaining = 0

                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=live_mid,
                    text=build_live_text(remaining),
                    reply_markup=join_button_markup(),
                )
        except Exception:
            pass

        # popup: first winner or normal join
        if str(data.get("first_winner_id") or "") == uid:
            try:
                query.answer(popup_first_winner(uname or "@username", uid), show_alert=True)
            except Exception:
                pass
        else:
            try:
                query.answer(popup_join_success(uname or "@Username", uid), show_alert=True)
            except Exception:
                pass
        return

    # Claim prize
    if qd == "claim_prize":
        winners = data.get("winners", {}) or {}
        if uid in winners:
            # check 24h expiry
            post_time = data.get("winners_post_time")
            if post_time is not None:
                if (now_ts() - float(post_time)) > CLAIM_EXPIRE_SECONDS:
                    try:
                        query.answer(popup_claim_expired(), show_alert=True)
                    except Exception:
                        pass
                    return

            uname = winners.get(uid, {}).get("username", "") or "@username"
            try:
                query.answer(popup_claim_winner(uname, uid), show_alert=True)
            except Exception:
                pass
        else:
            try:
                query.answer(popup_claim_not_winner(), show_alert=True)
            except Exception:
                pass
        return

    # default
    try:
        query.answer()
    except Exception:
        pass


# =========================================================
# MAIN
# =========================================================
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing in .env")

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("panel", cmd_panel))

    # verify
    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    # giveaway
    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))

    # complete prize + winnerlist
    dp.add_handler(CommandHandler("completePrize", cmd_completePrize))
    dp.add_handler(CommandHandler("completeprize", cmd_completePrize))
    dp.add_handler(CommandHandler("winnerlist", cmd_winnerlist))

    # bans
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # reset
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # text / callback
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # resume live giveaway if bot restarted
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    print("Bot is running (PTB 13, GSM compatible, non-async) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
