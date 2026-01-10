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

        "participants": {},  # uid(str) -> {"username": "@x", "name": ""}

        "verify_targets": [],  # [{"ref":"-100.. or @x", "display":".."}]

        "permanent_block": {},  # uid -> {"username": "@x"}

        # old winner protection
        "old_winner_mode": "skip",  # "block" or "skip"
        "old_winners": {},          # uid -> {"username": "@x"} only if mode=block

        # first join champion
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # winners final
        "winners": {},  # uid -> {"username": "@x"}
        "pending_winners_text": "",

        # claim window
        "claim_start_ts": None,
        "claim_expires_ts": None,

        # prize delivery
        "delivered_winners": {},  # uid -> {"username":"", "ts": float}
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


def format_hms(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d} : {m:02d} : {s:02d}"


def build_progress(percent: float) -> str:
    percent = max(0, min(100, percent))
    blocks = 9
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
            "🚫 No fake / duplicate accounts"
        )
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    return "\n".join(lines)


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


def reset_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
        ]]
    )


def winners_delivered_count() -> int:
    return len(data.get("delivered_winners", {}) or {})


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
# POPUPS
# =========================================================
def popup_verify_required() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 VERIFICATION REQUIRED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "To join this giveaway,\n"
        "you must join all required\n"
        "channels/groups first ✅\n\n"
        "After joining all of them,\n"
        "click JOIN GIVEAWAY again."
    )


def popup_old_winner_blocked() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 NOT ALLOWED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "You already won before.\n"
        "Repeat winners are restricted.\n"
        "Please wait for next giveaway."
    )


def popup_first_winner(username: str, uid: str) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✨ CONGRATULATIONS 🌟\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "You joined FIRST and secured\n"
        "the 🥇 1st Winner spot!\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        "📸 Take screenshot & post\n"
        "to confirm."
    )


def popup_already_joined() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 ENTRY UNSUCCESSFUL\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "You already joined.\n"
        "Multiple entries not allowed.\n\n"
        "Please wait for result ⏳"
    )


def popup_join_success(username: str, uid: str) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🌹 CONGRATULATIONS!\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "You’ve successfully joined ✅\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        "Stay until winners announced."
    )


def popup_permanent_blocked() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⛔ PERMANENTLY BLOCKED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "You are blocked from joining.\n"
        f"Contact admin: {ADMIN_CONTACT}"
    )


def popup_claim_winner(username: str, uid: str) -> str:
    return (
        "🌟Congratulations✨\n"
        "You’ve won this giveaway.\n"
        f"👤 {username} | 🆔 {uid}\n\n"
        "📩 Please contact admin:\n"
        f"👉 {ADMIN_CONTACT}"
    )


def popup_claim_not_winner() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "❌ NOT A WINNER\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Your User ID is not in winners list.\n"
        "Please wait next giveaway 🤍"
    )


def popup_prize_expired() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⏳ PRIZE EXPIRED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "24-hour claim time ended.\n"
        "This prize is no longer available."
    )


# =========================================================
# TEXT BUILDERS (GIVEAWAY STYLE)
# Title ONLY (no emoji auto add)
# =========================================================
def build_live_text(remaining: int) -> str:
    duration = data.get("duration_seconds", 1) or 1
    elapsed = duration - remaining
    elapsed = max(0, min(duration, elapsed))
    percent = int(round((elapsed / float(duration)) * 100))
    bar = build_progress(percent)

    title = (data.get("title") or "").strip()

    return (
        "━━━━━━━━━━━━━━━━━━\n"
        f"{title}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"🏆 {data.get('prize','')}\n"
        f"👥 TOTAL PARTICIPANTS: {participants_count()}\n"
        f"🏅 TOTAL WINNERS: {data.get('winner_count',0)}\n"
        "🎯 WINNER SELECTION\n"
        "100% Randomly\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(remaining)}\n"
        "📊 LIVE PROGRESS\n"
        f"{bar} {percent}%\n"
        "📜 RULES\n"
        f"{format_rules()}\n"
        "👇 TAP THE BUTTON\n"
        "BELOW & JOIN NOW 👇"
    )


DOTS = [".", "..", "...", "....", ".....", "......", "......."]


def build_closed_post_text(dots: str = ".......") -> str:
    title = (data.get("title") or "").strip()
    return (
        "━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{title}\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 TOTAL PARTICIPANTS: {participants_count()}\n"
        f"🏅 TOTAL WINNERS: {data.get('winner_count',0)}\n\n"
        f"🎯 Winner selection is in progress{dots}\n"
        "Please wait for official announcement.\n\n"
        f"— {HOST_NAME}"
    )


def build_winners_post_text() -> str:
    with lock:
        winners = data.get("winners", {}) or {}
        delivered = data.get("delivered_winners", {}) or {}
        title = (data.get("title") or "").strip()
        prize = (data.get("prize") or "").strip()

        total = len(winners) if winners else int(data.get("winner_count", 0) or 0)
        delivered_count = len(delivered)

        first_uid = str(data.get("first_winner_id") or "")
        first_info = (winners.get(first_uid, {}) or {})
        first_uname = first_info.get("username", "") or data.get("first_winner_username", "") or "@username"

        other = []
        for uid, info in winners.items():
            if str(uid) == first_uid:
                continue
            other.append((str(uid), (info or {}).get("username", "")))

    lines = []
    lines.append("🏆 GIVEAWAY WINNERS ANNOUNCEMENT 🏆")
    lines.append("")
    lines.append(title)
    lines.append("")
    lines.append(f"🎁 PRIZE: {prize}")
    lines.append(f"📦 Prize Delivery: {delivered_count}/{total}")
    lines.append("")
    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    lines.append(f"👑 {first_uname}")
    lines.append(f"🆔 {first_uid}")
    if first_uid in delivered:
        lines.append("✅ Delivered")
    lines.append("")
    lines.append("👑 OTHER WINNERS (RANDOMLY SELECTED)")
    i = 1
    for uid, uname in other:
        mark = "   ✅ Delivered" if uid in delivered else ""
        if uname:
            lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}{mark}")
        else:
            lines.append(f"{i}️⃣ 👤 User ID: {uid}{mark}")
        i += 1

    lines.append("")
    lines.append("⏳ Claim Rule: Claim within 24 hours. After 24 hours, claim expires.")
    lines.append(f"📢 Hosted By: {HOST_NAME}")
    lines.append("")
    lines.append("👇 Click the button below to claim your prize")
    return "\n".join(lines)


def winners_post_has_claim_open() -> bool:
    exp = data.get("claim_expires_ts")
    if not exp:
        return False
    try:
        return now_ts() < float(exp)
    except Exception:
        return False


def refresh_winners_post(context: CallbackContext):
    with lock:
        mid = data.get("winners_message_id")
        if not mid:
            return
        text = build_winners_post_text()
        rm = claim_button_markup() if winners_post_has_claim_open() else None
    try:
        context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=mid,
            text=text,
            reply_markup=rm
        )
    except Exception:
        pass


# =========================================================
# JOBS: COUNTDOWN / CLOSED ANIM / DRAW / CLAIM EXPIRY
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
            data["active"] = False
            data["closed"] = True
            save_data()

            if live_mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
                except Exception:
                    pass

            try:
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text("......."))
                data["closed_message_id"] = m.message_id
                save_data()
                start_closed_anim(context.job_queue)
            except Exception:
                pass

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


def stop_closed_anim():
    global closed_anim_job
    if closed_anim_job is not None:
        try:
            closed_anim_job.schedule_removal()
        except Exception:
            pass
    closed_anim_job = None


def start_closed_anim(job_queue):
    global closed_anim_job
    stop_closed_anim()
    closed_anim_job = job_queue.run_repeating(
        closed_anim_tick,
        interval=0.7,
        first=0,
        context={"tick": 0},
        name="closed_anim",
    )


def closed_anim_tick(context: CallbackContext):
    if data.get("winners_message_id"):
        stop_closed_anim()
        return
    mid = data.get("closed_message_id")
    if not mid:
        stop_closed_anim()
        return

    ctx = context.job.context or {}
    tick = int(ctx.get("tick", 0)) + 1
    ctx["tick"] = tick
    context.job.context = ctx

    dots = DOTS[(tick - 1) % len(DOTS)]
    try:
        context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=mid,
            text=build_closed_post_text(dots)
        )
    except Exception:
        pass


DRAW_DURATION_SECONDS = 40
DRAW_UPDATE_INTERVAL = 0.7
SPINNER = ["🔄", "🔃", "🔁", "🔂"]


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
        f"{spin} Winner selection is in progress{dots}\n\n"
        "✅ This draw is 100% fair & random.\n"
        "🔐 User ID based selection only.\n\n"
        f"Please wait{dots}"
    )


def start_draw_progress(context: CallbackContext, admin_chat_id: int):
    global draw_job, draw_finalize_job
    stop_draw_jobs()

    msg = context.bot.send_message(
        chat_id=admin_chat_id,
        text=build_draw_progress_text(0, ".", SPINNER[0]),
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

        dots = DOTS[(tick - 1) % len(DOTS)]
        spin = SPINNER[(tick - 1) % len(SPINNER)]

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

        winner_count = int(data.get("winner_count", 1)) or 1
        winner_count = max(1, winner_count)

        first_uid = data.get("first_winner_id")
        if not first_uid:
            first_uid = next(iter(participants.keys()))
            info = participants.get(first_uid, {}) or {}
            data["first_winner_id"] = str(first_uid)
            data["first_winner_username"] = info.get("username", "")
            data["first_winner_name"] = info.get("name", "")
            save_data()

        first_uid = str(data.get("first_winner_id") or "")
        first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

        pool = [uid for uid in participants.keys() if str(uid) != first_uid]
        remaining_needed = max(0, winner_count - 1)
        remaining_needed = min(remaining_needed, len(pool))
        selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

        winners_map = {}
        winners_map[first_uid] = {"username": first_uname}

        for uid in selected:
            info = participants.get(uid, {}) or {}
            winners_map[str(uid)] = {"username": info.get("username", "")}

        data["winners"] = winners_map
        data["pending_winners_text"] = build_winners_post_text()
        save_data()

    try:
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            text=data["pending_winners_text"],
            reply_markup=winners_approve_markup(),
        )
    except Exception:
        context.bot.send_message(
            chat_id=admin_chat_id,
            text=data["pending_winners_text"],
            reply_markup=winners_approve_markup(),
        )


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
# RESET (FULL)
# =========================================================
def stop_all_jobs():
    stop_live_countdown()
    stop_draw_jobs()
    stop_closed_anim()
    stop_claim_expire_job()


def do_full_reset(context: CallbackContext):
    global data
    stop_all_jobs()

    with lock:
        # delete channel messages best effort
        for key in ["live_message_id", "closed_message_id", "winners_message_id"]:
            mid = data.get(key)
            if mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
                except Exception:
                    pass

        keep_perma = data.get("permanent_block", {}) or {}
        keep_verify = data.get("verify_targets", []) or {}

        data = fresh_default_data()
        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
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
            "🚀 Giveaway Engine: READY\n"
            "🔐 Security Level: MAXIMUM\n\n"
            "🧭 Open the Admin Control Panel:\n"
            "/panel\n\n"
            f"⚡ POWERED BY: {HOST_NAME}"
        )
    else:
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{HOST_NAME} Giveaway System\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please join our official channel:\n"
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
        "/endgiveaway\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "📦 DELIVERY SYSTEM\n"
        "/prizedelivery\n\n"
        "🔒 BAN / BLOCK SYSTEM\n"
        "/blockpermanent\n"
        "/unban\n"
        "/blocklist\n"
        "/removeban\n\n"
        "♻️ RESET\n"
        "/reset"
    )


# ---------- VERIFY ----------
def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ ADD VERIFY (CHAT ID / @USERNAME)\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send Chat ID OR @username:\n\n"
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
    lines += [
        "",
        "Send a number to remove that target.",
        "11) Remove ALL verify targets",
    ]
    admin_state = "remove_verify_pick"
    update.message.reply_text("\n".join(lines))


# ---------- GIVEAWAY ----------
def cmd_newgiveaway(update: Update, context: CallbackContext):
    global admin_state, data
    if not is_admin(update):
        return

    stop_all_jobs()

    with lock:
        keep_perma = data.get("permanent_block", {}) or {}
        keep_verify = data.get("verify_targets", []) or []

        data.clear()
        data.update(fresh_default_data())
        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        save_data()

    admin_state = "title"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 NEW GIVEAWAY SETUP\n"
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
        "Are you sure you want to end now?",
        reply_markup=end_confirm_markup()
    )


def cmd_draw(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not data.get("closed"):
        update.message.reply_text("Giveaway is not closed yet.")
        return
    if not data.get("participants", {}):
        update.message.reply_text("No participants to draw winners from.")
        return
    start_draw_progress(context, update.effective_chat.id)


# ---------- DELIVERY ----------
def cmd_prizedelivery(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "prizedelivery_input"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📦 PRIZE DELIVERY MARK\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send Winner User ID (required)\n"
        "Username optional ✅\n\n"
        "Examples:\n"
        "5692210187\n"
        "@MinexxProo | 5692210187"
    )


# ---------- BAN / BLOCK ----------
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


def cmd_removeban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return

    admin_state = "removeban_choose"
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


# ---------- RESET ----------
def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ RESET CONFIRMATION\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Reset will clear current giveaway.\n"
        "Verify targets & permanent bans stay.\n\n"
        "Confirm?",
        reply_markup=reset_confirm_markup()
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
            update.message.reply_text("Invalid input. Send Chat ID like -100... or @username.")
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
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET ADDED\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Added: {ref}\n"
            f"Total: {len(data['verify_targets'])}",
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

    # GIVEAWAY SETUP FLOW
    if admin_state == "title":
        with lock:
            data["title"] = msg  # title only
            save_data()
        admin_state = "prize"
        update.message.reply_text("✅ Title saved!\nNow send Giveaway Prize:")
        return

    if admin_state == "prize":
        with lock:
            data["prize"] = msg
            save_data()
        admin_state = "winners"
        update.message.reply_text("✅ Prize saved!\nNow send Total Winner Count (1 - 1000000):")
        return

    if admin_state == "winners":
        if not msg.isdigit():
            update.message.reply_text("Send number only.")
            return
        count = max(1, min(1000000, int(msg)))
        with lock:
            data["winner_count"] = count
            save_data()
        admin_state = "duration"
        update.message.reply_text("✅ Winner count saved!\nNow send Duration (e.g. 3 Minute):")
        return

    if admin_state == "duration":
        seconds = parse_duration(msg)
        if seconds <= 0:
            update.message.reply_text("Invalid duration. Example: 30 Second / 3 Minute / 1 Hour")
            return
        with lock:
            data["duration_seconds"] = seconds
            save_data()
        admin_state = "old_winner_mode"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 OLD WINNER PROTECTION MODE\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "1️⃣ BLOCK OLD WINNERS\n"
            "• Old winners cannot join\n\n"
            "2️⃣ SKIP OLD WINNERS\n"
            "• Everyone can join\n\n"
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
            update.message.reply_text("✅ Old Winner Mode: SKIP\nNow send Rules (multi-line):")
            return

        with lock:
            data["old_winner_mode"] = "block"
            data["old_winners"] = {}
            save_data()

        admin_state = "old_winner_block_list"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⛔ OLD WINNER BLOCK LIST\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send old winners list (one per line):\n\n"
            "Format:\n"
            "@username | user_id\n"
            "OR\n"
            "user_id"
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
                ow[str(uid)] = {"username": uname}
            data["old_winners"] = ow
            save_data()

        admin_state = "rules"
        update.message.reply_text(f"✅ Old winners added: {len(data['old_winners']) - before}\nNow send Rules (multi-line):")
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        duration = int(data.get("duration_seconds", 0) or 1)
        update.message.reply_text("✅ Rules saved! Showing preview…")
        update.message.reply_text(build_live_text(duration), reply_markup=preview_markup())
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
                perma[str(uid)] = {"username": uname}
            data["permanent_block"] = perma
            save_data()

        admin_state = None
        update.message.reply_text(
            "✅ Permanent block saved!\n"
            f"New Added: {len(data['permanent_block']) - before}\n"
            f"Total Blocked: {len(data['permanent_block'])}"
        )
        return

    # UNBAN INPUTS
    if admin_state == "unban_permanent_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        uid = str(uid)
        with lock:
            perma = data.get("permanent_block", {}) or {}
            if uid in perma:
                del perma[uid]
                data["permanent_block"] = perma
                save_data()
                update.message.reply_text("✅ Unbanned from Permanent Block.")
            else:
                update.message.reply_text("User id not in Permanent Block list.")
        admin_state = None
        return

    if admin_state == "unban_oldwinner_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        uid = str(uid)
        with lock:
            ow = data.get("old_winners", {}) or {}
            if uid in ow:
                del ow[uid]
                data["old_winners"] = ow
                save_data()
                update.message.reply_text("✅ Unbanned from Old Winner Block.")
            else:
                update.message.reply_text("User id not in Old Winner Block list.")
        admin_state = None
        return

    # PRIZE DELIVERY INPUT
    if admin_state == "prizedelivery_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send: user_id OR @username | user_id")
            return
        uid, uname = entries[0]
        uid = str(uid)

        with lock:
            winners = data.get("winners", {}) or {}
            if uid not in winners:
                update.message.reply_text("❌ This User ID is not in winners list.")
                admin_state = None
                return

            delivered = data.get("delivered_winners", {}) or {}
            if uid in delivered:
                update.message.reply_text("✅ Already delivered for this user.")
                admin_state = None
                return

            delivered[uid] = {"username": uname or "", "ts": now_ts()}
            data["delivered_winners"] = delivered

            # save username if winners empty
            if uname and not (winners.get(uid, {}) or {}).get("username"):
                winners[uid] = {"username": uname}
                data["winners"] = winners

            save_data()

        refresh_winners_post(context)
        update.message.reply_text(f"✅ Delivered marked!\n🆔 {uid}")
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
            query.edit_message_text("✅ Verify setup completed.")
        except Exception:
            pass
        return

    # Preview actions
    if qd.startswith("preview_"):
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return

        if qd == "preview_approve":
            query.answer()
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
                    data["delivered_winners"] = {}

                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""

                    data["claim_start_ts"] = None
                    data["claim_expires_ts"] = None

                    save_data()

                stop_closed_anim()
                start_live_countdown(context.job_queue)
                query.edit_message_text("✅ Giveaway approved & posted!")
            except Exception as e:
                query.edit_message_text(f"Failed to post in channel.\nError: {e}")
            return

        if qd == "preview_reject":
            query.answer()
            query.edit_message_text("❌ Giveaway rejected.")
            return

        if qd == "preview_edit":
            query.answer()
            query.edit_message_text("✏️ Edit Mode\nStart again with /newgiveaway")
            return

    # End giveaway confirm/cancel
    if qd == "end_confirm":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()

        with lock:
            if not data.get("active"):
                try:
                    query.edit_message_text("No active giveaway.")
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
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text("......."))
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
            start_closed_anim(context.job_queue)
        except Exception:
            pass

        stop_live_countdown()
        try:
            query.edit_message_text("✅ Closed! Now use /draw")
        except Exception:
            pass
        return

    if qd == "end_cancel":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        try:
            query.edit_message_text("❌ Cancelled. Giveaway still running.")
        except Exception:
            pass
        return

    # Reset confirm/cancel
    if qd == "reset_confirm":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        do_full_reset(context)
        try:
            query.edit_message_text("✅ Reset completed. Start again with /newgiveaway")
        except Exception:
            pass
        return

    if qd == "reset_cancel":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        try:
            query.edit_message_text("❌ Reset cancelled.")
        except Exception:
            pass
        return

    # Unban choose
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

    # removeban choose
    if qd in ("reset_permanent_ban", "reset_oldwinner_ban"):
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()

        if qd == "reset_permanent_ban":
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm Reset Permanent", callback_data="confirm_reset_permanent"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
                ]]
            )
            try:
                query.edit_message_text("Confirm reset Permanent Ban List?", reply_markup=kb)
            except Exception:
                pass
            return

        if qd == "reset_oldwinner_ban":
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm Reset Old Winner", callback_data="confirm_reset_oldwinner"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
                ]]
            )
            try:
                query.edit_message_text("Confirm reset Old Winner Ban List?", reply_markup=kb)
            except Exception:
                pass
            return

    if qd == "cancel_reset_ban":
        query.answer()
        admin_state = None
        try:
            query.edit_message_text("Cancelled.")
        except Exception:
            pass
        return

    if qd == "confirm_reset_permanent":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        with lock:
            data["permanent_block"] = {}
            save_data()
        try:
            query.edit_message_text("✅ Permanent Ban List reset.")
        except Exception:
            pass
        return

    if qd == "confirm_reset_oldwinner":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        with lock:
            data["old_winners"] = {}
            save_data()
        try:
            query.edit_message_text("✅ Old Winner Ban List reset.")
        except Exception:
            pass
        return

    # Join giveaway
    if qd == "join_giveaway":
        if not data.get("active"):
            query.answer("This giveaway is not active right now.", show_alert=True)
            return

        if not verify_user_join(context.bot, int(uid)):
            query.answer(popup_verify_required(), show_alert=True)
            return

        if uid in (data.get("permanent_block", {}) or {}):
            query.answer(popup_permanent_blocked(), show_alert=True)
            return

        if data.get("old_winner_mode") == "block":
            if uid in (data.get("old_winners", {}) or {}):
                query.answer(popup_old_winner_blocked(), show_alert=True)
                return

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

        # update live post instantly
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

        if str(data.get("first_winner_id")) == uid:
            query.answer(popup_first_winner(uname or "@username", uid), show_alert=True)
        else:
            query.answer(popup_join_success(uname or "@Username", uid), show_alert=True)
        return

    # Winners Approve/Reject
    if qd == "winners_approve":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()

        text = (data.get("pending_winners_text") or "").strip()
        if not text:
            try:
                query.edit_message_text("No pending winners found.")
            except Exception:
                pass
            return

        stop_closed_anim()

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

                ts = now_ts()
                data["claim_start_ts"] = ts
                data["claim_expires_ts"] = ts + 24 * 3600
                save_data()

            schedule_claim_expire(context.job_queue)
            query.edit_message_text("✅ Winners posted to channel.")
        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post winners: {e}")
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
            save_data()
        try:
            query.edit_message_text("❌ Winners rejected.")
        except Exception:
            pass
        return

    # Claim prize
    if qd == "claim_prize":
        winners = data.get("winners", {}) or {}
        if uid not in winners:
            query.answer(popup_claim_not_winner(), show_alert=True)
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
        query.answer(popup_claim_winner(uname, uid), show_alert=True)
        return

    query.answer()


# =========================================================
# MAIN
# =========================================================
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing in .env")

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # basic
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

    # delivery
    dp.add_handler(CommandHandler("prizedelivery", cmd_prizedelivery))

    # bans
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # reset
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # handlers
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # Resume after restart
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    if data.get("closed") and data.get("closed_message_id") and not data.get("winners_message_id"):
        start_closed_anim(updater.job_queue)

    if data.get("winners_message_id") and data.get("claim_expires_ts"):
        remain = float(data["claim_expires_ts"]) - now_ts()
        if remain > 0:
            schedule_claim_expire(updater.job_queue)

    print("Bot is running (PTB 13.15, FULL A-Z, GSM hosting friendly) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
