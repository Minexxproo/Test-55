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
# CONSTANTS (ENGLISH ONLY)
# =========================================================
LINE = "━━━━━━━━━━━━━━━━━━━━"
LINE2 = "━━━━━━━━━━━━━━━━━━━━━━"

SPINNER = ["🔄", "🔃", "🔁", "🔂", "🌀"]

SHOW_COLORS = ["🟡", "🟠", "⚫", "🟣", "🟢", "🔵", "🔴", "🟤"]

AUTO_DRAW_DURATION_SECONDS = 10 * 60  # 10 minutes

# Lucky Draw is ACTIVE ONLY at remaining == 08:48 (one-second window)
LUCKY_TRIGGER_REMAINING = 8 * 60 + 48  # 08:48 remaining (1 second window)

# Showcase change timers (requested)
SHOW_LINE1_SEC = 5
SHOW_LINE2_SEC = 7
SHOW_LINE3_SEC = 9

# Live tick intervals cycle for smooth updates (requested)
AUTO_TICK_INTERVALS = [2, 3, 4, 5]

# Manual draw progress (admin only)
DRAW_DURATION_SECONDS = 40
DRAW_UPDATE_INTERVAL = 1  # smooth and safe

# Claim windows
CLAIM_WINDOW_SECONDS = 24 * 3600
POST_COMPLETE_AFTER_SECONDS = 24 * 3600  # after expiry, show "Giveaway Completed"

# =========================================================
# GLOBAL STATE
# =========================================================
data = {}
admin_state = None

countdown_job = None
closed_wait_job = None
draw_job = None
draw_finalize_job = None

auto_sel_job = None  # auto selection tick job


# =========================================================
# DATA / STORAGE
# =========================================================
def fresh_default_data():
    return {
        # giveaway status
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

        # participants: uid(str) -> {"username":"@x","name":""}
        "participants": {},

        # verify targets: [{"ref":"-100.." or "@x","display":"..."}]
        "verify_targets": [],

        # bans
        "permanent_block": {},  # uid -> {"username":"@x"}
        "old_winner_mode": "skip",  # "block" or "skip"
        "old_winners": {},  # uid -> {"username":"@x"} (only if mode=block)

        # first join champion
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # autodraw toggle
        "autodraw_enabled": False,

        # auto selection post state
        "autodraw_message_id": None,
        "autodraw_start_ts": None,
        "autodraw_bonus_winners": {},  # Lucky winner: uid -> {"username":"@x"}
        "lucky_draw_winner_uid": None,

        # history: gid -> snapshot
        # snapshot:
        # {
        #   "gid": "...",
        #   "title": "...",
        #   "prize": "...",
        #   "winners": {uid:{"username":"@x"}},
        #   "delivered": {uid: True},
        #   "created_ts": float,
        #   "claim_expires_ts": float,
        #   "winners_message_id": int,
        # }
        "history": {},
        "latest_gid": None,

        # winner log for /winnerlist
        # list of {"gid","username","uid","prize","date"}
        "winner_log": [],
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

    # deep defaults
    d.setdefault("participants", {})
    d.setdefault("verify_targets", [])
    d.setdefault("permanent_block", {})
    d.setdefault("old_winners", {})
    d.setdefault("autodraw_bonus_winners", {})
    d.setdefault("history", {})
    d.setdefault("winner_log", [])

    return d


def save_data():
    with lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


data = load_data()

# =========================================================
# HELPERS
# =========================================================
def now_ts() -> float:
    return datetime.utcnow().timestamp()


def format_date(ts: float) -> str:
    try:
        return datetime.utcfromtimestamp(float(ts)).strftime("%d-%m-%Y")
    except Exception:
        return ""


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


def is_valid_username(uname: str) -> bool:
    u = (uname or "").strip()
    return bool(u) and u.startswith("@") and len(u) >= 3


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


def make_gid() -> str:
    # Example: P788-P686-B6548
    a = random.randint(100, 999)
    b = random.randint(100, 999)
    c = random.randint(1000, 9999)
    return f"P{a}-P{b}-B{c}"


def format_entry(uid: str, uname: str) -> str:
    if uname and is_valid_username(uname):
        return f"{uname} | 🆔 {uid}"
    return f"User 🆔 {uid}"


def pick_three_distinct_colors():
    if len(SHOW_COLORS) >= 3:
        return random.sample(SHOW_COLORS, 3)
    return ["🟡", "🟠", "⚫"]


def make_random_pick_schedule(total_winners: int, total_seconds: int):
    """
    Random pick times across duration (no fixed interval), always finishes before end.
    """
    total_winners = max(1, int(total_winners))
    total_seconds = max(60, int(total_seconds))

    if total_winners == 1:
        t = random.randint(int(total_seconds * 0.25), int(total_seconds * 0.90))
        return [t]

    latest = int(total_seconds * 0.95)
    earliest = int(total_seconds * 0.08)

    span = max(1, latest - earliest)
    k = min(total_winners, span)
    times = sorted(random.sample(range(earliest, latest), k=k))

    MIN_GAP = 8
    fixed = [times[0]]
    for t in times[1:]:
        if t - fixed[-1] < MIN_GAP:
            t = fixed[-1] + MIN_GAP
        fixed.append(t)

    for i in range(len(fixed) - 1, -1, -1):
        max_allowed = latest - (len(fixed) - 1 - i) * MIN_GAP
        if fixed[i] > max_allowed:
            fixed[i] = max_allowed
        if i > 0 and fixed[i] - fixed[i - 1] < MIN_GAP:
            fixed[i - 1] = fixed[i] - MIN_GAP

    fixed = [max(1, min(latest, t)) for t in fixed]
    fixed = sorted(set(fixed))

    while len(fixed) < total_winners:
        fixed.append(random.randint(earliest, latest))
        fixed = sorted(set(fixed))
    fixed = fixed[:total_winners]
    return fixed


def should_pick_now(elapsed_sec: int, schedule: list, already_picked: int) -> bool:
    if already_picked >= len(schedule):
        return False
    return elapsed_sec >= schedule[already_picked]


# =========================================================
# UI MARKUPS
# =========================================================
def join_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data="join_giveaway")]]
    )


def claim_button_markup(gid: str):
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


def autodraw_toggle_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Auto Draw ON", callback_data="autodraw_on"),
            InlineKeyboardButton("⛔ Auto Draw OFF", callback_data="autodraw_off"),
        ]]
    )


def selection_buttons_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🍀 Try Your Luck", callback_data="try_luck"),
            InlineKeyboardButton("📌 Entry Rule", callback_data="entry_rule"),
        ]]
    )


# =========================================================
# POPUPS (ENGLISH)
# =========================================================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n"
        "You must join the required channels/groups first ✅\n\n"
        "After joining, click JOIN GIVEAWAY again."
    )


def popup_old_winner_blocked() -> str:
    return (
        "🚫 RESTRICTED\n"
        "You already won a previous giveaway.\n"
        "Repeat winners are blocked for fairness.\n\n"
        "Please wait for the next giveaway."
    )


def popup_first_join(username: str, uid: str) -> str:
    return (
        "🥇 FIRST JOIN CHAMPION 🌟\n"
        "Congratulations! You joined\n"
        "the giveaway FIRST and secured\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n"
        "📸 Please take a screenshot\n"
        "and post it in the group\n"
        "to confirm 👈"
    )


def popup_already_joined() -> str:
    return (
        "🚫 ENTRY UNSUCCESSFUL\n"
        "You’ve already joined this giveaway.\n\n"
        "Multiple entries aren’t allowed.\n"
        "Please wait for the final result ⏳"
    )


def popup_join_success(username: str, uid: str) -> str:
    return (
        "✅ JOINED SUCCESSFULLY!\n\n"
        "Your details:\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        f"— {HOST_NAME}"
    )


def popup_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n"
        "You are permanently blocked from joining giveaways.\n\n"
        f"Contact admin: {ADMIN_CONTACT}"
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
        "⏳ PRIZE EXPIRED\n\n"
        "Your 24-hour claim time has ended.\n"
        "This prize is no longer available."
    )


def popup_giveaway_completed() -> str:
    return (
        "✅ GIVEAWAY COMPLETED\n\n"
        "This giveaway has been completed.\n"
        f"If you have any issues, please contact admin 👉 {ADMIN_CONTACT}"
    )


def popup_claim_winner(username: str, uid: str) -> str:
    return (
        "🌟 CONGRATULATIONS!\n"
        "You’ve won this giveaway ✅\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        "📩 Please contact admin to claim:\n"
        f"👉 {ADMIN_CONTACT}"
    )


def popup_claim_delivered(username: str, uid: str) -> str:
    return (
        "📦 PRIZE ALREADY DELIVERED\n"
        "Your prize has already been\n"
        "successfully delivered ✅\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n"
        "If you face any issue,\n"
        f"contact admin 👉 {ADMIN_CONTACT}"
    )


def popup_entry_rule() -> str:
    return (
        "📌 ENTRY RULE\n\n"
        "⏰ Lucky Draw Time:\n"
        f"Available only at {format_hms(LUCKY_TRIGGER_REMAINING)} remaining\n\n"
        "• Tap 🍀 Try Your Luck at the exact moment\n"
        "• First click wins instantly (Lucky Draw)\n"
        "• Must have a valid @username\n"
        "• Winner is added live to the selection post\n"
        "• 100% fair: first-come-first-win"
    )


def popup_lucky_win(username: str, uid: str) -> str:
    return (
        "🌟 CONGRATULATIONS!\n"
        "You won the 🍀 Lucky Draw Winner slot ✅\n\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n"
        "📸 Take a screenshot and send it in the group to confirm 👈\n\n"
        "🏆 Added to winners list LIVE!"
    )


def popup_lucky_closed(winner_uname: str, winner_uid: str) -> str:
    return (
        "⏰ LUCKY DRAW CLOSED\n\n"
        "The Lucky Draw window has just ended.\n"
        "This slot is no longer available.\n\n"
        "🏆 Lucky Draw Winner:\n"
        f"👤 {winner_uname}\n"
        f"🆔 {winner_uid}\n\n"
        "Please wait for the final winners announcement."
    )


def popup_no_username_required() -> str:
    return (
        "🚫 ENTRY DENIED\n"
        "You don’t have a valid @username.\n"
        "You are automatically excluded."
    )


# =========================================================
# TEXT BUILDERS (CHANNEL POSTS)
# =========================================================
def format_rules() -> str:
    rules = (data.get("rules") or "").strip()
    if not rules:
        rules = (
            "Must join the official channel\n"
            "One entry per user only\n"
            "Stay active until result announcement\n"
            "Admin decision is final & binding"
        )
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    return "\n".join(f"• {l}" for l in lines)


def build_preview_text() -> str:
    remaining = int(data.get("duration_seconds", 0) or 0)
    return (
        f"{LINE}\n"
        "🔍 GIVEAWAY PREVIEW (ADMIN)\n"
        f"{LINE}\n\n"
        f"⚡ {data.get('title','')} ⚡\n\n"
        "🎁 Prize Pool 🌟\n"
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
        f"{LINE}\n"
        "👇 Tap below to join the giveaway 👇"
    )


def build_live_text(remaining: int) -> str:
    duration = int(data.get("duration_seconds", 1) or 1)
    elapsed = max(0, duration - remaining)
    percent = int(round((elapsed / float(duration)) * 100))
    return (
        f"{LINE}\n"
        f"⚡ {data.get('title','POWER POINT BREAK GIVEAWAY')} ⚡\n"
        f"{LINE}\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{data.get('prize','')}\n\n"
        f"👥 Total Participants: {participants_count()}  \n"
        f"🏆 Total Winners: {data.get('winner_count',0)}  \n\n"
        "🎯 Winner Selection\n"
        "• 100% Random & Fair  \n"
        "• Auto System  \n\n"
        f"⏱️ Time Remaining: {format_hms(remaining)}  \n"
        "📊 Live Progress\n"
        f"{build_progress(percent)}  \n\n"
        "📜 Official Rules  \n"
        f"{format_rules()}  \n\n"
        f"📢 Hosted by: {HOST_NAME}  \n\n"
        f"{LINE}\n"
        "👇 Tap below to join the giveaway 👇"
    )


def build_closed_simple_text() -> str:
    prize = (data.get("prize") or "").strip()
    return (
        f"{LINE2}\n"
        "🚫 GIVEAWAY CLOSED 🚫\n"
        f"{LINE2}\n\n"
        "⏰ The giveaway has officially ended.  \n"
        "🔒 All entries are now locked.\n\n"
        "📊 Giveaway Summary  \n"
        f"🎁 Prize: {prize}  \n\n"
        f"👥 Total Participants: {participants_count()}  \n"
        f"🏆 Total Winners: {data.get('winner_count',0)}  \n\n"
        "🎯 Winners will be announced very soon.  \n"
        "Please stay tuned for the final results.\n\n"
        "✨ Best of luck to everyone!\n\n"
        f"— {HOST_NAME} ⚡\n"
        f"{LINE2}"
    )


def build_winners_post_text(gid: str, title: str, prize: str, winners_map: dict, delivered_map: dict) -> str:
    delivered_count = len([1 for uid, ok in (delivered_map or {}).items() if ok])
    total_winners = len(winners_map or {})

    lines = []
    lines.append("🏆 GIVEAWAY WINNER ANNOUNCEMENT 🏆")
    lines.append("")
    lines.append(f"🆔 Giveaway ID: {gid}")
    lines.append(f"⚡ {title} ⚡")
    lines.append("")
    lines.append(f"🎁 PRIZE: {prize}")
    lines.append(f"📦 Prize Delivery: {delivered_count}/{total_winners}")
    lines.append("")
    lines.append("👑 WINNERS LIST")
    i = 1
    for uid, info in winners_map.items():
        uname = (info or {}).get("username", "") or ""
        mark = "  Delivery ✅" if delivered_map.get(uid) else ""
        lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}{mark}")
        i += 1
    lines.append("")
    lines.append("👇 Click the button below to claim your prize")
    lines.append("")
    lines.append("⏳ Rule: Claim within 24 hours — after that, prize expires.")
    return "\n".join(lines)


def build_live_autodraw_text(title: str, prize: str, selected_count: int, total_winners: int,
                            percent: int, remaining: int, spin: str,
                            line1: str, c1: str,
                            line2: str, c2: str,
                            line3: str, c3: str) -> str:
    bar = build_progress(percent)
    return (
        f"{LINE}\n"
        "🎲 LIVE RANDOM WINNER SELECTION\n"
        f"{LINE}\n\n"
        f"⚡ {title} ⚡\n\n"
        "🎁 GIVEAWAY SUMMARY  \n"
        f"🏆 Prize: {prize}  \n"
        f"✅ Winners Selected: {selected_count}/{total_winners}\n\n"
        "📌 Important Rule  \n"
        "Users without a valid @username  \n"
        "are automatically excluded.\n\n"
        f"{spin} Selection Progress: {percent}%  \n"
        f"📊 Progress Bar: {bar}  \n\n"
        f"🕒 Time Remaining: {format_hms(remaining)}  \n"
        "🔐 System Mode: 100% Random • Fair • Auto  \n\n"
        f"{LINE}\n"
        "👥 LIVE ENTRIES SHOWCASE\n"
        f"{LINE}\n"
        f"{c1} Now Showing → {line1}  \n"
        f"{c2} Now Showing → {line2}  \n"
        f"{c3} Now Showing → {line3}  \n"
        f"{LINE}"
    )


# =========================================================
# LIVE COUNTDOWN (CHANNEL GIVEAWAY POST)
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
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_simple_text())
                data["closed_message_id"] = m.message_id
                save_data()
            except Exception:
                pass

            # if autodraw enabled => start auto selection in channel
            if data.get("autodraw_enabled"):
                try:
                    start_autodraw_channel_progress(context.job_queue, context.bot)
                except Exception:
                    pass
            else:
                # manual draw: notify admin
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

    # edit outside lock
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
# AUTO SELECTION (CHANNEL) - 10 MIN + LIVE SHOWCASE + BUTTONS
# =========================================================
def stop_auto_selection_job():
    global auto_sel_job
    if auto_sel_job is not None:
        try:
            auto_sel_job.schedule_removal()
        except Exception:
            pass
    auto_sel_job = None


def start_autodraw_channel_progress(job_queue, bot):
    global auto_sel_job
    stop_auto_selection_job()

    # eligible participants (must have valid @username)
    with lock:
        parts = list((data.get("participants", {}) or {}).items())

    eligible = []
    for uid, info in parts:
        uname = (info or {}).get("username", "") or ""
        uname = uname.strip()
        if is_valid_username(uname):
            eligible.append((str(uid), uname))

    # if no eligible, still show a post (system running)
    if not eligible:
        eligible = [("0", "@username")]

    # showcase deck
    deck = []

    def refill_deck():
        nonlocal deck
        deck = eligible[:]
        random.shuffle(deck)

    def pick_next_excluding(exclude_ids):
        nonlocal deck
        tries = 0
        while True:
            if not deck:
                refill_deck()
            uid, uname = deck.pop(0)
            if uid not in exclude_ids:
                return (uid, uname)
            deck.append((uid, uname))
            tries += 1
            if tries > 60:
                return (uid, uname)

    refill_deck()

    with lock:
        total_winners = max(1, int(data.get("winner_count", 1) or 1))
        title = (data.get("title") or "POWER POINT BREAK").strip()
        prize = (data.get("prize") or "").strip()

    # Selection pool: unique eligible users
    eligible_ids = [u for u, _ in eligible if u != "0"]
    random.shuffle(eligible_ids)

    # first join champion ONLY if eligible with username
    with lock:
        first_uid = str(data.get("first_winner_id") or "")
        first_uname = (data.get("first_winner_username", "") or "").strip()

    selected = []
    if first_uid and first_uid in eligible_ids and is_valid_username(first_uname):
        selected.append(first_uid)
        eligible_ids = [x for x in eligible_ids if x != first_uid]

    # total possible
    max_possible = len(selected) + len(eligible_ids)
    if max_possible <= 0:
        total_winners = 1
    else:
        total_winners = min(total_winners, max_possible)

    pick_schedule = make_random_pick_schedule(total_winners, AUTO_DRAW_DURATION_SECONDS)

    # initial showcase
    l1 = pick_next_excluding(set())
    l2 = pick_next_excluding({l1[0]})
    l3 = pick_next_excluding({l1[0], l2[0]})
    c1, c2, c3 = pick_three_distinct_colors()

    # start post
    m = bot.send_message(
        chat_id=CHANNEL_ID,
        text=build_live_autodraw_text(
            title=title,
            prize=prize,
            selected_count=len([u for u in selected if u != "0"]),
            total_winners=total_winners,
            percent=0,
            remaining=AUTO_DRAW_DURATION_SECONDS,
            spin=SPINNER[0],
            line1=format_entry(l1[0], l1[1]), c1=c1,
            line2=format_entry(l2[0], l2[1]), c2=c2,
            line3=format_entry(l3[0], l3[1]), c3=c3,
        ),
        reply_markup=selection_buttons_markup(),
    )

    try:
        bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=m.message_id, disable_notification=True)
    except Exception:
        pass

    with lock:
        data["autodraw_message_id"] = m.message_id
        data["autodraw_start_ts"] = now_ts()
        data["autodraw_bonus_winners"] = {}
        data["lucky_draw_winner_uid"] = None
        save_data()

    state = {
        "mid": m.message_id,
        "start_ts": now_ts(),
        "tick": 0,
        "tick_idx": 0,

        "t1": now_ts(),
        "t2": now_ts(),
        "t3": now_ts(),

        "line1": l1,
        "line2": l2,
        "line3": l3,

        "selected": selected[:],
        "pool": eligible_ids[:],
        "total_winners": total_winners,

        "pick_schedule": pick_schedule,

        "title": title,
        "prize": prize,
    }

    def tick(context: CallbackContext):
        # compute
        state["tick"] += 1
        elapsed = int(now_ts() - state["start_ts"])
        remaining = max(0, AUTO_DRAW_DURATION_SECONDS - elapsed)
        percent = int(round(min(100, (elapsed / float(AUTO_DRAW_DURATION_SECONDS)) * 100)))
        spin = SPINNER[(state["tick"] - 1) % len(SPINNER)]

        n = now_ts()

        # showcase changes 5/7/9 seconds
        if n - state["t1"] >= SHOW_LINE1_SEC:
            state["line1"] = pick_next_excluding({state["line2"][0], state["line3"][0]})
            state["t1"] = n

        if n - state["t2"] >= SHOW_LINE2_SEC:
            state["line2"] = pick_next_excluding({state["line1"][0], state["line3"][0]})
            state["t2"] = n

        if n - state["t3"] >= SHOW_LINE3_SEC:
            state["line3"] = pick_next_excluding({state["line1"][0], state["line2"][0]})
            state["t3"] = n

        # pick winners at random schedule (no fixed interval)
        picked_count = len([u for u in state["selected"] if u != "0"])
        if picked_count < state["total_winners"]:
            if should_pick_now(elapsed, state["pick_schedule"], picked_count):
                if state["pool"]:
                    state["selected"].append(state["pool"].pop(0))

        # count includes Lucky winner
        with lock:
            bonus = data.get("autodraw_bonus_winners", {}) or {}
        bonus_count = len(bonus)

        selected_count = len([u for u in state["selected"] if u != "0"]) + bonus_count

        c1, c2, c3 = pick_three_distinct_colors()

        text = build_live_autodraw_text(
            title=state["title"],
            prize=state["prize"],
            selected_count=selected_count,
            total_winners=state["total_winners"],
            percent=percent,
            remaining=remaining,
            spin=spin,
            line1=format_entry(state["line1"][0], state["line1"][1]), c1=c1,
            line2=format_entry(state["line2"][0], state["line2"][1]), c2=c2,
            line3=format_entry(state["line3"][0], state["line3"][1]), c3=c3,
        )

        try:
            context.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=state["mid"],
                text=text,
                reply_markup=selection_buttons_markup(),
            )
        except Exception:
            pass

        if remaining <= 0:
            # finalize: remove closed + selection, post winners
            autodraw_finalize_from_state(context, state)
            stop_auto_selection_job()
            return

        # schedule next tick
        nxt = AUTO_TICK_INTERVALS[state["tick_idx"] % len(AUTO_TICK_INTERVALS)]
        state["tick_idx"] += 1
        context.job_queue.run_once(tick, when=nxt)

    auto_sel_job = job_queue.run_once(tick, when=0)


def autodraw_finalize_from_state(context: CallbackContext, state: dict):
    # remove closed + selection messages
    with lock:
        closed_mid = data.get("closed_message_id")
        auto_mid = data.get("autodraw_message_id")

    if closed_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
        except Exception:
            pass

    if auto_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=auto_mid)
        except Exception:
            pass

    # build winners map
    with lock:
        participants = data.get("participants", {}) or {}
        bonus = (data.get("autodraw_bonus_winners", {}) or {}).copy()

    winners_map = {}

    # add Lucky winner first (if exists)
    for uid, info in bonus.items():
        uname = (info or {}).get("username", "") or ""
        if is_valid_username(uname):
            winners_map[str(uid)] = {"username": uname}

    # add selected winners (valid username only)
    for uid in (state.get("selected") or []):
        uid = str(uid)
        info = participants.get(uid, {}) or {}
        uname = (info.get("username", "") or "").strip()
        if is_valid_username(uname):
            winners_map[uid] = {"username": uname}

    if not winners_map:
        return

    # trim to total winners (if Lucky adds extra, keep earliest)
    total_winners = int(state.get("total_winners", 1) or 1)
    winners_items = list(winners_map.items())[:total_winners]
    winners_map = {k: v for k, v in winners_items}

    gid = make_gid()
    ts = now_ts()

    snapshot = {
        "gid": gid,
        "title": (state.get("title") or "").strip(),
        "prize": (state.get("prize") or "").strip(),
        "winners": winners_map,
        "delivered": {},
        "created_ts": ts,
        "claim_expires_ts": ts + CLAIM_WINDOW_SECONDS,
        "winners_message_id": None,
    }

    with lock:
        hist = data.get("history", {}) or {}
        hist[gid] = snapshot
        data["history"] = hist
        data["latest_gid"] = gid

        # clear running selection references
        data["closed_message_id"] = None
        data["autodraw_message_id"] = None
        data["autodraw_start_ts"] = None
        save_data()

    # winner log
    with lock:
        for uid, info in winners_map.items():
            data["winner_log"].append({
                "gid": gid,
                "username": info.get("username", ""),
                "uid": uid,
                "prize": snapshot["prize"],
                "date": format_date(ts),
            })
        save_data()

    text = build_winners_post_text(
        gid=gid,
        title=snapshot["title"],
        prize=snapshot["prize"],
        winners_map=winners_map,
        delivered_map=snapshot["delivered"],
    )

    try:
        m = context.bot.send_message(chat_id=CHANNEL_ID, text=text, reply_markup=claim_button_markup(gid))
        with lock:
            data["history"][gid]["winners_message_id"] = m.message_id
            save_data()
    except Exception:
        pass


# =========================================================
# MANUAL DRAW (ADMIN) - PREVIEW + APPROVE
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


def build_draw_progress_text(percent: int, spin: str) -> str:
    bar = build_progress(percent)
    return (
        f"{LINE2}\n"
        "🎲 RANDOM WINNER SELECTION\n"
        f"{LINE2}\n\n"
        f"{spin} Selecting winners... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        "🔐 100% fair & random system\n"
        "✅ Username-only winners (no @username = excluded)\n\n"
        "Please wait..."
    )


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

        # eligible = username only
        eligible = []
        for uid, info in participants.items():
            uname = (info or {}).get("username", "") or ""
            uname = uname.strip()
            if is_valid_username(uname):
                eligible.append((str(uid), uname))

        if not eligible:
            try:
                context.bot.edit_message_text(
                    chat_id=admin_chat_id,
                    message_id=admin_msg_id,
                    text="No eligible users (@username required).",
                )
            except Exception:
                pass
            return

        total = max(1, int(data.get("winner_count", 1) or 1))
        total = min(total, len(eligible))

        # first join champ if eligible
        first_uid = str(data.get("first_winner_id") or "")
        first_uname = (data.get("first_winner_username", "") or "").strip()

        winners = {}
        pool = eligible[:]
        random.shuffle(pool)

        if first_uid and is_valid_username(first_uname):
            winners[first_uid] = {"username": first_uname}
            pool = [(u, n) for (u, n) in pool if u != first_uid]

        for uid, uname in pool:
            if len(winners) >= total:
                break
            winners[uid] = {"username": uname}

        # build preview text (admin)
        gid = make_gid()
        ts = now_ts()
        snapshot = {
            "gid": gid,
            "title": (data.get("title") or "").strip(),
            "prize": (data.get("prize") or "").strip(),
            "winners": winners,
            "delivered": {},
            "created_ts": ts,
            "claim_expires_ts": ts + CLAIM_WINDOW_SECONDS,
            "winners_message_id": None,
        }

        data["_pending_snapshot"] = snapshot
        save_data()

    preview_text = build_winners_post_text(
        gid=snapshot["gid"],
        title=snapshot["title"],
        prize=snapshot["prize"],
        winners_map=snapshot["winners"],
        delivered_map=snapshot["delivered"],
    )

    try:
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            text=preview_text,
            reply_markup=winners_approve_markup(),
        )
    except Exception:
        context.bot.send_message(
            chat_id=admin_chat_id,
            text=preview_text,
            reply_markup=winners_approve_markup(),
        )


# =========================================================
# COMMANDS (ADMIN + USERS)
# =========================================================
def cmd_start(update: Update, context: CallbackContext):
    u = update.effective_user

    if u and u.id == ADMIN_ID:
        update.message.reply_text(
            "👋 Welcome, Admin!\n\n"
            "You have successfully started the Giveaway Management Bot.\n\n"
            "From here, you can:\n"
            "• Create and manage giveaways\n"
            "• Control auto / manual winner selection\n"
            "• Review winners and delivery status\n"
            "• Access advanced admin commands\n\n"
            "Use /panel to get started.\n"
            "If you need help at any time, use /panel\n\n"
            "🚀 Let’s run a perfect giveaway!"
        )
        return

    # UNAUTHORIZED NOTICE (and notify admin)
    uname = user_tag(u.username or "") if u else ""
    uid = str(u.id) if u else ""
    update.message.reply_text(
        f"{LINE}\n"
        "⚠️ UNAUTHORIZED NOTICE\n"
        f"{LINE}\n\n"
        "Hi there!\n"
        f"Username: {uname or 'N/A'}\n"
        f"User ID: {uid or 'N/A'}\n\n"
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
        f"{LINE}"
    )

    try:
        context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🔔 Bot Start Attempt (User)\n\n"
                f"Username: {uname or 'N/A'}\n"
                f"User ID: {uid or 'N/A'}"
            ),
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
        "/draw\n"
        "/autodraw\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "🔒 BAN SYSTEM\n"
        "/blockpermanent\n"
        "/unban\n"
        "/blocklist\n"
        "/removeban\n\n"
        "📦 DELIVERY SYSTEM\n"
        "/prizeDelivered\n\n"
        "📜 WINNER HISTORY\n"
        "/winnerlist\n\n"
        "♻️ RESET\n"
        "/reset"
    )


def cmd_autodraw(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    status = "ON ✅" if data.get("autodraw_enabled") else "OFF ⛔"
    update.message.reply_text(
        f"🎲 Auto Draw Setting: {status}\n\nChoose an option:",
        reply_markup=autodraw_toggle_markup()
    )


def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        f"{LINE2}\n"
        "✅ ADD VERIFY TARGET\n"
        f"{LINE2}\n\n"
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
        LINE2,
        "🗑 REMOVE VERIFY TARGET",
        LINE2,
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
    stop_auto_selection_job()

    with lock:
        keep_perma = data.get("permanent_block", {}) or {}
        keep_verify = data.get("verify_targets", []) or {}

        data.clear()
        data.update(fresh_default_data())
        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        save_data()

    admin_state = "title"
    update.message.reply_text(
        f"{LINE2}\n"
        "🆕 NEW GIVEAWAY SETUP\n"
        f"{LINE2}\n\n"
        "STEP 1 — GIVEAWAY TITLE\n\n"
        "Send Giveaway Title:"
    )


def cmd_participants(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    parts = data.get("participants", {}) or {}
    if not parts:
        update.message.reply_text("👥 Participants list is empty.")
        return

    lines = [
        LINE2,
        "👥 PARTICIPANTS LIST (ADMIN)",
        LINE2,
        f"Total Participants: {len(parts)}",
        "",
    ]
    i = 1
    for uid, info in parts.items():
        uname = (info or {}).get("username", "")
        lines.append(f"{i}. {uname or 'N/A'} | User ID: {uid}")
        i += 1

    update.message.reply_text("\n".join(lines))


def cmd_endgiveaway(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not data.get("active"):
        update.message.reply_text("No active giveaway is running right now.")
        return

    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm End", callback_data="end_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="end_cancel"),
        ]]
    )
    update.message.reply_text(
        f"{LINE2}\n"
        "⚠️ END GIVEAWAY CONFIRMATION\n"
        f"{LINE2}\n\n"
        "Are you sure you want to end this giveaway now?",
        reply_markup=kb
    )


def cmd_draw(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not data.get("closed"):
        update.message.reply_text("Giveaway is not closed yet.")
        return
    if data.get("autodraw_enabled"):
        update.message.reply_text("Auto Draw is ON. Manual /draw is not required.")
        return
    start_draw_progress(context, update.effective_chat.id)


def cmd_blockpermanent(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "perma_block_list"
    update.message.reply_text(
        f"{LINE2}\n"
        "🔒 PERMANENT BLOCK\n"
        f"{LINE2}\n\n"
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
    lines.append(LINE2)
    lines.append("📌 BAN LISTS")
    lines.append(LINE2)
    lines.append("")
    lines.append(f"OLD WINNER MODE: {data.get('old_winner_mode','skip').upper()}")
    lines.append("")

    lines.append("⛔ OLD WINNER BLOCK LIST")
    lines.append(f"Total: {len(oldw)}")
    if oldw:
        i = 1
        for uid, info in oldw.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {uname or 'N/A'} | User ID: {uid}")
            i += 1
    else:
        lines.append("No old-winner blocked users.")
    lines.append("")

    lines.append("🔒 PERMANENT BLOCK LIST")
    lines.append(f"Total: {len(perma)}")
    if perma:
        i = 1
        for uid, info in perma.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {uname or 'N/A'} | User ID: {uid}")
            i += 1
    else:
        lines.append("No permanently blocked users.")

    update.message.reply_text("\n".join(lines))


def cmd_prize_delivered(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "prize_delivered_list"
    update.message.reply_text(
        "📦 PRIZE DELIVERY UPDATE\n\n"
        "Send delivered users list (one per line):\n"
        "@username | user_id\n\n"
        "Example:\n"
        "@MinexxProo | 5692210187\n\n"
        "Note: If you do not specify a Giveaway ID, it will update the latest giveaway."
    )


def cmd_winnerlist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    logs = data.get("winner_log", []) or []
    if not logs:
        update.message.reply_text("No winner history found yet.")
        return

    # last 50
    logs = logs[-50:]
    lines = [LINE2, "📜 WINNER HISTORY", LINE2, ""]
    for i, row in enumerate(reversed(logs), start=1):
        lines.append(f"{i}) Giveaway ID: {row.get('gid','')}")
        lines.append(f"   Prize: {row.get('prize','')}")
        lines.append(f"   Winner: {row.get('username','')} | 🆔 {row.get('uid','')}")
        lines.append(f"   Date: {row.get('date','')}")
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
    update.message.reply_text("Confirm reset?", reply_markup=kb)


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
            targets.append({"ref": ref, "display": ref})
            data["verify_targets"] = targets
            save_data()

        update.message.reply_text(f"✅ Verify target added: {ref}")
        admin_state = None
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
            data["title"] = msg
            save_data()
        admin_state = "prize"
        update.message.reply_text("✅ Title saved.\n\nSend Giveaway Prize (multi-line allowed):")
        return

    if admin_state == "prize":
        with lock:
            data["prize"] = msg
            save_data()
        admin_state = "winners"
        update.message.reply_text("✅ Prize saved.\n\nSend Total Winner Count (1 - 1000000):")
        return

    if admin_state == "winners":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number.")
            return
        count = max(1, min(1000000, int(msg)))
        with lock:
            data["winner_count"] = count
            save_data()
        admin_state = "duration"
        update.message.reply_text("✅ Winner count saved.\n\nSend Giveaway Duration (e.g. 30 Second / 5 Minute / 1 Hour):")
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
            f"{LINE2}\n"
            "🔐 OLD WINNER MODE\n"
            f"{LINE2}\n\n"
            "1) BLOCK old winners (cannot join)\n"
            "2) SKIP (allow everyone)\n\n"
            "Reply 1 or 2:"
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
            update.message.reply_text("✅ Old Winner Mode: SKIP\n\nNow send Giveaway Rules (multi-line):")
            return

        with lock:
            data["old_winner_mode"] = "block"
            data["old_winners"] = {}
            save_data()

        admin_state = "old_winner_block_list"
        update.message.reply_text(
            f"{LINE2}\n"
            "⛔ OLD WINNER BLOCK LIST\n"
            f"{LINE2}\n\n"
            "Send old winners list (one per line):\n"
            "@username | user_id\n"
            "or user_id"
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
                ow[uid] = {"username": uname}
            data["old_winners"] = ow
            save_data()
        admin_state = "rules"
        update.message.reply_text("✅ Old winner block list saved.\n\nNow send Giveaway Rules (multi-line):")
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        update.message.reply_text("✅ Rules saved.\n\nPreview:")
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
            "✅ Permanent block saved.\n"
            f"New Added: {len(data['permanent_block']) - before}\n"
            f"Total Blocked: {len(data['permanent_block'])}"
        )
        return

    # UNBAN
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
                update.message.reply_text("✅ Unbanned from Permanent Block.")
            else:
                update.message.reply_text("This user is not in Permanent Block list.")
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
                update.message.reply_text("✅ Unbanned from Old Winner Block.")
            else:
                update.message.reply_text("This user is not in Old Winner Block list.")
        admin_state = None
        return

    # PRIZE DELIVERED
    if admin_state == "prize_delivered_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: @username | user_id")
            return

        with lock:
            gid = data.get("latest_gid")
            if not gid or gid not in (data.get("history", {}) or {}):
                update.message.reply_text("No giveaway winners post found to update.")
                admin_state = None
                return

            snap = data["history"][gid]
            winners_map = snap.get("winners", {}) or {}
            delivered = snap.get("delivered", {}) or {}

            changed = 0
            for uid, _uname in entries:
                if uid in winners_map:
                    if not delivered.get(uid):
                        delivered[uid] = True
                        changed += 1

            snap["delivered"] = delivered
            data["history"][gid] = snap
            save_data()

        # update channel winners post
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
                    reply_markup=claim_button_markup(gid),
                )
            except Exception:
                pass

        admin_state = None
        update.message.reply_text(
            "✅ Prize delivery updated successfully.\n"
            f"Updated: {changed} winner(s)\n"
            f"Giveaway ID: {gid}"
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

    # AutoDraw toggles
    if qd == "autodraw_on":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        with lock:
            data["autodraw_enabled"] = True
            save_data()
        query.answer("Auto Draw turned ON ✅", show_alert=True)
        return

    if qd == "autodraw_off":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        with lock:
            data["autodraw_enabled"] = False
            save_data()
        query.answer("Auto Draw turned OFF ⛔", show_alert=True)
        return

    # Entry Rule
    if qd == "entry_rule":
        try:
            query.answer(popup_entry_rule(), show_alert=True)
        except Exception:
            pass
        return

    # Try Your Luck (Lucky Draw)
    if qd == "try_luck":
        tg_user = query.from_user
        uid_str = str(tg_user.id)
        uname = user_tag(tg_user.username or "")

        if not is_valid_username(uname):
            query.answer(popup_no_username_required(), show_alert=True)
            return

        with lock:
            mid = data.get("autodraw_message_id")
            start_ts = data.get("autodraw_start_ts")
            lucky_uid = data.get("lucky_draw_winner_uid")
            bonus = data.get("autodraw_bonus_winners", {}) or {}

        # must be running auto selection
        if not mid or not start_ts:
            query.answer("⏳ Not available right now.", show_alert=True)
            return

        elapsed = int(now_ts() - float(start_ts))
        remaining = max(0, AUTO_DRAW_DURATION_SECONDS - elapsed)

        # STRICT: allowed only when remaining == 08:48
        if remaining != LUCKY_TRIGGER_REMAINING:
            # show winner info if already taken
            if lucky_uid and lucky_uid in bonus:
                w_uname = bonus[lucky_uid].get("username", "@username")
                query.answer(popup_lucky_closed(w_uname, lucky_uid), show_alert=True)
                return

            query.answer(
                "⏰ LUCKY DRAW CLOSED\n\n"
                "The Lucky Draw window has just ended.\n"
                "This slot is no longer available.\n\n"
                "Please wait for the final winners announcement.",
                show_alert=True
            )
            return

        # first click wins (race-safe)
        with lock:
            # already taken
            if data.get("lucky_draw_winner_uid"):
                if data["lucky_draw_winner_uid"] == uid_str:
                    query.answer(popup_lucky_win(uname, uid_str), show_alert=True)
                    return

                lucky_uid2 = data["lucky_draw_winner_uid"]
                b2 = data.get("autodraw_bonus_winners", {}) or {}
                w_uname = (b2.get(lucky_uid2, {}) or {}).get("username", "@username")
                query.answer(popup_lucky_closed(w_uname, lucky_uid2), show_alert=True)
                return

            # save winner
            data["lucky_draw_winner_uid"] = uid_str
            b = data.get("autodraw_bonus_winners", {}) or {}
            b[uid_str] = {"username": uname}
            data["autodraw_bonus_winners"] = b
            save_data()

        query.answer(popup_lucky_win(uname, uid_str), show_alert=True)
        return

    # Preview actions
    if qd.startswith("preview_"):
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return

        if qd == "preview_approve":
            query.answer()
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

                    # reset per-giveaway state
                    data["participants"] = {}
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""

                    data["autodraw_message_id"] = None
                    data["autodraw_start_ts"] = None
                    data["autodraw_bonus_winners"] = {}
                    data["lucky_draw_winner_uid"] = None

                    save_data()

                start_live_countdown(context.job_queue)
                query.edit_message_text("✅ Giveaway approved and posted to channel.")
            except Exception as e:
                query.edit_message_text(f"Failed to post in channel. Make sure bot is admin.\nError: {e}")
            return

        if qd == "preview_reject":
            query.answer()
            query.edit_message_text("❌ Giveaway rejected.")
            return

        if qd == "preview_edit":
            query.answer()
            query.edit_message_text("✏️ Edit Mode\n\nStart again with /newgiveaway")
            return

    # End giveaway confirm/cancel
    if qd == "end_confirm":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()

        with lock:
            if not data.get("active"):
                query.edit_message_text("No active giveaway is running right now.")
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

        # post closed post
        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_simple_text())
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
        except Exception:
            pass

        stop_live_countdown()

        # auto draw behavior
        if data.get("autodraw_enabled"):
            try:
                start_autodraw_channel_progress(context.job_queue, context.bot)
            except Exception:
                pass
            query.edit_message_text("✅ Giveaway closed. Auto selection started in channel.")
        else:
            query.edit_message_text("✅ Giveaway closed. Use /draw (manual).")

        return

    if qd == "end_cancel":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        return

    # Reset confirm/cancel
    if qd == "reset_confirm":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()

        stop_live_countdown()
        stop_draw_jobs()
        stop_auto_selection_job()

        with lock:
            keep_perma = data.get("permanent_block", {}) or {}
            keep_verify = data.get("verify_targets", []) or []
            keep_hist = data.get("history", {}) or {}
            keep_log = data.get("winner_log", []) or []

            data.clear()
            data.update(fresh_default_data())
            data["permanent_block"] = keep_perma
            data["verify_targets"] = keep_verify
            data["history"] = keep_hist
            data["winner_log"] = keep_log
            save_data()

        query.edit_message_text("✅ Reset completed. Start with /newgiveaway")
        return

    if qd == "reset_cancel":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        query.edit_message_text("❌ Reset cancelled.")
        return

    # Unban choose
    if qd == "unban_permanent":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        admin_state = "unban_permanent_input"
        query.edit_message_text("Send User ID (or @name | id) to unban from Permanent Block:")
        return

    if qd == "unban_oldwinner":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        admin_state = "unban_oldwinner_input"
        query.edit_message_text("Send User ID (or @name | id) to unban from Old Winner Block:")
        return

    # removeban choose confirm
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
            query.edit_message_text("Confirm reset Permanent Ban List?", reply_markup=kb)
            return

        if qd == "reset_oldwinner_ban":
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm Reset Old Winner", callback_data="confirm_reset_oldwinner"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
                ]]
            )
            query.edit_message_text("Confirm reset Old Winner Ban List?", reply_markup=kb)
            return

    if qd == "cancel_reset_ban":
        query.answer()
        admin_state = None
        query.edit_message_text("Cancelled.")
        return

    if qd == "confirm_reset_permanent":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        with lock:
            data["permanent_block"] = {}
            save_data()
        query.edit_message_text("✅ Permanent Ban List has been reset.")
        return

    if qd == "confirm_reset_oldwinner":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        with lock:
            data["old_winners"] = {}
            save_data()
        query.edit_message_text("✅ Old Winner Ban List has been reset.")
        return

    # Join giveaway
    if qd == "join_giveaway":
        if not data.get("active"):
            query.answer("This giveaway is not active right now.", show_alert=True)
            return

        # verify required targets
        if not verify_user_join(context.bot, int(uid)):
            query.answer(popup_verify_required(), show_alert=True)
            return

        # permanent block
        with lock:
            if uid in (data.get("permanent_block", {}) or {}):
                query.answer(popup_permanent_blocked(), show_alert=True)
                return

            # old winner block
            if data.get("old_winner_mode") == "block":
                if uid in (data.get("old_winners", {}) or {}):
                    query.answer(popup_old_winner_blocked(), show_alert=True)
                    return

            # already joined?
            if uid in (data.get("participants", {}) or {}):
                query.answer(popup_already_joined(), show_alert=True)
                return

        tg_user = query.from_user
        uname = user_tag(tg_user.username or "")
        full_name = (tg_user.full_name or "").strip()

        # save participant
        with lock:
            # first join winner
            if not data.get("first_winner_id"):
                data["first_winner_id"] = uid
                data["first_winner_username"] = uname
                data["first_winner_name"] = full_name

            data["participants"][uid] = {"username": uname, "name": full_name}
            save_data()

        # update live post
        try:
            with lock:
                live_mid = data.get("live_message_id")
                start_ts = data.get("start_time")
                duration = int(data.get("duration_seconds", 1) or 1)
            if live_mid and start_ts:
                start = datetime.utcfromtimestamp(start_ts)
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

        # popup
        with lock:
            if data.get("first_winner_id") == uid:
                query.answer(popup_first_join(uname or "@username", uid), show_alert=True)
            else:
                query.answer(popup_join_success(uname or "@username", uid), show_alert=True)
        return

    # Winners Approve/Reject (manual draw)
    if qd == "winners_approve":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()

        with lock:
            snap = data.get("_pending_snapshot")
            if not snap:
                query.edit_message_text("No pending winners snapshot found.")
                return

            gid = snap["gid"]
            data["history"][gid] = snap
            data["latest_gid"] = gid

            # clear pending
            data.pop("_pending_snapshot", None)
            save_data()

        # remove closed message from channel
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

        # post winners in channel
        text = build_winners_post_text(
            gid=gid,
            title=snap.get("title", ""),
            prize=snap.get("prize", ""),
            winners_map=snap.get("winners", {}),
            delivered_map=snap.get("delivered", {}),
        )

        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=text, reply_markup=claim_button_markup(gid))
            with lock:
                data["history"][gid]["winners_message_id"] = m.message_id

                # winner log
                ts = snap.get("created_ts", now_ts())
                for wuid, winfo in (snap.get("winners") or {}).items():
                    data["winner_log"].append({
                        "gid": gid,
                        "username": winfo.get("username", ""),
                        "uid": wuid,
                        "prize": snap.get("prize", ""),
                        "date": format_date(ts),
                    })
                save_data()

            query.edit_message_text("✅ Approved! Winners list posted to channel.")
        except Exception as e:
            query.edit_message_text(f"Failed to post winners in channel: {e}")
        return

    if qd == "winners_reject":
        if uid != str(ADMIN_ID):
            query.answer("Admin only.", show_alert=True)
            return
        query.answer()
        with lock:
            data.pop("_pending_snapshot", None)
            save_data()
        query.edit_message_text("❌ Rejected! Winners list will NOT be posted.")
        return

    # Claim Prize (per giveaway id)
    if qd.startswith("claim:"):
        gid = qd.split(":", 1)[1].strip()
        with lock:
            hist = data.get("history", {}) or {}
            snap = hist.get(gid)

        if not snap:
            query.answer(popup_giveaway_completed(), show_alert=True)
            return

        winners_map = snap.get("winners", {}) or {}
        delivered_map = snap.get("delivered", {}) or {}
        exp = float(snap.get("claim_expires_ts") or 0)
        created = float(snap.get("created_ts") or 0)

        # after long time => giveaway completed for everyone
        if exp and now_ts() > exp + POST_COMPLETE_AFTER_SECONDS:
            query.answer(popup_giveaway_completed(), show_alert=True)
            return

        # not winner
        if uid not in winners_map:
            # after expiry => completed (requested)
            if exp and now_ts() > exp:
                query.answer(popup_giveaway_completed(), show_alert=True)
            else:
                query.answer(popup_claim_not_winner(), show_alert=True)
            return

        uname = (winners_map.get(uid, {}) or {}).get("username", "@username")

        # delivered?
        if delivered_map.get(uid):
            query.answer(popup_claim_delivered(uname, uid), show_alert=True)
            return

        # expired?
        if exp and now_ts() > exp:
            query.answer(popup_prize_expired(), show_alert=True)
            return

        query.answer(popup_claim_winner(uname, uid), show_alert=True)
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

    # base
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("panel", cmd_panel))

    # giveaway
    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))
    dp.add_handler(CommandHandler("autodraw", cmd_autodraw))

    # verify
    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    # bans
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # delivery + history
    dp.add_handler(CommandHandler("prizeDelivered", cmd_prize_delivered))
    dp.add_handler(CommandHandler("winnerlist", cmd_winnerlist))

    # reset
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # admin text handler + callbacks
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # resume systems after restart
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    print("Bot is running (ENGLISH, PTB v13 style) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
