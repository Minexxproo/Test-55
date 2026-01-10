# =========================================================
# POWER POINT BREAK — PREMIUM GIVEAWAY BOT (PTB v13)
# Full A to Z FINAL — All Fix + Auto Winner + Delivery Count
# Language: English | ID based system (username optional)
# =========================================================

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
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@PowerPointBreak")  # only for /start user text
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

draw_job = None
draw_finalize_job = None

# Auto winner (channel) jobs
auto_draw_job = None
auto_draw_finalize_job = None

# =========================================================
# STORAGE MODEL
# =========================================================
def fresh_default_data():
    return {
        # current giveaway
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

        # participants (current giveaway)
        "participants": {},  # uid(str) -> {"username":"@x" or "", "name":""}

        # verify targets
        "verify_targets": [],  # [{"ref":"-100..." or "@xxx", "display":"..."}]

        # permanent block
        "permanent_block": {},  # uid -> {"username":"@x" or ""}

        # giveaway setup old winner mode (per giveaway)
        "old_winner_mode": "skip",  # "block" / "skip"
        "old_winners": {},          # used ONLY when old_winner_mode="block"

        # first join winner (current giveaway)
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # pending winners for admin approval (manual /draw)
        "pending_winners_text": "",
        "pending_winners_mid": None,  # admin preview message id

        # ==========================
        # NEW FEATURES (GLOBAL)
        # ==========================
        "autowinnerpost": False,  # /autowinnerpost ON/OFF

        # forced old winner block (works always, even if setup skip)
        "forced_oldwinner_on": False,     # /blockoldwinner ON/OFF
        "forced_oldwinners": {},          # uid -> {"username":"@x" or ""}

        # Giveaways history for multiple claim posts (unique system)
        # key = winners_message_id(str)
        "giveaways_history": {
            # "12345": {
            #   "title": "",
            #   "prize": "",
            #   "winner_count": 0,
            #   "winners": {uid: {"username":""}},
            #   "first_uid": "....",
            #   "claim_start_ts": float,
            #   "claim_expires_ts": float,
            #   "delivery": {uid: {"username":""}},
            #   "delivery_count": 0
            # }
        },

        # for convenience (last posted winners post id)
        "last_winners_post_id": None,
    }


def load_data():
    base = fresh_default_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}

    # merge defaults
    for k, v in base.items():
        d.setdefault(k, v)

    # ensure nested dict exists
    d.setdefault("giveaways_history", {})
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


def format_hms(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def safe_line(n: int = 22) -> str:
    return "━" * n


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


def participants_count() -> int:
    return len(data.get("participants", {}) or {})


def format_rules_text(rules_raw: str) -> str:
    rules = (rules_raw or "").strip()
    if not rules:
        return (
            "✅ Must join official channel\n"
            "❌ One account per user\n"
            "🚫 No fake / duplicate accounts"
        )
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    # keep compact, no bullet spam
    return "\n".join(lines)


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


def toggle_onoff_markup(prefix: str):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🟢 ON", callback_data=f"{prefix}_on"),
            InlineKeyboardButton("🔴 OFF", callback_data=f"{prefix}_off"),
        ]]
    )


# =========================================================
# POPUPS (ALL ENGLISH — FIXED)
# =========================================================
def popup_verify_required() -> str:
    return (
        "🔐 Access Restricted\n"
        "You must join the required channels to proceed.\n"
        "After joining, tap JOIN once more."
    )


def popup_old_winner_blocked() -> str:
    return (
        "🚫 You are restricted.\n"
        "Repeat winners are not allowed in this giveaway.\n"
        "Please wait for the next giveaway."
    )


def popup_first_winner(username: str, uid: str) -> str:
    return (
        "✨ CONGRATULATIONS 🌟\n"
        "You joined FIRST and secured the 🥇 1st Winner Spot!\n"
        f"👑 {username} | {uid}\n"
        "Take a screenshot & Post in the group to confirm your win 👈"
    )


def popup_already_joined() -> str:
    return (
        "❌ ENTRY Unsuccessful\n"
        "You’ve already joined\n"
        "this giveaway 🫵\n\n"
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
        "🌟Congratulations ✨\n"
        "You’ve won this giveaway.✅\n"
        f"👤 {username} | 🆔 {uid}\n"
        "📩 Please contact admin to claim your prize now:\n"
        f"👉 {ADMIN_CONTACT}"
    )


def popup_claim_already_delivered() -> str:
    return (
        "🌟 Congratulations!\n"
        "Your prize has already been successfully delivered to you ✅\n"
        f"If you face any issues, please contact our admin 📩 {ADMIN_CONTACT}"
    )


def popup_claim_not_winner() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "❌ NOT A WINNER\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Sorry! Your User ID is not in the winners list.\n"
        "Please wait for the next giveaway 🤍"
    )


def popup_prize_expired() -> str:
    return (
        "⏳ PRIZE EXPIRED\n"
        "Your 24-hour claim time has ended.\n"
        "This prize is no longer available."
    )


# =========================================================
# TEXT BUILDERS (NO <pre> — NO COPY CODE)
# MOBILE SAFE LINES (NO BORDER BREAK)
# =========================================================
def build_preview_text() -> str:
    remaining = int(data.get("duration_seconds", 0) or 0)
    progress = build_progress(0)

    title = (data.get("title") or "").strip()
    prize = (data.get("prize") or "").rstrip()
    wc = int(data.get("winner_count", 0) or 0)
    rules = format_rules_text(data.get("rules", ""))

    return (
        f"{safe_line()}\n"
        "🔍 GIVEAWAY PREVIEW\n"
        f"{safe_line()}\n\n"
        f"⚡ {title} ⚡\n\n"
        "🎁 PRIZE POOL ✨\n"
        f"{prize}\n\n"
        f"👥 PARTICIPANTS: 0\n"
        f"🏆 WINNERS: {wc}\n"
        "🎯 SELECTION: Random\n\n"
        "⏳ TIME LEFT\n"
        f"{format_hms(remaining).replace(':',' : ')}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{progress} 0%\n\n"
        "📜 RULES\n"
        f"{rules}\n\n"
        f"📢 HOSTED BY: {HOST_NAME}\n\n"
        "👇 TAP JOIN BUTTON 👇"
    )


def build_live_text(remaining: int) -> str:
    duration = int(data.get("duration_seconds", 1) or 1)
    elapsed = max(0, duration - remaining)
    percent = int(round((elapsed / float(duration)) * 100))
    bar = build_progress(percent)

    title = (data.get("title") or "").strip()
    prize = (data.get("prize") or "").rstrip()
    wc = int(data.get("winner_count", 0) or 0)
    rules = format_rules_text(data.get("rules", ""))

    return (
        f"{safe_line()}\n"
        f"⚡ {title} ⚡\n"
        f"{safe_line()}\n\n"
        "🎁 PRIZE POOL ✨\n"
        f"{prize}\n\n"
        f"👥 PARTICIPANTS: {participants_count()}\n"
        f"🏆 WINNERS: {wc}\n"
        "🎯 SELECTION: Random\n\n"
        "⏳ TIME LEFT\n"
        f"{format_hms(remaining).replace(':',' : ')}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{bar} {percent}%\n\n"
        "📜 RULES\n"
        f"{rules}\n\n"
        f"📢 HOSTED BY: {HOST_NAME}\n\n"
        "👇 TAP JOIN BUTTON 👇"
    )


SPINNER = ["🔄", "🔃", "🔁", "🔂"]


def build_closed_post_text_static() -> str:
    title = (data.get("title") or "").strip()
    return (
        f"{safe_line()}\n"
        "🚫 GIVEAWAY CLOSED 🚫\n"
        f"{safe_line()}\n\n"
        f"⚡ {title} ⚡\n\n"
        "⏰ Giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {int(data.get('winner_count',0) or 0)}\n\n"
        "— " + HOST_NAME + " ⚡"
    )


def build_closed_post_text_spinner(spin: str) -> str:
    title = (data.get("title") or "").strip()
    return (
        f"{safe_line()}\n"
        "🚫 GIVEAWAY CLOSED 🚫\n"
        f"{safe_line()}\n\n"
        f"⚡ {title} ⚡\n\n"
        "⏰ Giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {int(data.get('winner_count',0) or 0)}\n\n"
        f"{spin} Winner selection in progress\n"
        "Please wait for announcement.\n\n"
        "🙏 Thank you for participating.\n"
        "— " + HOST_NAME + " ⚡"
    )


def build_draw_progress_text(percent: int, spin: str) -> str:
    bar = build_progress(percent)
    return (
        f"{safe_line()}\n"
        "🎲 RANDOM WINNER\n"
        "SELECTION\n"
        f"{safe_line()}\n\n"
        f"{spin} Selecting winners...\n\n"
        "📊 LIVE PROGRESS\n"
        f"{bar} {percent}%\n\n"
        "✅ Random & Fair\n"
        "🔐 User ID based only.\n\n"
        f"— {HOST_NAME} ⚡"
    )


def build_winners_post_text(title: str, prize: str, winner_count: int, delivery_count: int,
                           first_uid: str, first_user: str, random_winners: list) -> str:
    # NO numbering requirement was only for progress stage.
    # Winner list can show numbering (premium). Keeping numbering.
    lines = []
    lines.append("🏆 GIVEAWAY WINNERS ANNOUNCEMENT 🏆")
    lines.append("")
    if title:
        lines.append(f"⚡ {title} ⚡")
        lines.append("")
    if prize:
        lines.append("🎁 PRIZE:")
        lines.append(prize)
    lines.append(f"Winner Count: {winner_count}")
    lines.append(f"Prize delivery: {delivery_count}/{winner_count}")
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
    if not random_winners:
        lines.append("—")
    else:
        i = 1
        for uid, uname in random_winners:
            if uname:
                lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}")
            else:
                lines.append(f"{i}️⃣ 👤 User ID: {uid}")
            i += 1

    lines.append("")
    lines.append("⏳ Claim Rule:")
    lines.append("Prizes must be claimed within 24 hours.")
    lines.append("After 24 hours, claim will expire.")
    lines.append("")
    lines.append(f"📢 Hosted By: {HOST_NAME}")
    lines.append("👇 Click the button below to claim your prize")

    return "\n".join(lines)


# =========================================================
# CHANNEL SEND/EDIT (NO PARSE_MODE => NO COPY CODE)
# =========================================================
def ch_send(bot, text: str, reply_markup=None):
    return bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )


def ch_edit(bot, mid: int, text: str, reply_markup=None):
    return bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=mid,
        text=text,
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )


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
        duration = int(data.get("duration_seconds", 1) or 1)
        elapsed = int((datetime.utcnow() - start).total_seconds())
        remaining = duration - elapsed

        live_mid = data.get("live_message_id")

        if remaining <= 0:
            # auto close
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
                if data.get("autowinnerpost"):
                    m = ch_send(context.bot, build_closed_post_text_static())
                    data["closed_message_id"] = m.message_id
                    save_data()

                    # start channel auto draw (3 min)
                    start_auto_draw_channel(context)
                else:
                    m = ch_send(context.bot, build_closed_post_text_spinner(SPINNER[0]))
                    data["closed_message_id"] = m.message_id
                    save_data()
                    start_closed_anim(context.job_queue)
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
                        + ("Auto Winner Post: ON ✅\nChannel will post winners automatically."
                           if data.get("autowinnerpost") else
                           "Now use /draw to select winners.")
                    ),
                )
            except Exception:
                pass

            stop_live_countdown()
            return

        if not live_mid:
            return

        try:
            ch_edit(context.bot, live_mid, build_live_text(remaining), reply_markup=join_button_markup())
        except Exception:
            pass


# =========================================================
# JOBS: CLOSED SPINNER ANIM (ONLY when autowinnerpost OFF)
# =========================================================
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
        interval=5,   # every 5 seconds (your requirement)
        first=0,
        context={"tick": 0},
        name="closed_anim",
    )


def closed_anim_tick(context: CallbackContext):
    with lock:
        mid = data.get("closed_message_id")
        if not mid:
            stop_closed_anim()
            return
        if data.get("autowinnerpost"):
            # should not run in ON mode
            stop_closed_anim()
            return

    tick = int(context.job.context.get("tick", 0)) + 1
    context.job.context["tick"] = tick
    spin = SPINNER[(tick - 1) % len(SPINNER)]
    try:
        ch_edit(context.bot, mid, build_closed_post_text_spinner(spin))
    except Exception:
        pass


# =========================================================
# MANUAL DRAW (ADMIN) — 40s, update every 5 seconds
# =========================================================
DRAW_DURATION_SECONDS = 40
DRAW_UPDATE_INTERVAL = 5  # every 5 seconds (your requirement)

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


def start_draw_progress(context: CallbackContext, admin_chat_id: int):
    global draw_job, draw_finalize_job
    stop_draw_jobs()

    msg = context.bot.send_message(
        chat_id=admin_chat_id,
        text=build_draw_progress_text(0, SPINNER[0]),
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
        spin = SPINNER[(tick - 1) % len(SPINNER)]

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

        winner_count = int(data.get("winner_count", 1) or 1)
        winner_count = max(1, winner_count)

        first_uid = data.get("first_winner_id")
        if not first_uid:
            first_uid = next(iter(participants.keys()))
            info = participants.get(first_uid, {}) or {}
            data["first_winner_id"] = first_uid
            data["first_winner_username"] = info.get("username", "")
            data["first_winner_name"] = info.get("name", "")
            save_data()

        first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

        pool = [uid for uid in participants.keys() if uid != first_uid]
        remaining_needed = max(0, winner_count - 1)
        remaining_needed = min(remaining_needed, len(pool))
        selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

        winners_map = {first_uid: {"username": first_uname}}
        random_list = []
        for uid in selected:
            info = participants.get(uid, {}) or {}
            winners_map[uid] = {"username": info.get("username", "")}
            random_list.append((uid, info.get("username", "")))

        # store pending preview
        pending_text = build_winners_post_text(
            title=(data.get("title") or "").strip(),
            prize=(data.get("prize") or "").rstrip(),
            winner_count=winner_count,
            delivery_count=0,
            first_uid=first_uid,
            first_user=first_uname,
            random_winners=random_list,
        )
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
# AUTO WINNER POST (CHANNEL) — 3 minutes progress
# =========================================================
AUTO_DRAW_SECONDS = 180
AUTO_DRAW_INTERVAL = 5  # every 5 seconds

def stop_auto_draw_jobs():
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


def start_auto_draw_channel(context: CallbackContext):
    """Start 3 minute progress in channel then post winners + claim button automatically."""
    global auto_draw_job, auto_draw_finalize_job
    stop_auto_draw_jobs()

    # post progress message
    try:
        msg = ch_send(context.bot, build_draw_progress_text(0, SPINNER[0]))
    except Exception:
        return

    ctx = {
        "mid": msg.message_id,
        "start_ts": now_ts(),
        "tick": 0,
    }

    def auto_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        tick = int(jd.get("tick", 0)) + 1
        jd["tick"] = tick

        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(AUTO_DRAW_SECONDS)) * 100)))
        spin = SPINNER[(tick - 1) % len(SPINNER)]

        try:
            ch_edit(job_ctx.bot, jd["mid"], build_draw_progress_text(percent, spin))
        except Exception:
            pass

    auto_draw_job = context.job_queue.run_repeating(
        auto_tick,
        interval=AUTO_DRAW_INTERVAL,
        first=0,
        context=ctx,
        name="auto_draw_channel_progress",
    )

    auto_draw_finalize_job = context.job_queue.run_once(
        auto_draw_finalize,
        when=AUTO_DRAW_SECONDS,
        context=ctx,
        name="auto_draw_channel_finalize",
    )


def auto_draw_finalize(context: CallbackContext):
    stop_auto_draw_jobs()

    mid = context.job.context.get("mid")
    if not mid:
        return

    with lock:
        participants = data.get("participants", {}) or {}
        if not participants:
            # edit progress into "no participants"
            try:
                ch_edit(context.bot, mid, "No participants to draw winners from.")
            except Exception:
                pass
            return

        winner_count = int(data.get("winner_count", 1) or 1)
        winner_count = max(1, winner_count)

        first_uid = data.get("first_winner_id")
        if not first_uid:
            first_uid = next(iter(participants.keys()))
            info = participants.get(first_uid, {}) or {}
            data["first_winner_id"] = first_uid
            data["first_winner_username"] = info.get("username", "")
            data["first_winner_name"] = info.get("name", "")
            save_data()

        first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

        pool = [uid for uid in participants.keys() if uid != first_uid]
        remaining_needed = max(0, winner_count - 1)
        remaining_needed = min(remaining_needed, len(pool))
        selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

        winners_map = {first_uid: {"username": first_uname}}
        random_list = []
        for uid in selected:
            info = participants.get(uid, {}) or {}
            winners_map[uid] = {"username": info.get("username", "")}
            random_list.append((uid, info.get("username", "")))

        title = (data.get("title") or "").strip()
        prize = (data.get("prize") or "").rstrip()

        winners_text = build_winners_post_text(
            title=title,
            prize=prize,
            winner_count=winner_count,
            delivery_count=0,
            first_uid=first_uid,
            first_user=first_uname,
            random_winners=random_list,
        )

        # store history for unique claim posts
        claim_start = now_ts()
        record = {
            "title": title,
            "prize": prize,
            "winner_count": winner_count,
            "winners": winners_map,
            "first_uid": first_uid,
            "claim_start_ts": claim_start,
            "claim_expires_ts": claim_start + 24 * 3600,
            "delivery": {},
            "delivery_count": 0,
        }

    # edit progress message into winners list + claim button
    try:
        ch_edit(context.bot, mid, winners_text, reply_markup=claim_button_markup())
    except Exception:
        try:
            # fallback: send new winners post
            m2 = ch_send(context.bot, winners_text, reply_markup=claim_button_markup())
            mid = m2.message_id
        except Exception:
            return

    # remove old closed post if exists
    with lock:
        closed_mid = data.get("closed_message_id")
    if closed_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
        except Exception:
            pass

    with lock:
        data["last_winners_post_id"] = str(mid)
        data["giveaways_history"][str(mid)] = record
        data["closed_message_id"] = None
        save_data()

    # schedule claim expiry for this post
    schedule_claim_expire(context.job_queue, str(mid))


# =========================================================
# CLAIM EXPIRY (PER POST)
# =========================================================
def schedule_claim_expire(job_queue, winners_mid_str: str):
    with lock:
        rec = (data.get("giveaways_history", {}) or {}).get(str(winners_mid_str))
        if not rec:
            return
        exp = rec.get("claim_expires_ts")
        if not exp:
            return
        remain = float(exp) - now_ts()
        if remain <= 0:
            return

    # job context
    job_queue.run_once(
        expire_claim_button_job,
        when=remain,
        context={"mid": int(winners_mid_str)},
        name=f"claim_expire_{winners_mid_str}",
    )


def expire_claim_button_job(context: CallbackContext):
    mid = context.job.context.get("mid")
    if not mid:
        return
    try:
        context.bot.edit_message_reply_markup(chat_id=CHANNEL_ID, message_id=mid, reply_markup=None)
    except Exception:
        pass


# =========================================================
# RESET (FULL RESET ALL FEATURES)
# =========================================================
def do_full_reset_all(context: CallbackContext, admin_chat_id: int, admin_msg_id: int):
    global data, admin_state

    stop_live_countdown()
    stop_closed_anim()
    stop_draw_jobs()
    stop_auto_draw_jobs()

    with lock:
        # best effort delete current live/closed
        for mid_key in ["live_message_id", "closed_message_id"]:
            mid = data.get(mid_key)
            if mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
                except Exception:
                    pass

        data = fresh_default_data()
        save_data()
        admin_state = None

    try:
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            text=(
                f"{safe_line()}\n"
                "✅ FULL RESET COMPLETED!\n"
                f"{safe_line()}\n\n"
                "All features and all data have been reset.\n"
                "Start again with:\n"
                "/newgiveaway"
            ),
        )
    except Exception:
        pass


# =========================================================
# COMMANDS
# =========================================================
def cmd_start(update: Update, context: CallbackContext):
    u = update.effective_user
    if u and u.id == ADMIN_ID:
        update.message.reply_text(
            "🛡️ ADMIN PANEL READY ✅\n\n"
            "/panel"
        )
    else:
        update.message.reply_text(
            f"{safe_line()}\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            f"{safe_line()}\n\n"
            "Please join our official channel and wait for the giveaway post.\n\n"
            f"🔗 Channel: {CHANNEL_LINK}"
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
        "✅ VERIFY\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "🔒 BLOCK\n"
        "/blockpermanent\n"
        "/unban\n"
        "/blocklist\n"
        "/removeban\n\n"
        "🧩 EXTRA\n"
        "/autowinnerpost\n"
        "/blockoldwinner\n"
        "/prizedelivery\n\n"
        "♻️ FULL RESET\n"
        "/reset"
    )


def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "Auto Winner Post (Channel)\n\n"
        "🟢 ON  → giveaway ends → bot auto selects & posts winners\n"
        "🔴 OFF → you will use /draw manually\n\n"
        "Choose an option below:",
        reply_markup=toggle_onoff_markup("autowinnerpost"),
    )


def cmd_blockoldwinner(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "Forced Old Winner Block\n\n"
        "🟢 ON  → users in list cannot join (always)\n"
        "🔴 OFF → feature disabled\n\n"
        "Choose:",
        reply_markup=toggle_onoff_markup("forcedoldwinner"),
    )


def cmd_prizedelivery(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "prize_delivery_input"
    update.message.reply_text(
        f"{safe_line()}\n"
        "🏆 PRIZE DELIVERY\n"
        f"{safe_line()}\n\n"
        "Send list (one per line):\n"
        "User ID only OR username + id\n\n"
        "Examples:\n"
        "8293728\n"
        "@minexxproo | 8293728\n\n"
        "Bot will mark them as delivered and update\n"
        "Prize delivery count on the winners post automatically."
    )


def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        f"{safe_line()}\n"
        "✅ ADD VERIFY TARGET\n"
        f"{safe_line()}\n\n"
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
        f"{safe_line()}",
        "🗑 REMOVE VERIFY TARGET",
        f"{safe_line()}",
        "",
        "Current Verify Targets:",
        "",
    ]
    for i, t in enumerate(targets, start=1):
        lines.append(f"{i}) {t.get('display','')}")
    lines += [
        "",
        "Send a number to remove that target.",
        "99) Remove ALL verify targets",
    ]
    admin_state = "remove_verify_pick"
    update.message.reply_text("\n".join(lines))


def cmd_newgiveaway(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return

    stop_live_countdown()
    stop_draw_jobs()
    stop_auto_draw_jobs()
    stop_closed_anim()

    with lock:
        # keep global settings/lists
        keep_verify = data.get("verify_targets", [])
        keep_perma = data.get("permanent_block", {})
        keep_autow = bool(data.get("autowinnerpost"))
        keep_forced_on = bool(data.get("forced_oldwinner_on"))
        keep_forced_list = data.get("forced_oldwinners", {})
        keep_history = data.get("giveaways_history", {})
        keep_last = data.get("last_winners_post_id")

        # reset current giveaway only
        base = fresh_default_data()
        base["verify_targets"] = keep_verify
        base["permanent_block"] = keep_perma
        base["autowinnerpost"] = keep_autow
        base["forced_oldwinner_on"] = keep_forced_on
        base["forced_oldwinners"] = keep_forced_list
        base["giveaways_history"] = keep_history
        base["last_winners_post_id"] = keep_last

        data.clear()
        data.update(base)
        save_data()

    admin_state = "title"
    update.message.reply_text(
        f"{safe_line()}\n"
        "🆕 NEW GIVEAWAY SETUP\n"
        f"{safe_line()}\n\n"
        "STEP 1 — Send Giveaway Title:"
    )


def cmd_participants(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    parts = data.get("participants", {}) or {}
    if not parts:
        update.message.reply_text("👥 Participants list is empty.")
        return

    lines = [
        f"{safe_line()}",
        "👥 PARTICIPANTS (ADMIN)",
        f"{safe_line()}",
        f"Total: {len(parts)}",
        "",
    ]
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
        update.message.reply_text("No active giveaway is running right now.")
        return

    update.message.reply_text(
        f"{safe_line()}\n"
        "⚠️ END GIVEAWAY\n"
        f"{safe_line()}\n\n"
        "Are you sure you want to end this giveaway now?",
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
        f"{safe_line()}\n"
        "🔒 PERMANENT BLOCK\n"
        f"{safe_line()}\n\n"
        "Send list (one per line):\n"
        "User ID only OR username + id\n\n"
        "Examples:\n"
        "7297292\n"
        "@user | 7297292"
    )


def cmd_unban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "unban_choose"
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Unban Permanent Block", callback_data="unban_permanent"),
            InlineKeyboardButton("Unban Giveaway Old Winner List", callback_data="unban_oldwinner"),
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
            InlineKeyboardButton("Reset Giveaway Old Winner List", callback_data="reset_oldwinner_ban"),
        ]]
    )
    update.message.reply_text("Choose which ban list to reset:", reply_markup=kb)


def cmd_blocklist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    perma = data.get("permanent_block", {}) or {}
    ow = data.get("old_winners", {}) or {}
    forced_on = bool(data.get("forced_oldwinner_on"))
    forced_list = data.get("forced_oldwinners", {}) or {}

    lines = []
    lines.append(f"{safe_line()}")
    lines.append("📌 BAN LISTS")
    lines.append(f"{safe_line()}")
    lines.append("")
    lines.append(f"Giveaway Old Winner Mode: {data.get('old_winner_mode','skip').upper()}")
    lines.append("")

    lines.append("⛔ GIVEAWAY OLD WINNER LIST")
    lines.append(f"Total: {len(ow)}")
    if ow:
        i = 1
        for uid, info in ow.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {uname+' | ' if uname else ''}{uid}")
            i += 1
    else:
        lines.append("No users.")
    lines.append("")

    lines.append(f"🧩 Forced Old Winner Block: {'ON' if forced_on else 'OFF'}")
    lines.append(f"Total: {len(forced_list)}")
    if forced_list:
        i = 1
        for uid, info in forced_list.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {uname+' | ' if uname else ''}{uid}")
            i += 1
    else:
        lines.append("No users.")
    lines.append("")

    lines.append("🔒 PERMANENT BLOCK LIST")
    lines.append(f"Total: {len(perma)}")
    if perma:
        i = 1
        for uid, info in perma.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {uname+' | ' if uname else ''}{uid}")
            i += 1
    else:
        lines.append("No users.")

    update.message.reply_text("\n".join(lines))


def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    m = update.message.reply_text(
        "Confirm FULL RESET?\n\nThis will reset ALL features and ALL data.",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
            ]]
        )
    )
    context.user_data["reset_msg_id"] = m.message_id


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

    # VERIFY ADD
    if admin_state == "add_verify":
        ref = normalize_verify_ref(msg)
        if not ref:
            update.message.reply_text("Invalid input.\nSend Chat ID like -100123... or @username.")
            return

        with lock:
            targets = data.get("verify_targets", []) or []
            if len(targets) >= 100:
                update.message.reply_text("Max verify targets reached (100).")
                return
            targets.append({"ref": ref, "display": ref})
            data["verify_targets"] = targets
            save_data()

        update.message.reply_text(
            f"{safe_line()}\n"
            "✅ VERIFY TARGET ADDED\n"
            f"{safe_line()}\n\n"
            f"Added: {ref}\n"
            f"Total: {len(data.get('verify_targets', []) or [])}\n\n"
            "What next?",
            reply_markup=verify_add_more_done_markup()
        )
        return

    # VERIFY REMOVE
    if admin_state == "remove_verify_pick":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number (1,2,3... or 99).")
            return
        n = int(msg)

        with lock:
            targets = data.get("verify_targets", []) or []
            if not targets:
                admin_state = None
                update.message.reply_text("No verify targets remain.")
                return

            if n == 99:
                data["verify_targets"] = []
                save_data()
                admin_state = None
                update.message.reply_text("✅ All verify targets removed!")
                return

            if n < 1 or n > len(targets):
                update.message.reply_text("Invalid number.")
                return

            removed = targets.pop(n - 1)
            data["verify_targets"] = targets
            save_data()

        admin_state = None
        update.message.reply_text(
            f"{safe_line()}\n"
            "✅ VERIFY TARGET REMOVED\n"
            f"{safe_line()}\n\n"
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
            update.message.reply_text("Please send a valid number.")
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
            f"{safe_line()}\n"
            "🔐 GIVEAWAY OLD WINNER MODE\n"
            f"{safe_line()}\n\n"
            "1️⃣ BLOCK OLD WINNERS (cannot join)\n"
            "2️⃣ SKIP (everyone can join)\n\n"
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
            update.message.reply_text(
                "📌 Old Winner Mode: SKIP\n"
                "✅ Everyone can join.\n\n"
                "Now send Giveaway Rules (multi-line):"
            )
            return

        with lock:
            data["old_winner_mode"] = "block"
            data["old_winners"] = {}
            save_data()

        admin_state = "old_winner_block_list"
        update.message.reply_text(
            f"{safe_line()}\n"
            "⛔ GIVEAWAY OLD WINNER LIST\n"
            f"{safe_line()}\n\n"
            "Send list (one per line):\n"
            "@username | user_id   OR   user_id"
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
            f"{safe_line()}\n"
            "✅ OLD WINNER LIST SAVED\n"
            f"{safe_line()}\n\n"
            f"Added: {len(data['old_winners']) - before}\n\n"
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
            f"Added: {len(data['permanent_block']) - before}\n"
            f"Total: {len(data['permanent_block'])}"
        )
        return

    # PRIZE DELIVERY INPUT
    if admin_state == "prize_delivery_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return

        with lock:
            last_mid = data.get("last_winners_post_id")
            if not last_mid:
                admin_state = None
                update.message.reply_text("No winners post found to update delivery.")
                return
            rec = (data.get("giveaways_history", {}) or {}).get(str(last_mid))
            if not rec:
                admin_state = None
                update.message.reply_text("Winners post record not found.")
                return

            winners = rec.get("winners", {}) or {}
            delivery = rec.get("delivery", {}) or {}

            new_added = 0
            for uid, uname in entries:
                # only winners can be delivered
                if uid in winners and uid not in delivery:
                    delivery[uid] = {"username": uname or winners.get(uid, {}).get("username", "")}
                    new_added += 1

            rec["delivery"] = delivery
            rec["delivery_count"] = len(delivery)
            data["giveaways_history"][str(last_mid)] = rec
            save_data()

            # rebuild winners text with updated delivery count
            title = rec.get("title", "")
            prize = rec.get("prize", "")
            wc = int(rec.get("winner_count", 0) or 0)
            dc = int(rec.get("delivery_count", 0) or 0)
            first_uid = rec.get("first_uid", "")
            first_user = (winners.get(first_uid, {}) or {}).get("username", "")

            # rebuild random winners list (exclude first)
            random_list = []
            for uid in winners.keys():
                if uid == first_uid:
                    continue
                random_list.append((uid, (winners.get(uid, {}) or {}).get("username", "")))

            new_text = build_winners_post_text(
                title=title,
                prize=prize,
                winner_count=wc,
                delivery_count=dc,
                first_uid=first_uid,
                first_user=first_user,
                random_winners=random_list,
            )

        # edit channel post (NO new post)
        try:
            ch_edit(context.bot, int(last_mid), new_text, reply_markup=claim_button_markup())
        except Exception:
            pass

        admin_state = None
        update.message.reply_text(
            f"✅ Prize delivery updated!\n"
            f"New delivered added: {new_added}\n"
            f"Total delivered: {dc}/{wc}"
        )
        return

    # UNBAN INPUT HANDLERS
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
                update.message.reply_text("✅ Unbanned from Permanent Block!")
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
                update.message.reply_text("✅ Unbanned from Giveaway Old Winner list!")
            else:
                update.message.reply_text("This user id is not in Giveaway Old Winner list.")
        admin_state = None
        return

    if admin_state == "forced_oldwinner_list_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            ow = data.get("forced_oldwinners", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[uid] = {"username": uname}
            data["forced_oldwinners"] = ow
            save_data()
        admin_state = None
        update.message.reply_text(
            f"✅ Forced Old Winner list updated!\n"
            f"Added: {len(data['forced_oldwinners']) - before}\n"
            f"Total: {len(data['forced_oldwinners'])}"
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

    # toggle autowinnerpost
    if qd in ("autowinnerpost_on", "autowinnerpost_off"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        on = (qd == "autowinnerpost_on")
        with lock:
            data["autowinnerpost"] = on
            save_data()
        try:
            query.answer()
        except Exception:
            pass
        try:
            query.edit_message_text(f"✅ Auto Winner Post set to: {'ON' if on else 'OFF'}")
        except Exception:
            pass
        return

    # toggle forced old winner block
    if qd in ("forcedoldwinner_on", "forcedoldwinner_off"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        on = (qd == "forcedoldwinner_on")
        with lock:
            data["forced_oldwinner_on"] = on
            save_data()
        try:
            query.answer()
        except Exception:
            pass

        if on:
            admin_state = "forced_oldwinner_list_input"
            try:
                query.edit_message_text(
                    "✅ Forced Old Winner Block: ON\n\n"
                    "Now send list (one per line):\n"
                    "@username | user_id   OR   user_id"
                )
            except Exception:
                pass
        else:
            admin_state = None
            try:
                query.edit_message_text("✅ Forced Old Winner Block: OFF")
            except Exception:
                pass
        return

    # verify buttons
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
                f"{safe_line()}\n"
                "✅ VERIFY SETUP DONE\n"
                f"{safe_line()}\n\n"
                f"Total targets: {len(data.get('verify_targets', []) or [])}"
            )
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
                duration = int(data.get("duration_seconds", 0) or 1)
                m = ch_send(
                    context.bot,
                    build_live_text(duration),
                    reply_markup=join_button_markup(),
                )

                with lock:
                    data["live_message_id"] = m.message_id
                    data["active"] = True
                    data["closed"] = False
                    data["start_time"] = now_ts()
                    data["closed_message_id"] = None

                    # reset current giveaway members
                    data["participants"] = {}
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""
                    data["pending_winners_text"] = ""
                    save_data()

                stop_closed_anim()
                start_live_countdown(context.job_queue)

                query.edit_message_text("✅ Giveaway approved and posted to channel!")
            except Exception as e:
                query.edit_message_text(f"Failed to post in channel.\nError: {e}")
            return

        if qd == "preview_reject":
            try:
                query.answer()
            except Exception:
                pass
            query.edit_message_text("❌ Giveaway rejected.")
            return

        if qd == "preview_edit":
            try:
                query.answer()
            except Exception:
                pass
            query.edit_message_text("✏️ Edit Mode\n\nStart again with /newgiveaway")
            return

    # end giveaway confirm/cancel
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

        live_mid = data.get("live_message_id")
        if live_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception:
                pass

        try:
            if data.get("autowinnerpost"):
                m = ch_send(context.bot, build_closed_post_text_static())
                with lock:
                    data["closed_message_id"] = m.message_id
                    save_data()
                start_auto_draw_channel(context)
            else:
                m = ch_send(context.bot, build_closed_post_text_spinner(SPINNER[0]))
                with lock:
                    data["closed_message_id"] = m.message_id
                    save_data()
                start_closed_anim(context.job_queue)
        except Exception:
            pass

        stop_live_countdown()
        try:
            query.edit_message_text(
                "✅ Giveaway closed!\n\n" +
                ("Auto Winner Post: ON ✅ (Channel will post winners automatically)" if data.get("autowinnerpost")
                 else "Now use /draw")
            )
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

    # reset confirm/cancel
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
        do_full_reset_all(context, query.message.chat_id, query.message.message_id)
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
        try:
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
            query.edit_message_text("Send User ID (or @name | id) to remove from Giveaway Old Winner list:")
        except Exception:
            pass
        return

    # removeban choose confirm
    if qd in ("reset_permanent_ban", "reset_oldwinner_ban"):
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
                query.edit_message_text("Confirm reset Giveaway Old Winner list?", reply_markup=kb)
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
            query.edit_message_text("✅ Giveaway Old Winner list has been reset.")
        except Exception:
            pass
        return

    # JOIN GIVEAWAY
    if qd == "join_giveaway":
        if not data.get("active"):
            try:
                query.answer("This giveaway is not active right now.", show_alert=True)
            except Exception:
                pass
            return

        # verify required for joining
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

        # forced old winner block (always works)
        if data.get("forced_oldwinner_on"):
            if uid in (data.get("forced_oldwinners", {}) or {}):
                try:
                    query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception:
                    pass
                return

        # giveaway old winner block
        if data.get("old_winner_mode") == "block":
            if uid in (data.get("old_winners", {}) or {}):
                try:
                    query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception:
                    pass
                return

        # already joined?
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

        # quick update live post
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time")
            if live_mid and start_ts:
                start = datetime.utcfromtimestamp(start_ts)
                duration = int(data.get("duration_seconds", 1) or 1)
                elapsed = int((datetime.utcnow() - start).total_seconds())
                remaining = duration - elapsed
                if remaining < 0:
                    remaining = 0
                ch_edit(context.bot, live_mid, build_live_text(remaining), reply_markup=join_button_markup())
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

    # WINNERS APPROVE/REJECT (MANUAL DRAW)
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

        # post winners in channel
        try:
            m = ch_send(context.bot, text, reply_markup=claim_button_markup())
        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post winners in channel: {e}")
            except Exception:
                pass
            return

        # build record for unique claim post
        with lock:
            # reconstruct winners from participants (simple)
            # NOTE: winners inside preview are already based on participants. We'll rebuild winners_map now:
            participants = data.get("participants", {}) or {}
            winner_count = int(data.get("winner_count", 1) or 1)
            first_uid = data.get("first_winner_id") or ""
            winners_map = {}

            # best effort: include first winner + random from participants
            if first_uid and first_uid in participants:
                winners_map[first_uid] = {"username": (participants.get(first_uid, {}) or {}).get("username", "")}
            # pick rest
            pool = [x for x in participants.keys() if x != first_uid]
            need = max(0, winner_count - (1 if first_uid else 0))
            need = min(need, len(pool))
            for u in random.sample(pool, need) if need > 0 else []:
                winners_map[u] = {"username": (participants.get(u, {}) or {}).get("username", "")}

            title = (data.get("title") or "").strip()
            prize = (data.get("prize") or "").rstrip()
            claim_start = now_ts()

            rec = {
                "title": title,
                "prize": prize,
                "winner_count": winner_count,
                "winners": winners_map,
                "first_uid": first_uid,
                "claim_start_ts": claim_start,
                "claim_expires_ts": claim_start + 24 * 3600,
                "delivery": {},
                "delivery_count": 0,
            }

            data["giveaways_history"][str(m.message_id)] = rec
            data["last_winners_post_id"] = str(m.message_id)
            data["pending_winners_text"] = ""
            save_data()

        schedule_claim_expire(context.job_queue, str(m.message_id))

        # remove closed message if exists
        closed_mid = data.get("closed_message_id")
        if closed_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception:
                pass

        try:
            query.edit_message_text("✅ Approved! Winners posted to channel (Claim button active).")
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

    # CLAIM PRIZE (UNIQUE SYSTEM — MULTIPLE POSTS SUPPORTED)
    if qd == "claim_prize":
        # verify required for claim too (your requirement)
        if not verify_user_join(context.bot, int(uid)):
            try:
                query.answer(popup_verify_required(), show_alert=True)
            except Exception:
                pass
            return

        # identify which winners post this claim belongs to
        winners_mid = None
        try:
            winners_mid = str(query.message.message_id) if query.message else None
        except Exception:
            winners_mid = None

        with lock:
            history = data.get("giveaways_history", {}) or {}
            rec = history.get(str(winners_mid)) if winners_mid else None

        # if record not found, fallback to last winners post
        if not rec:
            with lock:
                last_mid = data.get("last_winners_post_id")
                rec = (data.get("giveaways_history", {}) or {}).get(str(last_mid)) if last_mid else None

        if not rec:
            try:
                query.answer("No giveaway record found for this claim.", show_alert=True)
            except Exception:
                pass
            return

        winners = rec.get("winners", {}) or {}
        delivery = rec.get("delivery", {}) or {}

        # already delivered
        if uid in delivery:
            try:
                query.answer(popup_claim_already_delivered(), show_alert=True)
            except Exception:
                pass
            return

        # not winner
        if uid not in winners:
            try:
                query.answer(popup_claim_not_winner(), show_alert=True)
            except Exception:
                pass
            return

        # expired
        exp_ts = rec.get("claim_expires_ts")
        if exp_ts and now_ts() > float(exp_ts):
            try:
                query.answer(popup_prize_expired(), show_alert=True)
            except Exception:
                pass
            return

        uname = (winners.get(uid, {}) or {}).get("username", "") or "@username"
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

    # basic
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("panel", cmd_panel))

    # giveaway
    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))

    # verify
    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    # bans
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # extra
    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))
    dp.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))
    dp.add_handler(CommandHandler("prizedelivery", cmd_prizedelivery))

    # reset
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # handlers
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # Resume after restart
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    # if closed and autowinnerpost OFF, keep spinner running
    if data.get("closed") and data.get("closed_message_id") and not data.get("autowinnerpost"):
        start_closed_anim(updater.job_queue)

    # re-schedule claim expiry for active history posts (best effort)
    try:
        history = data.get("giveaways_history", {}) or {}
        for mid_str, rec in history.items():
            exp = rec.get("claim_expires_ts")
            if exp and float(exp) > now_ts():
                schedule_claim_expire(updater.job_queue, str(mid_str))
    except Exception:
        pass

    print("Bot is running (PTB 13) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
