import os
import json
import random
import threading
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
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

# BD timezone
BD_TZ = timezone(timedelta(hours=6))

# Thread-safe lock
lock = threading.RLock()

# =========================================================
# GLOBAL STATE
# =========================================================
data = {}
admin_state = None

live_job = None

draw_job = None
draw_finalize_job = None

auto_draw_job = None
auto_draw_finalize_job = None

reset_job = None
reset_finalize_job = None


# =========================================================
# DEFAULT DATA
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

        "participants": {},  # uid(str) -> {"username":"@x","name":""}

        "verify_targets": [],  # [{"ref":"-100.. or @..","display":"..."}]

        "permanent_block": {},  # uid -> {"username":"@x" optional}

        # old winner protection
        # mode: "block" or "skip"
        "old_winner_mode": "skip",
        "old_winners": {},  # used when mode=block or /blockoldwinner

        # first join winner
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # winners current giveaway
        "winners": {},  # uid -> {"username":"@x"}
        "pending_winners_text": "",

        # claim expiry
        "claim_deadline_ts": None,  # utc timestamp when claim expires

        # auto winner post
        "auto_winner_post": False,

        # winner history (all time)
        "winners_history": [],  # list of dict entries

        # complete prize / delivery proof temp
        "delivery_photos": [],  # list of file_id
        "delivery_pending": False,
        "delivery_caption": "",
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


# =========================================================
# HELPERS
# =========================================================
def bd_now_str():
    dt = datetime.now(BD_TZ)
    return dt.strftime("%d/%m/%Y %H:%M:%S")


def utc_now_ts():
    return datetime.utcnow().timestamp()


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


def build_progress(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    blocks = 10
    filled = int(round(blocks * percent / 100.0))
    empty = blocks - filled
    return "▰" * filled + "▱" * empty


def parse_duration(text: str) -> int:
    """
    Accept:
      30 Second / 30 sec
      30 Minute / 30 min
      2 Hour / 2 hr
      3600
    """
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
    # fixed: button show + done show
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


def autowinner_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Auto Post ON", callback_data="autopost_on"),
            InlineKeyboardButton("❌ Auto Post OFF", callback_data="autopost_off"),
        ]]
    )


def reset_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ Reject", callback_data="reset_reject"),
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


def confirm_reset_ban_markup(kind: str):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_resetban_{kind}"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_resetban"),
        ]]
    )


def delivery_approve_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data="delivery_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="delivery_reject"),
        ]]
    )


# =========================================================
# VERIFY HELPERS
# =========================================================
def normalize_verify_ref(text: str) -> str:
    """
    Accept:
    -1001234567890 (chat id)
    @ChannelName
    https://t.me/ChannelName
    t.me/ChannelName
    """
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
# POPUPS (spacing only, no extra random emoji/words)
# =========================================================
def popup_verify_required_copy_admin() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n\n"
        "To join this giveaway, you must join the required channels/groups first ✅\n"
        "👇 After joining all of them, click JOIN GIVEAWAY again.\n\n"
        "Copy Admin Username:\n"
        f"{ADMIN_CONTACT}"
    )


def popup_access_locked_copy_admin() -> str:
    return (
        "🚫 ACCESS LOCKED\n\n"
        "You left the required channels/groups, so entry is restricted.\n"
        "You can’t join this giveaway right now.\n\n"
        "Copy Admin Username:\n"
        f"{ADMIN_CONTACT}"
    )


def popup_old_winner_blocked_copy_admin() -> str:
    return (
        "🚫 OLD WINNER DETECTED\n\n"
        "You have already won a previous giveaway.\n"
        "To keep it fair, repeat winners are restricted.\n\n"
        "Copy Admin Username:\n"
        f"{ADMIN_CONTACT}"
    )


def popup_permanent_blocked_copy_admin() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n\n"
        "You are permanently blocked from joining giveaways.\n\n"
        "Copy Admin Username:\n"
        f"{ADMIN_CONTACT}"
    )


def popup_first_winner(username: str, uid: str) -> str:
    return (
        "✨CONGRATULATIONS🌟\n"
        "You joined the giveaway FIRST and secured the🥇1st Winner spot!\n"
        f"👤{username}|🆔{uid}\n"
        "📸Screenshot & post in the group to confirm."
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


# This one: keep your old style (NO admin copy)
def popup_already_joined() -> str:
    return (
        "🚫 ENTRY UNSUCCESSFUL\n\n"
        "You’ve already joined this giveaway 🎁\n\n"
        "Only one entry is allowed.\n"
        "Please wait for the final result ⏳"
    )


def popup_claim_winner(username: str, uid: str) -> str:
    # short (your style)
    return (
        "🌟Congratulations✨\n"
        "You’ve won this giveaway.\n"
        f"👤 {username} | 🆔 {uid}\n"
        "📩 please contact admin to claim your prize:\n"
        f"👉 {ADMIN_CONTACT}"
    )


def popup_claim_expired() -> str:
    return (
        "⏳ PRIZE EXPIRED\n\n"
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
def build_user_start_text() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ {HOST_NAME} Giveaway System ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Stay ready for the next giveaway post.\n"
        "Join our official channel now.\n\n"
        "🔗 Official Channel:\n"
        f"{CHANNEL_LINK}"
    )


def build_admin_start_text() -> str:
    return (
        "🛡️👑 WELCOME BACK, ADMIN 👑🛡️\n\n"
        "⚙️ System Status: ONLINE ✅\n"
        "🚀 Giveaway Engine: READY\n"
        "🔐 Security Level: MAXIMUM\n\n"
        "🧭 Open the Admin Control Panel:\n"
        "/panel\n\n"
        f"⚡ POWERED BY: {HOST_NAME}"
    )


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
    # Your 2-line border version + emoji (as you wanted)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        "🎯 Winner selection is currently in progress.\n"
        "Please wait for the official announcement.\n\n"
        "🙏 Thank you to everyone who participated.\n"
        f"— {HOST_NAME} ⚡"
    )


def build_winners_post_text(first_uid: str, first_user: str, other_winners: list) -> str:
    # other_winners: list of tuples [(uid, username_or_empty), ...]
    lines = []
    lines.append("🏆 GIVEAWAY WINNERS ANNOUNCEMENT 🏆")
    lines.append("")
    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    if first_user:
        lines.append(f"👑 {first_user}")
        lines.append(f"🆔 {first_uid}")
    else:
        lines.append(f"🆔 {first_uid}")
    lines.append("🎯 Secured instantly by joining first")
    lines.append("")
    lines.append("👑 OTHER WINNERS (RANDOMLY SELECTED)")
    i = 1
    for uid, uname in other_winners:
        if uname:
            lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}")
        else:
            lines.append(f"{i}️⃣ 👤 User ID: {uid}")
        i += 1
    lines.append("")
    lines.append("🎉 Congratulations to all the winners!")
    lines.append("✅ This giveaway was completed using a")
    lines.append("100% fair & transparent random system.")
    lines.append("")
    lines.append(f"📢 Hosted By: {HOST_NAME}")
    lines.append("👇 Click the button below to claim your prize")
    lines.append("")
    lines.append("⏳ Rule: Claim within 24 hours or it will expire.")
    return "\n".join(lines)


def build_winner_history_text(history: list) -> str:
    if not history:
        return "No winner history found."

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆 WINNER HISTORY LIST")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    for idx, item in enumerate(history, start=1):
        lines.append(f"{idx}) 👤 {item.get('username','')} | 🆔 {item.get('user_id','')}")
        lines.append(f"🎁 Prize: {item.get('prize','')}")
        lines.append(f"⚡ Giveaway: {item.get('giveaway_title','')}")
        lines.append(f"🕒 BD Time: {item.get('bd_time','')}")
        lines.append(f"🏅 Type: {item.get('win_type','')}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# =========================================================
# LIVE UPDATE JOB (every 5 sec)
# =========================================================
def start_live(job_queue):
    global live_job
    stop_live()
    live_job = job_queue.run_repeating(live_tick, interval=5, first=0)


def stop_live():
    global live_job
    if live_job is not None:
        try:
            live_job.schedule_removal()
        except Exception:
            pass
    live_job = None


def live_tick(context: CallbackContext):
    global data
    with lock:
        if not data.get("active"):
            stop_live()
            return

        start_ts = data.get("start_time")
        if not start_ts:
            data["start_time"] = utc_now_ts()
            save_data()
            start_ts = data["start_time"]

        duration = int(data.get("duration_seconds", 1) or 1)
        elapsed = int(datetime.utcnow().timestamp() - float(start_ts))
        remaining = duration - elapsed

        live_mid = data.get("live_message_id")

        # end giveaway
        if remaining <= 0:
            data["active"] = False
            data["closed"] = True
            save_data()

            # delete live
            if live_mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
                except Exception:
                    pass

            # post closed
            try:
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text())
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

            stop_live()

            # auto winner post flow (channel)
            if data.get("auto_winner_post"):
                try:
                    start_auto_draw_in_channel(context)
                except Exception:
                    pass
            return

        if not live_mid:
            return

    # edit outside lock (avoid blocking)
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
# DRAW PROGRESS (2 min, 1 sec updates) - Admin
# =========================================================
DRAW_SECONDS = 120
DRAW_INTERVAL = 1  # fast
DOTS = [".", "..", "...", "....", ".....", "......", "......."]
SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def stop_draw():
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


def start_draw_admin(context: CallbackContext, admin_chat_id: int):
    global draw_job, draw_finalize_job
    stop_draw()

    msg = context.bot.send_message(admin_chat_id, build_draw_progress_text(0, ".", SPINNER[0]))

    ctx = {
        "admin_chat_id": admin_chat_id,
        "admin_msg_id": msg.message_id,
        "start_ts": utc_now_ts(),
        "tick": 0,
    }

    def tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] += 1
        elapsed = max(0, utc_now_ts() - jd["start_ts"])
        percent = int(round(min(100, (elapsed / float(DRAW_SECONDS)) * 100)))

        dots = DOTS[(jd["tick"] - 1) % len(DOTS)]
        spin = SPINNER[(jd["tick"] - 1) % len(SPINNER)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["admin_msg_id"],
                text=build_draw_progress_text(percent, dots, spin),
            )
        except Exception:
            pass

    draw_job = context.job_queue.run_repeating(tick, interval=DRAW_INTERVAL, first=0, context=ctx)
    draw_finalize_job = context.job_queue.run_once(draw_finalize_admin, when=DRAW_SECONDS, context=ctx)


def draw_finalize_admin(context: CallbackContext):
    global data
    stop_draw()

    jd = context.job.context
    admin_chat_id = jd["admin_chat_id"]
    admin_msg_id = jd["admin_msg_id"]

    with lock:
        participants = data.get("participants", {}) or {}
        if not participants:
            try:
                context.bot.edit_message_text(admin_chat_id, admin_msg_id, "No participants to draw winners from.")
            except Exception:
                pass
            return

        winner_count = max(1, int(data.get("winner_count", 1) or 1))

        # First winner id
        first_uid = data.get("first_winner_id")
        if not first_uid:
            first_uid = next(iter(participants.keys()))
            info = participants.get(first_uid, {}) or {}
            data["first_winner_id"] = first_uid
            data["first_winner_username"] = info.get("username", "")
            data["first_winner_name"] = info.get("name", "")

        first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

        pool = [uid for uid in participants.keys() if uid != first_uid]
        remaining_needed = max(0, winner_count - 1)
        remaining_needed = min(remaining_needed, len(pool))
        picked = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

        winners_map = {str(first_uid): {"username": first_uname}}
        other_winners = []
        for uid in picked:
            info = participants.get(uid, {}) or {}
            winners_map[str(uid)] = {"username": info.get("username", "")}
            other_winners.append((str(uid), info.get("username", "")))

        data["winners"] = winners_map

        # claim deadline 24h
        data["claim_deadline_ts"] = utc_now_ts() + 24 * 3600

        pending_text = build_winners_post_text(str(first_uid), first_uname, other_winners)
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
        try:
            context.bot.send_message(admin_chat_id, pending_text, reply_markup=winners_approve_markup())
        except Exception:
            pass


# =========================================================
# AUTO DRAW IN CHANNEL (2 min progress, then auto post winners)
# =========================================================
def stop_auto_draw():
    global auto_draw_job, auto_draw_finalize_job
    if auto_draw_job is not None:
        try:
            auto_draw_job.schedule_removal()
        except Exception:
            pass
    auto_draw_job = None

    if auto_draw_finalize_job is not None:
        try:
            auto_draw_finalize_job.schedule_removal()
        except Exception:
            pass
    auto_draw_finalize_job = None


def start_auto_draw_in_channel(context: CallbackContext):
    """
    Called when giveaway closed and auto_winner_post is ON.
    Posts progress message in channel, updates for 2 min, then posts winners automatically.
    """
    global auto_draw_job, auto_draw_finalize_job
    stop_auto_draw()

    msg = context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=build_draw_progress_text(0, ".", SPINNER[0]),
    )

    ctx = {
        "channel_msg_id": msg.message_id,
        "start_ts": utc_now_ts(),
        "tick": 0,
    }

    def tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] += 1
        elapsed = max(0, utc_now_ts() - jd["start_ts"])
        percent = int(round(min(100, (elapsed / float(DRAW_SECONDS)) * 100)))

        dots = DOTS[(jd["tick"] - 1) % len(DOTS)]
        spin = SPINNER[(jd["tick"] - 1) % len(SPINNER)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=jd["channel_msg_id"],
                text=build_draw_progress_text(percent, dots, spin),
            )
        except Exception:
            pass

    auto_draw_job = context.job_queue.run_repeating(tick, interval=DRAW_INTERVAL, first=0, context=ctx)
    auto_draw_finalize_job = context.job_queue.run_once(auto_draw_finalize, when=DRAW_SECONDS, context=ctx)


def auto_draw_finalize(context: CallbackContext):
    global data
    stop_auto_draw()

    jd = context.job.context
    channel_progress_mid = jd["channel_msg_id"]

    # delete progress message
    try:
        context.bot.delete_message(chat_id=CHANNEL_ID, message_id=channel_progress_mid)
    except Exception:
        pass

    # Prepare winners same logic as admin draw_finalize
    with lock:
        participants = data.get("participants", {}) or {}
        if not participants:
            return

        winner_count = max(1, int(data.get("winner_count", 1) or 1))

        first_uid = data.get("first_winner_id")
        if not first_uid:
            first_uid = next(iter(participants.keys()))
            info = participants.get(first_uid, {}) or {}
            data["first_winner_id"] = first_uid
            data["first_winner_username"] = info.get("username", "")
            data["first_winner_name"] = info.get("name", "")

        first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

        pool = [uid for uid in participants.keys() if uid != first_uid]
        remaining_needed = max(0, winner_count - 1)
        remaining_needed = min(remaining_needed, len(pool))
        picked = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

        winners_map = {str(first_uid): {"username": first_uname}}
        other_winners = []
        for uid in picked:
            info = participants.get(uid, {}) or {}
            winners_map[str(uid)] = {"username": info.get("username", "")}
            other_winners.append((str(uid), info.get("username", "")))

        data["winners"] = winners_map
        data["claim_deadline_ts"] = utc_now_ts() + 24 * 3600

        winners_text = build_winners_post_text(str(first_uid), first_uname, other_winners)
        data["pending_winners_text"] = winners_text
        save_data()

    # Delete closed post before posting winners
    closed_mid = data.get("closed_message_id")
    if closed_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
        except Exception:
            pass
        with lock:
            data["closed_message_id"] = None
            save_data()

    # Post winners automatically
    try:
        m = context.bot.send_message(chat_id=CHANNEL_ID, text=winners_text, reply_markup=claim_button_markup())
        with lock:
            data["winners_message_id"] = m.message_id
            save_data()
    except Exception:
        pass

    # Save winner history automatically (as if approved)
    save_current_winners_to_history()


def save_current_winners_to_history():
    global data
    with lock:
        winners = data.get("winners", {}) or {}
        if not winners:
            return

        title = data.get("title", "")
        prize = data.get("prize", "")
        bdtime = bd_now_str()

        first_uid = str(data.get("first_winner_id") or "")
        for uid, info in winners.items():
            uname = (info or {}).get("username", "") or ""
            win_type = "👑 Random Winner"
            if uid == first_uid:
                win_type = "🥇 1st Winner (First Join)"

            entry = {
                "user_id": str(uid),
                "username": uname if uname else "User",
                "giveaway_title": title,
                "prize": prize,
                "bd_time": bdtime,
                "win_type": win_type,
            }
            data["winners_history"].append(entry)

        save_data()


# =========================================================
# RESET PROGRESS (40s, no numbers)
# =========================================================
RESET_SECONDS = 40
RESET_INTERVAL = 1


def stop_reset():
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


def build_reset_progress_text(percent: int, dots: str, spin: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ FULL RESET\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Progress: {bar}\n"
        f"{spin} Resetting{dots}\n\n"
        "Please wait" + dots
    )


def start_reset_progress(context: CallbackContext, admin_chat_id: int):
    global reset_job, reset_finalize_job
    stop_reset()

    msg = context.bot.send_message(admin_chat_id, build_reset_progress_text(0, ".", SPINNER[0]))

    ctx = {
        "admin_chat_id": admin_chat_id,
        "admin_msg_id": msg.message_id,
        "start_ts": utc_now_ts(),
        "tick": 0,
    }

    def tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] += 1
        elapsed = max(0, utc_now_ts() - jd["start_ts"])
        percent = int(round(min(100, (elapsed / float(RESET_SECONDS)) * 100)))
        dots = DOTS[(jd["tick"] - 1) % len(DOTS)]
        spin = SPINNER[(jd["tick"] - 1) % len(SPINNER)]
        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["admin_msg_id"],
                text=build_reset_progress_text(percent, dots, spin),
            )
        except Exception:
            pass

    reset_job = context.job_queue.run_repeating(tick, interval=RESET_INTERVAL, first=0, context=ctx)
    reset_finalize_job = context.job_queue.run_once(reset_finalize, when=RESET_SECONDS, context=ctx)


def reset_finalize(context: CallbackContext):
    global data
    stop_reset()
    stop_live()
    stop_draw()
    stop_auto_draw()

    # delete channel messages
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

    # FULL RESET (remove ALL)
    with lock:
        data = fresh_default_data()
        save_data()

    jd = context.job.context
    try:
        context.bot.edit_message_text(
            chat_id=jd["admin_chat_id"],
            message_id=jd["admin_msg_id"],
            text=(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ RESET COMPLETED SUCCESSFULLY!\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Bot is now BRAND NEW.\n"
                "All data has been removed.\n\n"
                "Start again with:\n"
                "/newgiveaway"
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
      123
      @name | 123
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
    if u and u.id == ADMIN_ID:
        update.message.reply_text(build_admin_start_text())
    else:
        update.message.reply_text(build_user_start_text())


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
        "/autowinnerpost\n\n"
        "🏆 HISTORY\n"
        "/winnerlist\n\n"
        "🎁 PRIZE DELIVERY\n"
        "/completePrize\n\n"
        "🔒 BLOCK SYSTEM\n"
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


def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ AUTO WINNER POST\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Choose:\n"
        "✅ ON → Giveaway close হলে auto draw + auto winners channel post\n"
        "❌ OFF → close হলে admin notify; আপনি /draw দিবেন",
        reply_markup=autowinner_markup()
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
        # keep verify + ban lists but reset giveaway fields
        keep_verify = data.get("verify_targets", [])
        keep_perma = data.get("permanent_block", {})
        keep_oldw = data.get("old_winners", {})
        keep_mode = data.get("old_winner_mode", "skip")
        keep_auto = data.get("auto_winner_post", False)
        keep_history = data.get("winners_history", [])

        # stop jobs & cleanup messages
        # (don’t delete channel messages here; admin may want to keep old posts)
        data.clear()
        data.update(fresh_default_data())

        data["verify_targets"] = keep_verify
        data["permanent_block"] = keep_perma
        data["old_winners"] = keep_oldw
        data["old_winner_mode"] = keep_mode
        data["auto_winner_post"] = keep_auto
        data["winners_history"] = keep_history
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
    if not (data.get("participants", {}) or {}):
        update.message.reply_text("No participants to draw winners from.")
        return

    start_draw_admin(context, update.effective_chat.id)


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
        "Send list (one per line):\n"
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


def cmd_removeban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "removeban_choose"
    update.message.reply_text("Choose which ban list to reset:", reply_markup=removeban_markup())


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


def cmd_winnerlist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    history = data.get("winners_history", []) or []
    update.message.reply_text(build_winner_history_text(history))


def cmd_completePrize(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    # reset temp delivery state
    with lock:
        data["delivery_photos"] = []
        data["delivery_pending"] = True
        data["delivery_caption"] = ""
        save_data()
    admin_state = "delivery_collect_photos"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ PRIZE DELIVERY PROOF\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send prize delivery photos now.\n"
        "When finished, type: DONE"
    )


def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ FULL RESET CONFIRMATION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "This will remove EVERYTHING.\n"
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

        # Old winner protection choice
        admin_state = "old_winner_mode"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 OLD WINNER PROTECTION MODE\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "1️⃣ BLOCK OLD WINNERS\n"
            "• Old winners cannot join this giveaway\n\n"
            "2️⃣ SKIP OLD WINNERS\n"
            "• Everyone can join\n"
            "• No list needed\n\n"
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
                # no list needed
                save_data()
            admin_state = "rules"
            update.message.reply_text("✅ Old Winner Mode set to: SKIP\n\nNow send Giveaway Rules (multi-line):")
            return

        # block mode needs list
        with lock:
            data["old_winner_mode"] = "block"
            data["old_winners"] = {}
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
                ow[str(uid)] = {"username": uname}
            data["old_winners"] = ow
            save_data()
        admin_state = "rules"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ OLD WINNER BLOCK LIST SAVED!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 Total Added: {len(data['old_winners']) - before}\n"
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

    # MANUAL OLD WINNER BLOCK (/blockoldwinner)
    if admin_state == "oldwinner_block_list_manual":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: @name | user_id OR user_id")
            return
        with lock:
            # ensure mode=block
            data["old_winner_mode"] = "block"
            ow = data.get("old_winners", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[str(uid)] = {"username": uname}
            data["old_winners"] = ow
            save_data()
        admin_state = None
        update.message.reply_text(
            "✅ Old winner block list updated!\n"
            f"New Added: {len(data['old_winners']) - before}\n"
            f"Total Old Winner Blocked: {len(data['old_winners'])}"
        )
        return

    # DELIVERY PHOTO COLLECTION
    if admin_state == "delivery_collect_photos":
        if msg.upper() == "DONE":
            with lock:
                photos = data.get("delivery_photos", []) or []
                data["delivery_pending"] = False
                save_data()
            if not photos:
                admin_state = None
                update.message.reply_text("No photos received. Cancelled.")
                return

            # Build caption (short, no extra long talk)
            with lock:
                winners = data.get("winners", {}) or {}
                title = data.get("title", "")
                prize = data.get("prize", "")
            winner_lines = []
            for uid, info in winners.items():
                uname = (info or {}).get("username", "")
                if uname:
                    winner_lines.append(f"👤 {uname} | 🆔 {uid}")
                else:
                    winner_lines.append(f"🆔 {uid}")
            caption = (
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ PRIZE DELIVERY COMPLETED\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⚡ {title} ⚡\n\n"
                "Delivered to winners ✅\n\n"
                "🏆 Winners:\n" + ("\n".join(winner_lines) if winner_lines else "No winners") +
                "\n\n"
                f"🎁 Prize:\n{prize}\n\n"
                f"— {HOST_NAME} ⚡"
            )

            with lock:
                data["delivery_caption"] = caption
                save_data()

            admin_state = None
            update.message.reply_text(
                "✅ Delivery proof ready.\n\nApprove to post in channel?",
                reply_markup=delivery_approve_markup()
            )
            return

        # if admin typed something else, ignore (photos should come as photo messages)
        update.message.reply_text("Send photos, or type DONE when finished.")
        return


# =========================================================
# PHOTO HANDLER (for /completePrize)
# =========================================================
def admin_photo_handler(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    if admin_state != "delivery_collect_photos":
        return

    if not update.message.photo:
        return
    file_id = update.message.photo[-1].file_id
    with lock:
        data["delivery_photos"].append(file_id)
        save_data()
    update.message.reply_text(f"✅ Added photo ({len(data['delivery_photos'])})")


# =========================================================
# CALLBACK HANDLER
# =========================================================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state, data
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

    # FAST answer attempt
    try:
        query.answer()
    except Exception:
        pass

    # -----------------------------------------------------
    # GLOBAL BLOCK GUARD (any button click)
    # Permanent block => always show same popup on any callback
    if uid in (data.get("permanent_block", {}) or {}) and uid != str(ADMIN_ID):
        try:
            query.answer(popup_permanent_blocked_copy_admin(), show_alert=True)
        except Exception:
            pass
        return

    # Old winner block => if mode=block and in list => show popup on any callback
    if data.get("old_winner_mode") == "block":
        if uid in (data.get("old_winners", {}) or {}) and uid != str(ADMIN_ID):
            try:
                query.answer(popup_old_winner_blocked_copy_admin(), show_alert=True)
            except Exception:
                pass
            return

    # -----------------------------------------------------
    # AUTO WINNER POST toggle
    if qd in ("autopost_on", "autopost_off"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        with lock:
            data["auto_winner_post"] = (qd == "autopost_on")
            save_data()
        try:
            query.edit_message_text(
                "✅ Auto Winner Post: ON" if qd == "autopost_on" else "❌ Auto Winner Post: OFF"
            )
        except Exception:
            pass
        return

    # Verify buttons
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
                duration = int(data.get("duration_seconds", 1) or 1)
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
                data["start_time"] = utc_now_ts()

                # reset per-giveaway
                data["closed_message_id"] = None
                data["winners_message_id"] = None
                data["participants"] = {}
                data["winners"] = {}
                data["pending_winners_text"] = ""
                data["first_winner_id"] = None
                data["first_winner_username"] = ""
                data["first_winner_name"] = ""
                data["claim_deadline_ts"] = None
                save_data()

            start_live(context.job_queue)
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

    # End giveaway confirm/cancel
    if qd == "end_confirm":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
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

        stop_live()
        try:
            query.edit_message_text("✅ Giveaway Closed Successfully! Now use /draw")
        except Exception:
            pass

        # auto flow if enabled
        if data.get("auto_winner_post"):
            try:
                start_auto_draw_in_channel(context)
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
            query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        except Exception:
            pass
        return

    # Reset confirm/reject -> 40s progress then full reset
    if qd == "reset_confirm":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.edit_message_text("✅ Confirmed. Reset will start now...")
        except Exception:
            pass
        start_reset_progress(context, query.message.chat_id)
        return

    if qd == "reset_reject":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.edit_message_text("❌ Reset cancelled.")
        except Exception:
            pass
        return

    # Unban
    if qd == "unban_permanent":
        if uid != str(ADMIN_ID):
            return
        admin_state = "unban_permanent_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Permanent Block:")
        except Exception:
            pass
        return

    if qd == "unban_oldwinner":
        if uid != str(ADMIN_ID):
            return
        admin_state = "unban_oldwinner_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Old Winner Block:")
        except Exception:
            pass
        return

    # removeban choose
    if qd in ("reset_permanent_ban", "reset_oldwinner_ban"):
        if uid != str(ADMIN_ID):
            return
        kind = "permanent" if qd == "reset_permanent_ban" else "oldwinner"
        try:
            query.edit_message_text(
                f"Confirm reset {kind.upper()} ban list?",
                reply_markup=confirm_reset_ban_markup(kind)
            )
        except Exception:
            pass
        return

    if qd == "cancel_resetban":
        if uid != str(ADMIN_ID):
            return
        try:
            query.edit_message_text("Cancelled.")
        except Exception:
            pass
        return

    if qd.startswith("confirm_resetban_"):
        if uid != str(ADMIN_ID):
            return
        kind = qd.split("_", 2)[-1]
        with lock:
            if kind == "permanent":
                data["permanent_block"] = {}
            elif kind == "oldwinner":
                data["old_winners"] = {}
            save_data()
        try:
            query.edit_message_text(f"✅ {kind.upper()} ban list reset completed.")
        except Exception:
            pass
        return

    # Delivery approve/reject
    if qd == "delivery_approve":
        if uid != str(ADMIN_ID):
            return
        with lock:
            photos = data.get("delivery_photos", []) or []
            caption = data.get("delivery_caption", "") or ""
            data["delivery_photos"] = []
            data["delivery_caption"] = ""
            save_data()

        if not photos:
            try:
                query.edit_message_text("No photos found.")
            except Exception:
                pass
            return

        # send media group (caption only on first item)
        media = []
        for i, fid in enumerate(photos):
            if i == 0:
                media.append(InputMediaPhoto(media=fid, caption=caption))
            else:
                media.append(InputMediaPhoto(media=fid))
        try:
            context.bot.send_media_group(chat_id=CHANNEL_ID, media=media)
            query.edit_message_text("✅ Approved! Delivery proof posted in channel.")
        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post: {e}")
            except Exception:
                pass
        return

    if qd == "delivery_reject":
        if uid != str(ADMIN_ID):
            return
        with lock:
            data["delivery_photos"] = []
            data["delivery_caption"] = ""
            save_data()
        try:
            query.edit_message_text("❌ Rejected. Delivery proof not posted.")
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
                query.answer(popup_verify_required_copy_admin(), show_alert=True)
            except Exception:
                pass
            return

        # already closed safety
        if data.get("closed"):
            try:
                query.answer("This giveaway is closed.", show_alert=True)
            except Exception:
                pass
            return

        # First winner clicks again => same popup always
        first_uid = str(data.get("first_winner_id") or "")
        if first_uid and uid == first_uid:
            uname = user_tag(query.from_user.username or "") or (data.get("first_winner_username", "") or "@username")
            try:
                query.answer(popup_first_winner(uname, uid), show_alert=True)
            except Exception:
                pass
            return

        # already joined normal user
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

        is_first = False
        with lock:
            if not data.get("first_winner_id"):
                data["first_winner_id"] = uid
                data["first_winner_username"] = uname
                data["first_winner_name"] = full_name
                is_first = True

            data["participants"][uid] = {"username": uname, "name": full_name}
            save_data()

        # instant update live post (keep 5s job; but we also update once immediately)
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time")
            if live_mid and start_ts:
                duration = int(data.get("duration_seconds", 1) or 1)
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

        # popup response
        if is_first:
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

    # Winners approve/reject (admin)
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

        # delete closed post before posting winners
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

        # Save history automatically on approve
        save_current_winners_to_history()

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

    # Claim prize
    if qd == "claim_prize":
        # verify: must be in required targets too (optional, but you wanted strict)
        if not verify_user_join(context.bot, int(uid)):
            try:
                query.answer(popup_access_locked_copy_admin(), show_alert=True)
            except Exception:
                pass
            return

        winners = data.get("winners", {}) or {}
        if uid in winners:
            # check expiry 24h
            deadline = data.get("claim_deadline_ts")
            if deadline and utc_now_ts() > float(deadline):
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


# =========================================================
# EXTRA ADMIN TEXT HANDLERS (unban inputs)
# =========================================================
def admin_unban_text_handler(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    if admin_state not in ("unban_permanent_input", "unban_oldwinner_input"):
        return

    msg = (update.message.text or "").strip()
    entries = parse_user_lines(msg)
    if not entries:
        update.message.reply_text("Send User ID (or @name | id)")
        return

    uid, _ = entries[0]
    if admin_state == "unban_permanent_input":
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

    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))

    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))

    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    dp.add_handler(CommandHandler("winnerlist", cmd_winnerlist))

    dp.add_handler(CommandHandler("completePrize", cmd_completePrize))
    dp.add_handler(CommandHandler("prizedeliveryprove", cmd_completePrize))  # alias

    dp.add_handler(CommandHandler("reset", cmd_reset))

    # message handlers
    dp.add_handler(MessageHandler(Filters.photo, admin_photo_handler))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_unban_text_handler))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))

    dp.add_handler(CallbackQueryHandler(cb_handler))

    # resume live giveaway after restart
    if data.get("active"):
        start_live(updater.job_queue)

    print("Bot is running (python-telegram-bot v13, non-async) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
