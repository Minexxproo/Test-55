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

closed_anim_job = None
claim_expire_job = None

auto_draw_job = None  # repeating
# =========================================================
# CONSTANTS (AUTO DRAW)
# =========================================================
AUTO_DRAW_TOTAL_SECONDS = 5 * 60   # 5 minutes
AUTO_DRAW_UPDATE_INTERVAL = 0.8
AUTO_DRAW_SPINNER = ["⏳", "🔄", "🔃", "🔁"]
AUTO_DRAW_DOTS = [".", "..", "...", "....", ".....", "......", "......."]
COLOR_TAGS = ["🟣", "🟠", "🔵", "🟢", "🔴", "🟡", "🟤", "⚫️", "⚪️"]

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

        "participants": {},  # uid(str) -> {"username": "@x" or "", "name": ""}

        "verify_targets": [],  # [{"ref": "-100..." or "@xxx", "display": "..."}]

        "permanent_block": {},  # uid -> {"username": "@x" or ""}

        "old_winner_mode": "skip",  # "block" or "skip"
        "old_winners": {},          # uid -> {"username": "@x" or ""} used ONLY if mode=block

        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        "winners": {},  # uid -> {"username": "@x"}
        "pending_winners_text": "",

        "claim_start_ts": None,
        "claim_expires_ts": None,

        # DELIVERY TRACKING
        "delivered": {},  # uid -> {"username":"@x","at":ts}

        # AUTO DRAW
        "auto_draw": False,
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


def now_ts() -> float:
    return datetime.utcnow().timestamp()


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


def autodraw_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Auto Draw ON", callback_data="autodraw_on"),
            InlineKeyboardButton("❌ Auto Draw OFF", callback_data="autodraw_off"),
        ]]
    )

# =========================================================
# POPUPS (TEXT)
# =========================================================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n"
        "Please join the required\n"
        "channels first and then\n"
        "click JOIN GIVEAWAY again."
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
        "🥇 FIRST JOIN CHAMPION 🌟\n"
        "Congratulations! You joined\n"
        "the giveaway FIRST and secured\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        "📸 Please take a screenshot\n"
        "and post it in the group\n"
        "to confirm 👈"
    )


def popup_already_joined() -> str:
    return (
        "🚫 ENTRY UNSUCCESSFUL\n\n"
        "You have already joined\n"
        "this giveaway 🎁\n\n"
        "Multiple entries are not allowed.\n"
        "Please wait for the final result ⏳"
    )


def popup_join_success(username: str, uid: str) -> str:
    return (
        "🌹 CONGRATULATIONS!\n\n"
        "You have successfully joined\n"
        "the giveaway ✅\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}"
    )


def popup_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n\n"
        "You are permanently blocked\n"
        "from joining giveaways.\n\n"
        f"Contact admin 👉 {ADMIN_CONTACT}"
    )


def popup_claim_winner(username: str, uid: str) -> str:
    return (
        "🌟 Congratulations ✨\n\n"
        "You’ve won this giveaway.\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        "📩 Please contact admin\n"
        "to claim your prize now:\n"
        f"👉 {ADMIN_CONTACT}"
    )


def popup_claim_not_winner() -> str:
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry! Your User ID is not\n"
        "in the winners list.\n\n"
        "Please wait for the next\n"
        "giveaway ❤️‍🩹"
    )


def popup_prize_expired() -> str:
    return (
        "⏳ PRIZE EXPIRED\n"
        "Your 24-hour claim time\n"
        "has ended.\n\n"
        "This prize is no longer available."
    )


def popup_already_delivered(username: str, uid: str) -> str:
    return (
        "📦 PRIZE ALREADY DELIVERED\n"
        "Your prize has already been\n"
        "successfully delivered ✅\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        "If you face any issue,\n"
        f"contact admin 👉 {ADMIN_CONTACT}"
    )


# =========================================================
# TEXT BUILDERS (LIVE / PREVIEW)
# =========================================================
def build_preview_text() -> str:
    remaining = data.get("duration_seconds", 0)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ {data.get('title','')} ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{data.get('prize','')}\n\n"
        f"👥 Total Participants: 0  \n"
        f"🏆 Total Winners: {data.get('winner_count',0)}  \n\n"
        "🎯 Winner Selection\n"
        "• 100% Random & Fair  \n"
        "• Auto System  \n\n"
        f"⏱️ Time Remaining: {format_hms(remaining)}  \n"
        "📊 Live Progress\n"
        f"{build_progress(0)}  \n\n"
        "📜 Official Rules  \n"
        f"{format_rules()}  \n\n"
        f"📢 Hosted by: {HOST_NAME}  \n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Tap below to join the giveaway 👇  \n"
        "[🎁✨ JOIN GIVEAWAY NOW ✨🎁]"
    )


def build_live_text(remaining: int) -> str:
    duration = data.get("duration_seconds", 1) or 1
    elapsed = duration - remaining
    elapsed = max(0, min(duration, elapsed))
    percent = int(round((elapsed / float(duration)) * 100))
    progress = build_progress(percent)

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ {data.get('title','')} ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{data.get('prize','')}\n\n"
        f"👥 Total Participants: {participants_count()}  \n"
        f"🏆 Total Winners: {data.get('winner_count',0)}  \n\n"
        "🎯 Winner Selection\n"
        "• 100% Random & Fair  \n"
        "• Auto System  \n\n"
        f"⏱️ Time Remaining: {format_hms(remaining)}  \n"
        "📊 Live Progress\n"
        f"{progress}  \n\n"
        "📜 Official Rules  \n"
        f"{format_rules()}  \n\n"
        f"📢 Hosted by: {HOST_NAME}  \n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Tap below to join the giveaway 👇  "
    )


def build_closed_text(spin: str) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        f"{spin} Winner selection is currently\n"
        "in progress\n\n"
        "🙏 Thank you to everyone\n"
        "who participated.\n"
        f"— {HOST_NAME} ⚡"
    )


# =========================================================
# WINNERS POST (WITH DELIVERY)
# =========================================================
def delivery_count() -> int:
    return len((data.get("delivered", {}) or {}))


def is_delivered(uid: str) -> bool:
    return uid in (data.get("delivered", {}) or {})


def build_winners_post_text(first_uid: str, first_user: str, random_winners: list) -> str:
    total = int(data.get("winner_count", 0)) or 0
    delivered = delivery_count()

    lines = []
    lines.append("🏆 GIVEAWAY WINNERS ANNOUNCEMENT 🏆")
    lines.append("")
    lines.append("POWER POINT BREAK")
    lines.append("")
    lines.append(f"🎁 PRIZE: {data.get('prize','')}")
    lines.append(f"📦 Prize Delivery: {delivered}/{total}")
    lines.append("")
    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    if first_user:
        mark = "  ✅ Delivered" if is_delivered(first_uid) else ""
        lines.append(f"👑 {first_user}")
        lines.append(f"🆔 {first_uid}{mark}")
    else:
        mark = "  ✅ Delivered" if is_delivered(first_uid) else ""
        lines.append(f"🆔 {first_uid}{mark}")

    lines.append("")
    lines.append("👑 OTHER WINNERS")
    i = 1
    for uid, uname in random_winners:
        mark = "  ✅ Delivered" if is_delivered(uid) else ""
        if uname:
            lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}{mark}")
        else:
            lines.append(f"{i}️⃣ 👤 User 🆔 {uid}{mark}")
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

            # post closed message (spinner live via job)
            try:
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_text("🔄"))
                data["closed_message_id"] = m.message_id
                save_data()
                start_closed_spinner(context.job_queue)
            except Exception:
                pass

            # if AUTO DRAW ON → start auto draw in channel
            if data.get("auto_draw"):
                try:
                    start_auto_draw_in_channel(context)
                except Exception:
                    pass
            else:
                # notify admin (manual draw)
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
# CLOSED SPINNER (NO DOTS, SPINNER ONLY)
# =========================================================
def stop_closed_spinner():
    global closed_anim_job
    if closed_anim_job is not None:
        try:
            closed_anim_job.schedule_removal()
        except Exception:
            pass
    closed_anim_job = None


def start_closed_spinner(job_queue):
    global closed_anim_job
    stop_closed_spinner()
    closed_anim_job = job_queue.run_repeating(
        closed_spinner_tick,
        interval=0.8,
        first=0,
        context={"tick": 0},
        name="closed_spinner",
    )


def closed_spinner_tick(context: CallbackContext):
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

    spin = AUTO_DRAW_SPINNER[(tick - 1) % len(AUTO_DRAW_SPINNER)]
    try:
        context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=mid,
            text=build_closed_text(spin)
        )
    except Exception:
        pass


# =========================================================
# CLAIM EXPIRY JOB (REMOVE BUTTON AFTER 24H)
# =========================================================
def stop_claim_expire_job():
    global claim_expire_job
    if claim_expire_job is not None:
        try:
            claim_expire_job.schedule_removal()
        except Exception:
            pass
    claim_expire_job = None


def schedule_claim_expire(job_queue):
    global claim_expire_job
    stop_claim_expire_job()

    exp = data.get("claim_expires_ts")
    if not exp:
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
        context.bot.edit_message_reply_markup(
            chat_id=CHANNEL_ID,
            message_id=mid,
            reply_markup=None
        )
    except Exception:
        pass


# =========================================================
# MANUAL DRAW (ADMIN DM) - KEEP
# =========================================================
DRAW_DURATION_SECONDS = 40
DRAW_UPDATE_INTERVAL = 0.8

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


def build_draw_progress_text(percent: int, spin: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{spin} Selecting winners: {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        "🔐 100% fair & random system\n"
        "🔐 User ID based selection only\n\n"
        "Please wait"
    )


def start_draw_progress(context: CallbackContext, admin_chat_id: int):
    global draw_job, draw_finalize_job
    stop_draw_jobs()

    msg = context.bot.send_message(
        chat_id=admin_chat_id,
        text=build_draw_progress_text(0, "⏳"),
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
        percent = int(round(min(100, (elapsed / float(DRAW_DURATION_SECONDS)) * 100)))
        spin = AUTO_DRAW_SPINNER[(tick - 1) % len(AUTO_DRAW_SPINNER)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["admin_msg_id"],
                text=build_draw_progress_text(percent, spin),
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


def select_winners_build_text():
    participants = data.get("participants", {}) or {}
    if not participants:
        return None, None, None, None

    winner_count = int(data.get("winner_count", 1)) or 1
    winner_count = max(1, winner_count)

    first_uid = data.get("first_winner_id")
    if not first_uid:
        first_uid = next(iter(participants.keys()))
        info = participants.get(first_uid, {}) or {}
        data["first_winner_id"] = first_uid
        data["first_winner_username"] = info.get("username", "")
        data["first_winner_name"] = info.get("name", "")

    first_uname = data.get("first_winner_username", "")
    if not first_uname:
        first_uname = (participants.get(first_uid, {}) or {}).get("username", "")

    pool = [uid for uid in participants.keys() if uid != first_uid]
    remaining_needed = max(0, winner_count - 1)
    remaining_needed = min(remaining_needed, len(pool))
    selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

    winners_map = {}
    winners_map[first_uid] = {"username": first_uname}

    random_list = []
    for uid in selected:
        info = participants.get(uid, {}) or {}
        winners_map[uid] = {"username": info.get("username", "")}
        random_list.append((uid, info.get("username", "")))

    data["winners"] = winners_map
    text = build_winners_post_text(first_uid, first_uname, random_list)
    return text, first_uid, first_uname, random_list


def draw_finalize(context: CallbackContext):
    stop_draw_jobs()

    jd = context.job.context
    admin_chat_id = jd["admin_chat_id"]
    admin_msg_id = jd["admin_msg_id"]

    with lock:
        out = select_winners_build_text()
        if not out or not out[0]:
            try:
                context.bot.edit_message_text(
                    chat_id=admin_chat_id,
                    message_id=admin_msg_id,
                    text="No participants to draw winners from.",
                )
            except Exception:
                pass
            return

        pending_text = out[0]
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
# AUTO DRAW IN CHANNEL (PIN + SPINNER + DOTS + SHOWCASE)
# =========================================================
def stop_auto_draw():
    global auto_draw_job
    if auto_draw_job is not None:
        try:
            auto_draw_job.schedule_removal()
        except Exception:
            pass
    auto_draw_job = None


def pick_showcase_lines(ctx: dict, k: int = 2) -> list:
    ids = ctx.get("show_ids", []) or []
    if not ids:
        return ["➤ Loading participants..."]

    out = []
    i = int(ctx.get("show_i", 0))
    c = int(ctx.get("color_i", 0))

    for _ in range(k):
        uid = str(ids[i % len(ids)])
        i += 1
        info = (data.get("participants", {}) or {}).get(uid, {}) or {}
        uname = (info.get("username") or "").strip()
        tag = COLOR_TAGS[c % len(COLOR_TAGS)]
        c += 1
        if uname:
            out.append(f"{tag} ➤ Now showing: {uname}")
        else:
            out.append(f"{tag} ➤ Now showing: User 🆔 {uid}")

    ctx["show_i"] = i
    ctx["color_i"] = c
    return out


def build_auto_draw_progress_text(percent: int, spin: str, dots: str, remain_seconds: int, show_lines: list) -> str:
    bar = build_progress(percent)
    lines = [
        "━━━━━━━━━━━━━━━━━━",
        "🎲 AUTO RANDOM WINNER SELECTION",
        "━━━━━━━━━━━━━━━━━━",
        "",
        f"{spin} Selecting winners: {percent}%",
        f"📊 Progress: {bar}",
        "",
        f"🕒 Time Remaining: {format_hms(remain_seconds)}",
        "🔐 100% Random • Fair • Auto System",
        "",
        "👥 Live Entries Showcase",
    ]
    lines += show_lines
    lines += ["", f"Please wait {dots}"]
    return "\n".join(lines)


def start_auto_draw_in_channel(context: CallbackContext):
    global auto_draw_job
    stop_auto_draw()

    participants = list((data.get("participants", {}) or {}).keys())
    random.shuffle(participants)

    m = context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=build_auto_draw_progress_text(
            percent=0,
            spin="⏳",
            dots=".......",
            remain_seconds=AUTO_DRAW_TOTAL_SECONDS,
            show_lines=["➤ Loading participants..."]
        ),
    )

    # auto pin
    try:
        context.bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=m.message_id, disable_notification=True)
    except Exception:
        pass

    ctx = {
        "start_ts": now_ts(),
        "total_seconds": AUTO_DRAW_TOTAL_SECONDS,
        "tick": 0,
        "channel_msg_id": m.message_id,
        "show_ids": participants if participants else ["0"],
        "show_i": 0,
        "color_i": 0,
    }

    def auto_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] = int(jd.get("tick", 0)) + 1
        tick = jd["tick"]

        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        total = float(jd["total_seconds"])
        remain = max(0, int(total - elapsed))
        percent = int(round(min(100, (elapsed / total) * 100)))

        spin = AUTO_DRAW_SPINNER[(tick - 1) % len(AUTO_DRAW_SPINNER)]
        dots = AUTO_DRAW_DOTS[(tick - 1) % len(AUTO_DRAW_DOTS)]
        show_lines = pick_showcase_lines(jd, k=2)

        if percent >= 100:
            # stop job
            try:
                job_ctx.job.schedule_removal()
            except Exception:
                pass

            # delete closed spinner msg
            cmid = data.get("closed_message_id")
            if cmid:
                try:
                    job_ctx.bot.delete_message(chat_id=CHANNEL_ID, message_id=cmid)
                except Exception:
                    pass

            # winners auto post
            with lock:
                out = select_winners_build_text()
                if not out or not out[0]:
                    return
                winners_text = out[0]
                save_data()

            try:
                w = job_ctx.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=winners_text,
                    reply_markup=claim_button_markup(),
                )
                with lock:
                    data["winners_message_id"] = w.message_id
                    data["closed_message_id"] = None

                    ts = now_ts()
                    data["claim_start_ts"] = ts
                    data["claim_expires_ts"] = ts + 24 * 3600
                    save_data()

                schedule_claim_expire(job_ctx.job_queue)
            except Exception:
                pass

            return

        text = build_auto_draw_progress_text(percent, spin, dots, remain, show_lines)
        try:
            job_ctx.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=jd["channel_msg_id"],
                text=text
            )
        except Exception:
            pass

    auto_draw_job = context.job_queue.run_repeating(
        auto_tick,
        interval=AUTO_DRAW_UPDATE_INTERVAL,
        first=0,
        context=ctx,
        name="auto_draw_tick_job",
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
            "/panel"
        )
    else:
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please join our official channel\n"
            "and wait for the giveaway post.\n\n"
            f"🔗 Official Channel:\n{CHANNEL_LINK}"
        )


def cmd_panel(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "🛠 ADMIN CONTROL PANEL – POWER POINT BREAK\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/endgiveaway\n"
        "/draw\n\n"
        "⚙️ AUTO DRAW\n"
        "/Autodraw\n\n"
        "📦 PRIZE DELIVERY\n"
        "/prizeDelivered\n\n"
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
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ AUTO DRAW SETTINGS\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Choose Auto Draw Mode:\n\n"
        "✅ ON  → Auto winner selection will start\n"
        "automatically when giveaway ends.\n\n"
        "❌ OFF → Admin will run /draw manually.",
        reply_markup=autodraw_markup()
    )


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

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        "🗑 REMOVE VERIFY TARGET",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "Current Verify Targets:",
        "",
    ]
    for i, t in enumerate(targets, start=1):
        lines.append(f"{i}) {t.get('display','')}")
    lines += ["", "Send a number to remove that target.", "11) Remove ALL verify targets"]
    admin_state = "remove_verify_pick"
    update.message.reply_text("\n".join(lines))


def cmd_newgiveaway(update: Update, context: CallbackContext):
    global admin_state, data
    if not is_admin(update):
        return

    stop_live_countdown()
    stop_draw_jobs()
    stop_closed_spinner()
    stop_claim_expire_job()
    stop_auto_draw()

    with lock:
        keep_perma = data.get("permanent_block", {})
        keep_verify = data.get("verify_targets", {})
        auto_draw = bool(data.get("auto_draw", False))

        data.clear()
        data.update(fresh_default_data())
        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify if isinstance(keep_verify, list) else []
        data["auto_draw"] = auto_draw
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
    parts = data.get("participants", {})
    if not parts:
        update.message.reply_text("👥 Participants List is empty.")
        return

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        "👥 PARTICIPANTS LIST (ADMIN VIEW)",
        "━━━━━━━━━━━━━━━━━━━━",
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


def cmd_prize_delivered(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    if not data.get("winners"):
        update.message.reply_text("No winners found. Post winners first.")
        return
    admin_state = "prize_delivered_list"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🏆 PRIZE DELIVERY PANEL\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send delivered winners list\n"
        "(one per line)\n\n"
        "Format:\n"
        "@username | user_id\n\n"
        "Example:\n"
        "@MinexxProo | 5692210187\n"
        "6953353566\n\n"
        "You can send unlimited entries."
    )


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

    # ADD VERIFY
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
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET ADDED SUCCESSFULLY!\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Added: {ref}\n"
            f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}\n\n"
            "What do you want to do next?",
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
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET REMOVED\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
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
        update.message.reply_text("Now send Giveaway Prize (multi-line allowed):")
        return

    if admin_state == "prize":
        with lock:
            data["prize"] = msg
            save_data()
        admin_state = "winners"
        update.message.reply_text("Now send Total Winner Count (1 - 1000000):")
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
            "Send Giveaway Duration\n"
            "Example:\n"
            "30 Second\n"
            "5 Minute\n"
            "1 Hour"
        )
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
            "1 → BLOCK OLD WINNERS\n"
            "2 → SKIP OLD WINNERS"
        )
        return

    if admin_state == "old_winner_mode":
        if msg not in ("1", "2"):
            update.message.reply_text("Reply with 1 or 2 only.")
            return

        with lock:
            if msg == "2":
                data["old_winner_mode"] = "skip"
                data["old_winners"] = {}
            else:
                data["old_winner_mode"] = "block"
                data["old_winners"] = {}
            save_data()

        admin_state = "rules"
        update.message.reply_text("Send Giveaway Rules (multi-line):")
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        update.message.reply_text("✅ Rules saved!\nShowing preview…")
        update.message.reply_text(build_preview_text(), reply_markup=preview_markup())
        return

    # PRIZE DELIVERED LIST
    if admin_state == "prize_delivered_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return

        with lock:
            winners = data.get("winners", {}) or {}
            delivered = data.get("delivered", {}) or {}
            added = 0

            for uid, uname in entries:
                if uid not in winners:
                    # still allow marking (your choice), but usually winner-only:
                    continue
                if not uname:
                    uname = winners.get(uid, {}).get("username", "") or ""
                if uid not in delivered:
                    added += 1
                delivered[uid] = {"username": uname, "at": now_ts()}

            data["delivered"] = delivered
            save_data()

        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ PRIZE DELIVERY CONFIRMED\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"New Delivered Added: {added}\n"
            f"Total Delivered: {delivery_count()}"
        )

        # update channel winners post
        try:
            mid = data.get("winners_message_id")
            if mid:
                # rebuild winners post using stored winners
                with lock:
                    # rebuild random list from winners map (excluding first)
                    winners_map = data.get("winners", {}) or {}
                    first_uid = data.get("first_winner_id") or ""
                    first_uname = data.get("first_winner_username") or winners_map.get(first_uid, {}).get("username", "")
                    random_list = []
                    for uid, info in winners_map.items():
                        if uid == str(first_uid):
                            continue
                        random_list.append((uid, (info or {}).get("username", "")))
                    text = build_winners_post_text(str(first_uid), first_uname, random_list)
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=mid,
                    text=text,
                    reply_markup=claim_button_markup()
                )
        except Exception:
            pass

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
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        admin_state = "add_verify"
        try: query.edit_message_text("Send another Chat ID or @username:")
        except Exception: pass
        return

    if qd == "verify_add_done":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        admin_state = None
        try:
            query.edit_message_text(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "✅ VERIFY SETUP COMPLETED\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}\n"
                "All users must join ALL targets to join giveaway."
            )
        except Exception:
            pass
        return

    # AutoDraw ON/OFF
    if qd in ("autodraw_on", "autodraw_off"):
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass

        with lock:
            data["auto_draw"] = (qd == "autodraw_on")
            save_data()

        if data["auto_draw"]:
            txt = (
                "━━━━━━━━━━━━━━━━━━━━\n"
                "✅ AUTO DRAW ENABLED\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Auto Draw is now: ON ✅\n\n"
                "When this giveaway ends,\n"
                "winner selection will start\n"
                "automatically in the channel.\n\n"
                "5 Minute • Auto Pin • Spinner + Dots\n"
                "Live Entries Showcase"
            )
        else:
            txt = (
                "━━━━━━━━━━━━━━━━━━━━\n"
                "❌ AUTO DRAW DISABLED\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Auto Draw is now: OFF\n\n"
                "When giveaway ends,\n"
                "admin must use /draw manually."
            )
        try:
            query.edit_message_text(txt)
        except Exception:
            pass
        return

    # Preview actions
    if qd.startswith("preview_"):
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return

        if qd == "preview_approve":
            try: query.answer()
            except Exception: pass

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
                    data["winners_message_id"] = None

                    data["participants"] = {}
                    data["winners"] = {}
                    data["pending_winners_text"] = ""
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""

                    data["claim_start_ts"] = None
                    data["claim_expires_ts"] = None
                    data["delivered"] = {}

                    save_data()

                stop_closed_spinner()
                start_live_countdown(context.job_queue)

                query.edit_message_text("✅ Giveaway approved and posted to channel!")
            except Exception as e:
                query.edit_message_text(f"Failed to post in channel. Make sure bot is admin.\nError: {e}")
            return

        if qd == "preview_reject":
            try: query.answer()
            except Exception: pass
            query.edit_message_text("❌ Giveaway rejected and cleared.")
            return

    # End giveaway confirm/cancel
    if qd == "end_confirm":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return

        try: query.answer()
        except Exception: pass

        with lock:
            if not data.get("active"):
                try: query.edit_message_text("No active giveaway is running right now.")
                except Exception: pass
                return
            data["active"] = False
            data["closed"] = True
            save_data()

        live_mid = data.get("live_message_id")
        if live_mid:
            try: context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception: pass

        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_text("🔄"))
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
            start_closed_spinner(context.job_queue)
        except Exception:
            pass

        stop_live_countdown()
        try:
            query.edit_message_text("✅ Giveaway Closed Successfully!")
        except Exception:
            pass

        # auto draw if on
        if data.get("auto_draw"):
            start_auto_draw_in_channel(context)
        return

    if qd == "end_cancel":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        try: query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        except Exception: pass
        return

    # Join giveaway
    if qd == "join_giveaway":
        if not data.get("active"):
            try: query.answer("This giveaway is not active right now.", show_alert=True)
            except Exception: pass
            return

        if not verify_user_join(context.bot, int(uid)):
            try: query.answer(popup_verify_required(), show_alert=True)
            except Exception: pass
            return

        if uid in (data.get("permanent_block", {}) or {}):
            try: query.answer(popup_permanent_blocked(), show_alert=True)
            except Exception: pass
            return

        if data.get("old_winner_mode") == "block":
            if uid in (data.get("old_winners", {}) or {}):
                try: query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception: pass
                return

        with lock:
            first_uid = data.get("first_winner_id")

        # First join champion clicking again -> same popup
        if first_uid and uid == str(first_uid):
            tg_user = query.from_user
            uname = user_tag(tg_user.username or "") or data.get("first_winner_username", "") or "@username"
            try: query.answer(popup_first_winner(uname, uid), show_alert=True)
            except Exception: pass
            return

        if uid in (data.get("participants", {}) or {}):
            try: query.answer(popup_already_joined(), show_alert=True)
            except Exception: pass
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

        # update live post quickly
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time")
            if live_mid and start_ts:
                start = datetime.utcfromtimestamp(start_ts)
                duration = data.get("duration_seconds", 1) or 1
                elapsed = int((datetime.utcnow() - start).total_seconds())
                remaining = max(0, duration - elapsed)
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=live_mid,
                    text=build_live_text(remaining),
                    reply_markup=join_button_markup(),
                )
        except Exception:
            pass

        with lock:
            if data.get("first_winner_id") == uid:
                try: query.answer(popup_first_winner(uname or "@username", uid), show_alert=True)
                except Exception: pass
            else:
                try: query.answer(popup_join_success(uname or "@username", uid), show_alert=True)
                except Exception: pass
        return

    # Winners Approve/Reject (manual draw)
    if qd == "winners_approve":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass

        text = (data.get("pending_winners_text") or "").strip()
        if not text:
            try: query.edit_message_text("No pending winners preview found.")
            except Exception: pass
            return

        stop_closed_spinner()

        closed_mid = data.get("closed_message_id")
        if closed_mid:
            try: context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception: pass

        try:
            m = context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                reply_markup=claim_button_markup(),
            )
            with lock:
                data["winners_message_id"] = m.message_id
                data["closed_message_id"] = None
                ts = now_ts()
                data["claim_start_ts"] = ts
                data["claim_expires_ts"] = ts + 24 * 3600
                save_data()

            schedule_claim_expire(context.job_queue)
            query.edit_message_text("✅ Approved! Winners list posted to channel (with Claim button).")
        except Exception as e:
            try: query.edit_message_text(f"Failed to post winners in channel: {e}")
            except Exception: pass
        return

    if qd == "winners_reject":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        with lock:
            data["pending_winners_text"] = ""
            save_data()
        try: query.edit_message_text("❌ Rejected! Winners list will NOT be posted.")
        except Exception: pass
        return

    # Claim prize
    if qd == "claim_prize":
        winners = data.get("winners", {}) or {}

        if uid not in winners:
            try: query.answer(popup_claim_not_winner(), show_alert=True)
            except Exception: pass
            return

        # expired?
        exp_ts = data.get("claim_expires_ts")
        if exp_ts:
            try:
                if now_ts() > float(exp_ts):
                    query.answer(popup_prize_expired(), show_alert=True)
                    return
            except Exception:
                pass

        # delivered?
        if is_delivered(uid):
            uname = (data.get("delivered", {}) or {}).get(uid, {}).get("username", "") or winners.get(uid, {}).get("username", "") or "@username"
            try: query.answer(popup_already_delivered(uname, uid), show_alert=True)
            except Exception: pass
            return

        uname = winners.get(uid, {}).get("username", "") or "@username"
        try: query.answer(popup_claim_winner(uname, uid), show_alert=True)
        except Exception: pass
        return

    try:
        query.answer()
    except Exception:
        pass


# =========================================================
# OTHER COMMANDS (BLOCK/RESET minimal)
# =========================================================
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
    if not is_admin(update):
        return
    update.message.reply_text("Use /blocklist to view and edit JSON manually in file if needed.")


def cmd_removeban(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    with lock:
        data["permanent_block"] = {}
        data["old_winners"] = {}
        save_data()
    update.message.reply_text("✅ Ban lists reset done.")


def cmd_blocklist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    perma = data.get("permanent_block", {}) or {}
    oldw = data.get("old_winners", {}) or {}
    update.message.reply_text(
        f"OLD WINNER MODE: {data.get('old_winner_mode','skip')}\n"
        f"Old Winner Block Total: {len(oldw)}\n"
        f"Permanent Block Total: {len(perma)}"
    )


def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    # simple reset
    stop_live_countdown()
    stop_draw_jobs()
    stop_closed_spinner()
    stop_claim_expire_job()
    stop_auto_draw()

    with lock:
        keep_perma = data.get("permanent_block", {})
        keep_verify = data.get("verify_targets", [])
        auto_draw = bool(data.get("auto_draw", False))

        data.clear()
        data.update(fresh_default_data())
        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        data["auto_draw"] = auto_draw
        save_data()
    update.message.reply_text("✅ RESET COMPLETED. Start again with /newgiveaway")


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

    dp.add_handler(CommandHandler("Autodraw", cmd_autodraw))

    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))

    dp.add_handler(CommandHandler("prizeDelivered", cmd_prize_delivered))

    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    dp.add_handler(CommandHandler("reset", cmd_reset))

    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # resume after restart
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    if data.get("closed") and data.get("closed_message_id") and not data.get("winners_message_id"):
        start_closed_spinner(updater.job_queue)

    if data.get("winners_message_id") and data.get("claim_expires_ts"):
        remain = float(data["claim_expires_ts"]) - now_ts()
        if remain > 0:
            schedule_claim_expire(updater.job_queue)

    print("Bot is running (PTB v13 GSM compatible) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
