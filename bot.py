# ===========================
# POWER POINT BREAK GIVEAWAY BOT (PTB v13, non-async)
# python-telegram-bot==13.*
# ===========================

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
    ParseMode,
)
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
closed_anim_job = None

manual_draw_job = None
manual_draw_finalize_job = None

autodraw_job = None
autodraw_finalize_job = None

claim_expire_jobs = {}  # gid -> job

# =========================================================
# DATA / STORAGE
# =========================================================
def fresh_default_data():
    return {
        # active giveaway (single)
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

        # participants
        "participants": {},  # uid(str)-> {"username":"@x","name":""}

        # verify targets
        "verify_targets": [],  # [{"ref":"-100.. or @x", "display":".."}]

        # bans
        "permanent_block": {},  # uid -> {"username":"@x"}
        "old_winner_mode": "skip",  # "block" / "skip"
        "old_winners": {},  # uid -> {"username":"@x"} (only if mode=block)

        # first join champ
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # auto draw
        "autodraw_enabled": False,

        # history (multiple giveaways)
        "history": {},       # gid -> snapshot
        "latest_gid": None,  # last created gid
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
    # normalize
    d.setdefault("history", {})
    d.setdefault("verify_targets", [])
    d.setdefault("permanent_block", {})
    d.setdefault("old_winners", {})
    d.setdefault("participants", {})
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
            "• Must join the official channel\n"
            "• One entry per user only\n"
            "• Stay active until result announcement\n"
            "• Admin decision is final & binding"
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


def gen_giveaway_id() -> str:
    # Example: P788-P686-B6548
    a = random.randint(100, 999)
    b = random.randint(100, 999)
    c = random.randint(1000, 9999)
    return f"P{a}-P{b}-B{c}"


def safe_pin(bot, chat_id: int, message_id: int):
    try:
        bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
    except Exception:
        pass


# =========================================================
# MARKUPS
# =========================================================
def join_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data="join_giveaway")]]
    )


def winners_claim_markup(gid: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data=f"claim:{gid}")]]
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


def winners_approve_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data="winners_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="winners_reject"),
        ]]
    )


def autodraw_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Auto Draw ON", callback_data="autodraw_on"),
            InlineKeyboardButton("⛔ Auto Draw OFF", callback_data="autodraw_off"),
        ]]
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


def try_luck_markup(gid: str):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🍀 Try Your Luck", callback_data=f"luck:{gid}"),
            InlineKeyboardButton("📌 Entry Rule", callback_data="entry_rule"),
        ]]
    )


# =========================================================
# POPUPS
# =========================================================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n\n"
        "To join this giveaway, you must join the required channels/groups first ✅\n\n"
        "After joining all of them, click JOIN GIVEAWAY again."
    )


def popup_old_winner_blocked() -> str:
    return (
        "🚫 REPEAT WINNER RESTRICTED\n\n"
        "You have already won a previous giveaway.\n"
        "To keep it fair, repeat winners are blocked for this giveaway.\n\n"
        "Please wait for the next giveaway."
    )


def popup_first_winner(username: str, uid: str) -> str:
    return (
        "🥇 FIRST JOIN CHAMPION 🌟\n\n"
        "Congratulations! You joined the giveaway FIRST and secured the spot.\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        "📸 Please take a screenshot and post it in the group to confirm 👈"
    )


def popup_already_joined() -> str:
    return (
        "🚫 ENTRY UNSUCCESSFUL\n\n"
        "You’ve already joined this giveaway 🎁\n\n"
        "Multiple entries aren’t allowed.\n"
        "Please wait for the final result."
    )


def popup_join_success(username: str, uid: str) -> str:
    return (
        "✅ JOIN SUCCESSFUL\n\n"
        "You’ve successfully joined the giveaway.\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        f"— {HOST_NAME}"
    )


def popup_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n\n"
        "You are permanently blocked from joining giveaways.\n\n"
        f"Contact admin: {ADMIN_CONTACT}"
    )


def popup_claim_winner(username: str, uid: str) -> str:
    return (
        "🌟 CONGRATULATIONS!\n"
        "You’ve won this giveaway.\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        "📩 Please contact admin to claim your prize:\n"
        f"👉 {ADMIN_CONTACT}"
    )


def popup_claim_not_winner() -> str:
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry! Your User ID is not in the winners list.\n\n"
        "Please wait for the next giveaway ❤️‍🩹"
    )


def popup_prize_expired() -> str:
    return (
        "⏳ PRIZE EXPIRED\n\n"
        "Your 24-hour claim time has ended.\n"
        "This prize is no longer available."
    )


def popup_already_delivered(username: str, uid: str) -> str:
    return (
        "📦 PRIZE ALREADY DELIVERED\n\n"
        "Your prize has already been successfully delivered ✅\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        f"If you face any issue, contact admin 👉 {ADMIN_CONTACT}"
    )


def popup_giveaway_completed() -> str:
    return (
        "✅ GIVEAWAY COMPLETED\n\n"
        "This giveaway has been completed.\n"
        f"If you have any issues, please contact admin 👉 {ADMIN_CONTACT}"
    )


def popup_no_entries_yet() -> str:
    return (
        "⚠️ NO ENTRIES YET\n\n"
        "No one has joined this giveaway yet.\n"
        "So Lucky Draw cannot be played right now.\n\n"
        "✅ Please join the giveaway first, then try again.\n\n"
        f"— {HOST_NAME} ⚡"
    )


def popup_username_required() -> str:
    return (
        "🚫 USERNAME REQUIRED\n\n"
        "You must have a valid @username to use this feature."
    )


def popup_lucky_win(username: str, uid: str) -> str:
    return (
        "🌟 CONGRATULATIONS!\n"
        "You won the 🍀 Lucky Draw Winner slot ✅\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        "Take screenshot and send in the group to confirm 👈\n\n"
        "🏆 Added to winners list LIVE!"
    )


def popup_lucky_closed(winner_text: str = "") -> str:
    base = (
        "⏰ LUCKY DRAW CLOSED\n\n"
        "The Lucky Draw window has just ended.\n"
        "This slot is no longer available.\n\n"
        "Please wait for the final winners announcement."
    )
    if winner_text:
        base += f"\n\nWinner:\n{winner_text}"
    return base


def entry_rule_text(remaining: str) -> str:
    return (
        "📌 ENTRY RULE\n"
        "• Tap 🍀 Try Your Luck at the right moment\n"
        "• First click wins instantly (Lucky Draw)\n"
        "• Must have a valid @username\n"
        "• Winner is added live to the selection post\n"
        "• 100% fair: first-come-first-win\n\n"
        f"⏱ Time: {remaining}"
    )

# =========================================================
# TEXT BUILDERS (LIVE + CLOSED + SELECTION + WINNERS)
# =========================================================
def build_live_text(remaining: int) -> str:
    duration = data.get("duration_seconds", 1) or 1
    elapsed = duration - remaining
    elapsed = max(0, min(duration, elapsed))
    percent = int(round((elapsed / float(duration)) * 100))
    progress = build_progress(percent)

    # Short borders (not breaking telegram)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ {data.get('title','')} ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{data.get('prize','')}\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        "🎯 Winner Selection\n"
        "• 100% Random & Fair\n"
        "• Auto System\n\n"
        f"⏱️ Time Remaining: {format_hms(remaining)}\n"
        "📊 Live Progress\n"
        f"{progress}\n\n"
        "📜 Official Rules\n"
        f"{format_rules()}\n\n"
        f"📢 Hosted by: {HOST_NAME}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Tap below to join the giveaway 👇"
    )


def build_preview_text() -> str:
    remaining = data.get("duration_seconds", 0)
    progress = build_progress(0)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 GIVEAWAY PREVIEW (ADMIN)\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ {data.get('title','')} ⚡\n\n"
        "🎁 Prize:\n"
        f"{data.get('prize','')}\n\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n"
        "👥 Total Participants: 0\n\n"
        f"⏱️ Time Remaining: {format_hms(remaining)}\n"
        "📊 Progress\n"
        f"{progress}\n\n"
        "📜 Rules\n"
        f"{format_rules()}\n\n"
        f"📢 Hosted By: {HOST_NAME}\n"
        f"🔗 Official Channel: {CHANNEL_USERNAME}\n\n"
        "👇 Tap the button below to join"
    )


def build_closed_post_text(prize: str, total_p: int, total_w: int) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now locked.\n\n"
        "📊 Giveaway Summary\n"
        f"🎁 Prize: {prize}\n\n"
        f"👥 Total Participants: {total_p}\n"
        f"🏆 Total Winners: {total_w}\n\n"
        "🎯 Winners will be announced very soon.\n"
        "Please stay tuned for the final results.\n\n"
        "✨ Best of luck to everyone!\n\n"
        f"— {HOST_NAME} ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


SPIN = ["🔄", "🔃", "🔁", "🔂"]
COLORS = ["🟡", "🟠", "⚫", "🟣", "🔵", "🟢", "🔴", "⚪"]

def pick_three_colors():
    # ensure 3 unique each tick
    pool = COLORS[:]
    random.shuffle(pool)
    return pool[0], pool[1], pool[2]

def build_autodraw_text(gid: str, title: str, prize: str, winners_selected: int, total_winners: int,
                        percent: int, remaining: int, now_show: list, spin: str) -> str:
    progress = build_progress(percent)
    # now_show: [(color, "@u", "id"), ...] len=3
    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🎲 LIVE RANDOM WINNER SELECTION")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"⚡ {HOST_NAME} ⚡")
    lines.append("")
    lines.append("🎁 GIVEAWAY SUMMARY")
    lines.append(f"🏆 Prize: {prize}")
    lines.append(f"✅ Winners Selected: {winners_selected}/{total_winners}")
    lines.append("")
    lines.append("📌 Important Rule")
    lines.append("Users without a valid @username")
    lines.append("are automatically excluded.")
    lines.append("")
    lines.append(f"{spin} Selection Progress: {percent}%")
    lines.append(f"📊 Progress Bar: {progress}")
    lines.append("")
    lines.append(f"🕒 Time Remaining: {format_hms(remaining)}")
    lines.append("🔐 System Mode: 100% Random • Fair • Auto")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("👥 LIVE ENTRIES SHOWCASE")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    for color, uname, uid in now_show:
        lines.append(f"{color} Now Showing → {uname} | 🆔 {uid}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def build_winners_post_text(gid: str, title: str, prize: str, winners_map: dict, delivered_map: dict) -> str:
    # winners_map uid -> {"username": "@x", "first": bool, "lucky": bool}
    total = len(winners_map or {})
    delivered_count = sum(1 for u in (delivered_map or {}).values() if u)

    # keep headline in one line
    lines = []
    lines.append("🏆 GIVEAWAY WINNER ANNOUNCEMENT 🏆")
    lines.append("")
    lines.append(f"{HOST_NAME}")
    lines.append("")
    lines.append(f"🆔 Giveaway ID: {gid}")
    lines.append("")
    lines.append("🎁 PRIZE:")
    lines.append(f"{prize}")
    lines.append("")
    lines.append(f"📦 Prize Delivery: {delivered_count}/{total}")
    lines.append("")

    # first join
    first_uid = None
    for uid, info in (winners_map or {}).items():
        if (info or {}).get("first"):
            first_uid = uid
            break
    if first_uid:
        fu = (winners_map[first_uid] or {}).get("username", "") or f"User ID: {first_uid}"
        lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
        lines.append(f"👤 {fu}")
        lines.append(f"🆔 {first_uid}")
        lines.append("")

    # lucky winners flagged
    lucky_uids = [uid for uid, info in (winners_map or {}).items() if (info or {}).get("lucky")]
    if lucky_uids:
        lines.append("🍀 LUCKY DRAW WINNER")
        for uid in lucky_uids:
            lu = (winners_map[uid] or {}).get("username", "") or f"User ID: {uid}"
            mark = "✅ Delivered" if (delivered_map or {}).get(uid) else "⏳ Pending"
            lines.append(f"👤 {lu} | 🆔 {uid} | {mark}")
        lines.append("")

    # others
    lines.append("👑 OTHER WINNERS")
    i = 1
    for uid, info in (winners_map or {}).items():
        if uid == first_uid:
            continue
        if uid in lucky_uids:
            continue
        uname = (info or {}).get("username", "")
        if not uname:
            uname = f"User ID: {uid}"
        delivered = "✅ Delivered" if (delivered_map or {}).get(uid) else "⏳ Pending"
        lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid} | {delivered}")
        i += 1

    lines.append("")
    lines.append("👇 Click the button below to claim your prize")
    lines.append("")
    lines.append("⏳ Rule: Claim within 24 hours — after that, prize expires.")
    return "\n".join(lines)


# =========================================================
# JOBS: LIVE COUNTDOWN
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
        duration = data.get("duration_seconds", 1) or 1
        elapsed = int((datetime.utcnow() - start).total_seconds())
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

            # post closed message
            try:
                m = context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=build_closed_post_text(
                        prize=data.get("prize", ""),
                        total_p=participants_count(),
                        total_w=data.get("winner_count", 0),
                    ),
                )
                data["closed_message_id"] = m.message_id
                save_data()
            except Exception as e:
                try:
                    context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Failed to post closed message.\nReason: {e}")
                except Exception:
                    pass

            # if autodraw enabled -> start channel selection
            if data.get("autodraw_enabled"):
                try:
                    start_autodraw_channel_progress(context.job_queue, context.bot)
                except Exception as e:
                    try:
                        context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Auto Draw failed to start.\nReason: {e}")
                    except Exception:
                        pass
            else:
                try:
                    context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "✅ Giveaway closed.\n\n"
                            "Auto Draw is OFF.\n"
                            "Use /draw to select winners (manual)."
                        ),
                    )
                except Exception:
                    pass

            stop_live_countdown()
            return

        if not live_mid:
            return

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
# AUTO DRAW (10 minutes, live post pinned)
# - Updates every 1s (progress/time/spin)
# - Showcases 3 users; each line changes at 5/7/9 seconds cycles
# - Excludes users without @username from selection pool
# - Adds winners live during the 10 minutes
# - Adds "Try Your Luck" + "Entry Rule" buttons
# - Lucky window: only between elapsed=48 and 49 seconds (1 second)
# =========================================================
AUTO_DRAW_TOTAL_SECONDS = 10 * 60
AUTO_DRAW_TICK = 1

def stop_autodraw():
    global autodraw_job, autodraw_finalize_job
    if autodraw_job is not None:
        try:
            autodraw_job.schedule_removal()
        except Exception:
            pass
    autodraw_job = None
    if autodraw_finalize_job is not None:
        try:
            autodraw_finalize_job.schedule_removal()
        except Exception:
            pass
    autodraw_finalize_job = None


def start_autodraw_channel_progress(job_queue, bot):
    stop_autodraw()

    with lock:
        parts = data.get("participants", {}) or {}
        title = data.get("title", "")
        prize = data.get("prize", "")
        total_winners = int(data.get("winner_count", 1)) or 1

        # create giveaway snapshot in history (if not created)
        gid = gen_giveaway_id()
        data["latest_gid"] = gid

        # Exclude users without @username from pool
        pool = []
        for uid, info in parts.items():
            uname = (info or {}).get("username", "") or ""
            if uname and uname.startswith("@"):
                pool.append(uid)

        first_uid = data.get("first_winner_id")
        first_uname = data.get("first_winner_username", "")

        # snapshot
        snap = {
            "gid": gid,
            "created_ts": now_ts(),
            "title": title,
            "prize": prize,
            "winner_count": total_winners,
            "participants_total": len(parts),
            "eligible_total": len(pool),
            "winners": {},          # uid -> {"username":..., "first":bool, "lucky":bool}
            "delivered": {},        # uid -> bool
            "claim_start_ts": None,
            "claim_expires_ts": None,
            "winners_message_id": None,
            "selection_message_id": None,
            "completed": False,
            "lucky_won_by": None,   # uid
        }

        # add first join champ as winner if eligible and exists
        if first_uid and str(first_uid) in parts:
            fu = first_uname or (parts.get(str(first_uid), {}) or {}).get("username", "")
            if fu and fu.startswith("@"):
                snap["winners"][str(first_uid)] = {"username": fu, "first": True, "lucky": False}

        data["history"][gid] = snap
        save_data()

    # post selection message
    m = bot.send_message(
        chat_id=CHANNEL_ID,
        text=build_autodraw_text(
            gid=gid,
            title=title,
            prize=prize,
            winners_selected=len(data["history"][gid]["winners"]),
            total_winners=total_winners,
            percent=0,
            remaining=AUTO_DRAW_TOTAL_SECONDS,
            now_show=[("🟡", "@username", "0"), ("🟠", "@username", "0"), ("⚫", "@username", "0")],
            spin=SPIN[0],
        ),
        reply_markup=try_luck_markup(gid),
    )

    safe_pin(bot, CHANNEL_ID, m.message_id)

    with lock:
        data["history"][gid]["selection_message_id"] = m.message_id
        save_data()

    ctx = {
        "gid": gid,
        "start_ts": now_ts(),
        "tick": 0,
        "show_idx": 0,
        "rot_a": 0,
        "rot_b": 0,
        "rot_c": 0,
        "last_pick_a": None,
        "last_pick_b": None,
        "last_pick_c": None,
    }

    def _autodraw_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        gid2 = jd["gid"]

        with lock:
            snap2 = (data.get("history", {}) or {}).get(gid2)
            if not snap2:
                stop_autodraw()
                return

            title2 = snap2.get("title", "")
            prize2 = snap2.get("prize", "")
            total_winners2 = int(snap2.get("winner_count", 1)) or 1

            elapsed = int(now_ts() - float(jd["start_ts"]))
            remaining = max(0, AUTO_DRAW_TOTAL_SECONDS - elapsed)
            percent = int(round(min(100, (elapsed / float(AUTO_DRAW_TOTAL_SECONDS)) * 100)))

            # progress spinner (no dots)
            jd["tick"] += 1
            spin = SPIN[(jd["tick"] - 1) % len(SPIN)]

            # build eligible pool again from current participants (live joining allowed until giveaway closed)
            parts2 = data.get("participants", {}) or {}
            eligible = []
            for uid, info in parts2.items():
                uname = (info or {}).get("username", "") or ""
                if uname and uname.startswith("@"):
                    eligible.append(uid)

            # already winners
            winners_map = snap2.get("winners", {}) or {}
            winners_selected = len(winners_map)

            # while time running, keep selecting winners gradually (realistic)
            # choose chance based on percent and remaining slots
            if winners_selected < total_winners2 and eligible:
                # probability increases over time
                base_p = 0.03 + (percent / 100.0) * 0.12  # 3% to 15%
                if random.random() < base_p:
                    # choose new winner not already selected
                    available = [u for u in eligible if u not in winners_map]
                    if available:
                        uidw = random.choice(available)
                        unamew = (parts2.get(uidw, {}) or {}).get("username", "")
                        winners_map[uidw] = {"username": unamew, "first": False, "lucky": False}
                        snap2["winners"] = winners_map
                        data["history"][gid2] = snap2
                        save_data()

            # Showcase 3 rotating entries (5/7/9 seconds)
            jd["rot_a"] += 1
            jd["rot_b"] += 1
            jd["rot_c"] += 1

            # pick 3 distinct colors
            c1, c2, c3 = pick_three_colors()

            def pick_show(exclude_set):
                # show from eligible first; else show by user id list
                if eligible:
                    candidates = [u for u in eligible if u not in exclude_set]
                    if not candidates:
                        candidates = eligible[:]
                    uid_s = random.choice(candidates)
                    uname_s = (parts2.get(uid_s, {}) or {}).get("username", "") or "@username"
                    return uid_s, uname_s
                # fallback: any participants
                if parts2:
                    uid_s = random.choice(list(parts2.keys()))
                    uname_s = (parts2.get(uid_s, {}) or {}).get("username", "") or "User"
                    return uid_s, uname_s
                return "0", "@username"

            # change A every 5 seconds, B every 7 seconds, C every 9 seconds
            if jd["rot_a"] >= 5:
                jd["rot_a"] = 0
                jd["last_pick_a"] = None
            if jd["rot_b"] >= 7:
                jd["rot_b"] = 0
                jd["last_pick_b"] = None
            if jd["rot_c"] >= 9:
                jd["rot_c"] = 0
                jd["last_pick_c"] = None

            used = set()
            if jd["last_pick_a"] is None:
                ua, una = pick_show(used)
                jd["last_pick_a"] = (ua, una)
            used.add(jd["last_pick_a"][0])

            if jd["last_pick_b"] is None:
                ub, unb = pick_show(used)
                jd["last_pick_b"] = (ub, unb)
            used.add(jd["last_pick_b"][0])

            if jd["last_pick_c"] is None:
                uc, unc = pick_show(used)
                jd["last_pick_c"] = (uc, unc)

            show_list = [
                (c1, jd["last_pick_a"][1], jd["last_pick_a"][0]),
                (c2, jd["last_pick_b"][1], jd["last_pick_b"][0]),
                (c3, jd["last_pick_c"][1], jd["last_pick_c"][0]),
            ]

            # edit selection post
            mid = snap2.get("selection_message_id")
            if not mid:
                return

        try:
            job_ctx.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=mid,
                text=build_autodraw_text(
                    gid=gid2,
                    title=title2,
                    prize=prize2,
                    winners_selected=len((data["history"][gid2].get("winners", {}) or {})),
                    total_winners=total_winners2,
                    percent=percent,
                    remaining=remaining,
                    now_show=show_list,
                    spin=spin,
                ),
                reply_markup=try_luck_markup(gid2),
            )
        except Exception:
            pass

    autodraw_job = job_queue.run_repeating(
        _autodraw_tick,
        interval=AUTO_DRAW_TICK,
        first=0,
        context=ctx,
        name="autodraw_job",
    )

    autodraw_finalize_job = job_queue.run_once(
        autodraw_finalize,
        when=AUTO_DRAW_TOTAL_SECONDS,
        context=ctx,
        name="autodraw_finalize",
    )


def autodraw_finalize(context: CallbackContext):
    stop_autodraw()
    gid = (context.job.context or {}).get("gid")
    if not gid:
        return

    with lock:
        snap = (data.get("history", {}) or {}).get(gid)
        if not snap:
            return

        # delete closed message (if exists)
        closed_mid = data.get("closed_message_id")
        if closed_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception:
                pass
            data["closed_message_id"] = None

        # delete selection post (will be replaced by winners post)
        sel_mid = snap.get("selection_message_id")
        if sel_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=sel_mid)
            except Exception:
                pass

        # ensure winners filled up to total_winners (eligible only)
        parts = data.get("participants", {}) or {}
        eligible = [uid for uid, info in parts.items() if (info or {}).get("username", "").startswith("@")]
        winners_map = snap.get("winners", {}) or {}
        total_w = int(snap.get("winner_count", 1)) or 1

        available = [u for u in eligible if u not in winners_map]
        while len(winners_map) < total_w and available:
            u = random.choice(available)
            available.remove(u)
            winners_map[u] = {"username": (parts.get(u, {}) or {}).get("username", ""), "first": False, "lucky": False}

        snap["winners"] = winners_map

        # create winners post
        text = build_winners_post_text(
            gid=gid,
            title=snap.get("title", ""),
            prize=snap.get("prize", ""),
            winners_map=snap.get("winners", {}),
            delivered_map=snap.get("delivered", {}),
        )

        m = context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            reply_markup=winners_claim_markup(gid),
        )
        safe_pin(context.bot, CHANNEL_ID, m.message_id)

        # claim window
        ts = now_ts()
        snap["claim_start_ts"] = ts
        snap["claim_expires_ts"] = ts + 24 * 3600
        snap["winners_message_id"] = m.message_id
        snap["completed"] = False

        # save
        data["history"][gid] = snap
        save_data()

    schedule_claim_expire(context.job_queue, gid)


# =========================================================
# MANUAL DRAW (Admin side progress + approve/reject)
# =========================================================
MANUAL_DRAW_SECONDS = 40
MANUAL_DRAW_TICK = 1

def stop_manual_draw():
    global manual_draw_job, manual_draw_finalize_job
    if manual_draw_job is not None:
        try:
            manual_draw_job.schedule_removal()
        except Exception:
            pass
    manual_draw_job = None
    if manual_draw_finalize_job is not None:
        try:
            manual_draw_finalize_job.schedule_removal()
        except Exception:
            pass
    manual_draw_finalize_job = None


def build_manual_draw_progress(percent: int, spin: str) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{spin} Selecting winners: {percent}%\n"
        f"📊 Progress: {build_progress(percent)}\n\n"
        "🔐 100% fair & random system\n"
        "🆔 User ID based selection only"
    )


def start_manual_draw(context: CallbackContext, admin_chat_id: int):
    stop_manual_draw()

    msg = context.bot.send_message(
        chat_id=admin_chat_id,
        text=build_manual_draw_progress(0, SPIN[0]),
    )

    ctx = {
        "admin_chat_id": admin_chat_id,
        "admin_msg_id": msg.message_id,
        "start_ts": now_ts(),
        "tick": 0,
    }

    def _tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] += 1
        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(MANUAL_DRAW_SECONDS)) * 100)))
        spin = SPIN[(jd["tick"] - 1) % len(SPIN)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["admin_msg_id"],
                text=build_manual_draw_progress(percent, spin),
            )
        except Exception:
            pass

    global manual_draw_job, manual_draw_finalize_job
    manual_draw_job = context.job_queue.run_repeating(
        _tick,
        interval=MANUAL_DRAW_TICK,
        first=0,
        context=ctx,
        name="manual_draw_job",
    )
    manual_draw_finalize_job = context.job_queue.run_once(
        manual_draw_finalize,
        when=MANUAL_DRAW_SECONDS,
        context=ctx,
        name="manual_draw_finalize",
    )


def manual_draw_finalize(context: CallbackContext):
    stop_manual_draw()
    jd = context.job.context or {}
    admin_chat_id = jd.get("admin_chat_id")
    admin_msg_id = jd.get("admin_msg_id")
    if not admin_chat_id or not admin_msg_id:
        return

    with lock:
        parts = data.get("participants", {}) or {}
        if not parts:
            try:
                context.bot.edit_message_text(chat_id=admin_chat_id, message_id=admin_msg_id, text="No participants.")
            except Exception:
                pass
            return

        # create giveaway id + snapshot
        gid = gen_giveaway_id()
        data["latest_gid"] = gid

        title = data.get("title", "")
        prize = data.get("prize", "")
        total_w = int(data.get("winner_count", 1)) or 1

        # eligible = username only (rule)
        eligible = [uid for uid, info in parts.items() if (info or {}).get("username", "").startswith("@")]
        if not eligible:
            try:
                context.bot.edit_message_text(
                    chat_id=admin_chat_id,
                    message_id=admin_msg_id,
                    text="No eligible users (valid @username) found.",
                )
            except Exception:
                pass
            return

        winners_map = {}
        # first join champ (only if eligible)
        first_uid = data.get("first_winner_id")
        if first_uid and str(first_uid) in parts:
            fu = (parts.get(str(first_uid), {}) or {}).get("username", "")
            if fu and fu.startswith("@"):
                winners_map[str(first_uid)] = {"username": fu, "first": True, "lucky": False}

        available = [u for u in eligible if u not in winners_map]
        need = max(0, total_w - len(winners_map))
        if need > 0:
            picks = random.sample(available, min(need, len(available)))
            for u in picks:
                winners_map[u] = {"username": (parts.get(u, {}) or {}).get("username", ""), "first": False, "lucky": False}

        snap = {
            "gid": gid,
            "created_ts": now_ts(),
            "title": title,
            "prize": prize,
            "winner_count": total_w,
            "participants_total": len(parts),
            "eligible_total": len(eligible),
            "winners": winners_map,
            "delivered": {},
            "claim_start_ts": None,
            "claim_expires_ts": None,
            "winners_message_id": None,
            "selection_message_id": None,
            "completed": False,
            "lucky_won_by": None,
        }
        data["history"][gid] = snap
        save_data()

        pending_text = build_winners_post_text(gid, title, prize, winners_map, {})
        data["_pending_manual_gid"] = gid
        data["_pending_manual_text"] = pending_text
        save_data()

    try:
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            text=pending_text,
            reply_markup=winners_approve_markup(),
        )
    except Exception:
        context.bot.send_message(chat_id=admin_chat_id, text=pending_text, reply_markup=winners_approve_markup())


# =========================================================
# CLAIM EXPIRY (per giveaway id)
# =========================================================
def stop_claim_job(gid: str):
    j = claim_expire_jobs.get(gid)
    if j:
        try:
            j.schedule_removal()
        except Exception:
            pass
    claim_expire_jobs.pop(gid, None)


def schedule_claim_expire(job_queue, gid: str):
    stop_claim_job(gid)
    with lock:
        snap = (data.get("history", {}) or {}).get(gid)
        if not snap:
            return
        exp = snap.get("claim_expires_ts")
        mid = snap.get("winners_message_id")
    if not exp or not mid:
        return
    remain = float(exp) - now_ts()
    if remain <= 0:
        return
    claim_expire_jobs[gid] = job_queue.run_once(
        expire_claim_button_job,
        when=remain,
        context={"gid": gid},
        name=f"claim_expire_{gid}",
    )


def expire_claim_button_job(context: CallbackContext):
    gid = (context.job.context or {}).get("gid")
    if not gid:
        return
    with lock:
        snap = (data.get("history", {}) or {}).get(gid)
        if not snap:
            return
        mid = snap.get("winners_message_id")
        exp = snap.get("claim_expires_ts")
    if not mid or not exp:
        return
    try:
        # remove button
        context.bot.edit_message_reply_markup(chat_id=CHANNEL_ID, message_id=mid, reply_markup=None)
    except Exception:
        pass
    # mark completed (after expiry)
    with lock:
        snap = (data.get("history", {}) or {}).get(gid) or {}
        snap["completed"] = True
        data["history"][gid] = snap
        save_data()


# =========================================================
# COMMANDS
# =========================================================
def cmd_start(update: Update, context: CallbackContext):
    u = update.effective_user
    if not u:
        return

    if u.id == ADMIN_ID:
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
        return

    # unauthorized message + notify admin privately
    uname = user_tag(u.username or "")
    uid = str(u.id)
    txt = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ UNAUTHORIZED NOTICE\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Hi there!\n"
        f"Username: {uname or 'N/A'}\n"
        f"User ID: {uid}\n\n"
        "It looks like you tried to start the giveaway,\n"
        "but this action is available for admins only.\n\n"
        "😊 No worries — this is just a friendly heads-up.\n\n"
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
        "We’re always happy to help!\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    update.message.reply_text(txt)

    try:
        context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ Unauthorized /start attempt\nUsername: {uname or 'N/A'}\nUser ID: {uid}",
        )
    except Exception:
        pass


def cmd_panel(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "🛠 ADMIN CONTROL PANEL\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/endgiveaway\n"
        "/draw\n\n"
        "⚙️ AUTO DRAW\n"
        "/autodraw\n\n"
        "📦 PRIZE DELIVERY\n"
        "/prizedelivered\n\n"
        "📜 WINNER HISTORY\n"
        "/winnerlist\n\n"
        "🔒 BLOCK SYSTEM\n"
        "/blockpermanent\n"
        "/unban\n"
        "/blocklist\n"
        "/removeban\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "♻️ RESET\n"
        "/reset"
    )


def cmd_autodraw(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text("⚙️ Auto Draw Settings:", reply_markup=autodraw_markup())


def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        "✅ ADD VERIFY (CHAT ID / @USERNAME)\n\n"
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
    lines = ["🗑 REMOVE VERIFY TARGET", ""]
    for i, t in enumerate(targets, start=1):
        lines.append(f"{i}) {t.get('display','')}")
    lines += ["", "Send a number to remove. (11 = Remove ALL)"]
    admin_state = "remove_verify_pick"
    update.message.reply_text("\n".join(lines))


def cmd_newgiveaway(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return

    stop_live_countdown()
    stop_manual_draw()
    stop_autodraw()

    with lock:
        keep_perma = data.get("permanent_block", {}) or {}
        keep_verify = data.get("verify_targets", []) or []
        keep_history = data.get("history", {}) or {}

        data.clear()
        data.update(fresh_default_data())
        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        data["history"] = keep_history
        save_data()

    admin_state = "title"
    update.message.reply_text("STEP 1 — Send Giveaway Title:")


def cmd_participants(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    parts = data.get("participants", {}) or {}
    if not parts:
        update.message.reply_text("👥 Participants list is empty.")
        return
    lines = [f"👥 PARTICIPANTS ({len(parts)})", ""]
    i = 1
    for uid, info in parts.items():
        uname = (info or {}).get("username", "")
        if uname:
            lines.append(f"{i}. {uname} | {uid}")
        else:
            lines.append(f"{i}. {uid}")
        i += 1
    update.message.reply_text("\n".join(lines))


def cmd_endgiveaway(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not data.get("active"):
        update.message.reply_text("No active giveaway is running.")
        return
    update.message.reply_text(
        "⚠️ END GIVEAWAY CONFIRMATION\n\n"
        "Are you sure you want to end this giveaway now?",
        reply_markup=end_confirm_markup()
    )


def cmd_draw(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not data.get("closed"):
        update.message.reply_text("Giveaway is not closed yet.")
        return
    if not (data.get("participants", {}) or {}):
        update.message.reply_text("No participants to draw from.")
        return
    start_manual_draw(context, update.effective_chat.id)


def cmd_blockpermanent(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "perma_block_list"
    update.message.reply_text(
        "🔒 PERMANENT BLOCK\n\n"
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


def cmd_removeban(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Reset Permanent Ban List", callback_data="reset_permanent_ban"),
            InlineKeyboardButton("Reset Old Winner Ban List", callback_data="reset_oldwinner_ban"),
        ]]
    )
    update.message.reply_text("Choose which ban list to reset:", reply_markup=kb)


def cmd_blocklist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    perma = data.get("permanent_block", {}) or {}
    oldw = data.get("old_winners", {}) or {}
    lines = []
    lines.append("📌 BAN LISTS")
    lines.append("")
    lines.append(f"OLD WINNER MODE: {data.get('old_winner_mode','skip').upper()}")
    lines.append("")
    lines.append(f"⛔ OLD WINNER BLOCK: {len(oldw)}")
    for uid, info in oldw.items():
        u = (info or {}).get("username", "")
        lines.append(f"- {u+' | ' if u else ''}{uid}")
    lines.append("")
    lines.append(f"🔒 PERMANENT BLOCK: {len(perma)}")
    for uid, info in perma.items():
        u = (info or {}).get("username", "")
        lines.append(f"- {u+' | ' if u else ''}{uid}")
    update.message.reply_text("\n".join(lines))


def cmd_prizedelivered(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "prize_delivered_gid"
    update.message.reply_text(
        "📦 PRIZE DELIVERY UPDATE\n\n"
        "Step 1/2 — Send Giveaway ID (example: P857-P583-B6714)\n"
        "OR send: latest\n\n"
        "After that, I will ask for delivered users list."
    )


def cmd_winnerlist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    hist = data.get("history", {}) or {}
    if not hist:
        update.message.reply_text("No winner history yet.")
        return

    # newest first
    items = sorted(hist.items(), key=lambda kv: float((kv[1] or {}).get("created_ts", 0)), reverse=True)
    lines = ["📜 WINNER LIST (HISTORY)", ""]
    for gid, snap in items[:30]:
        dt = datetime.utcfromtimestamp(float(snap.get("created_ts", 0) or 0)).strftime("%d-%m-%Y")
        prize = snap.get("prize", "")
        winners = snap.get("winners", {}) or {}
        lines.append(f"🆔 {gid} | {dt}")
        lines.append(f"🎁 {prize}")
        for uid, info in winners.items():
            uname = (info or {}).get("username", "") or "User"
            lines.append(f"• {uname} | {uid}")
        lines.append("")
    update.message.reply_text("\n".join(lines))


def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
        ]]
    )
    update.message.reply_text("Confirm reset current giveaway state?", reply_markup=kb)


# =========================================================
# ADMIN TEXT FLOW
# =========================================================
def admin_text_handler(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    if not admin_state:
        return

    msg = (update.message.text or "").strip()
    if not msg:
        return

    # verify add
    if admin_state == "add_verify":
        ref = normalize_verify_ref(msg)
        if not ref:
            update.message.reply_text("Invalid input. Send -100... or @username.")
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

    if admin_state == "remove_verify_pick":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number.")
            return
        n = int(msg)
        with lock:
            targets = data.get("verify_targets", []) or []
            if n == 11:
                data["verify_targets"] = []
                save_data()
                admin_state = None
                update.message.reply_text("✅ All verify targets removed.")
                return
            if n < 1 or n > len(targets):
                update.message.reply_text("Invalid number.")
                return
            rem = targets.pop(n - 1)
            data["verify_targets"] = targets
            save_data()
        admin_state = None
        update.message.reply_text(f"✅ Removed: {rem.get('display','')}")
        return

    # new giveaway flow
    if admin_state == "title":
        with lock:
            data["title"] = msg
            save_data()
        admin_state = "prize"
        update.message.reply_text("STEP 2 — Send Prize (multi-line allowed):")
        return

    if admin_state == "prize":
        with lock:
            data["prize"] = msg
            save_data()
        admin_state = "winners"
        update.message.reply_text("STEP 3 — Send Total Winner Count (number):")
        return

    if admin_state == "winners":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number.")
            return
        with lock:
            data["winner_count"] = max(1, min(1000000, int(msg)))
            save_data()
        admin_state = "duration"
        update.message.reply_text("STEP 4 — Send Duration (example: 30 Second / 5 Minute / 1 Hour):")
        return

    if admin_state == "duration":
        seconds = parse_duration(msg)
        if seconds <= 0:
            update.message.reply_text("Invalid duration. Example: 30 Second / 5 Minute / 1 Hour")
            return
        with lock:
            data["duration_seconds"] = seconds
            save_data()
        admin_state = "old_winner_mode"
        update.message.reply_text(
            "OLD WINNER PROTECTION MODE\n\n"
            "1 → BLOCK OLD WINNERS\n"
            "2 → SKIP OLD WINNERS\n\n"
            "Reply with 1 or 2:"
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
            update.message.reply_text("✅ Old Winner Mode: SKIP\nNow send Giveaway Rules (multi-line):")
            return

        with lock:
            data["old_winner_mode"] = "block"
            data["old_winners"] = {}
            save_data()
        admin_state = "old_winner_block_list"
        update.message.reply_text(
            "⛔ OLD WINNER BLOCK LIST\n\n"
            "Send list (one per line):\n"
            "@username | user_id OR user_id"
        )
        return

    if admin_state == "old_winner_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            ow = data.get("old_winners", {}) or {}
            for uid, uname in entries:
                ow[str(uid)] = {"username": uname}
            data["old_winners"] = ow
            save_data()
        admin_state = "rules"
        update.message.reply_text("✅ Old Winner Block List saved.\nNow send Giveaway Rules (multi-line):")
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        update.message.reply_text("✅ Rules saved!\nShowing preview…")
        update.message.reply_text(build_preview_text(), reply_markup=preview_markup())
        return

    # permanent block list
    if admin_state == "perma_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            perma = data.get("permanent_block", {}) or {}
            for uid, uname in entries:
                perma[str(uid)] = {"username": uname}
            data["permanent_block"] = perma
            save_data()
        admin_state = None
        update.message.reply_text(f"✅ Permanent block saved. Total blocked: {len(data.get('permanent_block',{}))}")
        return

    # prize delivery step 1
    if admin_state == "prize_delivered_gid":
        gid = msg.strip()
        with lock:
            hist = data.get("history", {}) or {}
            latest = data.get("latest_gid")
        if gid.lower() == "latest":
            gid = latest or ""
        if not gid or gid not in hist:
            update.message.reply_text("❌ Giveaway ID not found. Send valid ID or: latest")
            return
        with lock:
            data["_prize_target_gid"] = gid
            save_data()
        admin_state = "prize_delivered_list"
        update.message.reply_text(
            f"✅ Giveaway selected: {gid}\n\n"
            "Step 2/2 — Send delivered users list (one per line):\n"
            "@username | user_id\n\n"
            "Example:\n"
            "@MinexxProo | 5692210187"
        )
        return

    # prize delivery step 2
    if admin_state == "prize_delivered_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: @username | user_id")
            return

        with lock:
            gid = data.get("_prize_target_gid") or data.get("latest_gid")
            hist = data.get("history", {}) or {}
            if not gid or gid not in hist:
                update.message.reply_text("No giveaway found to update.")
                admin_state = None
                return

            snap = hist[gid]
            winners_map = snap.get("winners", {}) or {}
            delivered = snap.get("delivered", {}) or {}

            changed = 0
            for uid2, _uname2 in entries:
                uid2 = str(uid2)
                if uid2 in winners_map:
                    if not delivered.get(uid2):
                        delivered[uid2] = True
                        changed += 1

            snap["delivered"] = delivered
            hist[gid] = snap
            data["history"] = hist
            data.pop("_prize_target_gid", None)
            save_data()

        # update channel post
        mid = snap.get("winners_message_id")
        if mid:
            try:
                text = build_winners_post_text(
                    gid=gid,
                    title=snap.get("title", ""),
                    prize=snap.get("prize", ""),
                    winners_map=snap.get("winners", {}),
                    delivered_map=snap.get("delivered", {}),
                )
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=mid,
                    text=text,
                    reply_markup=winners_claim_markup(gid),
                )
            except Exception as e:
                try:
                    context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Could not edit winners post.\nReason: {e}")
                except Exception:
                    pass

        admin_state = None
        update.message.reply_text(
            "✅ Prize delivery updated successfully.\n"
            f"Giveaway ID: {gid}\n"
            f"Updated: {changed} winner(s)"
        )
        return


# =========================================================
# CALLBACK HANDLER
# =========================================================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

    # verify buttons
    if qd == "verify_add_more":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        admin_state = "add_verify"
        try:
            query.answer()
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
        admin_state = None
        try:
            query.answer()
            query.edit_message_text(
                f"✅ Verify setup completed.\nTotal targets: {len(data.get('verify_targets',[]) or [])}"
            )
        except Exception:
            pass
        return

    # autodraw toggle
    if qd == "autodraw_on":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        with lock:
            data["autodraw_enabled"] = True
            save_data()
        try:
            query.answer()
            query.edit_message_text("✅ Auto Draw ON")
        except Exception:
            pass
        return

    if qd == "autodraw_off":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        with lock:
            data["autodraw_enabled"] = False
            save_data()
        try:
            query.answer()
            query.edit_message_text("⛔ Auto Draw OFF")
        except Exception:
            pass
        return

    # preview actions
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
                duration = int(data.get("duration_seconds", 0)) or 1
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

                    data["participants"] = {}
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""

                    save_data()

                start_live_countdown(context.job_queue)
                try:
                    query.edit_message_text("✅ Giveaway approved and posted to channel!")
                except Exception:
                    pass
            except Exception as e:
                try:
                    query.edit_message_text(f"Failed to post in channel.\nError: {e}")
                except Exception:
                    pass
            return

        if qd == "preview_reject":
            try:
                query.answer()
                query.edit_message_text("❌ Giveaway rejected.")
            except Exception:
                pass
            return

        if qd == "preview_edit":
            try:
                query.answer()
                query.edit_message_text("✏️ Edit Mode: Start again with /newgiveaway")
            except Exception:
                pass
            return

    # end giveaway
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
                    query.edit_message_text("No active giveaway is running.")
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
                    total_w=data.get("winner_count", 0),
                ),
            )
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
        except Exception:
            pass

        stop_live_countdown()

        # if autodraw enabled -> start
        if data.get("autodraw_enabled"):
            try:
                start_autodraw_channel_progress(context.job_queue, context.bot)
            except Exception as e:
                try:
                    context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Auto Draw failed to start.\nReason: {e}")
                except Exception:
                    pass

        try:
            query.edit_message_text("✅ Giveaway closed.")
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
            query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        except Exception:
            pass
        return

    # reset
    if qd == "reset_confirm":
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

        stop_live_countdown()
        stop_manual_draw()
        stop_autodraw()

        with lock:
            keep_perma = data.get("permanent_block", {}) or {}
            keep_verify = data.get("verify_targets", []) or []
            keep_history = data.get("history", {}) or {}

            data.clear()
            data.update(fresh_default_data())
            data["permanent_block"] = keep_perma
            data["verify_targets"] = keep_verify
            data["history"] = keep_history
            save_data()

        try:
            query.edit_message_text("✅ Reset completed. Start with /newgiveaway")
        except Exception:
            pass
        return

    if qd == "reset_cancel":
        try:
            query.answer()
            query.edit_message_text("❌ Reset cancelled.")
        except Exception:
            pass
        return

    # unban choose
    if qd == "unban_permanent":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        admin_state = "unban_permanent_input"
        try:
            query.answer()
            query.edit_message_text("Send User ID to unban from Permanent Block:")
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
        admin_state = "unban_oldwinner_input"
        try:
            query.answer()
            query.edit_message_text("Send User ID to unban from Old Winner Block:")
        except Exception:
            pass
        return

    if qd == "reset_permanent_ban":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        with lock:
            data["permanent_block"] = {}
            save_data()
        try:
            query.answer()
            query.edit_message_text("✅ Permanent Ban List has been reset.")
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
        with lock:
            data["old_winners"] = {}
            save_data()
        try:
            query.answer()
            query.edit_message_text("✅ Old Winner Ban List has been reset.")
        except Exception:
            pass
        return

    # winners approve/reject (manual draw)
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

        with lock:
            gid = data.get("_pending_manual_gid")
            text = data.get("_pending_manual_text") or ""
            if not gid or not text:
                try:
                    query.edit_message_text("No pending winners found.")
                except Exception:
                    pass
                return
            snap = (data.get("history", {}) or {}).get(gid) or {}

        # remove closed message
        closed_mid = data.get("closed_message_id")
        if closed_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception:
                pass
            with lock:
                data["closed_message_id"] = None
                save_data()

        try:
            m = context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                reply_markup=winners_claim_markup(gid),
            )
            safe_pin(context.bot, CHANNEL_ID, m.message_id)

            with lock:
                ts = now_ts()
                snap["claim_start_ts"] = ts
                snap["claim_expires_ts"] = ts + 24 * 3600
                snap["winners_message_id"] = m.message_id
                snap["completed"] = False
                data["history"][gid] = snap

                data.pop("_pending_manual_gid", None)
                data.pop("_pending_manual_text", None)
                save_data()

            schedule_claim_expire(context.job_queue, gid)
            try:
                query.edit_message_text("✅ Approved! Winners list posted to channel.")
            except Exception:
                pass
        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post winners: {e}")
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
            data.pop("_pending_manual_gid", None)
            data.pop("_pending_manual_text", None)
            save_data()
        try:
            query.edit_message_text("❌ Rejected. Winners will not be posted.")
        except Exception:
            pass
        return

    # join giveaway
    if qd == "join_giveaway":
        if not data.get("active"):
            try:
                query.answer("This giveaway is not active right now.", show_alert=True)
            except Exception:
                pass
            return

        if not verify_user_join(context.bot, int(uid)):
            try:
                query.answer(popup_verify_required(), show_alert=True)
            except Exception:
                pass
            return

        if uid in (data.get("permanent_block", {}) or {}):
            try:
                query.answer(popup_permanent_blocked(), show_alert=True)
            except Exception:
                pass
            return

        if data.get("old_winner_mode") == "block":
            if uid in (data.get("old_winners", {}) or {}):
                try:
                    query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception:
                    pass
                return

        if uid in (data.get("participants", {}) or {}):
            # if first join champ, show champ popup
            if data.get("first_winner_id") and uid == str(data.get("first_winner_id")):
                tg_user = query.from_user
                uname = user_tag(tg_user.username or "") or data.get("first_winner_username", "") or "@username"
                try:
                    query.answer(popup_first_winner(uname, uid), show_alert=True)
                except Exception:
                    pass
                return
            try:
                query.answer(popup_already_joined(), show_alert=True)
            except Exception:
                pass
            return

        tg_user = query.from_user
        uname = user_tag(tg_user.username or "")
        full_name = (tg_user.full_name or "").strip()

        with lock:
            # set first winner id
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
                duration = data.get("duration_seconds", 1) or 1
                elapsed = int((datetime.utcnow() - start).total_seconds())
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

        # popup success/first champ
        with lock:
            if data.get("first_winner_id") == uid:
                try:
                    query.answer(popup_first_winner(uname or "@username", uid), show_alert=True)
                except Exception:
                    pass
            else:
                try:
                    query.answer(popup_join_success(uname or "@username", uid), show_alert=True)
                except Exception:
                    pass
        return

    # Entry Rule button
    if qd == "entry_rule":
        # remaining time derived from latest selection if exists
        rem = "N/A"
        try:
            # show based on any active selection (latest)
            gid = data.get("latest_gid")
            if gid:
                snap = (data.get("history", {}) or {}).get(gid) or {}
                # not exact, but show generic
                rem = "Try during selection time"
        except Exception:
            pass
        try:
            query.answer(entry_rule_text(rem), show_alert=True)
        except Exception:
            pass
        return

    # Lucky Draw click: callback_data = "luck:<gid>"
    if qd.startswith("luck:"):
        gid = qd.split(":", 1)[1].strip()

        with lock:
            snap = (data.get("history", {}) or {}).get(gid) or {}
            if not snap:
                try:
                    query.answer(popup_giveaway_completed(), show_alert=True)
                except Exception:
                    pass
                return

            parts = data.get("participants", {}) or {}
            if not parts:
                try:
                    query.answer(popup_no_entries_yet(), show_alert=True)
                except Exception:
                    pass
                return

        # username required
        tg_user = query.from_user
        uname = user_tag(tg_user.username or "")
        if not uname:
            try:
                query.answer(popup_username_required(), show_alert=True)
            except Exception:
                pass
            return

        # Lucky window: only between elapsed second 48 and 49 from selection start
        # if no selection running, treat as closed
        with lock:
            start_sel = snap.get("created_ts", 0) or 0
        elapsed = int(now_ts() - float(start_sel))

        # window open only 48..48 (1 second)
        if elapsed != 48:
            # if already won, show closed with winner
            with lock:
                won_by = snap.get("lucky_won_by")
                winner_text = ""
                if won_by:
                    wuname = (snap.get("winners", {}) or {}).get(won_by, {}).get("username", "") or "Winner"
                    winner_text = f"👤 {wuname}\n🆔 {won_by}"
            try:
                query.answer(popup_lucky_closed(winner_text), show_alert=True)
            except Exception:
                pass
            return

        uid_str = str(tg_user.id)

        with lock:
            snap = (data.get("history", {}) or {}).get(gid) or {}
            if snap.get("lucky_won_by"):
                won_by = snap.get("lucky_won_by")
                wuname = (snap.get("winners", {}) or {}).get(won_by, {}).get("username", "") or "Winner"
                winner_text = f"👤 {wuname}\n🆔 {won_by}"
                try:
                    query.answer(popup_lucky_closed(winner_text), show_alert=True)
                except Exception:
                    pass
                return

            # must be participant and eligible
            info = (data.get("participants", {}) or {}).get(uid_str)
            if not info or not (info.get("username", "").startswith("@")):
                try:
                    query.answer("🚫 You must join the giveaway (with valid @username) first.", show_alert=True)
                except Exception:
                    pass
                return

            # add to winners live
            winners_map = snap.get("winners", {}) or {}
            if uid_str not in winners_map:
                winners_map[uid_str] = {"username": uname, "first": False, "lucky": True}
            snap["winners"] = winners_map
            snap["lucky_won_by"] = uid_str
            data["history"][gid] = snap
            save_data()

        # popup win
        try:
            query.answer(popup_lucky_win(uname, uid_str), show_alert=True)
        except Exception:
            pass
        return

    # claim: callback_data = claim:<gid>
    if qd.startswith("claim:"):
        gid = qd.split(":", 1)[1].strip()

        with lock:
            snap = (data.get("history", {}) or {}).get(gid) or {}
            if not snap:
                try:
                    query.answer(popup_giveaway_completed(), show_alert=True)
                except Exception:
                    pass
                return

            winners_map = snap.get("winners", {}) or {}
            delivered_map = snap.get("delivered", {}) or {}
            exp = snap.get("claim_expires_ts")
            completed = bool(snap.get("completed"))

        # if completed -> same popup for everyone
        if completed:
            try:
                query.answer(popup_giveaway_completed(), show_alert=True)
            except Exception:
                pass
            return

        # expired?
        if exp and now_ts() > float(exp):
            # mark completed
            with lock:
                snap = (data.get("history", {}) or {}).get(gid) or {}
                snap["completed"] = True
                data["history"][gid] = snap
                save_data()
            try:
                query.answer(popup_giveaway_completed(), show_alert=True)
            except Exception:
                pass
            return

        # not winner
        if uid not in winners_map:
            try:
                query.answer(popup_claim_not_winner(), show_alert=True)
            except Exception:
                pass
            return

        # delivered already
        if delivered_map.get(uid):
            uname = (winners_map.get(uid, {}) or {}).get("username", "") or "@username"
            try:
                query.answer(popup_already_delivered(uname, uid), show_alert=True)
            except Exception:
                pass
            return

        # winner claim ok
        uname = (winners_map.get(uid, {}) or {}).get("username", "") or "@username"
        try:
            query.answer(popup_claim_winner(uname, uid), show_alert=True)
        except Exception:
            pass
        return

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

    # commands
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("panel", cmd_panel))

    dp.add_handler(CommandHandler("autodraw", cmd_autodraw))

    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))

    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    dp.add_handler(CommandHandler("prizedelivered", cmd_prizedelivered))
    dp.add_handler(CommandHandler("winnerlist", cmd_winnerlist))

    dp.add_handler(CommandHandler("reset", cmd_reset))

    # handlers
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # resume systems after restart
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    # reschedule claim expiries for history (best effort)
    hist = data.get("history", {}) or {}
    for gid, snap in hist.items():
        try:
            exp = snap.get("claim_expires_ts")
            mid = snap.get("winners_message_id")
            completed = bool(snap.get("completed"))
            if exp and mid and not completed:
                remain = float(exp) - now_ts()
                if remain > 0:
                    schedule_claim_expire(updater.job_queue, gid)
                else:
                    snap["completed"] = True
                    data["history"][gid] = snap
                    save_data()
        except Exception:
            pass

    print("Bot is running (PTB v13, non-async) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
