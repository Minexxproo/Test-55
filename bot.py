import os
import json
import random
import threading
import io
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
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

BD_TZ = timezone(timedelta(hours=6))

# =========================================================
# THREAD SAFE STORAGE
# =========================================================
lock = threading.RLock()

# =========================================================
# GLOBAL STATE
# =========================================================
data = {}
admin_state = None

live_job = None

draw_job = None
draw_finalize_job = None

reset_job = None
reset_finalize_job = None

# =========================================================
# CONSTANTS
# =========================================================
LIVE_UPDATE_INTERVAL = 5  # seconds (time/progress update)
DRAW_DURATION_SECONDS = 120  # 2 minutes
DRAW_UPDATE_INTERVAL = 1  # very fast
RESET_DURATION_SECONDS = 40
RESET_UPDATE_INTERVAL = 1

DOTS = [".", "..", "...", "....", ".....", "......", "......."]
SPINNER = ["🔄", "🔃"]

# =========================================================
# DATA / STORAGE
# =========================================================
def fresh_default_data():
    return {
        # giveaway state
        "active": False,
        "closed": False,

        "title": "",
        "prize": "",
        "winner_count": 0,
        "duration_seconds": 0,
        "rules": "",
        "start_time": None,

        # messages ids
        "live_message_id": None,     # giveaway live post (channel)
        "closed_message_id": None,   # closed post (channel)
        "winners_message_id": None,  # winners post (channel)
        "channel_draw_progress_id": None,  # auto draw progress (channel)

        # participants
        "participants": {},  # uid(str) -> {"username": "@x" or "", "name": "", "joined_at": ts}
        "verified_once": {}, # uid(str) -> True (only after they joined successfully once)

        # verify targets (must join all to join)
        "verify_targets": [],  # [{"ref": "-100..." or "@xxx", "display": "..."}]

        # blocks
        "permanent_block": {},  # uid -> {"username": "@x" or "", "added_at": ts}
        "old_winners_block": {},# uid -> {"username": "@x" or "", "added_at": ts}  (manual /blockoldwinner or setup block list)
        "old_winner_mode": "skip",  # "skip" or "block" (setup step)
        # NOTE: even if mode=skip, old_winners_block list ALWAYS blocks those users (your rule)

        # first join winner
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",
        "first_winner_at": None,

        # winners
        "winners": {},                 # uid -> {"username": "@x" or "", "win_at": ts, "type": "first/random"}
        "pending_winners_text": "",    # admin preview text
        "claim_deadline_ts": None,     # when claim expires (unix ts)

        # history
        "winner_history": [],          # list of dict {uid, username, title, prize, win_at, type}
        # manager logs
        "logs": [],                    # list of dict {ts, type, uid, username, text, extra}

        # autowinnerpost
        "autowinnerpost": False,       # if True => on auto close, auto draw+post in channel
        # backrules
        "backrules_enabled": False,
        "backrules_banned": {},        # uid -> {"username": "@x", "banned_at": ts, "reason": "...", "failed_ref": "..."}
    }


def load_data():
    base = fresh_default_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}

    # keep backward compatibility
    for k, v in base.items():
        d.setdefault(k, v)

    # ensure dict types
    for k in ["participants","verified_once","verify_targets","permanent_block","old_winners_block",
              "winners","winner_history","logs","backrules_banned"]:
        if d.get(k) is None:
            d[k] = base[k]

    return d


def save_data():
    with lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)


data = load_data()

# =========================================================
# HELPERS
# =========================================================
def now_bd():
    return datetime.now(BD_TZ)

def ts_bd_str(ts: float) -> str:
    try:
        dt = datetime.fromtimestamp(float(ts), BD_TZ)
    except Exception:
        dt = now_bd()
    return dt.strftime("%d/%m/%Y %H:%M:%S")

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

def log_event(ev_type: str, uid: str = "", username: str = "", text: str = "", extra=None):
    with lock:
        data.setdefault("logs", [])
        data["logs"].append({
            "ts": datetime.utcnow().timestamp(),
            "type": ev_type,
            "uid": uid,
            "username": username,
            "text": text,
            "extra": extra or {},
        })
        # keep logs from growing too huge
        if len(data["logs"]) > 5000:
            data["logs"] = data["logs"][-3000:]
        save_data()

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

def reset_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ Reject", callback_data="reset_cancel"),
        ]]
    )

def autowinnerpost_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Auto Post ON", callback_data="auto_on"),
            InlineKeyboardButton("❌ Auto Post OFF", callback_data="auto_off"),
        ]]
    )

def backrules_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("ON", callback_data="br_on"),
            InlineKeyboardButton("OFF", callback_data="br_off"),
            InlineKeyboardButton("UNBAN", callback_data="br_unban"),
            InlineKeyboardButton("BANLIST", callback_data="br_banlist"),
        ]]
    )

def unban_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Unban Permanent Block", callback_data="unban_permanent"),
            InlineKeyboardButton("Unban Old Winner Block", callback_data="unban_oldwinner"),
        ]]
    )

def removeban_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Reset Permanent Ban List", callback_data="reset_permanent_ban"),
            InlineKeyboardButton("Reset Old Winner Ban List", callback_data="reset_oldwinner_ban"),
        ]]
    )

def removeban_confirm_markup(kind: str):
    # kind: "permanent" or "oldwinner"
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_reset_{kind}"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
        ]]
    )

# =========================================================
# VERIFY
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

def verify_user_join(bot, user_id: int):
    """
    Returns: (ok: bool, failed_ref: str)
    """
    targets = data.get("verify_targets", []) or []
    if not targets:
        return True, ""

    for t in targets:
        ref = (t or {}).get("ref", "")
        if not ref:
            return False, "unknown"
        try:
            member = bot.get_chat_member(chat_id=ref, user_id=user_id)
            status = getattr(member, "status", None)
            if status not in ("member", "administrator", "creator"):
                return False, ref
        except Exception:
            return False, ref

    return True, ""

# =========================================================
# POPUP TEXTS (Only spacing changes, no extra emoji/words)
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

def popup_access_locked() -> str:
    # backrules ban
    return (
        "🚫 ACCESS LOCKED\n\n"
        "You left the required channels/groups.\n"
        "Entry to this giveaway is restricted.\n\n"
        f"Contact Admin:\n{ADMIN_CONTACT}\n\n"
        "📋 Long-press the username to copy"
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
        "⏳ PRIZE EXPIRED\n"
        "Your claim time has ended.\n"
        "Please wait for the next giveaway."
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
    # your fixed 2-line border style
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        "🏆 Winner selection is in progress.\n"
        "Please wait for the official announcement.\n\n"
        "🙏 Thank you to everyone who participated.\n"
        f"— {HOST_NAME} ⚡"
    )

def build_winners_post_text(first_uid: str, first_user: str, random_winners: list) -> str:
    # random_winners: list of tuples [(uid, username_or_empty), ...]
    lines = []
    lines.append("🏆 GIVEAWAY WINNERS ANNOUNCEMENT 🏆")
    lines.append("")
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
    lines.append(f"📢 Hosted By: {HOST_NAME}")
    lines.append("👇 Click the button below to claim your prize")

    return "\n".join(lines)

# =========================================================
# LIVE COUNTDOWN JOB (Every 5s)
# =========================================================
def stop_live_job():
    global live_job
    if live_job is not None:
        try:
            live_job.schedule_removal()
        except Exception:
            pass
    live_job = None

def start_live_job(job_queue):
    global live_job
    stop_live_job()
    live_job = job_queue.run_repeating(live_tick, interval=LIVE_UPDATE_INTERVAL, first=0)

def live_tick(context: CallbackContext):
    global data
    with lock:
        if not data.get("active"):
            stop_live_job()
            return

        start_time = data.get("start_time")
        if start_time is None:
            data["start_time"] = datetime.utcnow().timestamp()
            save_data()
            start_time = data["start_time"]

        start = datetime.fromtimestamp(float(start_time), timezone.utc)
        now = datetime.now(timezone.utc)
        duration = int(data.get("duration_seconds", 1) or 1)
        elapsed = int((now - start).total_seconds())
        remaining = duration - elapsed

        live_mid = data.get("live_message_id")

    # end time
    if remaining <= 0:
        with lock:
            data["active"] = False
            data["closed"] = True
            save_data()

        # delete live post
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

        # notify admin
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

        stop_live_job()

        # AUTO WINNER POST
        with lock:
            auto = bool(data.get("autowinnerpost", False))
        if auto:
            # start channel draw progress + auto winners post
            try:
                start_draw_progress_in_channel(context)
            except Exception:
                pass
        return

    # update live message
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
# DRAW PROGRESS (ADMIN or CHANNEL)
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

def build_draw_progress_text(percent: int, dots: str, spinner: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 Selecting winners... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        f"{spinner} Winner selection is in progress...\n\n"
        "✅ This draw is 100% fair & random.\n"
        "🔐 User ID based selection only.\n\n"
        f"Please wait{dots}"
    )

def start_draw_progress(context: CallbackContext, chat_id: int, message_id_store_key: str, finalize_mode: str):
    """
    finalize_mode:
      - "admin_preview" => after progress, send preview to admin with Approve/Reject
      - "channel_auto"  => after progress, post winners directly to channel + claim button
    message_id_store_key:
      - "admin_draw_msg_id" or "channel_draw_progress_id"
    """
    global draw_job, draw_finalize_job
    stop_draw_jobs()

    msg = context.bot.send_message(chat_id=chat_id, text=build_draw_progress_text(0, DOTS[0], SPINNER[0]))

    with lock:
        data[message_id_store_key] = msg.message_id
        save_data()

    ctx = {
        "chat_id": chat_id,
        "msg_id": msg.message_id,
        "start_ts": datetime.utcnow().timestamp(),
        "tick": 0,
        "finalize_mode": finalize_mode,
    }

    def tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] = jd.get("tick", 0) + 1

        elapsed = max(0.0, datetime.utcnow().timestamp() - jd["start_ts"])
        percent = int(round(min(100.0, (elapsed / float(DRAW_DURATION_SECONDS)) * 100.0)))

        dots = DOTS[(jd["tick"] - 1) % len(DOTS)]
        spinner = SPINNER[(jd["tick"] - 1) % len(SPINNER)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["msg_id"],
                text=build_draw_progress_text(percent, dots, spinner),
            )
        except Exception:
            pass

    draw_job = context.job_queue.run_repeating(
        tick,
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
    global data
    stop_draw_jobs()

    jd = context.job.context
    chat_id = jd["chat_id"]
    msg_id = jd["msg_id"]
    finalize_mode = jd.get("finalize_mode", "admin_preview")

    # compute winners
    with lock:
        participants = data.get("participants", {}) or {}
        if not participants:
            try:
                context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="No participants to draw winners from.")
            except Exception:
                pass
            return

        winner_count = int(data.get("winner_count", 1) or 1)
        winner_count = max(1, winner_count)

        # first winner must exist: first join rule
        first_uid = data.get("first_winner_id")
        if not first_uid:
            first_uid = next(iter(participants.keys()))
            info = participants.get(first_uid, {}) or {}
            data["first_winner_id"] = first_uid
            data["first_winner_username"] = info.get("username", "")
            data["first_winner_name"] = info.get("name", "")
            data["first_winner_at"] = info.get("joined_at") or datetime.utcnow().timestamp()

        first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

        # pool exclude first
        pool = [uid for uid in participants.keys() if uid != first_uid]
        remaining_needed = max(0, winner_count - 1)
        if remaining_needed > len(pool):
            remaining_needed = len(pool)

        selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

        winners_map = {}
        random_list = []

        # first winner
        winners_map[first_uid] = {"username": first_uname, "win_at": datetime.utcnow().timestamp(), "type": "first"}

        # random winners
        for uid2 in selected:
            info2 = participants.get(uid2, {}) or {}
            winners_map[uid2] = {"username": info2.get("username", ""), "win_at": datetime.utcnow().timestamp(), "type": "random"}
            random_list.append((uid2, info2.get("username", "")))

        data["winners"] = winners_map
        winners_text = build_winners_post_text(first_uid, first_uname, random_list)
        data["pending_winners_text"] = winners_text

        # claim expires after 24 hours from posting approval/auto posting.
        # we set deadline now; for manual approve we will refresh again at approve moment.
        data["claim_deadline_ts"] = datetime.utcnow().timestamp() + 24 * 3600
        save_data()

    # finalize behaviour
    if finalize_mode == "channel_auto":
        # delete closed post before winners post
        with lock:
            closed_mid = data.get("closed_message_id")
        if closed_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception:
                pass
            with lock:
                data["closed_message_id"] = None
                save_data()

        # delete progress message itself
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=msg_id)
        except Exception:
            pass
        with lock:
            data["channel_draw_progress_id"] = None
            # refresh claim deadline at the moment of posting
            data["claim_deadline_ts"] = datetime.utcnow().timestamp() + 24 * 3600
            save_data()

        # post winners directly to channel
        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=winners_text, reply_markup=claim_button_markup())
            with lock:
                data["winners_message_id"] = m.message_id
                save_data()
        except Exception:
            pass
        return

    # admin preview with buttons
    try:
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=winners_text,
            reply_markup=winners_approve_markup(),
        )
    except Exception:
        try:
            context.bot.send_message(chat_id=chat_id, text=winners_text, reply_markup=winners_approve_markup())
        except Exception:
            pass

def start_draw_progress_in_channel(context: CallbackContext):
    # show progress message in channel and auto post winners
    start_draw_progress(context, CHANNEL_ID, "channel_draw_progress_id", "channel_auto")

# =========================================================
# RESET PROGRESS (40s, percent+bar, no countdown number)
# =========================================================
def stop_reset_jobs():
    global reset_job, reset_finalize_job
    if reset_job is not None:
        try:
            reset_job.schedule_removal()
        except Exception:
            pass
    reset_job = None
    if reset_finalize_job is not None:
        try:
            reset_finalize_job.schedule_removal()
        except Exception:
            pass
    reset_finalize_job = None

def build_reset_progress_text(percent: int, dots: str, spinner: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ FULL RESET\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 Resetting... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        f"{spinner} Please wait{dots}"
    )

def start_reset_progress(context: CallbackContext, admin_chat_id: int, admin_msg_id: int):
    global reset_job, reset_finalize_job
    stop_reset_jobs()

    ctx = {
        "chat_id": admin_chat_id,
        "msg_id": admin_msg_id,
        "start_ts": datetime.utcnow().timestamp(),
        "tick": 0,
    }

    def tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] = jd.get("tick", 0) + 1

        elapsed = max(0.0, datetime.utcnow().timestamp() - jd["start_ts"])
        percent = int(round(min(100.0, (elapsed / float(RESET_DURATION_SECONDS)) * 100.0)))

        dots = DOTS[(jd["tick"] - 1) % len(DOTS)]
        spinner = SPINNER[(jd["tick"] - 1) % len(SPINNER)]
        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["msg_id"],
                text=build_reset_progress_text(percent, dots, spinner),
            )
        except Exception:
            pass

    reset_job = context.job_queue.run_repeating(
        tick, interval=RESET_UPDATE_INTERVAL, first=0, context=ctx, name="reset_progress_job"
    )
    reset_finalize_job = context.job_queue.run_once(
        reset_finalize, when=RESET_DURATION_SECONDS, context=ctx, name="reset_finalize_job"
    )

def reset_finalize(context: CallbackContext):
    global data
    stop_reset_jobs()
    stop_live_job()
    stop_draw_jobs()

    jd = context.job.context
    chat_id = jd["chat_id"]
    msg_id = jd["msg_id"]

    # delete channel messages if exist
    with lock:
        mids = [
            data.get("live_message_id"),
            data.get("closed_message_id"),
            data.get("winners_message_id"),
            data.get("channel_draw_progress_id"),
        ]
    for mid in mids:
        if mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
            except Exception:
                pass

    # FULL reset: remove all information (your rule)
    with lock:
        data = fresh_default_data()
        save_data()

    try:
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ RESET COMPLETED SUCCESSFULLY!\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Bot is now BRAND NEW.\n"
                "All data has been removed."
            ),
        )
    except Exception:
        pass

# =========================================================
# PARSERS
# =========================================================
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
# COMMANDS
# =========================================================
def cmd_start(update: Update, context: CallbackContext):
    u = update.effective_user
    if not u:
        return

    if u.id == ADMIN_ID:
        update.message.reply_text(
            "🛡️👑 WELCOME BACK, ADMIN 👑🛡️\n\n"
            "⚙️ System Status: ONLINE ✅\n"
            "🚀 Giveaway Engine: READY\n"
            "🔐 Security Level: MAXIMUM\n\n"
            "🧭 Open the Admin Control Panel:\n"
            "/panel\n\n"
            f"⚡ POWERED BY: {HOST_NAME}"
        )
        log_event("admin_start", str(u.id), user_tag(u.username or ""), "Admin /start")
    else:
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please join our official channel and wait for the giveaway post.\n\n"
            "🔗 Official Channel:\n"
            f"{CHANNEL_LINK}"
        )
        log_event("user_start", str(u.id), user_tag(u.username or ""), "User /start")

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
        "/autowinnerpost\n"
        "/winnerlist\n\n"
        "🔒 BLOCK SYSTEM\n"
        "/blockpermanent\n"
        "/blockoldwinner\n"
        "/unban\n"
        "/blocklist\n"
        "/removeban\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "🛡 BACKRULES\n"
        "/backrules\n\n"
        "📄 MANAGER\n"
        "/manager DD/MM/YYYY\n\n"
        "♻️ RESET\n"
        "/reset"
    )

def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "Auto Winner Post Settings:\n\n"
        "ON  → Giveaway close হলে auto draw + auto winners post channel এ হবে\n"
        "OFF → Giveaway close হলে admin notify হবে, তারপর /draw দিয়ে করবে\n",
        reply_markup=autowinnerpost_markup(),
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
        # reset giveaway runtime only, keep blocks/verify/history/backrules/autowinner
        keep_verify = data.get("verify_targets", [])
        keep_perma = data.get("permanent_block", {})
        keep_oldblock = data.get("old_winners_block", {})
        keep_history = data.get("winner_history", [])
        keep_logs = data.get("logs", [])
        keep_auto = data.get("autowinnerpost", False)
        keep_br = data.get("backrules_enabled", False)
        keep_br_ban = data.get("backrules_banned", {})

        # fresh default then restore keepers
        fresh = fresh_default_data()
        fresh["verify_targets"] = keep_verify
        fresh["permanent_block"] = keep_perma
        fresh["old_winners_block"] = keep_oldblock
        fresh["winner_history"] = keep_history
        fresh["logs"] = keep_logs
        fresh["autowinnerpost"] = keep_auto
        fresh["backrules_enabled"] = keep_br
        fresh["backrules_banned"] = keep_br_ban

        data.clear()
        data.update(fresh)
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
            lines.append(f"{i}) {uname} | User ID: {uid}")
        else:
            lines.append(f"{i}) User ID: {uid}")
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
    if not data.get("participants", {}):
        update.message.reply_text("No participants to draw winners from.")
        return
    # admin draw progress -> preview with approve/reject
    start_draw_progress(context, update.effective_chat.id, "admin_draw_msg_id", "admin_preview")

def cmd_winnerlist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    hist = data.get("winner_history", []) or []
    if not hist:
        update.message.reply_text("Winner history is empty.")
        return

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆 WINNER HISTORY LIST (ALL WINNERS)")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    for i, w in enumerate(hist, start=1):
        uname = w.get("username", "")
        uid = w.get("uid", "")
        title = w.get("title", "")
        prize = w.get("prize", "")
        wtype = w.get("type", "")
        wts = w.get("win_at", 0)
        lines.append(f"{i}) {uname or 'User ID: '+str(uid)} | User ID: {uid}")
        lines.append(f"🗓 {ts_bd_str(wts)}")
        lines.append(f"🏅 {wtype}")
        lines.append("🎁 Prize:")
        lines.append(prize if prize else "-")
        lines.append("⚡ Giveaway:")
        lines.append(title if title else "-")
        lines.append("")

    update.message.reply_text("\n".join(lines))

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
    admin_state = "oldwinner_block_list_manual"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⛔ OLD WINNER BLOCK\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send old winners list (one per line):\n\n"
        "Format:\n"
        "@username | user_id\n\n"
        "Example:\n"
        "@minexxproo | 8392828\n"
        "@user2 | 889900\n"
        "556677"
    )

def cmd_unban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "unban_choose"
    update.message.reply_text("Choose Unban Type:", reply_markup=unban_markup())

def cmd_blocklist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    perma = data.get("permanent_block", {}) or {}
    oldw = data.get("old_winners_block", {}) or {}
    br = data.get("backrules_banned", {}) or {}

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📌 BAN LISTS")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
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
    lines.append("")

    lines.append("🚫 BACKRULES BANLIST")
    lines.append(f"Total: {len(br)}")
    if br:
        i = 1
        for uid, info in br.items():
            uname = (info or {}).get("username", "")
            if uname:
                lines.append(f"{i}) {uname} | User ID: {uid}")
            else:
                lines.append(f"{i}) User ID: {uid}")
            i += 1
    else:
        lines.append("No backrules locked users.")

    update.message.reply_text("\n".join(lines))

def cmd_removeban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "removeban_choose"
    update.message.reply_text("Choose which ban list to reset:", reply_markup=removeban_markup())

def cmd_backrules(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "BackRules Control Panel:\n\n"
        "ON  → leave করলে ACCESS LOCKED\n"
        "OFF → disabled\n"
        "UNBAN → unlock a user id\n"
        "BANLIST → show locked list\n",
        reply_markup=backrules_markup()
    )

def cmd_manager(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    # format: /manager DD/MM/YYYY
    parts = (update.message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        update.message.reply_text("Usage: /manager DD/MM/YYYY")
        return

    date_str = parts[1].strip()
    try:
        dd, mm, yy = date_str.split("/")
        target_date = datetime(int(yy), int(mm), int(dd), tzinfo=BD_TZ).date()
    except Exception:
        update.message.reply_text("Invalid date. Use: DD/MM/YYYY")
        return

    logs = data.get("logs", []) or []
    # filter by BD date
    out_lines = []
    out_lines.append(f"MANAGER REPORT — {date_str} (BD Time +06:00)")
    out_lines.append("")

    for ev in logs:
        ts = ev.get("ts", 0)
        dt_bd = datetime.fromtimestamp(float(ts), BD_TZ)
        if dt_bd.date() != target_date:
            continue
        uid = ev.get("uid", "")
        uname = ev.get("username", "")
        typ = ev.get("type", "")
        text = ev.get("text", "")
        extra = ev.get("extra", {}) or {}

        out_lines.append(f"[{dt_bd.strftime('%H:%M:%S')}] {typ}")
        if uname:
            out_lines.append(f"User: {uname} | ID: {uid}")
        else:
            out_lines.append(f"User ID: {uid}")
        if text:
            out_lines.append(text)
        if extra:
            out_lines.append(f"Extra: {json.dumps(extra, ensure_ascii=False)}")
        out_lines.append("")

    if len(out_lines) <= 2:
        out_lines.append("No logs found for this date.")

    content = "\n".join(out_lines).encode("utf-8")
    bio = io.BytesIO(content)
    bio.name = f"manager_{dd}-{mm}-{yy}.txt"

    update.message.reply_document(document=InputFile(bio))

def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ FULL RESET CONFIRMATION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ This will remove EVERYTHING.\n\n"
        "Confirm reset?",
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
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET REMOVED\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Removed: {removed.get('display','')}\n"
            f"Remaining: {len(data.get('verify_targets', []) or [])}"
        )
        return

    # GIVEAWAY SETUP
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

        admin_state = "old_winner_mode"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 OLD WINNER PROTECTION MODE\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "1️⃣ BLOCK OLD WINNERS\n"
            "• Old winners cannot join this giveaway\n\n"
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
                save_data()
            admin_state = "rules"
            update.message.reply_text("✅ Old Winner Mode set to: SKIP\n\nNow send Giveaway Rules (multi-line):")
            return

        # block
        with lock:
            data["old_winner_mode"] = "block"
            save_data()

        admin_state = "old_winner_block_list_setup"
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

    if admin_state == "old_winner_block_list_setup":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            ow = data.get("old_winners_block", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[uid] = {"username": uname, "added_at": datetime.utcnow().timestamp()}
            data["old_winners_block"] = ow
            save_data()

        admin_state = "rules"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ OLD WINNER BLOCK LIST SAVED!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 Total Added: {len(data['old_winners_block']) - before}\n\n"
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

    # PERMANENT BLOCK LIST
    if admin_state == "perma_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            perma = data.get("permanent_block", {}) or {}
            before = len(perma)
            for uid, uname in entries:
                perma[uid] = {"username": uname, "added_at": datetime.utcnow().timestamp()}
            data["permanent_block"] = perma
            save_data()
        admin_state = None
        update.message.reply_text(
            "✅ Permanent block saved!\n"
            f"New Added: {len(data['permanent_block']) - before}\n"
            f"Total Blocked: {len(data['permanent_block'])}"
        )
        return

    # OLD WINNER BLOCK (manual command)
    if admin_state == "oldwinner_block_list_manual":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            ow = data.get("old_winners_block", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[uid] = {"username": uname, "added_at": datetime.utcnow().timestamp()}
            data["old_winners_block"] = ow
            save_data()
        admin_state = None
        update.message.reply_text(
            "✅ Old winner block saved!\n"
            f"New Added: {len(data['old_winners_block']) - before}\n"
            f"Total Old Winner Blocked: {len(data['old_winners_block'])}"
        )
        return

    # UNBAN INPUTS
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
            ow = data.get("old_winners_block", {}) or {}
            if uid in ow:
                del ow[uid]
                data["old_winners_block"] = ow
                save_data()
                update.message.reply_text("✅ Unbanned from Old Winner Block successfully!")
            else:
                update.message.reply_text("This user id is not in Old Winner Block list.")
        admin_state = None
        return

    # BACKRULES UNBAN INPUT
    if admin_state == "br_unban_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        with lock:
            br = data.get("backrules_banned", {}) or {}
            if uid in br:
                del br[uid]
                data["backrules_banned"] = br
                save_data()
                update.message.reply_text("✅ Access unlocked successfully!")
            else:
                update.message.reply_text("This user id is not in BackRules ban list.")
        admin_state = None
        return

# =========================================================
# CALLBACK HANDLER
# =========================================================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state, data
    query = update.callback_query
    if not query:
        return
    qd = query.data
    uid = str(query.from_user.id)
    uname = user_tag(query.from_user.username or "")

    # ALWAYS FAST ANSWER (reduce "loading")
    try:
        query.answer(cache_time=0)
    except Exception:
        pass

    # ========== GLOBAL BUTTON LOCK RULES (ANY BUTTON) ==========
    # Permanent block => any button shows permanent popup
    if uid in (data.get("permanent_block", {}) or {}):
        try:
            query.answer(popup_permanent_blocked(), show_alert=True, cache_time=0)
        except Exception:
            pass
        return

    # Old winner block => any button shows old winner popup (your rule)
    if uid in (data.get("old_winners_block", {}) or {}):
        try:
            query.answer(popup_old_winner_blocked(), show_alert=True, cache_time=0)
        except Exception:
            pass
        return

    # Backrules ban => any button shows access locked
    if uid in (data.get("backrules_banned", {}) or {}):
        try:
            query.answer(popup_access_locked(), show_alert=True, cache_time=0)
        except Exception:
            pass
        return

    # ========== AUTOWINNERPOST ==========
    if qd in ("auto_on", "auto_off"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        with lock:
            data["autowinnerpost"] = (qd == "auto_on")
            save_data()
        try:
            query.edit_message_text(f"✅ Auto Winner Post is now: {'ON' if qd=='auto_on' else 'OFF'}")
        except Exception:
            pass
        return

    # ========== BACKRULES buttons ==========
    if qd in ("br_on", "br_off", "br_unban", "br_banlist"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return

        if qd == "br_on":
            with lock:
                data["backrules_enabled"] = True
                save_data()
            try:
                query.edit_message_text("✅ BackRules is now ON")
            except Exception:
                pass
            return

        if qd == "br_off":
            with lock:
                data["backrules_enabled"] = False
                save_data()
            try:
                query.edit_message_text("✅ BackRules is now OFF")
            except Exception:
                pass
            return

        if qd == "br_unban":
            admin_state = "br_unban_input"
            try:
                query.edit_message_text("Send User ID (or @name | id) to UNLOCK access:")
            except Exception:
                pass
            return

        if qd == "br_banlist":
            br = data.get("backrules_banned", {}) or {}
            lines = []
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("🚫 BACKRULES BANLIST")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"Total: {len(br)}")
            lines.append("")
            if not br:
                lines.append("No locked users.")
            else:
                i = 1
                for buid, info in br.items():
                    bun = (info or {}).get("username", "")
                    if bun:
                        lines.append(f"{i}) {bun} | User ID: {buid}")
                    else:
                        lines.append(f"{i}) User ID: {buid}")
                    i += 1
            try:
                query.edit_message_text("\n".join(lines))
            except Exception:
                pass
            return

    # ========== UNBAN choose ==========
    if qd in ("unban_permanent", "unban_oldwinner"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        if qd == "unban_permanent":
            admin_state = "unban_permanent_input"
            try:
                query.edit_message_text("Send User ID (or @name | id) to unban from Permanent Block:")
            except Exception:
                pass
            return
        if qd == "unban_oldwinner":
            admin_state = "unban_oldwinner_input"
            try:
                query.edit_message_text("Send User ID (or @name | id) to unban from Old Winner Block:")
            except Exception:
                pass
            return

    # ========== REMOVEBAN choose ==========
    if qd in ("reset_permanent_ban", "reset_oldwinner_ban"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        if qd == "reset_permanent_ban":
            try:
                query.edit_message_text("Confirm reset Permanent Ban List?", reply_markup=removeban_confirm_markup("permanent"))
            except Exception:
                pass
            return
        if qd == "reset_oldwinner_ban":
            try:
                query.edit_message_text("Confirm reset Old Winner Ban List?", reply_markup=removeban_confirm_markup("oldwinner"))
            except Exception:
                pass
            return

    if qd == "cancel_reset_ban":
        if uid != str(ADMIN_ID):
            return
        try:
            query.edit_message_text("Cancelled.")
        except Exception:
            pass
        return

    if qd in ("confirm_reset_permanent", "confirm_reset_oldwinner"):
        if uid != str(ADMIN_ID):
            return
        if qd == "confirm_reset_permanent":
            with lock:
                data["permanent_block"] = {}
                save_data()
            try:
                query.edit_message_text("✅ Permanent Ban List has been reset.")
            except Exception:
                pass
            return
        if qd == "confirm_reset_oldwinner":
            with lock:
                data["old_winners_block"] = {}
                save_data()
            try:
                query.edit_message_text("✅ Old Winner Ban List has been reset.")
            except Exception:
                pass
            return

    # ========== Verify add buttons ==========
    if qd == "verify_add_more":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
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
        admin_state = None
        try:
            query.edit_message_text(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ VERIFY SETUP COMPLETED\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}\n"
                "All users must join ALL targets to join giveaway."
            )
        except Exception:
            pass
        return

    # ========== Preview ==========
    if qd.startswith("preview_"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return

        if qd == "preview_approve":
            try:
                duration = int(data.get("duration_seconds", 0) or 1)
                m = context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=build_live_text(duration),
                    reply_markup=join_button_markup(),
                )
            except Exception as e:
                try:
                    query.edit_message_text(f"Failed to post in channel. Make sure bot is admin.\nError: {e}")
                except Exception:
                    pass
                return

            with lock:
                data["live_message_id"] = m.message_id
                data["active"] = True
                data["closed"] = False
                data["start_time"] = datetime.utcnow().timestamp()
                data["closed_message_id"] = None
                data["winners_message_id"] = None
                data["channel_draw_progress_id"] = None

                # reset giveaway runtime participants/winners for new round
                data["participants"] = {}
                data["verified_once"] = {}
                data["winners"] = {}
                data["pending_winners_text"] = ""
                data["first_winner_id"] = None
                data["first_winner_username"] = ""
                data["first_winner_name"] = ""
                data["first_winner_at"] = None
                data["claim_deadline_ts"] = None

                save_data()

            start_live_job(context.job_queue)

            try:
                query.edit_message_text("✅ Giveaway approved and posted to channel!")
            except Exception:
                pass
            return

        if qd == "preview_reject":
            try:
                query.edit_message_text("❌ Giveaway rejected and cleared.")
            except Exception:
                pass
            return

        if qd == "preview_edit":
            try:
                query.edit_message_text("✏️ Edit Mode\n\nStart again with /newgiveaway")
            except Exception:
                pass
            return

    # ========== End giveaway confirm/cancel ==========
    if qd == "end_confirm":
        if uid != str(ADMIN_ID):
            return

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
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text())
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
        except Exception:
            pass

        stop_live_job()
        try:
            query.edit_message_text("✅ Giveaway Closed Successfully! Now use /draw")
        except Exception:
            pass
        return

    if qd == "end_cancel":
        if uid != str(ADMIN_ID):
            return
        try:
            query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        except Exception:
            pass
        return

    # ========== Reset confirm/cancel ==========
    if qd == "reset_confirm":
        if uid != str(ADMIN_ID):
            return
        # start 40s progress reset in THIS chat message
        try:
            query.edit_message_text(build_reset_progress_text(0, DOTS[0], SPINNER[0]))
        except Exception:
            pass
        start_reset_progress(context, query.message.chat_id, query.message.message_id)
        return

    if qd == "reset_cancel":
        if uid != str(ADMIN_ID):
            return
        try:
            query.edit_message_text("❌ Reset cancelled.")
        except Exception:
            pass
        return

    # ========== Join giveaway ==========
    if qd == "join_giveaway":
        # giveaway inactive
        if not data.get("active"):
            try:
                query.answer("This giveaway is not active right now.", show_alert=True, cache_time=0)
            except Exception:
                pass
            return

        # backrules enabled?
        br_enabled = bool(data.get("backrules_enabled", False))

        # verify check
        ok, failed_ref = verify_user_join(context.bot, int(uid))
        if not ok:
            # backrules ban logic:
            # If user previously verified_once AND backrules ON => lock permanently
            if br_enabled and (data.get("verified_once", {}) or {}).get(uid):
                with lock:
                    br = data.get("backrules_banned", {}) or {}
                    br[uid] = {
                        "username": uname,
                        "banned_at": datetime.utcnow().timestamp(),
                        "reason": "left_required",
                        "failed_ref": failed_ref,
                    }
                    data["backrules_banned"] = br
                    save_data()

                log_event(
                    "backrules_locked",
                    uid=uid,
                    username=uname,
                    text="User left required targets (locked on click)",
                    extra={"failed_ref": failed_ref},
                )
                try:
                    query.answer(popup_access_locked(), show_alert=True, cache_time=0)
                except Exception:
                    pass
                return

            # normal verify required popup
            log_event("verify_failed", uid=uid, username=uname, text="Verify required", extra={"failed_ref": failed_ref})
            try:
                query.answer(popup_verify_required(), show_alert=True, cache_time=0)
            except Exception:
                pass
            return

        # If passed verify, continue
        # First winner clicks again -> always same first popup
        with lock:
            first_uid = data.get("first_winner_id")

        if first_uid and uid == str(first_uid):
            # show first-winner popup always
            try:
                query.answer(popup_first_winner(uname or "@username", uid), show_alert=True, cache_time=0)
            except Exception:
                pass
            return

        # already joined normal
        if uid in (data.get("participants", {}) or {}):
            log_event("join_again", uid=uid, username=uname, text="Already joined clicked")
            try:
                query.answer(popup_already_joined(), show_alert=True, cache_time=0)
            except Exception:
                pass
            return

        # success join -> save participant
        tg_user = query.from_user
        full_name = (tg_user.full_name or "").strip()
        joined_ts = datetime.utcnow().timestamp()

        with lock:
            # mark verified once
            vo = data.get("verified_once", {}) or {}
            vo[uid] = True
            data["verified_once"] = vo

            # first join winner
            if not data.get("first_winner_id"):
                data["first_winner_id"] = uid
                data["first_winner_username"] = uname
                data["first_winner_name"] = full_name
                data["first_winner_at"] = joined_ts

            parts = data.get("participants", {}) or {}
            parts[uid] = {"username": uname, "name": full_name, "joined_at": joined_ts}
            data["participants"] = parts
            save_data()

        log_event("join_success", uid=uid, username=uname, text="Joined giveaway")

        # update live post instantly
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time")
            if live_mid and start_ts:
                start = datetime.fromtimestamp(float(start_ts), timezone.utc)
                nowu = datetime.now(timezone.utc)
                duration = int(data.get("duration_seconds", 1) or 1)
                elapsed = int((nowu - start).total_seconds())
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

        # popup: if first winner -> first popup ALWAYS
        with lock:
            if data.get("first_winner_id") == uid:
                try:
                    query.answer(popup_first_winner(uname or "@username", uid), show_alert=True, cache_time=0)
                except Exception:
                    pass
            else:
                try:
                    query.answer(popup_join_success(uname or "@Username", uid), show_alert=True, cache_time=0)
                except Exception:
                    pass
        return

    # ========== Winners approve/reject ==========
    if qd == "winners_approve":
        if uid != str(ADMIN_ID):
            return

        text = (data.get("pending_winners_text") or "").strip()
        if not text:
            try:
                query.edit_message_text("No pending winners preview found.")
            except Exception:
                pass
            return

        # delete CLOSED post before posting winners
        closed_mid = data.get("closed_message_id")
        if closed_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception:
                pass
            with lock:
                data["closed_message_id"] = None
                save_data()

        # refresh claim deadline at posting time
        with lock:
            data["claim_deadline_ts"] = datetime.utcnow().timestamp() + 24 * 3600
            save_data()

        # post winners to channel
        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=text, reply_markup=claim_button_markup())
            with lock:
                data["winners_message_id"] = m.message_id
                save_data()
        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post winners in channel: {e}")
            except Exception:
                pass
            return

        # auto-save history
        with lock:
            winners_map = data.get("winners", {}) or {}
            hist = data.get("winner_history", []) or []
            for wuid, winfo in winners_map.items():
                hist.append({
                    "uid": wuid,
                    "username": winfo.get("username", ""),
                    "title": data.get("title", ""),
                    "prize": data.get("prize", ""),
                    "win_at": winfo.get("win_at", datetime.utcnow().timestamp()),
                    "type": "🥇 1st Winner (First Join)" if winfo.get("type") == "first" else "👑 Random Winner",
                })
            data["winner_history"] = hist
            save_data()

        try:
            query.edit_message_text("✅ Approved! Winners list posted to channel (with Claim button).")
        except Exception:
            pass
        return

    if qd == "winners_reject":
        if uid != str(ADMIN_ID):
            return
        with lock:
            data["pending_winners_text"] = ""
            save_data()
        try:
            query.edit_message_text("❌ Rejected! Winners list will NOT be posted.")
        except Exception:
            pass
        return

    # ========== Claim prize ==========
    if qd == "claim_prize":
        winners = data.get("winners", {}) or {}
        if uid in winners:
            # check expiry
            deadline = data.get("claim_deadline_ts")
            if deadline and datetime.utcnow().timestamp() > float(deadline):
                try:
                    query.answer(popup_claim_expired(), show_alert=True, cache_time=0)
                except Exception:
                    pass
                return

            uname2 = winners.get(uid, {}).get("username", "") or uname or "@username"
            try:
                query.answer(popup_claim_winner(uname2, uid), show_alert=True, cache_time=0)
            except Exception:
                pass
        else:
            try:
                query.answer(popup_claim_not_winner(), show_alert=True, cache_time=0)
            except Exception:
                pass
        return

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

    # giveaway
    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))
    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))
    dp.add_handler(CommandHandler("winnerlist", cmd_winnerlist))

    # verify
    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    # blocks
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))

    # backrules + manager + reset
    dp.add_handler(CommandHandler("backrules", cmd_backrules))
    dp.add_handler(CommandHandler("manager", cmd_manager))
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # admin text
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))

    # callbacks
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # resume live giveaway if bot restarted
    if data.get("active"):
        start_live_job(updater.job_queue)

    print("Bot is running (PTB 13, non-async, fast callbacks) ...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
