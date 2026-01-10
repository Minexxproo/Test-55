# -*- coding: utf-8 -*-
# POWER POINT BREAK Giveaway Bot (PTB v13 / non-async)
# Full A-to-Z Working Code + Premium Wrap-proof Text + Verify + Multi-Claim + Auto Winner + Prize Delivery + Old Winner Block Toggle
# ---------------------------------------------------------------
# REQUIREMENTS:
#   pip install python-telegram-bot==13.15 python-dotenv
#
# .env EXAMPLE:
#   BOT_TOKEN=123:ABC
#   ADMIN_ID=123456789
#   CHANNEL_ID=-1001234567890
#   HOST_NAME=POWER POINT BREAK
#   CHANNEL_USERNAME=@PowerPointBreak
#   CHANNEL_LINK=https://t.me/PowerPointBreak
#   ADMIN_CONTACT=@MinexxProo
#   DATA_FILE=giveaway_data.json

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

closed_anim_job = None            # spinner when autowinner OFF
auto_select_job = None            # 3 min selection animation when autowinner ON
auto_select_finalize_job = None

draw_job = None                   # admin draw progress msg (admin chat)
draw_finalize_job = None

claim_expire_job = None

# =========================================================
# DATA / STORAGE
# =========================================================
def fresh_default_data():
    return {
        # current giveaway runtime flags
        "active": False,
        "closed": False,

        # giveaway inputs
        "title": "",
        "prize": "",
        "winner_count": 0,
        "duration_seconds": 0,
        "rules": "",

        # runtime
        "start_time": None,
        "live_message_id": None,
        "closed_message_id": None,

        # participants map
        "participants": {},  # uid(str) -> {"username": "@x" or "", "name": ""}

        # verify targets
        "verify_targets": [],  # [{"ref": "-100..." or "@xxx", "display": "..."}]

        # permanent block
        "permanent_block": {},  # uid -> {"username": "@x" or ""}

        # old winner system (setup flow)
        "old_winner_mode": "skip",  # "block" or "skip"
        "old_winners": {},          # uid -> {"username": "@x"} (used if mode=block)

        # extra old winner block system (/blockoldwinner)
        "old_winner_block_enabled": False,  # ON/OFF
        "old_winner_block_list": {},        # uid -> {"username": "@x"}

        # first join winner
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # auto winner post system
        "auto_winner_post": False,  # /autowinnerpost toggle

        # multi claim system: every winners post is stored as a giveaway record
        "giveaways": {},  # gid -> {record}

        # fallback (legacy)
        "winners": {},              # uid -> {"username": "@x"}
        "pending_winners_text": "", # admin preview
    }


def load_data():
    base = fresh_default_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}

    for k, v in base.items():
        if k not in d:
            d[k] = v
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
    # compact (wrap-proof)
    percent = max(0, min(100, percent))
    blocks = 8
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


def now_ts() -> float:
    return datetime.utcnow().timestamp()


def sep_line() -> str:
    return "━━━━━━━━━━━━━━━━━━━━"


def safe_title_line(title: str) -> str:
    t = (title or "").strip()
    return t if t else "🎁 GIVEAWAY"


def format_prize_block(prize: str) -> str:
    p = (prize or "").strip()
    return p if p else "—"


def format_rules_block(rules: str) -> str:
    r = (rules or "").strip()
    return r if r else "—"


# =========================================================
# INLINE BUTTONS
# =========================================================
def join_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data="join_giveaway")]]
    )


def claim_winners_post_markup(gid: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data=f"claim_prize:{gid}")]]
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


def autowinner_toggle_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🟢 ON", callback_data="autowinner_on"),
            InlineKeyboardButton("🔴 OFF", callback_data="autowinner_off"),
        ]]
    )


def blockoldwinner_toggle_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🟢 ON", callback_data="blockold_on"),
            InlineKeyboardButton("🔴 OFF", callback_data="blockold_off"),
        ]]
    )


# =========================================================
# VERIFY TARGETS
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


def verify_user_join(bot, user_id: int, targets_snapshot=None) -> bool:
    targets = targets_snapshot if targets_snapshot is not None else (data.get("verify_targets", []) or [])
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
# POPUP TEXTS (WRAP-PROOF)
# =========================================================
def popup_verify_required() -> str:
    return (
        "🔐 Access Restricted\n"
        "You must join the required channels to proceed.\n"
        "After joining, tap JOIN once more."
    )


def popup_old_winner_blocked() -> str:
    return (
        "🚫 You have already won a previous giveaway.\n"
        "Repeat winners are restricted.\n"
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


def popup_claim_winner(username: str, uid: str, prize: str = "") -> str:
    p = (prize or "").strip()
    prize_line = f"\n🎁 Prize:\n{p}\n" if p else "\n"
    return (
        "🌟Congratulations ✨\n"
        "You’ve won this giveaway.✅"
        f"{prize_line}"
        f"👤 {username} | 🆔 {uid}\n"
        "📩   please  Contract admin Claim your prize now:\n"
        f"👉 {ADMIN_CONTACT}"
    )


def popup_claim_not_winner_clean() -> str:
    return (
        f"{sep_line()}\n"
        "❌ NOT A WINNER\n"
        f"{sep_line()}\n\n"
        "Sorry! Your User ID is not in the winners list.\n"
        "Please wait for the next giveaway 🤍"
    )


def popup_prize_expired() -> str:
    return (
        "⏳ PRIZE EXPIRED\n"
        "Your 24-hour claim time has ended.\n"
        "This prize is no longer available."
    )


def popup_already_delivered() -> str:
    return (
        "🌟 Congratulations!\n"
        "Your prize has already been successfully delivered to you ✅\n"
        "If you face any issues, please contact our admin 📩\n"
        f"{ADMIN_CONTACT}"
    )


# =========================================================
# GIVEAWAY TEXT BUILDERS (WRAP-PROOF)
# =========================================================
def build_preview_text() -> str:
    remaining = int(data.get("duration_seconds", 0) or 0)
    title = safe_title_line(data.get("title", ""))
    prize = format_prize_block(data.get("prize", ""))
    rules = format_rules_block(data.get("rules", ""))

    return (
        f"{sep_line()}\n"
        "🔍 GIVEAWAY PREVIEW (ADMIN ONLY)\n"
        f"{sep_line()}\n\n"
        f"⚡ {title}\n\n"
        "🎁 PRIZE POOL ✨\n"
        f"{prize}\n\n"
        "👥 TOTAL PARTICIPANTS: 0\n"
        f"🏅 TOTAL WINNERS: {int(data.get('winner_count',0) or 0)}\n"
        "🎯 WINNER SELECTION: 100% Randomly\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(remaining).replace(':',' : ')}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{build_progress(0)} 0%\n\n"
        "📜 RULES....\n"
        f"{rules}\n\n"
        f"📢 HOSTED BY⚡️ {HOST_NAME}\n"
        "👇 TAP THE BUTTON BELOW &\n"
        "JOIN NOW 👇"
    )


def build_live_text(remaining: int) -> str:
    duration = int(data.get("duration_seconds", 1) or 1)
    elapsed = max(0, min(duration, duration - remaining))
    percent = int(round((elapsed / float(duration)) * 100))
    progress = build_progress(percent)

    title = safe_title_line(data.get("title", ""))
    prize = format_prize_block(data.get("prize", ""))
    rules = format_rules_block(data.get("rules", ""))

    return (
        f"{sep_line()}\n"
        f"{title}\n"
        f"{sep_line()}\n\n"
        "🎁 PRIZE POOL ✨\n"
        f"{prize}\n\n"
        f"👥 TOTAL PARTICIPANTS: {participants_count()}\n"
        f"🏅 TOTAL WINNERS: {int(data.get('winner_count',0) or 0)}\n"
        "🎯 WINNER SELECTION: 100% Randomly\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(remaining).replace(':',' : ')}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{progress} {percent}%\n\n"
        "📜 RULES....\n"
        f"{rules}\n\n"
        f"📢 HOSTED BY⚡️ {HOST_NAME}\n"
        "👇 TAP THE BUTTON BELOW &\n"
        "JOIN NOW 👇"
    )


CLOSED_SPINNER = ["⏳", "🔄", "🔃", "🔁"]

def build_closed_post_text(spin: str = "") -> str:
    title = safe_title_line(data.get("title", ""))
    spin_line = f"{spin} WINNER SELECTION IN PROGRESS" if spin else "WINNER SELECTION WILL START SOON"
    return (
        f"{sep_line()}\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        f"{sep_line()}\n\n"
        f"⚡ {title}\n\n"
        "⏰ Giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {int(data.get('winner_count',0) or 0)}\n\n"
        f"{spin_line}\n"
        "Please wait for the announcement.\n\n"
        "🙏 Thank you for participating.\n"
        f"— {HOST_NAME} ⚡"
    )


def build_draw_progress_text(percent: int, spin: str) -> str:
    bar = build_progress(percent)
    return (
        f"{sep_line()}\n"
        "🎲 RANDOM WINNER SELECTION\n"
        f"{sep_line()}\n\n"
        f"{spin} Winner selection is in progress\n\n"
        "📊 Progress\n"
        f"{bar} {percent}%\n\n"
        "✅ 100% Random & Fair\n"
        "🔐 User ID based selection only.\n\n"
        "⏳ Please wait while system\n"
        "finalizes the winners...\n"
        f"— {HOST_NAME} ⚡"
    )


def build_winners_post_text(first_uid: str, first_user: str, random_winners: list) -> str:
    title = safe_title_line(data.get("title", ""))
    prize = format_prize_block(data.get("prize", ""))

    lines = []
    lines.append(sep_line())
    lines.append("🏆 GIVEAWAY WINNERS ANNOUNCEMENT 🏆")
    lines.append(sep_line())
    lines.append("")
    lines.append(f"⚡ {title}")
    lines.append("")
    lines.append("🎁 PRIZE")
    lines.append(prize)
    lines.append("")
    lines.append("🥇 FIRST JOIN CHAMPION")
    if first_user:
        lines.append(f"👑 {first_user} | 🆔 {first_uid}")
    else:
        lines.append(f"👑 ID: {first_uid}")
    lines.append("🎯 Secured instantly by joining first")
    lines.append("")
    lines.append("👑 OTHER WINNERS (RANDOMLY SELECTED)")
    if random_winners:
        i = 1
        for uid, uname in random_winners:
            if uname:
                lines.append(f"{i}️⃣ {uname} | 🆔 {uid}")
            else:
                lines.append(f"{i}️⃣ ID: {uid}")
            i += 1
    else:
        lines.append("—")
    lines.append("")
    lines.append("⏳ Claim Rule")
    lines.append("Claim within 24 hours.")
    lines.append("After 24 hours, claim expires.")
    lines.append("")
    lines.append(f"📢 Hosted By: {HOST_NAME}")
    lines.append("👇 Tap CLAIM button below 👇")
    return "\n".join(lines)


def build_winners_text_with_delivery(base_text: str, delivered_count: int, total_winners: int) -> str:
    return (
        base_text
        + "\n\n"
        + f"{sep_line()}\n"
        + f"✅ Prize Delivery Done: {delivered_count}/{total_winners}\n"
        + f"{sep_line()}"
    )

# =========================================================
# JOBS CONTROL
# =========================================================
def stop_live_countdown():
    global countdown_job
    if countdown_job is not None:
        try:
            countdown_job.schedule_removal()
        except Exception:
            pass
    countdown_job = None


def stop_closed_anim():
    global closed_anim_job
    if closed_anim_job is not None:
        try:
            closed_anim_job.schedule_removal()
        except Exception:
            pass
    closed_anim_job = None


def stop_auto_select_jobs():
    global auto_select_job, auto_select_finalize_job
    if auto_select_job is not None:
        try:
            auto_select_job.schedule_removal()
        except Exception:
            pass
    auto_select_job = None

    if auto_select_finalize_job is not None:
        try:
            auto_select_finalize_job.schedule_removal()
        except Exception:
            pass
    auto_select_finalize_job = None


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


def stop_claim_expire_job():
    global claim_expire_job
    if claim_expire_job is not None:
        try:
            claim_expire_job.schedule_removal()
        except Exception:
            pass
    claim_expire_job = None


# =========================================================
# LIVE COUNTDOWN (CHANNEL POST UPDATE)
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

        start = datetime.utcfromtimestamp(start_time)
        duration = int(data.get("duration_seconds", 1) or 1)
        elapsed = int((datetime.utcnow() - start).total_seconds())
        remaining = duration - elapsed

        live_mid = data.get("live_message_id")

        # time end -> close
        if remaining <= 0:
            data["active"] = False
            data["closed"] = True
            save_data()

            # delete live message
            if live_mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
                except Exception:
                    pass

            # post closed
            try:
                if data.get("auto_winner_post"):
                    m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text(""))
                else:
                    m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text(CLOSED_SPINNER[0]))
                data["closed_message_id"] = m.message_id
                save_data()
            except Exception:
                pass

            stop_live_countdown()

            # behavior depends on autowinnerpost
            if data.get("auto_winner_post"):
                # no spinner on closed post; start 3-min selection animation in channel then auto post winners
                start_auto_channel_selection(context.job_queue, context.bot)
                # notify admin
                try:
                    context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "⏰ Giveaway Closed Automatically!\n\n"
                            f"Giveaway: {safe_title_line(data.get('title',''))}\n"
                            f"Total Participants: {participants_count()}\n\n"
                            "Auto Winner: ON ✅\n"
                            "Winner selection is running in channel."
                        ),
                    )
                except Exception:
                    pass
            else:
                # spinner animation until admin /draw
                start_closed_anim(context.job_queue)
                try:
                    context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "⏰ Giveaway Closed Automatically!\n\n"
                            f"Giveaway: {safe_title_line(data.get('title',''))}\n"
                            f"Total Participants: {participants_count()}\n\n"
                            "Now use /draw to select winners."
                        ),
                    )
                except Exception:
                    pass
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
# CLOSED SPINNER ANIMATION (ONLY WHEN AUTOWINNER OFF)
# =========================================================
def start_closed_anim(job_queue):
    global closed_anim_job
    stop_closed_anim()

    # if autowinner ON -> no spinner
    if data.get("auto_winner_post"):
        return

    closed_anim_job = job_queue.run_repeating(
        closed_anim_tick,
        interval=1,
        first=0,
        context={"tick": 0},
        name="closed_anim",
    )


def closed_anim_tick(context: CallbackContext):
    mid = data.get("closed_message_id")
    if not mid:
        stop_closed_anim()
        return

    # stop if any new winners posted for the latest giveaway? we keep it simple: stop if giveaway is not closed
    if not data.get("closed"):
        stop_closed_anim()
        return

    ctx = context.job.context or {}
    tick = int(ctx.get("tick", 0)) + 1
    ctx["tick"] = tick
    context.job.context = ctx

    spin = CLOSED_SPINNER[(tick - 1) % len(CLOSED_SPINNER)]
    try:
        context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=mid,
            text=build_closed_post_text(spin),
        )
    except Exception:
        pass


# =========================================================
# AUTO CHANNEL SELECTION (3 MIN) -> AUTO POST WINNERS
# =========================================================
AUTO_SELECT_SECONDS = 180
AUTO_SELECT_INTERVAL = 5
AUTO_SPINNER = ["🔄", "🔃", "🔁", "🔂"]

def start_auto_channel_selection(job_queue, bot):
    global auto_select_job, auto_select_finalize_job
    stop_auto_select_jobs()

    mid = data.get("closed_message_id")
    if not mid:
        return

    ctx = {"tick": 0, "start_ts": now_ts(), "mid": mid}

    def auto_select_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        tick = int(jd.get("tick", 0)) + 1
        jd["tick"] = tick

        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(AUTO_SELECT_SECONDS)) * 100)))
        spin = AUTO_SPINNER[(tick - 1) % len(AUTO_SPINNER)]
        text = build_draw_progress_text(percent, spin)

        # edit closed message into selection text (wrap-proof)
        try:
            job_ctx.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=jd["mid"],
                text=text,
            )
        except Exception:
            pass

    auto_select_job = job_queue.run_repeating(
        auto_select_tick,
        interval=AUTO_SELECT_INTERVAL,
        first=0,
        context=ctx,
        name="auto_channel_selection",
    )

    auto_select_finalize_job = job_queue.run_once(
        auto_channel_finalize,
        when=AUTO_SELECT_SECONDS,
        context=ctx,
        name="auto_channel_finalize",
    )


def auto_channel_finalize(context: CallbackContext):
    stop_auto_select_jobs()

    # auto draw and post winners
    winners_text, gid = perform_draw_and_store_record()
    if not winners_text or not gid:
        # edit message if no participants
        mid = data.get("closed_message_id")
        if mid:
            try:
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=mid,
                    text=(
                        f"{sep_line()}\n"
                        "❌ NO PARTICIPANTS\n"
                        f"{sep_line()}\n\n"
                        "No participants to draw winners from.\n"
                        f"— {HOST_NAME}"
                    ),
                )
            except Exception:
                pass
        return

    # delete selection msg (closed msg) and post winners msg
    closed_mid = data.get("closed_message_id")
    if closed_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
        except Exception:
            pass

    try:
        base = winners_text
        total_winners = int((data.get("giveaways", {}).get(gid, {}) or {}).get("total_winners", 0) or 0)
        delivered_count = int((data.get("giveaways", {}).get(gid, {}) or {}).get("delivered_count", 0) or 0)
        final_text = build_winners_text_with_delivery(base, delivered_count, total_winners)

        m = context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=final_text,
            reply_markup=claim_winners_post_markup(gid),
        )
        with lock:
            data["giveaways"][gid]["winners_message_id"] = m.message_id
            data["closed_message_id"] = None
            save_data()

        # schedule claim expire
        schedule_claim_expire(context.job_queue, gid)

    except Exception:
        pass


# =========================================================
# ADMIN DRAW PROGRESS (ADMIN CHAT) + FINALIZE PREVIEW
# =========================================================
DRAW_DURATION_SECONDS = 40
DRAW_UPDATE_INTERVAL = 5  # every 5 seconds (as requested)
SPINNER = ["🔄", "🔃", "🔁", "🔂"]

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


def perform_draw_and_store_record():
    """
    Draw winners from current participants.
    Creates a new gid record in data["giveaways"] with winners + delivered map.
    Returns (winners_text, gid).
    """
    with lock:
        participants = data.get("participants", {}) or {}
        if not participants:
            return ("", "")

        winner_count = int(data.get("winner_count", 1) or 1)
        winner_count = max(1, winner_count)

        # first join
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
        selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

        winners_map = {first_uid: {"username": first_uname}}
        random_list = []
        for uid in selected:
            info = participants.get(uid, {}) or {}
            winners_map[uid] = {"username": info.get("username", "")}
            random_list.append((uid, info.get("username", "")))

        # store legacy winners + pending text
        pending_text = build_winners_post_text(first_uid, first_uname, random_list)
        data["winners"] = winners_map
        data["pending_winners_text"] = pending_text

        # new giveaway record
        gid = str(int(now_ts()))
        total_winners = len(winners_map)
        record = {
            "gid": gid,
            "title": data.get("title", ""),
            "prize": data.get("prize", ""),
            "base_text": pending_text,
            "winners": winners_map,
            "verify_targets": list(data.get("verify_targets", []) or []),
            "delivered": {},
            "delivered_count": 0,
            "total_winners": total_winners,
            "claim_start_ts": now_ts(),
            "claim_expires_ts": now_ts() + 24 * 3600,
            "winners_message_id": None,
        }
        gmap = data.get("giveaways", {}) or {}
        gmap[gid] = record
        data["giveaways"] = gmap

        save_data()
        return (pending_text, gid)


def draw_finalize(context: CallbackContext):
    stop_draw_jobs()
    jd = context.job.context
    admin_chat_id = jd["admin_chat_id"]
    admin_msg_id = jd["admin_msg_id"]

    winners_text, _gid = perform_draw_and_store_record()
    if not winners_text:
        try:
            context.bot.edit_message_text(
                chat_id=admin_chat_id,
                message_id=admin_msg_id,
                text="No participants to draw winners from.",
            )
        except Exception:
            pass
        return

    try:
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            text=winners_text,
            reply_markup=winners_approve_markup(),
        )
    except Exception:
        try:
            context.bot.send_message(
                chat_id=admin_chat_id,
                text=winners_text,
                reply_markup=winners_approve_markup(),
            )
        except Exception:
            pass


# =========================================================
# CLAIM EXPIRY JOB (PER GID)
# =========================================================
def schedule_claim_expire(job_queue, gid: str):
    global claim_expire_job
    # single job okay; it will edit best effort
    stop_claim_expire_job()

    with lock:
        g = (data.get("giveaways", {}) or {}).get(gid, {})
        exp = g.get("claim_expires_ts")
        mid = g.get("winners_message_id")
    if not exp or not mid:
        return

    remain = float(exp) - now_ts()
    if remain <= 0:
        return

    claim_expire_job = job_queue.run_once(
        expire_claim_button_job,
        when=remain,
        context={"gid": gid},
        name="claim_expire_job",
    )


def expire_claim_button_job(context: CallbackContext):
    gid = (context.job.context or {}).get("gid", "")
    if not gid:
        return
    with lock:
        g = (data.get("giveaways", {}) or {}).get(gid, {}) or {}
        mid = g.get("winners_message_id")
    if not mid:
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
# RESET
# =========================================================
def do_full_reset(context: CallbackContext, admin_chat_id: int, admin_msg_id: int):
    global data
    stop_live_countdown()
    stop_draw_jobs()
    stop_closed_anim()
    stop_auto_select_jobs()
    stop_claim_expire_job()

    with lock:
        # delete channel messages if exist (best effort)
        for mid_key in ["live_message_id", "closed_message_id"]:
            mid = data.get(mid_key)
            if mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
                except Exception:
                    pass

        keep_perma = data.get("permanent_block", {})
        keep_verify = data.get("verify_targets", [])
        keep_oldblock_enabled = data.get("old_winner_block_enabled", False)
        keep_oldblock_list = data.get("old_winner_block_list", {})

        data = fresh_default_data()
        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        data["old_winner_block_enabled"] = keep_oldblock_enabled
        data["old_winner_block_list"] = keep_oldblock_list
        save_data()

    try:
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            text=(
                f"{sep_line()}\n"
                "✅ RESET COMPLETED SUCCESSFULLY!\n"
                f"{sep_line()}\n\n"
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
            "🛡️👑 WELCOME BACK, ADMIN 👑🛡️\n\n"
            "⚙️ System Status: ONLINE ✅\n"
            "🚀 Giveaway Engine: READY\n\n"
            "🧭 Open Admin Panel:\n"
            "/panel\n\n"
            f"⚡ POWERED BY: {HOST_NAME}"
        )
    else:
        update.message.reply_text(
            f"{sep_line()}\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            f"{sep_line()}\n\n"
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
        "⚙️ SYSTEM\n"
        "/autowinnerpost\n"
        "/blockoldwinner\n"
        "/prizedelivery\n\n"
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


def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        f"{sep_line()}\n"
        "✅ ADD VERIFY (CHAT ID / @USERNAME)\n"
        f"{sep_line()}\n\n"
        "Send Chat ID (recommended) OR @username:\n\n"
        "Examples:\n"
        "-1001234567890\n"
        "@PowerPointBreak\n\n"
        "After adding, users must join ALL verify targets."
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
        sep_line(),
        "🗑 REMOVE VERIFY TARGET",
        sep_line(),
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
    global admin_state, data
    if not is_admin(update):
        return

    stop_live_countdown()
    stop_draw_jobs()
    stop_closed_anim()
    stop_auto_select_jobs()
    stop_claim_expire_job()

    with lock:
        keep_perma = data.get("permanent_block", {})
        keep_verify = data.get("verify_targets", [])
        keep_oldblock_enabled = data.get("old_winner_block_enabled", False)
        keep_oldblock_list = data.get("old_winner_block_list", {})

        data.clear()
        data.update(fresh_default_data())
        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        data["old_winner_block_enabled"] = keep_oldblock_enabled
        data["old_winner_block_list"] = keep_oldblock_list
        save_data()

    admin_state = "title"
    update.message.reply_text(
        f"{sep_line()}\n"
        "🆕 NEW GIVEAWAY SETUP STARTED\n"
        f"{sep_line()}\n\n"
        "STEP 1️⃣ — GIVEAWAY TITLE\n\n"
        "Send Giveaway Title (emoji allowed):"
    )


def cmd_participants(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    parts = data.get("participants", {})
    if not parts:
        update.message.reply_text("👥 Participants List is empty.")
        return

    lines = [
        sep_line(),
        "👥 PARTICIPANTS LIST (ADMIN VIEW)",
        sep_line(),
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
        f"{sep_line()}\n"
        "⚠️ END GIVEAWAY CONFIRMATION\n"
        f"{sep_line()}\n\n"
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

    # if autowinner is ON, admin draw still allowed (manual)
    start_draw_progress(context, update.effective_chat.id)


def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    state = "🟢 ON" if data.get("auto_winner_post") else "🔴 OFF"
    update.message.reply_text(
        f"{sep_line()}\n"
        "⚙️ AUTO WINNER POST SYSTEM\n"
        f"{sep_line()}\n\n"
        f"Current Status: {state}\n\n"
        "🟢 ON  → Giveaway ends → 3 min selection in channel → Auto winners post\n"
        "🔴 OFF → Giveaway ends → Closed spinner → Admin uses /draw\n\n"
        "Choose option below:",
        reply_markup=autowinner_toggle_markup()
    )


def cmd_blockoldwinner(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    state = "🟢 ON" if data.get("old_winner_block_enabled") else "🔴 OFF"
    update.message.reply_text(
        f"{sep_line()}\n"
        "⛔ OLD WINNER BLOCK SYSTEM\n"
        f"{sep_line()}\n\n"
        f"Current Status: {state}\n\n"
        "If ON, users in Old Winner Block List cannot join.\n"
        "To add list (when ON): send usernames/ids after turning ON.\n\n"
        "Choose option below:",
        reply_markup=blockoldwinner_toggle_markup()
    )


def cmd_prizedelivery(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "prize_delivery_list"
    update.message.reply_text(
        f"{sep_line()}\n"
        "✅ PRIZE DELIVERY SYSTEM\n"
        f"{sep_line()}\n\n"
        "Send delivered users list (one per line):\n\n"
        "Format:\n"
        "@username | user_id\n"
        "or\n"
        "user_id\n\n"
        "Example:\n"
        "@minexxproo | 8293728\n"
        "556677"
    )


def cmd_blockpermanent(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "perma_block_list"
    update.message.reply_text(
        f"{sep_line()}\n"
        "🔒 PERMANENT BLOCK\n"
        f"{sep_line()}\n\n"
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
    oldw_setup = data.get("old_winners", {}) or {}
    oldw_cmd = data.get("old_winner_block_list", {}) or {}

    lines = []
    lines.append(sep_line())
    lines.append("📌 BAN LISTS")
    lines.append(sep_line())
    lines.append("")
    lines.append(f"SETUP OLD WINNER MODE: {str(data.get('old_winner_mode','skip')).upper()}")
    lines.append(f"/blockoldwinner STATUS: {'ON' if data.get('old_winner_block_enabled') else 'OFF'}")
    lines.append("")

    lines.append("⛔ OLD WINNER BLOCK (SETUP LIST)")
    lines.append(f"Total: {len(oldw_setup)}")
    if oldw_setup:
        i = 1
        for uid, info in oldw_setup.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {uname+' | ' if uname else ''}{uid}")
            i += 1
    else:
        lines.append("—")
    lines.append("")

    lines.append("⛔ OLD WINNER BLOCK (/blockoldwinner LIST)")
    lines.append(f"Total: {len(oldw_cmd)}")
    if oldw_cmd:
        i = 1
        for uid, info in oldw_cmd.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {uname+' | ' if uname else ''}{uid}")
            i += 1
    else:
        lines.append("—")
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
        lines.append("—")

    update.message.reply_text("\n".join(lines))


def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    m = update.message.reply_text(
        "Confirm reset?",
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
            f"{sep_line()}\n"
            "✅ VERIFY TARGET ADDED SUCCESSFULLY!\n"
            f"{sep_line()}\n\n"
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
            f"{sep_line()}\n"
            "✅ VERIFY TARGET REMOVED\n"
            f"{sep_line()}\n\n"
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

        admin_state = "old_winner_mode"
        update.message.reply_text(
            f"{sep_line()}\n"
            "🔐 OLD WINNER PROTECTION MODE (SETUP)\n"
            f"{sep_line()}\n\n"
            "1️⃣ BLOCK OLD WINNERS (setup list)\n"
            "2️⃣ SKIP OLD WINNERS\n\n"
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
                "📌 Old Winner Mode set to: SKIP\n"
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
            f"{sep_line()}\n"
            "⛔ OLD WINNER BLOCK LIST SETUP\n"
            f"{sep_line()}\n\n"
            "Send old winners list (one per line):\n\n"
            "Format:\n"
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
            f"{sep_line()}\n"
            "✅ OLD WINNER BLOCK LIST SAVED!\n"
            f"{sep_line()}\n\n"
            f"📌 Total Added: {len(data['old_winners']) - before}\n\n"
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
            f"New Added: {len(data['permanent_block']) - before}\n"
            f"Total Blocked: {len(data['permanent_block'])}"
        )
        return

    # /blockoldwinner list add (when ON)
    if admin_state == "blockold_list_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            ow = data.get("old_winner_block_list", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[uid] = {"username": uname}
            data["old_winner_block_list"] = ow
            save_data()
        admin_state = None
        update.message.reply_text(
            f"{sep_line()}\n"
            "✅ OLD WINNER LIST UPDATED\n"
            f"{sep_line()}\n\n"
            f"Added: {len(data['old_winner_block_list']) - before}\n"
            f"Total: {len(data['old_winner_block_list'])}\n"
        )
        return

    # PRIZE DELIVERY
    if admin_state == "prize_delivery_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return

        delivered_now = 0
        updated_posts = 0

        with lock:
            gmap = data.get("giveaways", {}) or {}

            for uid2, _uname2 in entries:
                for _gid, g in gmap.items():
                    winners = g.get("winners", {}) or {}
                    if uid2 in winners:
                        delivered = g.get("delivered", {}) or {}
                        if uid2 not in delivered:
                            delivered[uid2] = {"ts": now_ts(), "by": str(ADMIN_ID)}
                            g["delivered"] = delivered
                            g["delivered_count"] = len(delivered)
                            delivered_now += 1

            save_data()

        # edit posts to update delivery count
        with lock:
            gmap = data.get("giveaways", {}) or {}

        for gid, g in gmap.items():
            mid = g.get("winners_message_id")
            if not mid:
                continue
            try:
                base_text = g.get("base_text", "")
                total_winners = int(g.get("total_winners", 0) or 0)
                dcount = int(g.get("delivered_count", 0) or 0)
                new_text = build_winners_text_with_delivery(base_text, dcount, total_winners)

                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=mid,
                    text=new_text,
                    reply_markup=claim_winners_post_markup(gid),
                )
                updated_posts += 1
            except Exception:
                pass

        admin_state = None
        update.message.reply_text(
            f"{sep_line()}\n"
            "✅ PRIZE DELIVERY UPDATED\n"
            f"{sep_line()}\n\n"
            f"Delivered Added: {delivered_now}\n"
            f"Posts Updated: {updated_posts}\n\n"
            "Delivered users will see 'already delivered' popup if they claim again."
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

    # verify add more/done
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
                f"{sep_line()}\n"
                "✅ VERIFY SETUP COMPLETED\n"
                f"{sep_line()}\n\n"
                f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}\n"
                "All users must join ALL targets to join giveaway."
            )
        except Exception:
            pass
        return

    # autowinner toggle
    if qd in ("autowinner_on", "autowinner_off"):
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        with lock:
            data["auto_winner_post"] = (qd == "autowinner_on")
            save_data()
        try: query.answer("Updated ✅", show_alert=False)
        except Exception: pass
        state = "🟢 ON" if data.get("auto_winner_post") else "🔴 OFF"
        try:
            query.edit_message_text(
                f"{sep_line()}\n"
                "⚙️ AUTO WINNER POST SYSTEM\n"
                f"{sep_line()}\n\n"
                f"Current Status: {state}\n\n"
                "🟢 ON  → Giveaway ends → 3 min selection in channel → Auto winners post\n"
                "🔴 OFF → Giveaway ends → Closed spinner → Admin uses /draw",
                reply_markup=autowinner_toggle_markup()
            )
        except Exception:
            pass
        return

    # blockoldwinner toggle
    if qd in ("blockold_on", "blockold_off"):
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        with lock:
            data["old_winner_block_enabled"] = (qd == "blockold_on")
            save_data()
        try: query.answer("Updated ✅", show_alert=False)
        except Exception: pass

        if data["old_winner_block_enabled"]:
            admin_state = "blockold_list_input"
            try:
                query.edit_message_text(
                    f"{sep_line()}\n"
                    "⛔ OLD WINNER BLOCK: ON ✅\n"
                    f"{sep_line()}\n\n"
                    "Now send old winners list (one per line):\n"
                    "@username | user_id\n"
                    "or\n"
                    "user_id"
                )
            except Exception:
                pass
        else:
            admin_state = None
            try:
                query.edit_message_text(
                    f"{sep_line()}\n"
                    "⛔ OLD WINNER BLOCK: OFF ❌\n"
                    f"{sep_line()}\n\n"
                    "This system is now disabled.",
                    reply_markup=blockoldwinner_toggle_markup()
                )
            except Exception:
                pass
        return

    # preview actions
    if qd.startswith("preview_"):
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return

        if qd == "preview_approve":
            try: query.answer()
            except Exception: pass

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

                    data["participants"] = {}
                    data["winners"] = {}
                    data["pending_winners_text"] = ""
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""

                    save_data()

                stop_closed_anim()
                stop_auto_select_jobs()
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

        if qd == "preview_edit":
            try: query.answer()
            except Exception: pass
            query.edit_message_text("✏️ Edit Mode\n\nStart again with /newgiveaway")
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

        # post closed
        try:
            if data.get("auto_winner_post"):
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text(""))
            else:
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text(CLOSED_SPINNER[0]))
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
        except Exception:
            pass

        stop_live_countdown()

        # start animations depending on autowinner
        if data.get("auto_winner_post"):
            stop_closed_anim()
            start_auto_channel_selection(context.job_queue, context.bot)
            try: query.edit_message_text("✅ Giveaway Closed! Auto winner selection running in channel.")
            except Exception: pass
        else:
            start_closed_anim(context.job_queue)
            try: query.edit_message_text("✅ Giveaway Closed Successfully! Now use /draw")
            except Exception: pass
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

    # Reset confirm/cancel
    if qd == "reset_confirm":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        do_full_reset(context, query.message.chat_id, query.message.message_id)
        return

    if qd == "reset_cancel":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        try: query.edit_message_text("❌ Reset cancelled.")
        except Exception: pass
        return

    # Unban choose
    if qd == "unban_permanent":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        admin_state = "unban_permanent_input"
        try: query.edit_message_text("Send User ID (or @name | id) to unban from Permanent Block:")
        except Exception: pass
        return

    if qd == "unban_oldwinner":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        admin_state = "unban_oldwinner_input"
        try: query.edit_message_text("Send User ID (or @name | id) to unban from Old Winner Block:")
        except Exception: pass
        return

    # removeban choose confirm
    if qd in ("reset_permanent_ban", "reset_oldwinner_ban"):
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass

        if qd == "reset_permanent_ban":
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm Reset Permanent", callback_data="confirm_reset_permanent"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
                ]]
            )
            try: query.edit_message_text("Confirm reset Permanent Ban List?", reply_markup=kb)
            except Exception: pass
            return

        if qd == "reset_oldwinner_ban":
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm Reset Old Winner", callback_data="confirm_reset_oldwinner"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
                ]]
            )
            try: query.edit_message_text("Confirm reset Old Winner Ban List?", reply_markup=kb)
            except Exception: pass
            return

    if qd == "cancel_reset_ban":
        try: query.answer()
        except Exception: pass
        admin_state = None
        try: query.edit_message_text("Cancelled.")
        except Exception: pass
        return

    if qd == "confirm_reset_permanent":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        with lock:
            data["permanent_block"] = {}
            save_data()
        try: query.edit_message_text("✅ Permanent Ban List has been reset.")
        except Exception: pass
        return

    if qd == "confirm_reset_oldwinner":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except Exception: pass
            return
        try: query.answer()
        except Exception: pass
        with lock:
            data["old_winners"] = {}
            save_data()
        try: query.edit_message_text("✅ Old Winner Ban List has been reset.")
        except Exception: pass
        return

    # Join giveaway
    if qd == "join_giveaway":
        if not data.get("active"):
            try: query.answer("This giveaway is not active right now.", show_alert=True)
            except Exception: pass
            return

        # verify join
        if not verify_user_join(context.bot, int(uid), targets_snapshot=data.get("verify_targets", []) or []):
            try: query.answer(popup_verify_required(), show_alert=True)
            except Exception: pass
            return

        # permanent block
        if uid in (data.get("permanent_block", {}) or {}):
            try: query.answer(popup_permanent_blocked(), show_alert=True)
            except Exception: pass
            return

        # old winner block (setup)
        if data.get("old_winner_mode") == "block":
            if uid in (data.get("old_winners", {}) or {}):
                try: query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception: pass
                return

        # old winner block (command system) - works even if setup skip
        if data.get("old_winner_block_enabled"):
            if uid in (data.get("old_winner_block_list", {}) or {}):
                try: query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception: pass
                return

        with lock:
            first_uid = data.get("first_winner_id")

        # same first winner clicking again
        if first_uid and uid == str(first_uid):
            tg_user = query.from_user
            uname = user_tag(tg_user.username or "") or data.get("first_winner_username", "") or "@username"
            try: query.answer(popup_first_winner(uname, uid), show_alert=True)
            except Exception: pass
            return

        # already joined
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
                duration = int(data.get("duration_seconds", 1) or 1)
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

        # popup
        with lock:
            if data.get("first_winner_id") == uid:
                try: query.answer(popup_first_winner(uname or "@username", uid), show_alert=True)
                except Exception: pass
            else:
                try: query.answer(popup_join_success(uname or "@Username", uid), show_alert=True)
                except Exception: pass
        return

    # Winners Approve/Reject (manual draw workflow)
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

        stop_closed_anim()
        stop_auto_select_jobs()

        # delete closed message if exist
        closed_mid = data.get("closed_message_id")
        if closed_mid:
            try: context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception: pass

        # find latest record gid by base_text match (or last created)
        with lock:
            gmap = data.get("giveaways", {}) or {}
            # pick last gid by numeric max
            gid = ""
            if gmap:
                gid = max(gmap.keys(), key=lambda x: int(x) if str(x).isdigit() else 0)
            if not gid:
                # create record if missing
                _, gid = perform_draw_and_store_record()
            g = (data.get("giveaways", {}) or {}).get(gid, {}) or {}
            base = g.get("base_text", text)
            total_winners = int(g.get("total_winners", 0) or 0)
            delivered_count = int(g.get("delivered_count", 0) or 0)

        final_text = build_winners_text_with_delivery(base, delivered_count, total_winners)

        try:
            m = context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=final_text,
                reply_markup=claim_winners_post_markup(gid),
            )
            with lock:
                data["giveaways"][gid]["winners_message_id"] = m.message_id
                data["closed_message_id"] = None
                save_data()

            schedule_claim_expire(context.job_queue, gid)
            query.edit_message_text("✅ Approved! Winners posted to channel (Claim button added).")
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

    # Claim prize (MULTI-GIVEAWAY) + verify + delivered check
    if qd.startswith("claim_prize:"):
        gid = qd.split(":", 1)[1].strip()
        with lock:
            g = (data.get("giveaways", {}) or {}).get(gid)
        if not g:
            try: query.answer("This claim post is no longer available.", show_alert=True)
            except Exception: pass
            return

        # verify check on claim (snapshot)
        targets = g.get("verify_targets", []) or []
        if targets and not verify_user_join(context.bot, int(uid), targets_snapshot=targets):
            try: query.answer(popup_verify_required(), show_alert=True)
            except Exception: pass
            return

        winners = g.get("winners", {}) or {}

        if uid not in winners:
            try: query.answer(popup_claim_not_winner_clean(), show_alert=True)
            except Exception: pass
            return

        delivered = g.get("delivered", {}) or {}
        if uid in delivered:
            try: query.answer(popup_already_delivered(), show_alert=True)
            except Exception: pass
            return

        exp_ts = g.get("claim_expires_ts")
        if exp_ts:
            try:
                if now_ts() > float(exp_ts):
                    query.answer(popup_prize_expired(), show_alert=True)
                    return
            except Exception:
                pass

        uname = (winners.get(uid, {}) or {}).get("username", "") or "@username"
        prize = g.get("prize", "") or data.get("prize", "")
        try:
            query.answer(popup_claim_winner(uname, uid, prize=prize), show_alert=True)
        except Exception:
            pass
        return

    # default answer
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

    # verify
    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    # giveaway
    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))

    # systems
    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))
    dp.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))
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

    # Resume systems after restart
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    if data.get("closed") and data.get("closed_message_id"):
        # if autowinner ON, do nothing (it will only run when close happens)
        if not data.get("auto_winner_post"):
            start_closed_anim(updater.job_queue)

    print("Bot is running (PTB 13, GSM compatible, non-async) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
