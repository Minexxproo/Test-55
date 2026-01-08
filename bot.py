import os
import json
import re
import random
import threading
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler,
    MessageHandler, Filters, CallbackContext
)

# =========================
# ENV
# =========================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))  # -100xxxx

HOST_NAME = os.getenv("HOST_NAME", "POWER POINT BREAK").strip()
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@MinexxProo").strip()
DATA_FILE = os.getenv("DATA_FILE", "giveaway_data.json").strip()

BD_TZ = timezone(timedelta(hours=6))

# =========================
# SPEED SETTINGS
# =========================
LIVE_UPDATE_INTERVAL = 5
DRAW_UPDATE_INTERVAL = 5
MANUAL_DRAW_SECONDS = 40
AUTO_DRAW_SECONDS = 120

SPINNER = ["🔄", "🔃"]  # you wanted this
DOTS = ["........"]     # you wanted "Please wait........" style only

MAX_VERIFY_TARGETS = 10
lock = threading.RLock()

# =========================
# DATA
# =========================
def fresh_default_data():
    return {
        "active": False,
        "closed": False,

        # admin-configured
        "title": "",
        "prize": "",
        "winner_count": 0,
        "duration_seconds": 0,
        "rules": "",

        # runtime
        "start_time": None,
        "live_message_id": None,
        "closed_message_id": None,
        "winners_message_id": None,

        "participants": {},  # uid(str)->{"username":"@x","name":""}
        "verify_targets": [],  # [{"ref":"-100.. or @..","display":".."}]

        # blocks
        "permanent_block": {},  # uid -> {"username":"@x"}
        "old_winners": {},      # uid -> {"username":"@x"} (ALWAYS enforced if exists)

        # first winner
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # current giveaway winners map
        "winners": {},  # uid -> {"username":"@x"}

        # pending winners preview
        "pending_winners_text": "",

        # auto winner post
        "auto_winner_post": False,

        # history
        "winner_history": []  # list of {"date_bd": "...", "prize": "...", "winners": [{"uid":"..","username":"@.."}]}
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
            json.dump(data, f, indent=2, ensure_ascii=False)

data = load_data()

# =========================
# HELPERS
# =========================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def user_tag(username: str) -> str:
    if not username:
        return ""
    u = username.strip()
    if not u:
        return ""
    return u if u.startswith("@") else "@" + u

def now_bd_str():
    return datetime.now(BD_TZ).strftime("%d/%m/%Y %I:%M %p")

def participants_count() -> int:
    return len(data.get("participants", {}) or {})

def format_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d} : {m:02d} : {s:02d}"

def build_progress_bar(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    total = 10
    filled = int(round(total * percent / 100.0))
    empty = total - filled
    return "▰" * filled + "▱" * empty

def parse_duration(text: str) -> int:
    t = (text or "").strip().lower()
    if not t:
        return 0
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
        slug = slug.split("?", 1)[0].split("/", 1)[0]
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
    Accept multi-lines:
    @name | 123456
    123456
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

def extract_winners_from_text(text: str):
    """
    Extract @username and user id patterns from winners announcement text.
    Returns list of dicts: [{"uid":"..","username":"@.."}]
    """
    if not text:
        return []

    winners = []
    # patterns like: 👑 Username: @FirstWinner OR 1️⃣ 👤 @WinnerOne  | 🆔 987...
    # we'll capture @xxx and first numeric after 🆔
    lines = text.splitlines()
    for ln in lines:
        uid_match = re.search(r"🆔\s*([0-9]{5,})", ln)
        if uid_match:
            uid = uid_match.group(1).strip()
            uname_match = re.search(r"@[\w\d_]+", ln)
            uname = uname_match.group(0) if uname_match else "N/A"
            winners.append({"uid": uid, "username": uname})
        else:
            # handle "👑 Username: @X" lines (id may be in next line)
            pass

    # remove duplicates by uid
    seen = set()
    uniq = []
    for w in winners:
        if w["uid"] not in seen:
            seen.add(w["uid"])
            uniq.append(w)
    return uniq

# =========================
# POPUPS (ENGLISH) - SAME FEEL
# =========================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n"
        "Join all required channels/groups first,\n"
        "then tap JOIN GIVEAWAY again."
    )

def popup_old_winner_blocked() -> str:
    return (
        "🚫 You have already won a previous giveaway.\n"
        "To keep the giveaway fair for everyone,\n"
        "repeat winners are restricted from participating.\n"
        "Please wait for the next Giveaway."
    )

def popup_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n"
        "You are permanently blocked from giveaways.\n"
        f"Contact Admin: {ADMIN_CONTACT}"
    )

def popup_already_joined() -> str:
    return (
        "🚫 ENTRY UNSUCCESSFUL\n\n"
        "You’ve already joined this giveaway.\n"
        "Only one entry is allowed."
    )

def popup_first_winner(username: str, uid: str) -> str:
    return (
        "✨CONGRATULATIONS✨\n"
        "You joined the giveaway FIRST and secured the 🥇 1st Winner spot!\n"
        f"👤 {username} | 🆔 {uid}\n"
        "📸 Screenshot & post in the group to confirm."
    )

def popup_join_success(username: str, uid: str) -> str:
    return (
        "✅ ENTRY CONFIRMED\n"
        "You successfully joined the giveaway!\n"
        f"👤 {username}\n"
        f"🆔 {uid}"
    )

def popup_claim_winner(username: str, uid: str) -> str:
    return (
        "🎉 YOU WON!\n"
        f"👤 {username} | 🆔 {uid}\n\n"
        f"Contact Admin: {ADMIN_CONTACT}"
    )

def popup_not_winner() -> str:
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry🥺! Your User ID is not in the winners list.\n"
        "Please wait for the next giveaway❤️‍🩹"
    )

# =========================
# BUTTONS
# =========================
def join_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data="join_giveaway")]]
    )

def claim_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data="claim_prize")]]
    )

def preview_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve & Post", callback_data="preview_approve"),
                InlineKeyboardButton("❌ Reject Giveaway", callback_data="preview_reject"),
            ],
            [InlineKeyboardButton("✏️ Edit Again", callback_data="preview_edit")],
        ]
    )

def auto_winner_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Auto Post ON", callback_data="auto_on"),
            InlineKeyboardButton("❌ Auto Post OFF", callback_data="auto_off"),
        ]]
    )

def winners_approve_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data="winners_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="winners_reject"),
        ]]
    )

def reset_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
        ]]
    )

# =========================
# TEXT BUILDERS (SAME DESIGN)
# =========================
def rules_text() -> str:
    rules = (data.get("rules") or "").strip()
    if not rules:
        return "✅ Must join official channel\n❌ One account per user\n🚫 No fake / duplicate accounts"
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    return "\n".join(lines)

def build_preview_text() -> str:
    remaining = data.get("duration_seconds", 0)
    bar = build_progress_bar(0)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{data.get('title','')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL✨\n"
        f"{data.get('prize','')}\n\n"
        "👥 TOTAL PARTICIPANTS: 0\n"
        f"🏅 TOTAL WINNERS: {data.get('winner_count',0)}\n"
        "🎯 WINNER SELECTION: 100% Random & Fair\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(remaining)}\n\n"
        f"📊 LIVE PROGRESS  {bar} 0%\n\n"
        "📜 RULES....\n"
        f"{rules_text()}\n\n"
        f"📢 HOSTED BY⚡️ {HOST_NAME}\n\n"
        "👇 READY TO WIN?\n"
        "👇✨ TAP THE BUTTON BELOW & JOIN NOW 👇"
    )

def build_live_text(remaining: int) -> str:
    duration = max(1, int(data.get("duration_seconds", 1) or 1))
    elapsed = duration - remaining
    elapsed = max(0, min(duration, elapsed))
    percent = int(round((elapsed / float(duration)) * 100))
    bar = build_progress_bar(percent)

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{data.get('title','')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL✨\n"
        f"{data.get('prize','')}\n\n"
        f"👥 TOTAL PARTICIPANTS: {participants_count()}\n"
        f"🏅 TOTAL WINNERS: {data.get('winner_count',0)}\n"
        "🎯 WINNER SELECTION: 100% Random & Fair\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(remaining)}\n\n"
        f"📊 LIVE PROGRESS  {bar} {percent}%\n\n"
        "📜 RULES....\n"
        f"{rules_text()}\n\n"
        f"📢 HOSTED BY⚡️ {HOST_NAME}\n\n"
        "👇 READY TO WIN?\n"
        "👇✨ TAP THE BUTTON BELOW & JOIN NOW 👇"
    )

def build_closed_text_aggressive() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⛔ TIME IS UP. ENTRY IS LOCKED.\n"
        "🔒 NO MORE JOINS ACCEPTED.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        "🏆 Winner selection is in progress.\n"
        "Please wait for the official announcement.\n\n"
        f"— {HOST_NAME} ⚡"
    )

def build_winners_post_text(first_uid: str, first_uname: str, random_winners: list) -> str:
    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆✨ GIVEAWAY WINNERS ANNOUNCEMENT ✨🏆")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("🎉 The wait is over!  ")
    lines.append("Here are the official winners of today’s giveaway 👇")
    lines.append("")
    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    lines.append(f"👑 Username: {first_uname if first_uname else 'N/A'}")
    lines.append(f"🆔 User ID: {first_uid}")
    lines.append("⚡ Secured instantly by joining first")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("👑 OTHER WINNERS (RANDOMLY SELECTED)")
    lines.append("")
    i = 1
    for uid, uname in random_winners:
        u = uname if uname else "N/A"
        lines.append(f"{i}️⃣ 👤 {u}  | 🆔 {uid}  ")
        i += 1
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("✅ This giveaway was completed using a  ")
    lines.append("100% fair & transparent random system.  ")
    lines.append("🔐 User ID based selection only.")
    lines.append("")
    lines.append("⏰ Important:")
    lines.append("🎁 Winners must claim their prize within **24 hours**.  ")
    lines.append("❌ Unclaimed prizes will automatically expire.")
    lines.append("")
    lines.append(f"📢 Hosted By: ⚡ {HOST_NAME}  ")
    lines.append("👇 Tap the button below to claim your prize 👇")
    return "\n".join(lines)

# =========================
# JOBS (LIVE + DRAW + RESET)
# =========================
countdown_job = None
draw_job = None
draw_finalize_job = None
reset_job = None
reset_finalize_job = None

def stop_job(j):
    if j is None:
        return
    try:
        j.schedule_removal()
    except Exception:
        pass

def start_live_countdown(job_queue):
    global countdown_job
    stop_job(countdown_job)
    countdown_job = job_queue.run_repeating(live_tick, interval=LIVE_UPDATE_INTERVAL, first=0)

def live_tick(context: CallbackContext):
    global data
    with lock:
        if not data.get("active"):
            stop_job(globals().get("countdown_job"))
            globals()["countdown_job"] = None
            return

        if data.get("start_time") is None:
            data["start_time"] = datetime.utcnow().timestamp()
            save_data()

        start_ts = float(data["start_time"])
        duration = max(1, int(data.get("duration_seconds", 1) or 1))
        elapsed = int(datetime.utcnow().timestamp() - start_ts)
        remaining = duration - elapsed
        live_mid = data.get("live_message_id")

    if remaining <= 0:
        with lock:
            data["active"] = False
            data["closed"] = True
            save_data()

        if live_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception:
                pass

        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_text_aggressive())
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
        except Exception:
            pass

        if data.get("auto_winner_post"):
            start_draw_progress(context, CHANNEL_ID, AUTO_DRAW_SECONDS, pin_message=True, auto_post=True)
        else:
            try:
                context.bot.send_message(chat_id=ADMIN_ID, text="✅ Giveaway closed. Use /draw to select winners.")
            except Exception:
                pass

        stop_job(globals().get("countdown_job"))
        globals()["countdown_job"] = None
        return

    if live_mid:
        try:
            context.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=live_mid,
                text=build_live_text(remaining),
                reply_markup=join_button_markup(),
            )
        except Exception:
            pass

# -------- DRAW (spinner at marked line, dots only at end) --------
def build_draw_text(percent: int, spinner: str) -> str:
    bar = build_progress_bar(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 Selecting winners... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        f"{spinner} Winner selection is in progress.\n\n"
        "✅ This draw is 100% fair & random.\n"
        "🔐 User ID based selection only.\n\n"
        f"Please wait{DOTS[0]}"
    )

def stop_draw_jobs():
    global draw_job, draw_finalize_job
    stop_job(draw_job); stop_job(draw_finalize_job)
    draw_job = None; draw_finalize_job = None

def start_draw_progress(context: CallbackContext, chat_id: int, total_seconds: int, pin_message: bool=False, auto_post: bool=False):
    global draw_job, draw_finalize_job
    stop_draw_jobs()

    msg = context.bot.send_message(chat_id=chat_id, text=build_draw_text(0, SPINNER[0]))
    if pin_message:
        try:
            context.bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id, disable_notification=True)
        except Exception:
            pass

    ctx = {
        "chat_id": chat_id,
        "msg_id": msg.message_id,
        "start_ts": datetime.utcnow().timestamp(),
        "tick": 0,
        "total_seconds": int(total_seconds),
        "auto_post": bool(auto_post),
    }

    def draw_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] += 1
        elapsed = int(datetime.utcnow().timestamp() - jd["start_ts"])
        percent = int(round(min(100, (elapsed / float(jd["total_seconds"])) * 100)))
        spinner = SPINNER[jd["tick"] % len(SPINNER)]
        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["msg_id"],
                text=build_draw_text(percent, spinner),
            )
        except Exception:
            pass

    draw_job = context.job_queue.run_repeating(draw_tick, interval=DRAW_UPDATE_INTERVAL, first=0, context=ctx)
    draw_finalize_job = context.job_queue.run_once(draw_finalize, when=int(total_seconds), context=ctx)

def draw_finalize(context: CallbackContext):
    global data
    stop_draw_jobs()

    jd = context.job.context
    chat_id = jd["chat_id"]
    msg_id = jd["msg_id"]
    auto_post = jd.get("auto_post", False)

    with lock:
        participants = data.get("participants", {}) or {}
        if not participants:
            try:
                context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="No participants found.")
            except Exception:
                pass
            return

        winner_count = max(1, int(data.get("winner_count", 1) or 1))

        first_uid = data.get("first_winner_id")
        if not first_uid:
            first_uid = next(iter(participants.keys()))
            info = participants.get(first_uid, {}) or {}
            data["first_winner_id"] = first_uid
            data["first_winner_username"] = info.get("username", "")
            data["first_winner_name"] = info.get("name", "")

        first_uname = data.get("first_winner_username") or (participants.get(first_uid, {}) or {}).get("username", "")

        pool = [uid for uid in participants.keys() if uid != first_uid]
        need = max(0, winner_count - 1)
        need = min(need, len(pool))
        picked = random.sample(pool, need) if need > 0 else []

        winners_map = {first_uid: {"username": first_uname}}
        random_list = []
        for uid in picked:
            info = participants.get(uid, {}) or {}
            winners_map[uid] = {"username": info.get("username", "")}
            random_list.append((uid, info.get("username", "")))

        data["winners"] = winners_map
        pending = build_winners_post_text(first_uid, first_uname, random_list)
        data["pending_winners_text"] = pending
        save_data()

    if auto_post:
        # delete closed post if exists
        closed_mid = data.get("closed_message_id")
        if closed_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception:
                pass

        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=pending, reply_markup=claim_button_markup())
            with lock:
                data["winners_message_id"] = m.message_id
                data["closed_message_id"] = None
                data["auto_winner_post"] = False  # auto OFF after post
                save_data()
        except Exception:
            pass

        # store history + old winners auto
        store_winner_history_from_text(pending)

        try:
            context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

        try:
            context.bot.send_message(chat_id=ADMIN_ID, text="✅ Auto winner selection completed and posted to channel.")
        except Exception:
            pass
        return

    # manual approve/reject in admin chat
    try:
        context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=pending, reply_markup=winners_approve_markup())
    except Exception:
        context.bot.send_message(chat_id=chat_id, text=pending, reply_markup=winners_approve_markup())

# -------- RESET (40s, all reset = ALL) --------
def stop_reset_jobs():
    global reset_job, reset_finalize_job
    stop_job(reset_job); stop_job(reset_finalize_job)
    reset_job = None; reset_finalize_job = None

def build_reset_text(percent: int, spinner: str) -> str:
    bar = build_progress_bar(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ RESET IN PROGRESS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{spinner} Resetting system...\n"
        f"📊 Progress: {bar} {percent}%\n\n"
        "Please wait..."
    )

def start_reset_progress(context: CallbackContext, chat_id: int, msg_id: int):
    global reset_job, reset_finalize_job
    stop_reset_jobs()

    ctx = {"chat_id": chat_id, "msg_id": msg_id, "start_ts": datetime.utcnow().timestamp(), "tick": 0, "total": 40}

    def reset_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] += 1
        elapsed = int(datetime.utcnow().timestamp() - jd["start_ts"])
        percent = int(round(min(100, (elapsed / float(jd["total"])) * 100)))
        spinner = SPINNER[jd["tick"] % len(SPINNER)]
        try:
            job_ctx.bot.edit_message_text(chat_id=jd["chat_id"], message_id=jd["msg_id"], text=build_reset_text(percent, spinner))
        except Exception:
            pass

    reset_job = context.job_queue.run_repeating(reset_tick, interval=5, first=0, context=ctx)
    reset_finalize_job = context.job_queue.run_once(reset_finalize, when=40, context=ctx)

def reset_finalize(context: CallbackContext):
    global data
    stop_reset_jobs()

    jd = context.job.context
    chat_id = jd["chat_id"]
    msg_id = jd["msg_id"]

    try:
        for key in ["live_message_id", "closed_message_id", "winners_message_id"]:
            mid = data.get(key)
            if mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
                except Exception:
                    pass
    except Exception:
        pass

    with lock:
        data = fresh_default_data()  # ALL reset means ALL
        save_data()

    try:
        context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="✅ RESET COMPLETED SUCCESSFULLY!\n\nUse /newgiveaway to start again.")
    except Exception:
        pass

# =========================
# AUTO SAVE WINNER HISTORY (from channel post or bot post)
# =========================
def store_winner_history_from_text(text: str):
    global data
    winners = extract_winners_from_text(text)
    if not winners:
        return

    with lock:
        # save history
        data["winner_history"].append({
            "date_bd": now_bd_str(),
            "prize": data.get("prize", ""),
            "winners": winners
        })

        # auto add to old_winners (repeat winner restriction)
        ow = data.get("old_winners", {}) or {}
        for w in winners:
            uid = w["uid"]
            ow[uid] = {"username": w.get("username", "N/A")}
        data["old_winners"] = ow
        save_data()

# =========================
# COMMANDS
# =========================
admin_state = None

def cmd_start(update: Update, context: CallbackContext):
    if update.effective_user and is_admin(update.effective_user.id):
        update.message.reply_text("✅ Admin ready.\nUse /panel")
    else:
        update.message.reply_text(f"⚡ {HOST_NAME} Giveaway Bot")

def cmd_panel(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        return
    update.message.reply_text(
        "ADMIN PANEL\n\n"
        "/newgiveaway\n"
        "/addverifylink\n"
        "/removeverifylink\n"
        "/blockoldwinner\n"
        "/blockpermanent\n"
        "/unban\n"
        "/blocklist\n"
        "/participants\n"
        "/winnerlist\n"
        "/draw\n"
        "/autowinnerpost\n"
        "/reset\n"
    )

def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        return
    status = "ON ✅" if data.get("auto_winner_post") else "OFF ❌"
    update.message.reply_text(f"Auto Winner Post setting: {status}\n\nChoose:", reply_markup=auto_winner_markup())

def cmd_newgiveaway(update: Update, context: CallbackContext):
    global admin_state, data
    if not is_admin(update.effective_user.id):
        return

    with lock:
        # keep block lists + history (safe)
        keep_perma = data.get("permanent_block", {}) or {}
        keep_old = data.get("old_winners", {}) or {}
        keep_hist = data.get("winner_history", []) or []
        keep_verify = data.get("verify_targets", []) or []

        data = fresh_default_data()
        data["permanent_block"] = keep_perma
        data["old_winners"] = keep_old
        data["winner_history"] = keep_hist
        data["verify_targets"] = keep_verify
        save_data()

    admin_state = "title"
    update.message.reply_text("STEP 1 — Send Giveaway Title:")

def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update.effective_user.id):
        return
    admin_state = "add_verify"
    update.message.reply_text(f"Send verify target (Chat ID -100... OR @username)\nMax {MAX_VERIFY_TARGETS} targets.")

def cmd_removeverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update.effective_user.id):
        return

    targets = data.get("verify_targets", []) or []
    if not targets:
        update.message.reply_text("No verify targets set.")
        return

    lines = ["Verify targets:"]
    for i, t in enumerate(targets, start=1):
        lines.append(f"{i}) {t.get('display','')}")
    lines.append("\nSend number to remove. Send 99 to remove ALL.")
    admin_state = "remove_verify"
    update.message.reply_text("\n".join(lines))

def cmd_blockoldwinner(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update.effective_user.id):
        return
    admin_state = "block_old_list"
    update.message.reply_text("Send Old Winner block list (multi-line).\nFormat:\n@username | userid\nOR\nuserid")

def cmd_blockpermanent(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update.effective_user.id):
        return
    admin_state = "block_perma_list"
    update.message.reply_text("Send Permanent block list (multi-line).\n@username | userid\nOR\nuserid")

def cmd_unban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update.effective_user.id):
        return
    admin_state = "unban_choose"
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Unban Permanent", callback_data="unban_permanent"),
            InlineKeyboardButton("Unban Old Winner", callback_data="unban_old"),
        ]]
    )
    update.message.reply_text("Choose unban type:", reply_markup=kb)

def cmd_blocklist(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        return
    perma = data.get("permanent_block", {}) or {}
    oldw = data.get("old_winners", {}) or {}
    lines = []
    lines.append("BAN LISTS\n")
    lines.append(f"OLD WINNER BLOCK: {len(oldw)}")
    for uid, info in oldw.items():
        u = (info or {}).get("username", "")
        lines.append(f"- {u+' | ' if u else ''}{uid}")
    lines.append("")
    lines.append(f"PERMANENT BLOCK: {len(perma)}")
    for uid, info in perma.items():
        u = (info or {}).get("username", "")
        lines.append(f"- {u+' | ' if u else ''}{uid}")
    update.message.reply_text("\n".join(lines))

def cmd_participants(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        return
    parts = data.get("participants", {}) or {}
    if not parts:
        update.message.reply_text("Participants list is empty.")
        return
    lines = [f"Total Participants: {len(parts)}", ""]
    i = 1
    for uid, info in parts.items():
        u = (info or {}).get("username", "")
        lines.append(f"{i}. {u+' | ' if u else ''}{uid}")
        i += 1
    update.message.reply_text("\n".join(lines))

def cmd_winnerlist(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        return
    hist = data.get("winner_history", []) or []
    if not hist:
        update.message.reply_text("Winner history is empty.")
        return

    lines = []
    lines.append("WINNER LIST (HISTORY)\n")
    idx = 1
    for entry in hist[-20:]:  # last 20
        lines.append(f"{idx}) Date: {entry.get('date_bd','')}")
        lines.append(f"Prize: {entry.get('prize','')}")
        winners = entry.get("winners", []) or []
        for w in winners:
            lines.append(f"- {w.get('username','N/A')} | {w.get('uid','')}")
        lines.append("")
        idx += 1
    update.message.reply_text("\n".join(lines))

def cmd_draw(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        return
    if not data.get("closed"):
        update.message.reply_text("Giveaway is not closed yet.")
        return
    if not (data.get("participants", {}) or {}):
        update.message.reply_text("No participants to draw from.")
        return
    start_draw_progress(context, update.effective_chat.id, MANUAL_DRAW_SECONDS, pin_message=False, auto_post=False)

def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        return
    update.message.reply_text("Confirm FULL reset? (ALL means ALL)", reply_markup=reset_confirm_markup())

# =========================
# ADMIN TEXT FLOW (SETUP + LIST INPUT)
# =========================
def admin_text_handler(update: Update, context: CallbackContext):
    global admin_state, data
    if not is_admin(update.effective_user.id):
        return
    if not admin_state:
        return

    msg = (update.message.text or "").strip()
    if not msg:
        return

    if admin_state == "add_verify":
        ref = normalize_verify_ref(msg)
        if not ref:
            update.message.reply_text("Invalid. Send -100... or @username.")
            return
        with lock:
            targets = data.get("verify_targets", []) or []
            if len(targets) >= MAX_VERIFY_TARGETS:
                update.message.reply_text(f"Max verify targets reached ({MAX_VERIFY_TARGETS}).")
                admin_state = None
                return
            targets.append({"ref": ref, "display": ref})
            data["verify_targets"] = targets
            save_data()
        update.message.reply_text(f"✅ Added verify target: {ref}\nTotal: {len(data['verify_targets'])}")
        admin_state = None
        return

    if admin_state == "remove_verify":
        if not msg.isdigit():
            update.message.reply_text("Send a number.")
            return
        n = int(msg)
        with lock:
            targets = data.get("verify_targets", []) or []
            if n == 99:
                data["verify_targets"] = []
                save_data()
                update.message.reply_text("✅ All verify targets removed.")
                admin_state = None
                return
            if n < 1 or n > len(targets):
                update.message.reply_text("Invalid number.")
                return
            removed = targets.pop(n - 1)
            data["verify_targets"] = targets
            save_data()
        update.message.reply_text(f"✅ Removed: {removed.get('display','')}")
        admin_state = None
        return

    if admin_state == "block_old_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list.")
            return
        with lock:
            ow = data.get("old_winners", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[uid] = {"username": uname}
            data["old_winners"] = ow
            save_data()
        update.message.reply_text(f"✅ Old Winner Block updated. Added: {len(data['old_winners']) - before}")
        admin_state = None
        return

    if admin_state == "block_perma_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list.")
            return
        with lock:
            pb = data.get("permanent_block", {}) or {}
            before = len(pb)
            for uid, uname in entries:
                pb[uid] = {"username": uname}
            data["permanent_block"] = pb
            save_data()
        update.message.reply_text(f"✅ Permanent Block updated. Added: {len(data['permanent_block']) - before}")
        admin_state = None
        return

    # setup flow
    if admin_state == "title":
        with lock:
            data["title"] = msg  # no extra emoji added (your rule)
            save_data()
        admin_state = "prize"
        update.message.reply_text("STEP 2 — Send Prize Text (multi-line allowed):")
        return

    if admin_state == "prize":
        with lock:
            data["prize"] = msg
            save_data()
        admin_state = "winners"
        update.message.reply_text("STEP 3 — Send Total Winners (number):")
        return

    if admin_state == "winners":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number.")
            return
        with lock:
            data["winner_count"] = max(1, min(1000000, int(msg)))
            save_data()
        admin_state = "duration"
        update.message.reply_text("STEP 4 — Send Duration (example: 3 Minute / 30 Second / 1 Hour):")
        return

    if admin_state == "duration":
        seconds = parse_duration(msg)
        if seconds <= 0:
            update.message.reply_text("Invalid duration.")
            return
        with lock:
            data["duration_seconds"] = seconds
            save_data()
        admin_state = "rules"
        update.message.reply_text("STEP 5 — Send Rules (multi-line):")
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        update.message.reply_text("✅ Preview below:")
        update.message.reply_text(build_preview_text(), reply_markup=preview_markup())
        return

# =========================
# CALLBACKS (MAIN FIX: ADMIN BYPASS)
# =========================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state, data
    query = update.callback_query
    qd = query.data
    uid_int = query.from_user.id
    uid = str(uid_int)

    try:
        query.answer()
    except Exception:
        pass

    # ✅ ADMIN BYPASS (no block popups ever for admin)
    if is_admin(uid_int):
        if qd == "auto_on":
            with lock:
                data["auto_winner_post"] = True
                save_data()
            try:
                query.edit_message_text("✅ Auto Post ON")
            except Exception:
                pass
            return

        if qd == "auto_off":
            with lock:
                data["auto_winner_post"] = False
                save_data()
            try:
                query.edit_message_text("❌ Auto Post OFF")
            except Exception:
                pass
            return

        if qd == "preview_approve":
            try:
                duration = max(1, int(data.get("duration_seconds", 1) or 1))
                m = context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=build_live_text(duration),
                    reply_markup=join_button_markup(),
                )
                with lock:
                    data["live_message_id"] = m.message_id
                    data["active"] = True
                    data["closed"] = False
                    data["start_time"] = datetime.utcnow().timestamp()

                    # reset giveaway runtime (but keep blocks+verify+history)
                    data["participants"] = {}
                    data["winners"] = {}
                    data["pending_winners_text"] = ""
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""
                    data["closed_message_id"] = None
                    data["winners_message_id"] = None
                    save_data()

                start_live_countdown(context.job_queue)

                try:
                    query.edit_message_text("✅ Posted to channel.")
                except Exception:
                    pass
            except Exception as e:
                try:
                    query.edit_message_text(f"Failed. Bot must be admin in channel.\nError: {e}")
                except Exception:
                    pass
            return

        if qd == "preview_reject":
            try:
                query.edit_message_text("❌ Rejected.")
            except Exception:
                pass
            return

        if qd == "preview_edit":
            try:
                query.edit_message_text("✏️ Run /newgiveaway again to edit.")
            except Exception:
                pass
            return

        if qd == "winners_approve":
            text = (data.get("pending_winners_text") or "").strip()
            if not text:
                try:
                    query.edit_message_text("No pending winners found.")
                except Exception:
                    pass
                return

            closed_mid = data.get("closed_message_id")
            if closed_mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
                except Exception:
                    pass

            try:
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=text, reply_markup=claim_button_markup())
                with lock:
                    data["winners_message_id"] = m.message_id
                    data["closed_message_id"] = None
                    save_data()
            except Exception:
                pass

            # store history + old winners auto
            store_winner_history_from_text(text)

            try:
                query.edit_message_text("✅ Winners posted to channel.")
            except Exception:
                pass
            return

        if qd == "winners_reject":
            with lock:
                data["pending_winners_text"] = ""
                save_data()
            try:
                query.edit_message_text("❌ Rejected.")
            except Exception:
                pass
            return

        if qd == "reset_confirm":
            start_reset_progress(context, query.message.chat_id, query.message.message_id)
            return

        if qd == "reset_cancel":
            try:
                query.edit_message_text("❌ Cancelled.")
            except Exception:
                pass
            return

        if qd == "unban_permanent":
            admin_state = "unban_permanent_input"
            try:
                query.edit_message_text("Send userid (or @user | id) to unban Permanent:")
            except Exception:
                pass
            return

        if qd == "unban_old":
            admin_state = "unban_old_input"
            try:
                query.edit_message_text("Send userid (or @user | id) to unban Old Winner:")
            except Exception:
                pass
            return

        return

    # =========================
    # USER SIDE (block applies to ANY button)
    # =========================
    if uid in (data.get("permanent_block", {}) or {}):
        try:
            query.answer(popup_permanent_blocked(), show_alert=True)
        except Exception:
            pass
        return

    oldw = data.get("old_winners", {}) or {}
    if oldw and uid in oldw:
        try:
            query.answer(popup_old_winner_blocked(), show_alert=True)
        except Exception:
            pass
        return

    if qd == "join_giveaway":
        if not data.get("active"):
            try:
                query.answer("This giveaway is not active right now.", show_alert=True)
            except Exception:
                pass
            return

        if not verify_user_join(context.bot, uid_int):
            try:
                query.answer(popup_verify_required(), show_alert=True)
            except Exception:
                pass
            return

        if uid in (data.get("participants", {}) or {}):
            try:
                query.answer(popup_already_joined(), show_alert=True)
            except Exception:
                pass
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

        # fast update live post (participants & progress)
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time")
            if live_mid and start_ts:
                duration = max(1, int(data.get("duration_seconds", 1) or 1))
                elapsed = int(datetime.utcnow().timestamp() - float(start_ts))
                remaining = max(0, duration - elapsed)
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=live_mid,
                    text=build_live_text(remaining),
                    reply_markup=join_button_markup(),
                )
        except Exception:
            pass

        if data.get("first_winner_id") == uid:
            try:
                query.answer(popup_first_winner(uname or "N/A", uid), show_alert=True)
            except Exception:
                pass
        else:
            try:
                query.answer(popup_join_success(uname or "N/A", uid), show_alert=True)
            except Exception:
                pass
        return

    if qd == "claim_prize":
        winners = data.get("winners", {}) or {}
        if uid in winners:
            uname = (winners.get(uid, {}) or {}).get("username", "") or "N/A"
            try:
                query.answer(popup_claim_winner(uname, uid), show_alert=True)
            except Exception:
                pass
        else:
            try:
                query.answer(popup_not_winner(), show_alert=True)
            except Exception:
                pass
        return

# =========================
# UNBAN INPUT (admin)
# =========================
def admin_unban_text(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update.effective_user.id):
        return
    if admin_state not in ("unban_permanent_input", "unban_old_input"):
        return

    msg = (update.message.text or "").strip()
    entries = parse_user_lines(msg)
    if not entries:
        update.message.reply_text("Send userid (or @user | id).")
        return

    uid, _ = entries[0]

    if admin_state == "unban_permanent_input":
        with lock:
            pb = data.get("permanent_block", {}) or {}
            if uid in pb:
                del pb[uid]
                data["permanent_block"] = pb
                save_data()
                update.message.reply_text("✅ Unbanned from Permanent Block.")
            else:
                update.message.reply_text("Not found in Permanent Block.")
        admin_state = None
        return

    if admin_state == "unban_old_input":
        with lock:
            ow = data.get("old_winners", {}) or {}
            if uid in ow:
                del ow[uid]
                data["old_winners"] = ow
                save_data()
                update.message.reply_text("✅ Unbanned from Old Winner Block.")
            else:
                update.message.reply_text("Not found in Old Winner Block.")
        admin_state = None
        return

# =========================
# CHANNEL POST LISTENER (AUTO SET WINNERLIST)
# =========================
def channel_post_handler(update: Update, context: CallbackContext):
    # if admin manually posts winners announcement in channel, bot auto saves history + old_winners
    msg = update.channel_post
    if not msg or not msg.text:
        return
    text = msg.text

    if "GIVEAWAY WINNERS ANNOUNCEMENT" in text:
        store_winner_history_from_text(text)

# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing in .env")

    updater = Updater(BOT_TOKEN, use_context=True, workers=8)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("panel", cmd_panel))
    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))

    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))
    dp.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("winnerlist", cmd_winnerlist))

    dp.add_handler(CommandHandler("draw", cmd_draw))
    dp.add_handler(CommandHandler("reset", cmd_reset))

    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_unban_text), group=0)
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler), group=1)

    dp.add_handler(CallbackQueryHandler(cb_handler))
    dp.add_handler(MessageHandler(Filters.update.channel_posts, channel_post_handler))

    if data.get("active"):
        start_live_countdown(updater.job_queue)

    print("Bot running (PTB 13.15) ...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
