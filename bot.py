import os
import json
import random
import threading
from datetime import datetime, timedelta

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

# jobs
countdown_job = None
closed_spinner_job = None

draw_job = None
draw_finalize_job = None

auto_select_job = None
auto_finalize_job = None
auto_delete_closed_job = None

claim_expire_job = None

reset_job = None
reset_finalize_job = None

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
        "selection_message_id": None,
        "winners_message_id": None,

        # participants
        "participants": {},  # uid(str) -> {"username": "@x" or "", "name": ""}

        # verify targets
        "verify_targets": [],  # [{"ref": "-100..." or "@xxx", "display": "..."}]

        # permanent block
        "permanent_block": {},  # uid -> {"username": "@x" or ""}

        # old winner protection
        "old_winner_mode": "skip",  # "skip" or "block"
        "old_winners": {},          # uid -> {"username": "@x"} (only if mode=block)

        # first join
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # winners final
        "winners": {},  # uid -> {"username": "@x"}
        "pending_winners_text": "",

        # claim window
        "claim_start_ts": None,
        "claim_expires_ts": None,

        # auto winner post
        "auto_winner_post": False,

        # winner history for /winnerlist
        "winner_history": [],  # list of records
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


def participants_count() -> int:
    return len(data.get("participants", {}))


def now_ts() -> float:
    return datetime.utcnow().timestamp()


def format_hms_spaced(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d} : {m:02d} : {s:02d}"


def build_progress(percent: int) -> str:
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


def format_rules() -> str:
    rules = (data.get("rules") or "").strip()
    if not rules:
        return (
            "✅ Must join official channel\n"
            "❌ One account per user\n"
            "🚫 No fake / duplicate accounts\n"
            "📌 Stay until winners announced"
        )
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    # keep user provided bullets clean
    out = []
    for l in lines:
        if l.startswith(("✅", "❌", "🚫", "📌", "•")):
            out.append(l)
        else:
            out.append("• " + l)
    return "\n".join(out)


def extract_single_prize(prize_text: str) -> str:
    """
    User says: '10× ChatGPT PREMIUM' means pool, but each winner gets one.
    We'll show a clean single prize name in claim popup:
    - takes first non-empty line
    - removes leading like '10x', '10×', '10 X'
    """
    lines = [x.strip() for x in (prize_text or "").splitlines() if x.strip()]
    if not lines:
        return "PRIZE"
    first = lines[0]
    # remove leading count patterns
    # examples: "10× ChatGPT PREMIUM", "10x ChatGPT PREMIUM"
    for token in ["×", "x", "X"]:
        # split by token if starts with digit
        if token in first:
            left, right = first.split(token, 1)
            if left.strip().replace(" ", "").isdigit():
                first = right.strip()
                break
    # also remove "10 *" style
    parts = first.split()
    if parts and parts[0].isdigit():
        first = " ".join(parts[1:]).strip() or first
    return first or "PRIZE"


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


def end_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm End", callback_data="end_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="end_cancel"),
        ]]
    )


def autowinner_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ ON", callback_data="autowin_on"),
            InlineKeyboardButton("❌ OFF", callback_data="autowin_off"),
        ]]
    )


def reset_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ Reject", callback_data="reset_cancel"),
        ]]
    )

# =========================================================
# VERIFY HELPERS
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
# POPUP TEXTS
# =========================================================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n"
        "Please join all required channels/groups first,\n"
        "then tap JOIN GIVEAWAY again ✅"
    )


def popup_old_winner_blocked() -> str:
    return (
        "🚫 YOU HAVE ALREADY WON BEFORE\n\n"
        "To keep the giveaway fair,\n"
        "repeat winners are restricted.\n\n"
        "Please wait for the next giveaway."
    )


def popup_first_winner(username: str, uid: str) -> str:
    return (
        "✨ CONGRATULATIONS 🌟\n"
        "You joined the giveaway FIRST\n"
        "and secured the 🥇 1st Winner spot!\n\n"
        f"👤 {username} | 🆔 {uid}\n\n"
        "📸 Screenshot & post in the group to confirm."
    )


def popup_already_joined() -> str:
    return (
        "🚫 ENTRY UNSUCCESSFUL\n\n"
        "You’ve already joined this giveaway.\n"
        "Multiple entries are not allowed.\n\n"
        "Please wait for the final result ⏳"
    )


def popup_join_success(username: str, uid: str) -> str:
    return (
        "🌹 CONGRATULATIONS!\n\n"
        "You’re successfully joined ✅\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        f"— {HOST_NAME}"
    )


def popup_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n\n"
        "You are permanently blocked from joining giveaways.\n\n"
        "If you believe this is a mistake, contact admin:\n"
        f"{ADMIN_CONTACT}"
    )


def popup_claim_not_winner() -> str:
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry! Your User ID is not in the winners list.\n"
        "Please wait for the next giveaway ❤️‍🩹"
    )


def popup_prize_expired() -> str:
    return (
        "⏳ PRIZE EXPIRED\n\n"
        "Your 24-hour claim time has ended.\n"
        "This prize is no longer available."
    )


def popup_claim_winner(username: str, uid: str) -> str:
    prize_single = extract_single_prize(data.get("prize", ""))
    return (
        "🌟 CONGRATULATIONS! ✨\n\n"
        "You’re an official winner of this giveaway 🏆\n\n"
        f"👤 {username} | 🆔 {uid}\n\n"
        "🎁 PRIZE WON\n"
        f"🏆 {prize_single}\n\n"
        "📩 Claim your prize — contact admin:\n"
        f"{ADMIN_CONTACT}"
    )

# =========================================================
# TEXT BUILDERS (TELEGRAM STYLE)
# =========================================================
def build_preview_text() -> str:
    remaining = int(data.get("duration_seconds", 0) or 0)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 GIVEAWAY PREVIEW (ADMIN ONLY)\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡️🔥 {data.get('title','')} 🔥⚡️\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{(data.get('prize','') or '').strip()}\n\n"
        f"👥 TOTAL PARTICIPANTS: 0\n"
        f"🏅 TOTAL WINNERS: {data.get('winner_count',0)}\n"
        "🎯 WINNER SELECTION: 100% Random & Fair\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms_spaced(remaining)}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{build_progress(0)} 0%\n\n"
        "📜 RULES\n"
        f"{format_rules()}\n\n"
        "📢 HOSTED BY\n"
        f"⚡️ {HOST_NAME} ⚡️\n\n"
        "👇✨ TAP THE BUTTON BELOW & JOIN NOW 👇"
    )


def build_live_text(remaining: int) -> str:
    duration = int(data.get("duration_seconds", 1) or 1)
    elapsed = max(0, min(duration, duration - remaining))
    percent = int(round((elapsed / float(duration)) * 100)) if duration else 0
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡️🔥 POWER POINT BREAK GIVEAWAY 🔥⚡️\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{(data.get('prize','') or '').strip()}\n\n"
        f"👥 TOTAL PARTICIPANTS: {participants_count()}\n"
        f"🏅 TOTAL WINNERS: {data.get('winner_count',0)}\n"
        "🎯 WINNER SELECTION: 100% Random & Fair\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms_spaced(remaining)}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{build_progress(percent)} {percent}%\n\n"
        "📜 RULES\n"
        f"{format_rules()}\n\n"
        "📢 HOSTED BY\n"
        f"⚡️ {HOST_NAME} ⚡️\n\n"
        "👇✨ TAP THE BUTTON BELOW & JOIN NOW 👇"
    )


SPINNER = ["🔄", "🔃", "🔁", "🔂"]

def build_closed_text(spin: str = "🔄") -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY HAS ENDED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway window is officially closed.\n"
        "🔒 All entries are now final and locked.\n\n"
        f"👥 Participants: {participants_count()}\n"
        f"🏆 Winners: {data.get('winner_count',0)}\n\n"
        "🎯 Winner selection is underway\n"
        f"{spin} Please stay tuned for the official announcement.\n\n"
        f"— {HOST_NAME} ⚡"
    )


def build_selection_text(percent: int, spin: str) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 WINNER SELECTION STARTED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{spin} Selecting winners...\n\n"
        "📊 PROGRESS\n"
        f"{build_progress(percent)} {percent}%\n\n"
        "✅ 100% Fair & Random\n"
        "🔐 User ID Based Selection\n\n"
        "Please wait..."
    )


def build_winners_post_text(first_uid: str, first_user: str, random_winners: list) -> str:
    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆✨ GIVEAWAY WINNERS ANNOUNCEMENT ✨🏆")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("🎉 The wait is over!")
    lines.append("Here are the official winners 👇")
    lines.append("")
    lines.append("🎁 PRIZE POOL 🌟")
    prize = (data.get("prize", "") or "").strip()
    lines += [p.strip() for p in prize.splitlines() if p.strip()] or ["PRIZE"]
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    if first_user:
        lines.append(f"👑 Username: {first_user}")
        lines.append(f"🆔 User ID: {first_uid}")
    else:
        lines.append(f"🆔 User ID: {first_uid}")
    lines.append("⚡ Secured instantly by joining first")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("👑 OTHER WINNERS (RANDOMLY SELECTED)")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    if not random_winners:
        lines.append("• None")
    else:
        i = 1
        for uid, uname in random_winners:
            if uname:
                lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}")
            else:
                lines.append(f"{i}️⃣ 👤 User ID: {uid}")
            i += 1
    lines.append("")
    lines.append("✅ This giveaway was completed using a")
    lines.append("100% fair & transparent random system.")
    lines.append("🔐 User ID based selection only.")
    lines.append("")
    lines.append("⏰ IMPORTANT")
    lines.append("🎁 Winners must claim within 24 hours.")
    lines.append("❌ Unclaimed prizes will expire.")
    lines.append("")
    lines.append("📢 HOSTED BY")
    lines.append(f"⚡️ {HOST_NAME} ⚡️")
    lines.append("")
    lines.append("👇✨ TAP THE BUTTON BELOW & CLAIM YOUR PRIZE 👇")
    return "\n".join(lines)

# =========================================================
# WINNER HISTORY (AUTO SAVE WHEN ANNOUNCED)
# =========================================================
def save_winner_history_record():
    with lock:
        record = {
            "ts": now_ts(),
            "title": data.get("title", ""),
            "prize": data.get("prize", ""),
            "winner_count": int(data.get("winner_count", 0) or 0),
            "participants": participants_count(),
            "first_winner_id": data.get("first_winner_id"),
            "first_winner_username": data.get("first_winner_username", ""),
            "winners": data.get("winners", {}) or {},
        }
        hist = data.get("winner_history", []) or []
        hist.insert(0, record)
        data["winner_history"] = hist[:300]
        save_data()

# =========================================================
# JOB CONTROL
# =========================================================
def stop_job(j):
    if j is not None:
        try:
            j.schedule_removal()
        except Exception:
            pass
    return None


def stop_live_countdown():
    global countdown_job
    countdown_job = stop_job(countdown_job)


def stop_closed_spinner():
    global closed_spinner_job
    closed_spinner_job = stop_job(closed_spinner_job)


def stop_draw_jobs():
    global draw_job, draw_finalize_job
    draw_job = stop_job(draw_job)
    draw_finalize_job = stop_job(draw_finalize_job)


def stop_auto_jobs():
    global auto_select_job, auto_finalize_job, auto_delete_closed_job
    auto_select_job = stop_job(auto_select_job)
    auto_finalize_job = stop_job(auto_finalize_job)
    auto_delete_closed_job = stop_job(auto_delete_closed_job)


def stop_claim_expire_job():
    global claim_expire_job
    claim_expire_job = stop_job(claim_expire_job)


def stop_reset_jobs():
    global reset_job, reset_finalize_job
    reset_job = stop_job(reset_job)
    reset_finalize_job = stop_job(reset_finalize_job)

# =========================================================
# LIVE COUNTDOWN (EDIT LIVE POST)
# =========================================================
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

        duration = int(data.get("duration_seconds", 1) or 1)
        elapsed = int(now_ts() - float(start_time))
        remaining = duration - elapsed

        live_mid = data.get("live_message_id")

        # time end -> close giveaway
        if remaining <= 0:
            # mark closed
            data["active"] = False
            data["closed"] = True
            save_data()

            # delete live post
            if live_mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
                except Exception:
                    pass

            # post closed message
            try:
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_text("🔄"))
                data["closed_message_id"] = m.message_id
                data["live_message_id"] = None
                save_data()
                start_closed_spinner(context.job_queue)
            except Exception:
                pass

            # notify admin
            try:
                if data.get("auto_winner_post"):
                    context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text="⏰ Giveaway Closed!\nAuto winner is ON ✅\nWinner selection will start automatically."
                    )
                else:
                    context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text="⏰ Giveaway Closed!\nAuto winner is OFF ❌\n\nNow use /draw to select winners."
                    )
            except Exception:
                pass

            stop_live_countdown()

            # auto winner
            if data.get("auto_winner_post"):
                start_auto_winner_selection(context)

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
# CLOSED SPINNER (NO DOTS)
# =========================================================
def start_closed_spinner(job_queue):
    global closed_spinner_job
    stop_closed_spinner()
    closed_spinner_job = job_queue.run_repeating(
        closed_spinner_tick, interval=2, first=0, context={"tick": 0}, name="closed_spinner"
    )


def closed_spinner_tick(context: CallbackContext):
    # stop if winners posted
    if data.get("winners_message_id"):
        stop_closed_spinner()
        return

    mid = data.get("closed_message_id")
    if not mid:
        stop_closed_spinner()
        return

    ctx = context.job.context or {}
    tick = int(ctx.get("tick", 0)) + 1
    ctx["tick"] = tick
    context.job.context = ctx

    spin = SPINNER[(tick - 1) % len(SPINNER)]
    try:
        context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=mid,
            text=build_closed_text(spin)
        )
    except Exception:
        pass

# =========================================================
# AUTO WINNER SELECTION (3 MIN, 5 SEC UPDATE)
# + closed post auto delete after 2 minutes
# =========================================================
AUTO_TOTAL_SECONDS = 180
AUTO_UPDATE_INTERVAL = 5
AUTO_DELETE_CLOSED_AFTER = 120  # 2 minutes

def start_auto_winner_selection(context: CallbackContext):
    global auto_select_job, auto_finalize_job, auto_delete_closed_job
    stop_auto_jobs()

    # post selection message
    try:
        m = context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=build_selection_text(0, "🔄")
        )
        with lock:
            data["selection_message_id"] = m.message_id
            save_data()
    except Exception:
        return

    # schedule delete closed post after 2 minutes
    def delete_closed(cb: CallbackContext):
        with lock:
            cmid = data.get("closed_message_id")
            # only delete the closed message (not selection)
            if cmid:
                try:
                    cb.bot.delete_message(chat_id=CHANNEL_ID, message_id=cmid)
                except Exception:
                    pass
                data["closed_message_id"] = None
                save_data()

    auto_delete_closed_job = context.job_queue.run_once(delete_closed, when=AUTO_DELETE_CLOSED_AFTER)

    # update selection progress
    ctx = {
        "start_ts": now_ts(),
        "tick": 0,
        "mid": data.get("selection_message_id"),
    }

    def auto_tick(cb: CallbackContext):
        jd = cb.job.context
        jd["tick"] = int(jd.get("tick", 0)) + 1
        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(AUTO_TOTAL_SECONDS)) * 100)))
        spin = SPINNER[(jd["tick"] - 1) % len(SPINNER)]
        mid = jd.get("mid")

        if not mid:
            return

        try:
            cb.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=mid,
                text=build_selection_text(percent, spin)
            )
        except Exception:
            pass

    auto_select_job = context.job_queue.run_repeating(
        auto_tick, interval=AUTO_UPDATE_INTERVAL, first=0, context=ctx, name="auto_select_job"
    )

    auto_finalize_job = context.job_queue.run_once(
        auto_finalize, when=AUTO_TOTAL_SECONDS, context=ctx, name="auto_finalize_job"
    )


def auto_finalize(context: CallbackContext):
    stop_auto_jobs()

    # delete selection post
    with lock:
        sel_mid = data.get("selection_message_id")
        data["selection_message_id"] = None
        save_data()

    if sel_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=sel_mid)
        except Exception:
            pass

    # if closed still exists, delete it (best effort)
    with lock:
        cmid = data.get("closed_message_id")
    if cmid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=cmid)
        except Exception:
            pass
        with lock:
            data["closed_message_id"] = None
            save_data()

    # select winners and post
    finalize_winners_and_post(context, auto_mode=True)

# =========================================================
# DRAW (MANUAL) - ADMIN SIDE (lightweight)
# =========================================================
DRAW_DURATION_SECONDS = 40
DRAW_UPDATE_INTERVAL = 5  # keep low resource

def start_draw_progress(context: CallbackContext, admin_chat_id: int):
    global draw_job, draw_finalize_job
    stop_draw_jobs()

    msg = context.bot.send_message(chat_id=admin_chat_id, text=build_selection_text(0, "🔄"))

    ctx = {
        "admin_chat_id": admin_chat_id,
        "admin_msg_id": msg.message_id,
        "start_ts": now_ts(),
        "tick": 0,
    }

    def draw_tick(cb: CallbackContext):
        jd = cb.job.context
        jd["tick"] = int(jd.get("tick", 0)) + 1
        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(DRAW_DURATION_SECONDS)) * 100)))
        spin = SPINNER[(jd["tick"] - 1) % len(SPINNER)]

        try:
            cb.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["admin_msg_id"],
                text=build_selection_text(percent, spin),
            )
        except Exception:
            pass

    draw_job = context.job_queue.run_repeating(
        draw_tick, interval=DRAW_UPDATE_INTERVAL, first=0, context=ctx, name="draw_job"
    )

    draw_finalize_job = context.job_queue.run_once(
        draw_finalize, when=DRAW_DURATION_SECONDS, context=ctx, name="draw_finalize_job"
    )


def draw_finalize(context: CallbackContext):
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

    # build winners preview
    pending_text = finalize_winners_preview_only()
    if not pending_text:
        try:
            context.bot.edit_message_text(
                chat_id=admin_chat_id,
                message_id=admin_msg_id,
                text="Failed to generate winners preview.",
            )
        except Exception:
            pass
        return

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
# WINNER FINALIZE CORE (used by auto + manual approve)
# =========================================================
def finalize_winners_preview_only() -> str:
    """
    Select winners and save pending text, but DO NOT post to channel.
    Used by /draw.
    """
    with lock:
        participants = data.get("participants", {}) or {}
        if not participants:
            return ""

        winner_count = int(data.get("winner_count", 1) or 1)
        winner_count = max(1, winner_count)

        # first winner
        first_uid = data.get("first_winner_id")
        if not first_uid:
            first_uid = next(iter(participants.keys()))
            info = participants.get(first_uid, {}) or {}
            data["first_winner_id"] = first_uid
            data["first_winner_username"] = info.get("username", "")
            data["first_winner_name"] = info.get("name", "")

        first_uid = str(first_uid)
        first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

        # random pool excluding first
        pool = [uid for uid in participants.keys() if str(uid) != first_uid]
        needed = max(0, winner_count - 1)
        needed = min(needed, len(pool))
        selected = random.sample(pool, needed) if needed > 0 else []

        winners_map = {first_uid: {"username": first_uname}}
        random_list = []
        for uid in selected:
            info = participants.get(uid, {}) or {}
            winners_map[str(uid)] = {"username": info.get("username", "")}
            random_list.append((str(uid), info.get("username", "")))

        data["winners"] = winners_map
        pending_text = build_winners_post_text(first_uid, first_uname, random_list)
        data["pending_winners_text"] = pending_text
        save_data()
        return pending_text


def finalize_winners_and_post(context: CallbackContext, auto_mode: bool = False):
    """
    Select winners and post directly to channel (auto mode), or used after manual approve.
    """
    pending_text = finalize_winners_preview_only()
    if not pending_text:
        return

    stop_closed_spinner()

    # delete closed post if exists
    with lock:
        closed_mid = data.get("closed_message_id")
        data["closed_message_id"] = None
        save_data()
    if closed_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
        except Exception:
            pass

    # post winners in channel
    try:
        m = context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=pending_text,
            reply_markup=claim_button_markup(),
        )
        with lock:
            data["winners_message_id"] = m.message_id

            # claim window 24h
            ts = now_ts()
            data["claim_start_ts"] = ts
            data["claim_expires_ts"] = ts + 24 * 3600

            save_data()

        # save history automatically
        save_winner_history_record()

        # schedule claim button removal
        schedule_claim_expire(context.job_queue)

        # notify admin if auto
        if auto_mode:
            try:
                context.bot.send_message(chat_id=ADMIN_ID, text="✅ Auto winners posted to channel successfully!")
            except Exception:
                pass

    except Exception:
        # if failed, keep data but don't crash
        pass

# =========================================================
# CLAIM EXPIRY (REMOVE BUTTON AFTER 24H)
# =========================================================
def schedule_claim_expire(job_queue):
    global claim_expire_job
    stop_claim_expire_job()

    exp = data.get("claim_expires_ts")
    mid = data.get("winners_message_id")
    if not exp or not mid:
        return

    remain = float(exp) - now_ts()
    if remain <= 0:
        return

    claim_expire_job = job_queue.run_once(expire_claim_button_job, when=remain, name="claim_expire_job")


def expire_claim_button_job(context: CallbackContext):
    with lock:
        mid = data.get("winners_message_id")
        exp = data.get("claim_expires_ts")
        if not mid or not exp:
            return

    try:
        context.bot.edit_message_reply_markup(chat_id=CHANNEL_ID, message_id=mid, reply_markup=None)
    except Exception:
        pass

# =========================================================
# RESET (CONFIRM -> 40s PROGRESS -> FULL WIPE)
# =========================================================
RESET_TOTAL_SECONDS = 40
RESET_UPDATE_INTERVAL = 5

def build_reset_progress_text(percent: int, spin: str) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ RESET IN PROGRESS\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{spin} Resetting system...\n\n"
        "📊 PROGRESS\n"
        f"{build_progress(percent)} {percent}%\n\n"
        "⚠️ Please wait...\n"
        "All data will be removed."
    )

def do_full_reset(context: CallbackContext):
    global data
    stop_live_countdown()
    stop_draw_jobs()
    stop_closed_spinner()
    stop_auto_jobs()
    stop_claim_expire_job()
    stop_reset_jobs()

    # delete important channel messages if possible
    with lock:
        mids = [
            data.get("live_message_id"),
            data.get("closed_message_id"),
            data.get("selection_message_id"),
            data.get("winners_message_id"),
        ]

    for mid in mids:
        if mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
            except Exception:
                pass

    # FULL WIPE EVERYTHING (NO KEEP)
    with lock:
        data = fresh_default_data()
        save_data()

# =========================================================
# COMMANDS
# =========================================================
def cmd_start(update: Update, context: CallbackContext):
    u = update.effective_user
    if u and u.id == ADMIN_ID:
        update.message.reply_text(
            "🛡️👑 WELCOME BACK, ADMIN 👑🛡️\n\n"
            "⚙️ System Status: ONLINE ✅\n"
            "🚀 Giveaway Engine: READY\n\n"
            "🧭 Open Admin Panel:\n"
            "/panel\n\n"
            f"⚡ POWERED BY: {HOST_NAME}"
        )
    else:
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please join our official channel and wait for the giveaway post.\n\n"
            "🔗 Official Channel:\n"
            f"{CHANNEL_LINK}"
        )


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
        "⚙️ AUTO\n"
        "/autowinnerpost\n"
        "/winnerlist\n\n"
        "🔒 BLOCK\n"
        "/blockpermanent\n"
        "/unban\n"
        "/blocklist\n"
        "/removeban\n\n"
        "✅ VERIFY\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "♻️ RESET\n"
        "/reset"
    )


def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    st = "ON ✅" if data.get("auto_winner_post") else "OFF ❌"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ AUTO WINNER POST SETTINGS\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Current Status: {st}\n\n"
        "✅ ON  → Giveaway end হলে auto winner select + channel post\n"
        "❌ OFF → Automatic বন্ধ (Manual /draw লাগবে)\n",
        reply_markup=autowinner_markup()
    )


def cmd_newgiveaway(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return

    # stop running jobs
    stop_live_countdown()
    stop_draw_jobs()
    stop_closed_spinner()
    stop_auto_jobs()
    stop_claim_expire_job()

    with lock:
        auto_state = data.get("auto_winner_post", False)
        data.clear()
        data.update(fresh_default_data())
        data["auto_winner_post"] = auto_state
        save_data()

    admin_state = "title"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 NEW GIVEAWAY SETUP STARTED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "STEP 1️⃣ — GIVEAWAY TITLE\n\n"
        "Send Giveaway Title:"
    )


def cmd_participants(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    parts = data.get("participants", {})
    if not parts:
        update.message.reply_text("👥 Participants List is empty.")
        return

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        "👥 PARTICIPANTS LIST",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Total Participants: {len(parts)}",
        "",
    ]
    i = 1
    for uid, info in parts.items():
        uname = (info or {}).get("username", "")
        if uname:
            lines.append(f"{i}. {uname} | 🆔 {uid}")
        else:
            lines.append(f"{i}. 🆔 {uid}")
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
    if not data.get("participants", {}):
        update.message.reply_text("No participants to draw winners from.")
        return
    start_draw_progress(context, update.effective_chat.id)


def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ RESET CONFIRMATION\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Are you sure?\n"
        "✅ Confirm → FULL reset (everything removed)\n"
        "❌ Reject → Reset cancelled",
        reply_markup=reset_confirm_markup()
    )


def cmd_winnerlist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    hist = data.get("winner_history", []) or []
    if not hist:
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🏆 WINNER LIST (HISTORY)\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "No winner history found yet."
        )
        return

    show = hist[:10]
    now = datetime.utcnow()

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆 WINNER LIST (HISTORY)")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    for idx, r in enumerate(show, start=1):
        title = (r.get("title") or "").strip() or "Giveaway"
        prize = (r.get("prize") or "").strip() or "Prize"

        ts = r.get("ts")
        try:
            ts = float(ts) if ts else None
        except Exception:
            ts = None

        if ts:
            dt_utc = datetime.utcfromtimestamp(ts)
            dt_bdt = dt_utc + timedelta(hours=6)
            date_str = dt_bdt.strftime("%d %b %Y")
            time_str = dt_bdt.strftime("%I:%M %p").lstrip("0")
            days_ago = (now - dt_utc).days
            ago_str = f"{days_ago} days ago"
        else:
            date_str = "Unknown Date"
            time_str = "Unknown Time"
            ago_str = "Unknown"

        lines.append(f"#{idx}️⃣ {title}")
        lines.append(f"🗓 {date_str} • {time_str} (BDT)")
        lines.append(f"⏳ {ago_str}")
        lines.append("")
        lines.append("🎁 PRIZE WON:")
        for pl in [p.strip() for p in prize.splitlines() if p.strip()] or ["PRIZE"]:
            lines.append(f"🏆 {pl}")

        fw_id = str(r.get("first_winner_id") or "").strip()
        fw_u = (r.get("first_winner_username") or "").strip()

        lines.append("")
        lines.append("🥇 FIRST JOIN CHAMPION")
        if fw_u and fw_id:
            lines.append(f"👑 {fw_u} | 🆔 {fw_id}")
        elif fw_id:
            lines.append(f"🆔 {fw_id}")
        else:
            lines.append("Not found")

        winners = r.get("winners", {}) or {}
        others = []
        for uid, info in winners.items():
            uid = str(uid)
            if fw_id and uid == fw_id:
                continue
            uname = (info or {}).get("username", "") if isinstance(info, dict) else ""
            others.append((uid, uname))

        lines.append("")
        lines.append("👑 OTHER WINNERS")
        if not others:
            lines.append("• None")
        else:
            i = 1
            for uid, uname in others[:30]:
                if uname:
                    lines.append(f"{i}️⃣ {uname} | 🆔 {uid}")
                else:
                    lines.append(f"{i}️⃣ 🆔 {uid}")
                i += 1

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("")

    update.message.reply_text("\n".join(lines))

# =========================================================
# ADMIN SETUP TEXT FLOW (title -> prize -> winners -> duration -> rules)
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

    if admin_state == "title":
        with lock:
            data["title"] = msg
            save_data()
        admin_state = "prize"
        update.message.reply_text("✅ Title saved!\n\nNow send Prize (multi-line allowed):")
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
            f"🏅 TOTAL WINNERS: {count}\n\n"
            "⏱ Send Giveaway Duration\n"
            "Example:\n"
            "30 Second\n"
            "3 Minute\n"
            "1 Hour"
        )
        return

    if admin_state == "duration":
        seconds = parse_duration(msg)
        if seconds <= 0:
            update.message.reply_text("Invalid duration. Example: 30 Second / 3 Minute / 1 Hour")
            return
        with lock:
            data["duration_seconds"] = seconds
            save_data()
        admin_state = "rules"
        update.message.reply_text("✅ Duration saved!\n\nNow send Rules (multi-line):")
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        update.message.reply_text("✅ Rules saved!\nShowing preview…")
        update.message.reply_text(build_preview_text(), reply_markup=preview_markup())
        return

# =========================================================
# CALLBACK HANDLER
# =========================================================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

    # Auto winner ON/OFF
    if qd in ("autowin_on", "autowin_off"):
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

        if qd == "autowin_on":
            with lock:
                data["auto_winner_post"] = True
                save_data()
            st = "ON ✅"
        else:
            # stop any running auto selection instantly
            stop_auto_jobs()
            with lock:
                data["auto_winner_post"] = False
                save_data()
            st = "OFF ❌"

        try:
            query.edit_message_text(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "⚙️ AUTO WINNER POST SETTINGS\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Current Status: {st}\n\n"
                "✅ ON  → Giveaway end হলে auto winner select + channel post\n"
                "❌ OFF → Automatic বন্ধ (Manual /draw লাগবে)\n",
                reply_markup=autowinner_markup()
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
                    data["selection_message_id"] = None
                    data["winners_message_id"] = None

                    data["participants"] = {}
                    data["winners"] = {}
                    data["pending_winners_text"] = ""
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""

                    data["claim_start_ts"] = None
                    data["claim_expires_ts"] = None

                    save_data()

                stop_closed_spinner()
                stop_auto_jobs()
                start_live_countdown(context.job_queue)

                query.edit_message_text("✅ Giveaway approved and posted to channel!")
            except Exception as e:
                query.edit_message_text(f"Failed to post in channel. Make sure bot is admin.\nError: {e}")
            return

        if qd == "preview_reject":
            try:
                query.answer()
            except Exception:
                pass
            query.edit_message_text("❌ Giveaway rejected and cleared.")
            return

        if qd == "preview_edit":
            try:
                query.answer()
            except Exception:
                pass
            query.edit_message_text("✏️ Edit Mode\n\nStart again with /newgiveaway")
            return

    # End giveaway
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

        # delete live
        live_mid = data.get("live_message_id")
        if live_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception:
                pass
            with lock:
                data["live_message_id"] = None
                save_data()

        # post closed
        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_text("🔄"))
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
            start_closed_spinner(context.job_queue)
        except Exception:
            pass

        stop_live_countdown()

        # if auto on -> start selection
        if data.get("auto_winner_post"):
            start_auto_winner_selection(context)
            try:
                query.edit_message_text("✅ Giveaway Closed! Auto winner ON ✅")
            except Exception:
                pass
        else:
            try:
                query.edit_message_text("✅ Giveaway Closed! Now use /draw")
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

    # Reset confirm/reject
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

        stop_reset_jobs()

        admin_chat_id = query.message.chat_id
        admin_msg_id = query.message.message_id
        ctx = {"start_ts": now_ts(), "tick": 0, "chat": admin_chat_id, "mid": admin_msg_id}

        def reset_tick(cb: CallbackContext):
            jd = cb.job.context
            jd["tick"] = int(jd.get("tick", 0)) + 1
            elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
            percent = int(round(min(100, (elapsed / float(RESET_TOTAL_SECONDS)) * 100)))
            spin = SPINNER[(jd["tick"] - 1) % len(SPINNER)]
            try:
                cb.bot.edit_message_text(
                    chat_id=jd["chat"],
                    message_id=jd["mid"],
                    text=build_reset_progress_text(percent, spin),
                )
            except Exception:
                pass

        def reset_finalize(cb: CallbackContext):
            stop_reset_jobs()
            do_full_reset(cb)
            try:
                cb.bot.edit_message_text(
                    chat_id=admin_chat_id,
                    message_id=admin_msg_id,
                    text=(
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "✅ RESET COMPLETED SUCCESSFULLY!\n"
                        "━━━━━━━━━━━━━━━━━━━━\n\n"
                        "Everything has been removed.\n"
                        "Start again with:\n"
                        "/newgiveaway"
                    ),
                )
            except Exception:
                pass

        global reset_job, reset_finalize_job
        reset_job = context.job_queue.run_repeating(reset_tick, interval=RESET_UPDATE_INTERVAL, first=0, context=ctx)
        reset_finalize_job = context.job_queue.run_once(reset_finalize, when=RESET_TOTAL_SECONDS, context=ctx)
        return

    if qd == "reset_cancel":
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
        stop_reset_jobs()
        try:
            query.edit_message_text("❌ Reset cancelled. Nothing was removed.")
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

        # already first winner
        with lock:
            first_uid = data.get("first_winner_id")

        if first_uid and uid == str(first_uid):
            tg_user = query.from_user
            uname = user_tag(tg_user.username or "") or data.get("first_winner_username", "") or "@username"
            try:
                query.answer(popup_first_winner(uname, uid), show_alert=True)
            except Exception:
                pass
            return

        # already joined
        if uid in (data.get("participants", {}) or {}):
            try:
                query.answer(popup_already_joined(), show_alert=True)
            except Exception:
                pass
            return

        # save participant
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

        # update live post (best effort)
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time")
            if live_mid and start_ts:
                duration = int(data.get("duration_seconds", 1) or 1)
                elapsed = int(now_ts() - float(start_ts))
                remaining = max(0, duration - elapsed)
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=live_mid,
                    text=build_live_text(remaining),
                    reply_markup=join_button_markup(),
                )
        except Exception:
            pass

        # popup
        with lock:
            if data.get("first_winner_id") == uid:
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

        # When approved, post winners + delete closed message automatically
        finalize_winners_and_post(context, auto_mode=False)

        try:
            query.edit_message_text("✅ Approved! Winners posted to channel (with Claim button).")
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

    # Claim prize
    if qd == "claim_prize":
        winners = data.get("winners", {}) or {}

        if uid not in winners:
            try:
                query.answer(popup_claim_not_winner(), show_alert=True)
            except Exception:
                pass
            return

        exp_ts = data.get("claim_expires_ts")
        if exp_ts:
            try:
                if now_ts() > float(exp_ts):
                    query.answer(popup_prize_expired(), show_alert=True)
                    return
            except Exception:
                pass

        uname = winners.get(uid, {}).get("username", "") or "@username"
        try:
            query.answer(popup_claim_winner(uname, uid), show_alert=True)
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

    # commands
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("panel", cmd_panel))
    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))
    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))
    dp.add_handler(CommandHandler("winnerlist", cmd_winnerlist))
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # text + callbacks
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # resume after restart (light)
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    if data.get("closed") and data.get("closed_message_id") and not data.get("winners_message_id"):
        start_closed_spinner(updater.job_queue)

    if data.get("winners_message_id") and data.get("claim_expires_ts"):
        remain = float(data["claim_expires_ts"]) - now_ts()
        if remain > 0:
            schedule_claim_expire(updater.job_queue)

    print("Bot is running (PTB 13, GSM compatible, non-async) ...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
