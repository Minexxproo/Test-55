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

HOST_NAME = os.getenv("HOST_NAME", "POWER POINT BREAK").strip() or "POWER POINT BREAK"
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@MinexxProo").strip() or "@MinexxProo"
DATA_FILE = os.getenv("DATA_FILE", "giveaway_data.json").strip() or "giveaway_data.json"

# =========================================================
# THREAD SAFE
# =========================================================
lock = threading.RLock()

# =========================================================
# CONSTANT UI (BORDER SAFE)
# =========================================================
BORDER = "━━━━━━━━━━━━━━━━━━━━"  # short border to avoid Telegram wrap
SPINNER = ["🔄", "🔃", "🔁", "🔂"]

LIVE_BAR_BLOCKS = 9
DRAW_BAR_BLOCKS = 10

LIVE_TICK_SEC = 5               # giveaway post update every 5 sec
DRAW_TOTAL_SEC = 40             # draw animation total time
DRAW_TICK_SEC = 5               # draw progress update every 5 sec
RESET_TOTAL_SEC = 40            # reset animation total time
RESET_TICK_SEC = 5              # reset progress update every 5 sec

CLAIM_WINDOW_SEC = 24 * 3600

# =========================================================
# GLOBAL RUNTIME JOBS
# =========================================================
countdown_job = None
closed_anim_job = None

draw_job = None
draw_finalize_job = None

reset_job = None
reset_finalize_job = None

# =========================================================
# DATA MODEL
# =========================================================
def fresh_data():
    """
    Everything lives here.
    Supports multiple winners posts (many claim buttons).
    """
    return {
        # giveaway run state
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

        # participants (current giveaway only)
        "participants": {},  # uid(str)-> {"username":"@x","name":"..."}

        # first join winner (current giveaway only)
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # verify system
        "verify_targets": [],  # [{"ref":"-100.. or @..","display":".."}]

        # settings
        "auto_winner_post": False,  # /autowinnerpost ON/OFF
        "block_old_winner": False,  # /blockoldwinner ON/OFF

        # old winners list (manual + stays forever unless reset)
        "old_winners": {},  # uid -> {"username":"@x"}

        # permanent block (optional if you want later)
        "permanent_block": {},  # uid -> {"username":"@x"}

        # winners posts history (multiple claim buttons supported)
        # key = winners_message_id (str)
        "winners_posts": {
            # "12345": {
            #   "title": "...",
            #   "prize": "...",
            #   "winner_count": 10,
            #   "first_uid": "...",
            #   "first_username": "@..",
            #   "random_winners": [{"uid":"..","username":"@.."}],
            #   "winners_map": {"uid":{"username":"@.."}},
            #   "created_ts": 0,
            #   "claim_expires_ts": 0,
            #   "delivered": {"uid": {"username":"@..", "ts":0}},
            # }
        },
        "last_winners_message_id": None,  # to let /prizedelivery update latest post
    }


def load_data():
    base = fresh_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}

    # merge defaults
    for k, v in base.items():
        if k not in d:
            d[k] = v

    # ensure dict types
    d.setdefault("participants", {})
    d.setdefault("verify_targets", [])
    d.setdefault("old_winners", {})
    d.setdefault("permanent_block", {})
    d.setdefault("winners_posts", {})
    return d


def save_data():
    with lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


data = load_data()

# =========================================================
# HELPERS
# =========================================================
def now_ts() -> float:
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
    return u if u.startswith("@") else ("@" + u)


def participants_count() -> int:
    return len(data.get("participants", {}) or {})


def format_hms(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d} : {m:02d} : {s:02d}"


def build_bar(percent: int, blocks: int) -> str:
    percent = max(0, min(100, int(percent)))
    filled = int(round((percent / 100.0) * blocks))
    filled = max(0, min(blocks, filled))
    empty = blocks - filled
    return ("▰" * filled) + ("▱" * empty)


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


def verify_user(bot, user_id: int) -> bool:
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
# POPUPS (ENGLISH, BORDER SAFE)
# =========================================================
def pop_verify_required() -> str:
    return (
        "🔐 Access Restricted\n"
        "You must join the required channels to proceed.\n"
        "After joining, tap JOIN once more."
    )


def pop_old_winner_blocked() -> str:
    return (
        "🚫 You have already won a previous giveaway.\n"
        "Repeat winners are restricted to keep it fair.\n"
        "Please wait for the next giveaway."
    )


def pop_first_winner(username: str, uid: str) -> str:
    return (
        "✨ CONGRATULATIONS 🌟\n"
        "You joined FIRST and secured the 🥇 1st Winner Spot!\n"
        f"👑 {username} | {uid}\n"
        "Take a screenshot and post in the group to confirm your win 👈"
    )


def pop_already_joined() -> str:
    return (
        "❌ ENTRY Unsuccessful\n"
        "You’ve already joined this giveaway 🫵\n\n"
        "Multiple entries aren’t allowed.\n"
        "Please wait for the final result ⏳"
    )


def pop_join_success(username: str, uid: str) -> str:
    return (
        "🌹 CONGRATULATIONS!\n"
        "You’ve successfully joined\n"
        "the giveaway ✅\n\n"
        "Your details:\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        f"— {HOST_NAME}"
    )


def pop_not_winner() -> str:
    return (
        "❌ NOT A WINNER\n\n"
        "Sorry, your User ID is not in the winners list.\n"
        "Please wait for the next giveaway 🤍"
    )


def pop_prize_expired() -> str:
    return (
        "⏳ PRIZE EXPIRED\n"
        "Your 24-hour claim time has ended.\n"
        "This prize is no longer available."
    )


def pop_claim_winner(username: str, uid: str) -> str:
    return (
        "🌟 Congratulations ✨\n"
        "You’ve won this giveaway.✅\n"
        f"👤 {username} | 🆔 {uid}\n"
        "📩 Please contact admin to claim your prize now:\n"
        f"👉 {ADMIN_CONTACT}"
    )


def pop_prize_delivered() -> str:
    return (
        "🌟 Congratulations!\n"
        "Your prize has already been successfully delivered to you ✅\n"
        f"If you face any issues, please contact our admin 📩 {ADMIN_CONTACT}"
    )


# =========================================================
# MARKUPS
# =========================================================
def join_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data="join_giveaway")]]
    )


def claim_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data="claim_prize")]]
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


def reset_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
        ]]
    )


def onoff_markup(prefix: str, current: bool):
    # callback: f"{prefix}_on" / f"{prefix}_off"
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🟢 ON" if not current else "✅ ON", callback_data=f"{prefix}_on"),
            InlineKeyboardButton("🔴 OFF" if current else "✅ OFF", callback_data=f"{prefix}_off"),
        ]]
    )


# =========================================================
# TEXT BUILDERS (BORDER SAFE)
# =========================================================
def format_rules_block(rules_text: str) -> str:
    rules_text = (rules_text or "").strip()
    if not rules_text:
        return (
            "✅ Must join official channel\n"
            "❌ One account per user\n"
            "🚫 No fake / duplicate accounts"
        )
    lines = [l.strip() for l in rules_text.splitlines() if l.strip()]
    out = []
    for l in lines:
        # keep short (no bullets needed)
        out.append(l)
    return "\n".join(out)


def build_preview_text() -> str:
    # 0% preview
    return (
        f"{BORDER}\n"
        "🔍 GIVEAWAY PREVIEW\n"
        "ADMIN ONLY\n"
        f"{BORDER}\n\n"
        f"⚡️🔥 {HOST_NAME}\n"
        "GIVEAWAY 🔥⚡️\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{data.get('prize','')}\n\n"
        f"👥 TOTAL PARTICIPANTS: 0\n"
        f"🏅 TOTAL WINNERS: {data.get('winner_count',0)}\n\n"
        "🎯 WINNER SELECTION\n"
        "100% Randomly\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(int(data.get('duration_seconds',0) or 0))}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{build_bar(0, LIVE_BAR_BLOCKS)} 0%\n\n"
        "📜 RULES\n"
        f"{format_rules_block(data.get('rules',''))}\n\n"
        "👇 TAP THE BUTTON\n"
        "BELOW & JOIN NOW 👇"
    )


def build_live_text(remaining: int) -> str:
    duration = int(data.get("duration_seconds", 1) or 1)
    elapsed = max(0, duration - remaining)
    percent = int(round((elapsed / float(duration)) * 100))
    bar = build_bar(percent, LIVE_BAR_BLOCKS)

    return (
        f"{BORDER}\n"
        f"⚡️🔥 {HOST_NAME}\n"
        "GIVEAWAY 🔥⚡️\n"
        f"{BORDER}\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{data.get('prize','')}\n\n"
        f"👥 TOTAL PARTICIPANTS: {participants_count()}\n"
        f"🏅 TOTAL WINNERS: {data.get('winner_count',0)}\n\n"
        "🎯 WINNER SELECTION\n"
        "100% Randomly\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(remaining)}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{bar} {percent}%\n\n"
        "📜 RULES\n"
        f"{format_rules_block(data.get('rules',''))}\n\n"
        f"📢 HOSTED BY {HOST_NAME}\n\n"
        "👇 TAP THE BUTTON\n"
        "BELOW & JOIN NOW 👇"
    )


def build_closed_text(spin: str = "") -> str:
    # spinner only when auto_winner_post OFF
    spin_line = ""
    if spin:
        spin_line = f"{spin} WINNER SELECTION IN PROGRESS\n"

    title_line = (data.get("title", "") or "").strip()
    if title_line:
        title_line = f"⚡ {title_line} ⚡\n\n"

    return (
        f"{BORDER}\n"
        "🚫 GIVEAWAY\n"
        "OFFICIALLY CLOSED 🚫\n"
        f"{BORDER}\n\n"
        f"{title_line}"
        "⏰ The giveaway has ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        f"{spin_line}"
        "Please wait for the announcement.\n\n"
        "🙏 Thank you for participating.\n"
        f"— {HOST_NAME} ⚡"
    )


def build_draw_text(percent: int, spin: str) -> str:
    bar = build_bar(percent, DRAW_BAR_BLOCKS)
    return (
        f"{BORDER}\n"
        "🎲 RANDOM WINNER\n"
        "SELECTION\n"
        f"{BORDER}\n\n"
        f"{spin} Winner selection is in progress...\n\n"
        "📊 LIVE PROGRESS\n"
        f"{bar} {percent}%\n\n"
        "✅ 100% Random & Fair\n"
        "🔐 User ID based selection only.\n"
    )


def build_reset_text(percent: int, spin: str) -> str:
    bar = build_bar(percent, DRAW_BAR_BLOCKS)
    return (
        f"{BORDER}\n"
        "♻️ SYSTEM RESET\n"
        f"{BORDER}\n\n"
        f"{spin} Resetting all systems...\n\n"
        "📊 PROGRESS\n"
        f"{bar} {percent}%\n\n"
        "⏳ Please wait...\n"
    )


def build_winners_post_text(post_obj: dict) -> str:
    title = (post_obj.get("title") or "").strip()
    prize = (post_obj.get("prize") or "").strip()
    winner_count = int(post_obj.get("winner_count", 0) or 0)

    delivered = post_obj.get("delivered", {}) or {}
    delivered_count = len(delivered)

    first_uid = post_obj.get("first_uid", "")
    first_username = post_obj.get("first_username", "")

    random_winners = post_obj.get("random_winners", []) or []

    lines = []
    lines.append("🏆 GIVEAWAY WINNERS")
    lines.append("ANNOUNCEMENT 🏆")
    lines.append("")
    if title:
        lines.append(f"⚡ {title} ⚡")
        lines.append("")
    lines.append("🎁 PRIZE:")
    lines.append(prize if prize else "Prize")
    lines.append(f"Winner Count: {winner_count}")
    lines.append(f"Prize delivery: {delivered_count}/{winner_count} ✅" if delivered_count > 0 else f"Prize delivery: {delivered_count}/{winner_count}")
    lines.append("")
    lines.append("🥇 ⭐ FIRST JOIN")
    lines.append("CHAMPION ⭐")
    if first_username:
        lines.append(f"👑 {first_username}")
    else:
        lines.append("👑 User")
    lines.append(f"🆔 {first_uid}")
    lines.append("🎯 Secured instantly")
    lines.append("by joining first")
    lines.append("")
    lines.append("👑 OTHER WINNERS")
    lines.append("(RANDOMLY SELECTED)")

    i = 1
    for w in random_winners:
        uid = w.get("uid", "")
        uname = w.get("username", "")
        mark = " ✅ Delivered" if uid in delivered else ""
        if uname:
            lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}{mark}")
        else:
            lines.append(f"{i}️⃣ 👤 User ID: {uid}{mark}")
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
# STATE MACHINE (ADMIN SETUP)
# =========================================================
admin_state = None

def reset_current_giveaway_only():
    """Reset current giveaway run fields but keep verify/old_winners/settings."""
    keep_verify = data.get("verify_targets", []) or []
    keep_old_winners = data.get("old_winners", {}) or {}
    keep_auto = bool(data.get("auto_winner_post", False))
    keep_block_old = bool(data.get("block_old_winner", False))

    keep_perma = data.get("permanent_block", {}) or {}
    keep_winners_posts = data.get("winners_posts", {}) or {}
    keep_last_winners = data.get("last_winners_message_id")

    data.clear()
    data.update(fresh_data())

    data["verify_targets"] = keep_verify
    data["old_winners"] = keep_old_winners
    data["auto_winner_post"] = keep_auto
    data["block_old_winner"] = keep_block_old

    data["permanent_block"] = keep_perma
    data["winners_posts"] = keep_winners_posts
    data["last_winners_message_id"] = keep_last_winners

    save_data()


# =========================================================
# JOBS
# =========================================================
def stop_job(job):
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:
            pass


def stop_live_countdown():
    global countdown_job
    stop_job(countdown_job)
    countdown_job = None


def stop_closed_anim():
    global closed_anim_job
    stop_job(closed_anim_job)
    closed_anim_job = None


def stop_draw_jobs():
    global draw_job, draw_finalize_job
    stop_job(draw_job)
    stop_job(draw_finalize_job)
    draw_job = None
    draw_finalize_job = None


def stop_reset_jobs():
    global reset_job, reset_finalize_job
    stop_job(reset_job)
    stop_job(reset_finalize_job)
    reset_job = None
    reset_finalize_job = None


def start_live_countdown(job_queue):
    global countdown_job
    stop_live_countdown()
    countdown_job = job_queue.run_repeating(live_tick, interval=LIVE_TICK_SEC, first=0, name="live_countdown")


def start_closed_spinner(job_queue):
    global closed_anim_job
    stop_closed_anim()
    closed_anim_job = job_queue.run_repeating(closed_tick, interval=LIVE_TICK_SEC, first=0, context={"tick": 0}, name="closed_spin")


def live_tick(context: CallbackContext):
    with lock:
        if not data.get("active"):
            stop_live_countdown()
            return

        st = data.get("start_time")
        if st is None:
            data["start_time"] = now_ts()
            save_data()
            st = data["start_time"]

        start = datetime.utcfromtimestamp(float(st))
        duration = int(data.get("duration_seconds", 1) or 1)
        elapsed = int((datetime.utcnow() - start).total_seconds())
        remaining = duration - elapsed

        live_mid = data.get("live_message_id")

        if remaining <= 0:
            # close
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
                spin = "" if data.get("auto_winner_post") else SPINNER[0]
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_text(spin))
                data["closed_message_id"] = m.message_id
                save_data()
            except Exception:
                pass

            stop_live_countdown()

            # closed spinner only when auto winner OFF
            if not data.get("auto_winner_post"):
                start_closed_spinner(context.job_queue)

            # auto draw if ON
            if data.get("auto_winner_post"):
                # start draw in channel (3 mins was requested earlier, but final you set 40s draw system)
                # We'll do draw animation in channel, then auto post winners.
                start_channel_draw(context)

            # notify admin
            try:
                context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "⏰ Giveaway Closed Automatically!\n\n"
                        f"Giveaway: {data.get('title','')}\n"
                        f"Total Participants: {participants_count()}\n\n"
                        "Use /draw (if auto winner is OFF)."
                    )
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
                reply_markup=join_markup(),
                disable_web_page_preview=True,
            )
        except Exception:
            pass


def closed_tick(context: CallbackContext):
    # spinner rotates
    with lock:
        mid = data.get("closed_message_id")
        if not mid:
            stop_closed_anim()
            return
        if data.get("auto_winner_post"):
            # should not spin when auto ON
            stop_closed_anim()
            return
        tick = int((context.job.context or {}).get("tick", 0)) + 1
        context.job.context = {"tick": tick}
        spin = SPINNER[(tick - 1) % len(SPINNER)]

    try:
        context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=mid,
            text=build_closed_text(spin),
            disable_web_page_preview=True,
        )
    except Exception:
        pass


# =========================================================
# DRAW SYSTEM
# =========================================================
def pick_winners():
    """
    Select winners from current participants.
    first_winner_id is always included.
    """
    with lock:
        participants = data.get("participants", {}) or {}
        if not participants:
            return None, "No participants to draw winners from."

        winner_count = int(data.get("winner_count", 1) or 1)
        winner_count = max(1, winner_count)

        # ensure first winner exists
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
        need = max(0, winner_count - 1)
        need = min(need, len(pool))
        selected = random.sample(pool, need) if need > 0 else []

        random_list = []
        winners_map = {}
        winners_map[str(first_uid)] = {"username": first_uname}

        for uid in selected:
            info = participants.get(uid, {}) or {}
            winners_map[str(uid)] = {"username": info.get("username", "")}
            random_list.append({"uid": str(uid), "username": info.get("username", "")})

        post_obj = {
            "title": data.get("title", ""),
            "prize": data.get("prize", ""),
            "winner_count": winner_count,
            "first_uid": str(first_uid),
            "first_username": first_uname,
            "random_winners": random_list,
            "winners_map": winners_map,
            "created_ts": now_ts(),
            "claim_expires_ts": now_ts() + CLAIM_WINDOW_SEC,
            "delivered": {},
        }
        return post_obj, None


def start_admin_draw(context: CallbackContext, chat_id: int):
    """
    Admin /draw -> show 40s progress in admin chat, then preview winners with approve button.
    (Auto winner OFF path)
    """
    stop_draw_jobs()

    msg = context.bot.send_message(
        chat_id=chat_id,
        text=build_draw_text(0, SPINNER[0]),
        disable_web_page_preview=True,
    )

    ctx = {
        "chat_id": chat_id,
        "msg_id": msg.message_id,
        "start_ts": now_ts(),
        "tick": 0,
        "mode": "admin",
    }

    def tick_fn(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        tick = int(jd.get("tick", 0)) + 1
        jd["tick"] = tick

        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(DRAW_TOTAL_SEC)) * 100)))
        spin = SPINNER[(tick - 1) % len(SPINNER)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["msg_id"],
                text=build_draw_text(percent, spin),
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    global draw_job, draw_finalize_job
    draw_job = context.job_queue.run_repeating(tick_fn, interval=DRAW_TICK_SEC, first=0, context=ctx, name="admin_draw_tick")
    draw_finalize_job = context.job_queue.run_once(draw_finalize_admin, when=DRAW_TOTAL_SEC, context=ctx, name="admin_draw_finalize")


def draw_finalize_admin(context: CallbackContext):
    stop_draw_jobs()
    jd = context.job.context
    chat_id = jd["chat_id"]
    msg_id = jd["msg_id"]

    post_obj, err = pick_winners()
    if err:
        try:
            context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=err)
        except Exception:
            pass
        return

    # store as pending in memory (admin will approve)
    with lock:
        data["pending_post_obj"] = post_obj
        save_data()

    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data="winners_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="winners_reject"),
        ]]
    )

    try:
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=build_winners_post_text(post_obj),
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        context.bot.send_message(chat_id=chat_id, text=build_winners_post_text(post_obj), reply_markup=kb)


def start_channel_draw(context: CallbackContext):
    """
    Auto winner ON -> show draw progress in channel, then auto post winners.
    """
    stop_draw_jobs()

    # delete closed spinner msg if exists (keep it or not? user wanted no spinner when auto ON)
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

    msg = context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=build_draw_text(0, SPINNER[0]),
        disable_web_page_preview=True,
    )

    ctx = {
        "chat_id": CHANNEL_ID,
        "msg_id": msg.message_id,
        "start_ts": now_ts(),
        "tick": 0,
        "mode": "channel",
    }

    def tick_fn(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        tick = int(jd.get("tick", 0)) + 1
        jd["tick"] = tick

        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(DRAW_TOTAL_SEC)) * 100)))
        spin = SPINNER[(tick - 1) % len(SPINNER)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["msg_id"],
                text=build_draw_text(percent, spin),
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    global draw_job, draw_finalize_job
    draw_job = context.job_queue.run_repeating(tick_fn, interval=DRAW_TICK_SEC, first=0, context=ctx, name="channel_draw_tick")
    draw_finalize_job = context.job_queue.run_once(draw_finalize_channel, when=DRAW_TOTAL_SEC, context=ctx, name="channel_draw_finalize")


def draw_finalize_channel(context: CallbackContext):
    stop_draw_jobs()
    jd = context.job.context
    draw_msg_id = jd["msg_id"]

    post_obj, err = pick_winners()
    if err:
        # replace draw message with error
        try:
            context.bot.edit_message_text(chat_id=CHANNEL_ID, message_id=draw_msg_id, text=err)
        except Exception:
            pass
        return

    # delete draw progress msg
    try:
        context.bot.delete_message(chat_id=CHANNEL_ID, message_id=draw_msg_id)
    except Exception:
        pass

    # post winners with claim button
    winners_text = build_winners_post_text(post_obj)

    try:
        m = context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=winners_text,
            reply_markup=claim_markup(),
            disable_web_page_preview=True,
        )
        winners_mid = m.message_id
    except Exception:
        return

    # store this winners post
    with lock:
        post_obj["claim_expires_ts"] = now_ts() + CLAIM_WINDOW_SEC
        data["winners_posts"][str(winners_mid)] = post_obj
        data["last_winners_message_id"] = str(winners_mid)
        save_data()

    # giveaway cycle ends (keep winners posts history)
    with lock:
        data["closed"] = True
        data["active"] = False
        save_data()


# =========================================================
# RESET SYSTEM (40s progress, then full wipe)
# =========================================================
def start_reset_progress(context: CallbackContext, chat_id: int, msg_id: int):
    stop_reset_jobs()

    ctx = {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "start_ts": now_ts(),
        "tick": 0,
    }

    def tick_fn(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        tick = int(jd.get("tick", 0)) + 1
        jd["tick"] = tick
        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(RESET_TOTAL_SEC)) * 100)))
        spin = SPINNER[(tick - 1) % len(SPINNER)]
        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["msg_id"],
                text=build_reset_text(percent, spin),
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    global reset_job, reset_finalize_job
    reset_job = context.job_queue.run_repeating(tick_fn, interval=RESET_TICK_SEC, first=0, context=ctx, name="reset_tick")
    reset_finalize_job = context.job_queue.run_once(reset_finalize, when=RESET_TOTAL_SEC, context=ctx, name="reset_finalize")


def reset_finalize(context: CallbackContext):
    stop_reset_jobs()

    # stop other jobs too
    stop_live_countdown()
    stop_closed_anim()
    stop_draw_jobs()

    # best effort: delete live/closed message
    with lock:
        live_mid = data.get("live_message_id")
        closed_mid = data.get("closed_message_id")

    for mid in [live_mid, closed_mid]:
        if mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
            except Exception:
                pass

    # FULL RESET (everything)
    with lock:
        data.clear()
        data.update(fresh_data())
        save_data()

    jd = context.job.context
    chat_id = jd["chat_id"]
    msg_id = jd["msg_id"]

    try:
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=(
                f"{BORDER}\n"
                "✅ RESET COMPLETED\n"
                f"{BORDER}\n\n"
                "All systems reset successfully.\n\n"
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
            f"{BORDER}\n"
            "🛡️ ADMIN WELCOME\n"
            f"{BORDER}\n\n"
            "🛡️👑 WELCOME BACK, ADMIN 👑🛡️\n"
            "⚙️ System Status: ONLINE ✅\n"
            "🚀 Giveaway Engine: READY\n"
            "🔐 Security Level: MAXIMUM\n\n"
            "🧭 Open the Admin Control Panel:\n"
            "/panel\n\n"
            f"⚡ POWERED BY: {HOST_NAME}",
            disable_web_page_preview=True,
        )
    else:
        update.message.reply_text(
            f"{BORDER}\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            f"{BORDER}\n\n"
            "Please wait for the giveaway post.",
            disable_web_page_preview=True,
        )


def cmd_panel(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        f"{BORDER}\n"
        "🛠 ADMIN CONTROL PANEL\n"
        f"{BORDER}\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/endgiveaway\n"
        "/draw\n\n"
        "⚙️ AUTO SYSTEM\n"
        "/autowinnerpost\n\n"
        "🔒 OLD WINNER BLOCK\n"
        "/blockoldwinner\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "🎁 PRIZE DELIVERY\n"
        "/prizedelivery\n\n"
        "♻️ RESET\n"
        "/reset",
        disable_web_page_preview=True,
    )


def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    cur = bool(data.get("auto_winner_post", False))
    update.message.reply_text(
        f"{BORDER}\n"
        "⚙️ AUTO WINNER POST\n"
        f"{BORDER}\n\n"
        "Choose an option below:",
        reply_markup=onoff_markup("autowinner", cur),
    )


def cmd_blockoldwinner(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    cur = bool(data.get("block_old_winner", False))
    update.message.reply_text(
        f"{BORDER}\n"
        "🔒 OLD WINNER BLOCK\n"
        f"{BORDER}\n\n"
        "Choose an option below:",
        reply_markup=onoff_markup("oldwinner", cur),
    )


def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        f"{BORDER}\n"
        "✅ ADD VERIFY TARGET\n"
        f"{BORDER}\n\n"
        "Send Chat ID or @username.\n\n"
        "Examples:\n"
        "-1001234567890\n"
        "@PowerPointBreak",
        disable_web_page_preview=True,
    )


def cmd_removeverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    targets = data.get("verify_targets", []) or []
    if not targets:
        update.message.reply_text("No verify targets are set.")
        return

    lines = [BORDER, "🗑 REMOVE VERIFY TARGET", BORDER, ""]
    for i, t in enumerate(targets, start=1):
        lines.append(f"{i}) {t.get('display','')}")
    lines += ["", "Send a number to remove.", "99) Remove ALL targets"]
    admin_state = "remove_verify_pick"
    update.message.reply_text("\n".join(lines), disable_web_page_preview=True)


def cmd_newgiveaway(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return

    stop_live_countdown()
    stop_closed_anim()
    stop_draw_jobs()

    with lock:
        reset_current_giveaway_only()

    admin_state = "title"
    update.message.reply_text(
        f"{BORDER}\n"
        "🆕 NEW GIVEAWAY SETUP\n"
        f"{BORDER}\n\n"
        "STEP 1 — GIVEAWAY TITLE\n"
        "Send Giveaway Title:",
        disable_web_page_preview=True,
    )


def cmd_participants(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    parts = data.get("participants", {}) or {}
    if not parts:
        update.message.reply_text("Participants list is empty.")
        return

    lines = [BORDER, "👥 PARTICIPANTS LIST", BORDER, f"Total: {len(parts)}", ""]
    i = 1
    for uid, info in parts.items():
        uname = (info or {}).get("username", "")
        if uname:
            lines.append(f"{i}. {uname} | {uid}")
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
        f"{BORDER}\n"
        "⚠️ END GIVEAWAY\n"
        f"{BORDER}\n\n"
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
        update.message.reply_text("No participants to draw winners from.")
        return
    if data.get("auto_winner_post"):
        update.message.reply_text("Auto Winner Post is ON. Winners will be posted automatically.")
        return

    start_admin_draw(context, update.effective_chat.id)


def cmd_prizedelivery(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "prizedelivery_input"
    update.message.reply_text(
        f"{BORDER}\n"
        "🎁 PRIZE DELIVERY\n"
        f"{BORDER}\n\n"
        "Send list (one per line):\n"
        "User ID only OR username + id\n\n"
        "Examples:\n"
        "8293728\n"
        "@minexxproo | 8293728",
        disable_web_page_preview=True,
    )


def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        f"{BORDER}\n"
        "♻️ FULL RESET\n"
        f"{BORDER}\n\n"
        "Are you sure you want to reset everything?\n"
        "This will wipe ALL features and data.",
        reply_markup=reset_confirm_markup()
    )


# =========================================================
# ADMIN TEXT HANDLER (SETUP FLOWS)
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

    # add verify
    if admin_state == "add_verify":
        ref = normalize_verify_ref(msg)
        if not ref:
            update.message.reply_text("Invalid input. Send Chat ID like -100.. or @username.")
            return
        with lock:
            targets = data.get("verify_targets", []) or []
            targets.append({"ref": ref, "display": ref})
            data["verify_targets"] = targets
            save_data()
        admin_state = None
        update.message.reply_text(f"✅ Verify target added: {ref}")
        return

    # remove verify
    if admin_state == "remove_verify_pick":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number.")
            return
        n = int(msg)
        with lock:
            targets = data.get("verify_targets", []) or []
            if n == 99:
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

    # giveaway setup flow
    if admin_state == "title":
        with lock:
            data["title"] = msg
            save_data()
        admin_state = "prize"
        update.message.reply_text("✅ Title saved.\nNow send Giveaway Prize (multi-line allowed).")
        return

    if admin_state == "prize":
        with lock:
            data["prize"] = msg
            save_data()
        admin_state = "winners"
        update.message.reply_text("✅ Prize saved.\nNow send Total Winner Count (1 - 1000000).")
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
        update.message.reply_text(
            "✅ Winner count saved.\n\n"
            "Send Giveaway Duration.\n"
            "Examples:\n"
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
        admin_state = "rules"
        update.message.reply_text("✅ Duration saved.\nNow send Giveaway Rules (multi-line).")
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        update.message.reply_text("✅ Rules saved!\nShowing preview…")
        update.message.reply_text(build_preview_text(), reply_markup=preview_markup(), disable_web_page_preview=True)
        return

    # prize delivery input
    if admin_state == "prizedelivery_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return

        with lock:
            last_mid = data.get("last_winners_message_id")
            if not last_mid or last_mid not in (data.get("winners_posts", {}) or {}):
                admin_state = None
                update.message.reply_text("No winners post found to update. (Run a giveaway and post winners first.)")
                return

            post_obj = data["winners_posts"][last_mid]
            delivered = post_obj.get("delivered", {}) or {}

            added = 0
            for uid, uname in entries:
                if uid not in delivered:
                    delivered[uid] = {"username": uname, "ts": now_ts()}
                    added += 1

            post_obj["delivered"] = delivered
            data["winners_posts"][last_mid] = post_obj
            save_data()

        # edit winners post in channel
        try:
            context.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=int(last_mid),
                text=build_winners_post_text(post_obj),
                reply_markup=claim_markup(),
                disable_web_page_preview=True,
            )
        except Exception:
            pass

        admin_state = None
        update.message.reply_text(f"✅ Prize delivery saved! Added: {added}")
        return


# =========================================================
# CALLBACK HANDLER
# =========================================================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

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
                m = context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=build_live_text(duration),
                    reply_markup=join_markup(),
                    disable_web_page_preview=True,
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
            query.edit_message_text("✏️ Edit Mode\nStart again with /newgiveaway")
            return

    # end confirm/cancel
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

        # closed post
        try:
            spin = "" if data.get("auto_winner_post") else SPINNER[0]
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_text(spin), disable_web_page_preview=True)
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
        except Exception:
            pass

        stop_live_countdown()

        # spinner only when auto OFF
        if not data.get("auto_winner_post"):
            start_closed_spinner(context.job_queue)
        else:
            start_channel_draw(context)

        try:
            query.edit_message_text("✅ Giveaway closed successfully.")
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
        # start 40s reset progress on this message
        start_reset_progress(context, query.message.chat_id, query.message.message_id)
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

    # auto winner on/off
    if qd in ("autowinner_on", "autowinner_off"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        with lock:
            data["auto_winner_post"] = (qd == "autowinner_on")
            save_data()
        try:
            query.answer()
        except Exception:
            pass
        try:
            query.edit_message_text(
                f"{BORDER}\n"
                "⚙️ AUTO WINNER POST\n"
                f"{BORDER}\n\n"
                f"Status: {'ON ✅' if data['auto_winner_post'] else 'OFF ✅'}"
            )
        except Exception:
            pass
        return

    # old winner block on/off
    if qd in ("oldwinner_on", "oldwinner_off"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        with lock:
            data["block_old_winner"] = (qd == "oldwinner_on")
            save_data()

        try:
            query.answer()
        except Exception:
            pass

        if data["block_old_winner"]:
            admin_state = "oldwinner_list_input"
            try:
                query.edit_message_text(
                    f"{BORDER}\n"
                    "🔒 OLD WINNER BLOCK\n"
                    f"{BORDER}\n\n"
                    "Status: ON ✅\n\n"
                    "Now send old winners list (multi-line):\n"
                    "@username | user_id\n"
                    "or user_id only"
                )
            except Exception:
                pass
        else:
            admin_state = None
            try:
                query.edit_message_text(
                    f"{BORDER}\n"
                    "🔒 OLD WINNER BLOCK\n"
                    f"{BORDER}\n\n"
                    "Status: OFF ✅"
                )
            except Exception:
                pass
        return

    # old winner list input (admin text)
    if admin_state == "oldwinner_list_input" and uid == str(ADMIN_ID):
        # handled in admin_text_handler? We'll catch via message handler.
        pass

    # Join giveaway
    if qd == "join_giveaway":
        if not data.get("active"):
            try:
                query.answer("This giveaway is not active right now.", show_alert=True)
            except Exception:
                pass
            return

        # permanent block check
        if uid in (data.get("permanent_block", {}) or {}):
            try:
                query.answer("⛔ You are permanently blocked.", show_alert=True)
            except Exception:
                pass
            return

        # verify required (JOIN)
        if not verify_user(context.bot, int(uid)):
            try:
                query.answer(pop_verify_required(), show_alert=True)
            except Exception:
                pass
            return

        # old winner block check (if ON)
        if data.get("block_old_winner"):
            if uid in (data.get("old_winners", {}) or {}):
                try:
                    query.answer(pop_old_winner_blocked(), show_alert=True)
                except Exception:
                    pass
                return

        # already set first winner?
        with lock:
            first_uid = data.get("first_winner_id")

        # if user is first winner and taps again -> show first winner popup (NOT blocked)
        if first_uid and uid == str(first_uid):
            uname = user_tag(query.from_user.username or "") or data.get("first_winner_username", "") or "@user"
            try:
                query.answer(pop_first_winner(uname, uid), show_alert=True)
            except Exception:
                pass
            return

        # already joined?
        if uid in (data.get("participants", {}) or {}):
            try:
                query.answer(pop_already_joined(), show_alert=True)
            except Exception:
                pass
            return

        # add participant
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

        # update live post immediately (best effort)
        try:
            live_mid = data.get("live_message_id")
            st = data.get("start_time")
            if live_mid and st:
                start = datetime.utcfromtimestamp(float(st))
                duration = int(data.get("duration_seconds", 1) or 1)
                elapsed = int((datetime.utcnow() - start).total_seconds())
                remaining = max(0, duration - elapsed)
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=live_mid,
                    text=build_live_text(remaining),
                    reply_markup=join_markup(),
                    disable_web_page_preview=True,
                )
        except Exception:
            pass

        # show popup
        with lock:
            if data.get("first_winner_id") == uid:
                try:
                    query.answer(pop_first_winner(uname or "@user", uid), show_alert=True)
                except Exception:
                    pass
            else:
                try:
                    query.answer(pop_join_success(uname or "@user", uid), show_alert=True)
                except Exception:
                    pass
        return

    # Winners approve/reject (admin draw path)
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
            post_obj = data.get("pending_post_obj")
        if not post_obj:
            try:
                query.edit_message_text("No pending winners found.")
            except Exception:
                pass
            return

        # delete closed message if exists
        stop_closed_anim()
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

        # post winners
        text = build_winners_post_text(post_obj)
        try:
            m = context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                reply_markup=claim_markup(),
                disable_web_page_preview=True,
            )
            winners_mid = str(m.message_id)
        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post winners: {e}")
            except Exception:
                pass
            return

        with lock:
            data["winners_posts"][winners_mid] = post_obj
            data["last_winners_message_id"] = winners_mid
            data["pending_post_obj"] = None
            save_data()

        try:
            query.edit_message_text("✅ Approved! Winners list posted to channel.")
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
            data["pending_post_obj"] = None
            save_data()
        try:
            query.edit_message_text("❌ Rejected! Winners will NOT be posted.")
        except Exception:
            pass
        return

    # Claim prize (supports MANY winners posts)
    if qd == "claim_prize":
        # verify required (CLAIM)
        if not verify_user(context.bot, int(uid)):
            try:
                query.answer(pop_verify_required(), show_alert=True)
            except Exception:
                pass
            return

        # determine which winners post was clicked
        mid = None
        try:
            mid = str(query.message.message_id)
        except Exception:
            mid = None

        posts = data.get("winners_posts", {}) or {}
        if not mid or mid not in posts:
            # fallback: try last
            mid = data.get("last_winners_message_id")
            if not mid or mid not in posts:
                try:
                    query.answer("Winners data not found for this post.", show_alert=True)
                except Exception:
                    pass
                return

        post_obj = posts[mid]
        winners_map = post_obj.get("winners_map", {}) or {}
        delivered = post_obj.get("delivered", {}) or {}

        # already delivered
        if uid in delivered:
            try:
                query.answer(pop_prize_delivered(), show_alert=True)
            except Exception:
                pass
            return

        # not winner
        if uid not in winners_map:
            try:
                query.answer(pop_not_winner(), show_alert=True)
            except Exception:
                pass
            return

        # expired?
        exp = float(post_obj.get("claim_expires_ts", 0) or 0)
        if exp and now_ts() > exp:
            try:
                query.answer(pop_prize_expired(), show_alert=True)
            except Exception:
                pass
            return

        uname = winners_map.get(uid, {}).get("username", "") or "@user"
        try:
            query.answer(pop_claim_winner(uname, uid), show_alert=True)
        except Exception:
            pass
        return

    # default
    try:
        query.answer()
    except Exception:
        pass


# =========================================================
# OLD WINNER LIST INPUT VIA ADMIN TEXT (WHEN ON)
# =========================================================
def old_winner_text_router(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    if admin_state != "oldwinner_list_input":
        return

    msg = (update.message.text or "").strip()
    if not msg:
        return

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

    admin_state = None
    update.message.reply_text(f"✅ Old winner block list saved! Added: {len(data['old_winners']) - before}")


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

    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))
    dp.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))

    dp.add_handler(CommandHandler("prizedelivery", cmd_prizedelivery))
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # text handlers
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, old_winner_text_router))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))

    # callbacks
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # Resume after restart
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    if data.get("closed") and data.get("closed_message_id") and not data.get("auto_winner_post"):
        start_closed_spinner(updater.job_queue)

    print("Bot is running (python-telegram-bot v13, non-async) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
