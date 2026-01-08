import os
import json
import random
import asyncio
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, BadRequest, Forbidden
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

HOST_NAME = os.getenv("HOST_NAME", "POWER POINT BREAK").strip() or "POWER POINT BREAK"
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@PowerPointBreak").strip() or "@PowerPointBreak"
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/PowerPointBreak").strip() or "https://t.me/PowerPointBreak"
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@MinexxProo").strip() or "@MinexxProo"

DATA_FILE = os.getenv("DATA_FILE", "giveaway_data.json").strip() or "giveaway_data.json"

BD_TZ = timezone(timedelta(hours=6))

# =========================
# SPEED / UI
# =========================
LIVE_TICK_SEC = 5           # live giveaway post update
DRAW_TICK_SEC = 5           # draw progress update
RESET_TICK_SEC = 5          # reset progress update

DRAW_SECONDS_AUTO_ON = 120  # 2 min
DRAW_SECONDS_AUTO_OFF = 40  # 40 sec
RESET_SECONDS = 40          # reset 40 sec

SPINNER = ["🔄", "🔃"]

# =========================
# STATE
# =========================
data_lock = asyncio.Lock()
admin_state = {}  # per-admin chat flow

# active jobs
live_job = None
draw_job = None
reset_job = None

# =========================
# STORAGE
# =========================
def fresh_default_data():
    return {
        "active": False,
        "closed": False,

        "title": "",
        "prize": "",
        "winner_count": 0,
        "duration_seconds": 0,
        "rules": "",

        "start_ts": None,            # utc timestamp
        "live_message_id": None,
        "closed_message_id": None,
        "winners_message_id": None,
        "draw_message_id": None,     # channel draw progress message id

        "participants": {},          # uid -> {"username": "@x", "name": ""}
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        "verify_targets": [],        # [{"ref":"-100.. or @..", "display": "..."}] max 10

        "permanent_block": {},       # uid -> {"username": "@x"}
        "old_winners": {},           # uid -> {"username": "@x"} ALWAYS enforced by /blockoldwinner

        "auto_winner_post": False,   # ON/OFF

        "winners": {},               # current giveaway winners: uid -> {"username":"@x","prize": "...", "date": "YYYY-MM-DD"}
        "winner_history": [],        # list of {"date":"YYYY-MM-DD","title":"...","prize":"...","winners":[...]}
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

async def save_data():
    async with data_lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(G, f, ensure_ascii=False, indent=2)

G = load_data()

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

def participants_count() -> int:
    return len(G.get("participants", {}) or {})

def now_bd_str() -> str:
    return datetime.now(BD_TZ).strftime("%d/%m/%Y %I:%M:%S %p")

def format_hms(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d} : {m:02d} : {s:02d}"

def build_progress_bar(percent: int, blocks: int = 9) -> str:
    percent = max(0, min(100, percent))
    filled = int(round(blocks * percent / 100))
    empty = blocks - filled
    return "▰" * filled + "▱" * empty

def parse_duration(text: str) -> int:
    t = (text or "").strip().lower()
    if not t:
        return 0
    parts = t.split()
    if len(parts) == 1 and parts[0].isdigit():
        return int(parts[0])
    if not parts[0].isdigit():
        return 0
    n = int(parts[0])
    unit = " ".join(parts[1:])
    if "sec" in unit:
        return n
    if "min" in unit:
        return n * 60
    if "hour" in unit or "hr" in unit:
        return n * 3600
    return n

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
        slug = raw.split("t.me/", 1)[1].split("?", 1)[0].split("/", 1)[0]
        if slug and not slug.startswith("+"):
            return user_tag(slug)
    return ""

def parse_user_lines(text: str):
    """
    Accept:
    123456789
    @name | 123456789
    name | 123456789
    """
    out = []
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for line in lines:
        if "|" in line:
            left, right = line.split("|", 1)
            uid = right.strip().replace(" ", "")
            if uid.isdigit():
                uname = user_tag(left.strip().lstrip("@"))
                out.append((uid, uname))
        else:
            uid = line.strip().replace(" ", "")
            if uid.isdigit():
                out.append((uid, ""))
    return out

# =========================
# UI TEXTS (YOUR FINAL)
# =========================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n"
        "Join all required channels/groups first ✅\n"
        "Then tap JOIN GIVEAWAY again."
    )

def popup_old_winner_blocked() -> str:
    return (
        "⛔ You already won before.\n"
        "Repeat winners are blocked for fairness.\n"
        "Please wait for the next giveaway 🙏"
    )

def popup_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n"
        f"Contact Admin: {ADMIN_CONTACT}"
    )

def popup_already_joined() -> str:
    return (
        "🚫 ENTRY UNSUCCESSFUL\n"
        "You’ve already joined this giveaway.\n"
        "Only one entry is allowed."
    )

def popup_first_winner(username: str, uid: str) -> str:
    # changed as you requested (more clean)
    return (
        "✨ CONGRATULATIONS ✨\n"
        "You joined FIRST and secured the 🥇 1st Winner spot!\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n"
        "📸 Screenshot & post in the group to confirm."
    )

def popup_join_success(username: str, uid: str) -> str:
    return (
        "✅ JOINED SUCCESSFULLY\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n"
        f"— {HOST_NAME}"
    )

def popup_claim_winner(username: str, uid: str) -> str:
    return (
        "🏆 YOU ARE A WINNER!\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n"
        f"Contact Admin: {ADMIN_CONTACT}"
    )

def popup_claim_not_winner() -> str:
    # your final text (no border)
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry🥺! Your User ID is not in the winners list.\n"
        "Please wait for the next giveaway❤️‍🩹"
    )

# =========================
# MARKUPS
# =========================
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

def winners_approve_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data="winners_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="winners_reject"),
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
            InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
        ]]
    )

# =========================
# GIVEAWAY POSTS
# =========================
def format_rules_text() -> str:
    rules = (G.get("rules") or "").strip()
    if not rules:
        return "✅ Must join official channel\n❌ One account per user\n🚫 No fake / duplicate accounts"
    lines = [x.strip() for x in rules.splitlines() if x.strip()]
    # keep exactly what admin sends, but bullet-like
    return "\n".join(lines)

def build_preview_text() -> str:
    # YOUR TEMPLATE (no extra emoji in title)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{G.get('title','')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL✨\n"
        f"{G.get('prize','')}\n\n"
        f"👥 TOTAL PARTICIPANTS: 0\n"
        f"🏅 TOTAL WINNERS: {int(G.get('winner_count',0) or 0)}\n"
        "🎯 WINNER SELECTION: 100% Random & Fair\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(int(G.get('duration_seconds',0) or 0))}\n\n"
        f"📊 LIVE PROGRESS  {build_progress_bar(0)} 0%\n\n"
        "📜 RULES....\n"
        f"{format_rules_text()}\n\n"
        "📢 HOSTED BY⚡️ POWER POINT BREAK\n\n"
        "👇 READY TO WIN?\n"
        "👇✨ TAP THE BUTTON BELOW & JOIN NOW 👇"
    )

def build_live_text(remaining: int) -> str:
    dur = int(G.get("duration_seconds", 1) or 1)
    elapsed = max(0, min(dur, dur - remaining))
    percent = int(round((elapsed / float(dur)) * 100))
    bar = build_progress_bar(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{G.get('title','')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL✨\n"
        f"{G.get('prize','')}\n\n"
        f"👥 TOTAL PARTICIPANTS: {participants_count()}\n"
        f"🏅 TOTAL WINNERS: {int(G.get('winner_count',0) or 0)}\n"
        "🎯 WINNER SELECTION: 100% Random & Fair\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(remaining)}\n\n"
        f"📊 LIVE PROGRESS  {bar} {percent}%\n\n"
        "📜 RULES....\n"
        f"{format_rules_text()}\n\n"
        "📢 HOSTED BY⚡️ POWER POINT BREAK\n\n"
        "👇 READY TO WIN?\n"
        "👇✨ TAP THE BUTTON BELOW & JOIN NOW 👇"
    )

def build_closed_text_unique() -> str:
    # more aggressive, but borders safe
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⛔ Time is OVER.\n"
        "🔒 Entries are LOCKED.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {int(G.get('winner_count',0) or 0)}\n\n"
        "⚠️ Winner selection will start now.\n"
        f"— {HOST_NAME} ⚡"
    )

def build_winners_announcement(first_uid: str, first_uname: str, random_list: list) -> str:
    # your provided winners announcement (kept)
    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆✨ GIVEAWAY WINNERS ANNOUNCEMENT ✨🏆")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("🎉 The wait is over!")
    lines.append("Here are the official winners of today’s giveaway 👇")
    lines.append("")
    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    lines.append(f"👑 Username: {first_uname or '@FirstWinner'}")
    lines.append(f"🆔 User ID: {first_uid}")
    lines.append("⚡ Secured instantly by joining first")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("👑 OTHER WINNERS (RANDOMLY SELECTED)")
    lines.append("")
    i = 1
    for uid, uname in random_list:
        lines.append(f"{i}️⃣ 👤 {uname or 'User'}  | 🆔 {uid}")
        i += 1
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("✅ This giveaway was completed using a")
    lines.append("100% fair & transparent random system.")
    lines.append("🔐 User ID based selection only.")
    lines.append("")
    lines.append(f"📢 Hosted By: ⚡ {HOST_NAME}")
    lines.append("👇 Tap the button below to claim your prize 👇")
    return "\n".join(lines)

# =========================
# VERIFY CHECK
# =========================
async def verify_user_join(bot, user_id: int) -> bool:
    targets = G.get("verify_targets", []) or []
    if not targets:
        return True
    for t in targets:
        ref = (t or {}).get("ref", "")
        if not ref:
            return False
        try:
            member = await bot.get_chat_member(chat_id=ref, user_id=user_id)
            status = getattr(member, "status", None)
            if status not in ("member", "administrator", "creator"):
                return False
        except Exception:
            return False
    return True

# =========================
# SAFE EDIT/SEND (ANTI-FREEZE)
# =========================
async def safe_edit(bot, chat_id: int, message_id: int, text: str, reply_markup=None):
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    except RetryAfter as e:
        await asyncio.sleep(float(getattr(e, "retry_after", 1.0)))
    except (TimedOut, BadRequest, Forbidden):
        pass
    except Exception:
        pass

async def safe_send(bot, chat_id: int, text: str, reply_markup=None):
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    except RetryAfter as e:
        await asyncio.sleep(float(getattr(e, "retry_after", 1.0)))
    except Exception:
        return None

# =========================
# JOBS: LIVE COUNTDOWN
# =========================
async def stop_live_job():
    global live_job
    if live_job:
        live_job.schedule_removal()
    live_job = None

async def live_tick(context: ContextTypes.DEFAULT_TYPE):
    async with data_lock:
        if not G.get("active"):
            return

        if not G.get("start_ts"):
            G["start_ts"] = datetime.utcnow().timestamp()

        start_ts = float(G["start_ts"])
        dur = int(G.get("duration_seconds", 1) or 1)
        elapsed = int(datetime.utcnow().timestamp() - start_ts)
        remaining = dur - elapsed

        live_mid = G.get("live_message_id")

    if not live_mid:
        return

    if remaining <= 0:
        # CLOSE
        async with data_lock:
            G["active"] = False
            G["closed"] = True
        await save_data()

        # delete live post
        try:
            await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
        except Exception:
            pass

        # post closed text (no dots)
        m = await safe_send(context.bot, CHANNEL_ID, build_closed_text_unique())
        async with data_lock:
            G["closed_message_id"] = m.message_id if m else None
        await save_data()

        # auto winner?
        if bool(G.get("auto_winner_post")):
            await start_draw_to_channel(context, auto_mode=True)

        await stop_live_job()
        return

    # update live post
    await safe_edit(context.bot, CHANNEL_ID, live_mid, build_live_text(remaining), reply_markup=join_markup())

async def start_live_job(app):
    global live_job
    await stop_live_job()
    live_job = app.job_queue.run_repeating(live_tick, interval=LIVE_TICK_SEC, first=0)

# =========================
# DRAW: progress (spinner line) + finalize
# =========================
async def stop_draw_job():
    global draw_job
    if draw_job:
        draw_job.schedule_removal()
    draw_job = None

def build_draw_text(percent: int, spin: str) -> str:
    bar = build_progress_bar(percent, blocks=10)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 Selecting winners... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        f"{spin} Winner selection is in progress.\n\n"
        "✅ This draw is 100% fair & random.\n"
        "🔐 User ID based selection only.\n\n"
        "Please wait........"
    )

async def start_draw_to_channel(context: ContextTypes.DEFAULT_TYPE, auto_mode: bool):
    """
    auto_mode=True => 2 minutes
    auto_mode=False => 40 seconds
    """
    async with data_lock:
        # prevent multi draw
        if G.get("_drawing"):
            return
        G["_drawing"] = True

    await save_data()
    await stop_draw_job()

    seconds = DRAW_SECONDS_AUTO_ON if auto_mode else DRAW_SECONDS_AUTO_OFF

    # send draw message to CHANNEL
    msg = await safe_send(context.bot, CHANNEL_ID, build_draw_text(0, SPINNER[0]))
    if not msg:
        async with data_lock:
            G["_drawing"] = False
        await save_data()
        return

    # try pin
    try:
        await context.bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=msg.message_id, disable_notification=True)
    except Exception:
        pass

    async with data_lock:
        G["draw_message_id"] = msg.message_id
    await save_data()

    start_ts = datetime.utcnow().timestamp()

    async def draw_tick(ctx: ContextTypes.DEFAULT_TYPE):
        # compute percent by time
        elapsed = int(datetime.utcnow().timestamp() - start_ts)
        percent = int(round(min(100, (elapsed / float(seconds)) * 100)))
        tickn = int(elapsed // DRAW_TICK_SEC)
        spin = SPINNER[tickn % len(SPINNER)]
        await safe_edit(ctx.bot, CHANNEL_ID, msg.message_id, build_draw_text(percent, spin))

        if elapsed >= seconds:
            await finalize_draw(ctx)

    global draw_job
    draw_job = context.job_queue.run_repeating(lambda c: asyncio.create_task(draw_tick(c)), interval=DRAW_TICK_SEC, first=0)

async def finalize_draw(context: ContextTypes.DEFAULT_TYPE):
    await stop_draw_job()

    async with data_lock:
        parts = G.get("participants", {}) or {}
        winner_count = int(G.get("winner_count", 1) or 1)
        draw_mid = G.get("draw_message_id")
    if not parts:
        if draw_mid:
            await safe_edit(context.bot, CHANNEL_ID, draw_mid, "No participants to draw winners from.")
        async with data_lock:
            G["_drawing"] = False
        await save_data()
        return

    winner_count = max(1, winner_count)
    # ensure first winner exists
    async with data_lock:
        first_uid = G.get("first_winner_id")
        if not first_uid:
            first_uid = next(iter(parts.keys()))
            info = parts.get(first_uid, {}) or {}
            G["first_winner_id"] = first_uid
            G["first_winner_username"] = info.get("username", "")
            G["first_winner_name"] = info.get("name", "")
    await save_data()

    async with data_lock:
        first_uname = G.get("first_winner_username", "") or (parts.get(first_uid, {}) or {}).get("username", "")
        pool = [uid for uid in parts.keys() if uid != first_uid]
    remaining_needed = max(0, winner_count - 1)
    remaining_needed = min(remaining_needed, len(pool))
    selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

    random_list = []
    async with data_lock:
        winners_map = {}
        winners_map[str(first_uid)] = {"username": first_uname or "", "prize": G.get("prize",""), "date": datetime.now(BD_TZ).strftime("%d/%m/%Y")}
        for uid in selected:
            info = parts.get(uid, {}) or {}
            random_list.append((uid, info.get("username", "")))
            winners_map[str(uid)] = {"username": info.get("username",""), "prize": G.get("prize",""), "date": datetime.now(BD_TZ).strftime("%d/%m/%Y")}

        G["winners"] = winners_map

    await save_data()

    # delete draw progress message
    draw_mid = G.get("draw_message_id")
    if draw_mid:
        try:
            await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=draw_mid)
        except Exception:
            pass

    # delete closed message before winners post (clean)
    closed_mid = G.get("closed_message_id")
    if closed_mid:
        try:
            await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
        except Exception:
            pass

    # post winners (with claim button)
    text = build_winners_announcement(str(first_uid), first_uname or "@FirstWinner", random_list)
    m = await safe_send(context.bot, CHANNEL_ID, text, reply_markup=claim_markup())

    # save history + add old_winners automatically
    async with data_lock:
        # winners_message_id
        G["winners_message_id"] = m.message_id if m else None
        G["draw_message_id"] = None

        # auto add to old_winners
        ow = G.get("old_winners", {}) or {}
        for uid, meta in (G.get("winners", {}) or {}).items():
            ow[str(uid)] = {"username": (meta or {}).get("username","")}
        G["old_winners"] = ow

        # history
        hist = G.get("winner_history", []) or []
        hist.insert(0, {
            "date": datetime.now(BD_TZ).strftime("%d/%m/%Y"),
            "title": G.get("title",""),
            "prize": G.get("prize",""),
            "winners": [{"uid": k, "username": (v or {}).get("username","")} for k, v in (G.get("winners") or {}).items()]
        })
        G["winner_history"] = hist[:500]  # cap
        G["auto_winner_post"] = False   # auto off after post
        G["_drawing"] = False

    await save_data()

# =========================
# RESET: confirm -> 40s progress -> ALL wipe
# =========================
async def stop_reset_job():
    global reset_job
    if reset_job:
        reset_job.schedule_removal()
    reset_job = None

def build_reset_text(percent: int, spin: str) -> str:
    bar = build_progress_bar(percent, blocks=10)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ FULL RESET IN PROGRESS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{spin} Resetting... {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        "Please wait........"
    )

async def start_reset_progress(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    await stop_reset_job()
    start_ts = datetime.utcnow().timestamp()

    async def tick(ctx: ContextTypes.DEFAULT_TYPE):
        elapsed = int(datetime.utcnow().timestamp() - start_ts)
        percent = int(round(min(100, (elapsed / float(RESET_SECONDS)) * 100)))
        spin = SPINNER[(elapsed // RESET_TICK_SEC) % len(SPINNER)]
        await safe_edit(ctx.bot, chat_id, message_id, build_reset_text(percent, spin))

        if elapsed >= RESET_SECONDS:
            await do_full_reset(ctx, chat_id, message_id)

    global reset_job
    reset_job = context.job_queue.run_repeating(lambda c: asyncio.create_task(tick(c)), interval=RESET_TICK_SEC, first=0)

async def do_full_reset(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    await stop_live_job()
    await stop_draw_job()
    await stop_reset_job()

    # delete channel messages if exist
    async with data_lock:
        mids = [G.get("live_message_id"), G.get("closed_message_id"), G.get("winners_message_id"), G.get("draw_message_id")]
    for mid in mids:
        if mid:
            try:
                await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
            except Exception:
                pass

    # ALL wipe
    async with data_lock:
        newd = fresh_default_data()
        G.clear()
        G.update(newd)
    await save_data()

    await safe_edit(
        context.bot,
        chat_id,
        message_id,
        "✅ RESET COMPLETED.\nAll data removed.\nUse /newgiveaway to start again."
    )

# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u:
        return
    if is_admin(u.id):
        await update.message.reply_text(
            "🛡️ ADMIN ONLINE ✅\n\n"
            "Commands:\n"
            "/panel\n"
            "/newgiveaway\n"
            "/autowinnerpost\n"
            "/draw\n"
            "/endgiveaway\n"
            "/winnerlist\n"
            "/reset\n\n"
            f"🕒 BD Time: {now_bd_str()}"
        )
    else:
        await update.message.reply_text(
            f"⚡ {HOST_NAME} Giveaway System\n\n"
            "Wait for the giveaway post in our channel:\n"
            f"{CHANNEL_LINK}"
        )

async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "🧩 ADMIN PANEL\n\n"
        "Giveaway:\n"
        "/newgiveaway\n"
        "/draw\n"
        "/endgiveaway\n\n"
        "Verify:\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "Blocks:\n"
        "/blockpermanent\n"
        "/blockoldwinner\n"
        "/blocklist\n\n"
        "Others:\n"
        "/autowinnerpost\n"
        "/winnerlist\n"
        "/reset"
    )

async def cmd_autowinnerpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    status = "ON ✅" if G.get("auto_winner_post") else "OFF ❌"
    await update.message.reply_text(
        f"Auto Winner Post setting: {status}\n\nChoose:",
        reply_markup=autowinner_markup()
    )

async def cmd_addverifylink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    admin_state[update.effective_chat.id] = "add_verify"
    await update.message.reply_text(
        "Send verify target (max 10):\n"
        "-1001234567890\n"
        "@ChannelUsername\n"
        "or t.me/username"
    )

async def cmd_removeverifylink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    targets = G.get("verify_targets", []) or []
    if not targets:
        await update.message.reply_text("No verify targets set.")
        return
    lines = ["Current verify targets:"]
    for i, t in enumerate(targets, start=1):
        lines.append(f"{i}) {t.get('display','')}")
    lines.append("\nSend number to remove (1..n) OR 11 to remove ALL.")
    admin_state[update.effective_chat.id] = "remove_verify"
    await update.message.reply_text("\n".join(lines))

async def cmd_blockpermanent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    admin_state[update.effective_chat.id] = "perma_block"
    await update.message.reply_text(
        "Send permanent block list (multi-line):\n"
        "@user | 123456\n"
        "123456"
    )

async def cmd_blockoldwinner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    admin_state[update.effective_chat.id] = "oldwinner_block"
    await update.message.reply_text(
        "Send old winner block list (multi-line):\n"
        "@user | 123456\n"
        "123456\n\n"
        "Note: This list is ALWAYS enforced."
    )

async def cmd_blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    perma = G.get("permanent_block", {}) or {}
    oldw = G.get("old_winners", {}) or {}
    lines = []
    lines.append("📌 BLOCK LISTS\n")
    lines.append(f"Permanent Block: {len(perma)}")
    for uid, info in perma.items():
        lines.append(f"- {(info or {}).get('username','') or 'User'} | {uid}")
    lines.append("")
    lines.append(f"Old Winner Block: {len(oldw)}")
    for uid, info in oldw.items():
        lines.append(f"- {(info or {}).get('username','') or 'User'} | {uid}")
    await update.message.reply_text("\n".join(lines)[:3900])

async def cmd_winnerlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    hist = G.get("winner_history", []) or []
    if not hist:
        await update.message.reply_text("No winner history yet.")
        return
    lines = ["🏆 WINNER HISTORY (latest first)\n"]
    for item in hist[:20]:
        lines.append(f"📅 {item.get('date','')}")
        lines.append(f"🏷 {item.get('title','')}")
        lines.append("🎁 Prize:")
        lines.append(item.get("prize",""))
        lines.append("👑 Winners:")
        for w in item.get("winners", []):
            lines.append(f"- {w.get('username','') or 'User'} | {w.get('uid','')}")
        lines.append("━━━━━━━━━━━━")
    await update.message.reply_text("\n".join(lines)[:3900])

async def cmd_newgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    # fresh giveaway but keep blocks/verify/history (NOT reset)
    async with data_lock:
        keep_verify = G.get("verify_targets", [])
        keep_perma = G.get("permanent_block", {})
        keep_oldw = G.get("old_winners", {})
        keep_hist = G.get("winner_history", [])

        newd = fresh_default_data()
        newd["verify_targets"] = keep_verify
        newd["permanent_block"] = keep_perma
        newd["old_winners"] = keep_oldw
        newd["winner_history"] = keep_hist

        G.clear()
        G.update(newd)
    await save_data()

    admin_state[update.effective_chat.id] = "title"
    await update.message.reply_text("STEP 1 — Send Giveaway Title (exact, no extra emoji will be added).")

async def cmd_endgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    if not G.get("active"):
        await update.message.reply_text("No active giveaway running.")
        return
    await update.message.reply_text("End giveaway now?", reply_markup=end_confirm_markup())

async def cmd_draw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    if not G.get("closed"):
        await update.message.reply_text("Giveaway not closed yet.")
        return
    # manual draw => auto_mode depends on setting? you wanted: Auto OFF => 40s, Auto ON => 2min
    await start_draw_to_channel(context, auto_mode=bool(G.get("auto_winner_post")))

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("⚠️ FULL RESET will remove ALL data.\nConfirm?", reply_markup=reset_confirm_markup())

# =========================
# ADMIN TEXT FLOW HANDLER
# =========================
async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    state = admin_state.get(chat_id)
    if not state:
        return

    msg = (update.message.text or "").strip()
    if not msg:
        return

    if state == "add_verify":
        ref = normalize_verify_ref(msg)
        if not ref:
            await update.message.reply_text("Invalid. Send -100... OR @username OR t.me/username")
            return
        async with data_lock:
            targets = G.get("verify_targets", []) or []
            if len(targets) >= 10:
                await update.message.reply_text("Max verify targets reached (10).")
                return
            targets.append({"ref": ref, "display": ref})
            G["verify_targets"] = targets
        await save_data()
        admin_state.pop(chat_id, None)
        await update.message.reply_text(f"✅ Verify target added: {ref}\nTotal: {len(G.get('verify_targets',[]))}")
        return

    if state == "remove_verify":
        if not msg.isdigit():
            await update.message.reply_text("Send a number (1..n) or 11 for ALL.")
            return
        n = int(msg)
        async with data_lock:
            targets = G.get("verify_targets", []) or []
            if n == 11:
                G["verify_targets"] = []
            elif 1 <= n <= len(targets):
                targets.pop(n - 1)
                G["verify_targets"] = targets
            else:
                await update.message.reply_text("Invalid number.")
                return
        await save_data()
        admin_state.pop(chat_id, None)
        await update.message.reply_text("✅ Verify targets updated.")
        return

    if state == "perma_block":
        entries = parse_user_lines(msg)
        if not entries:
            await update.message.reply_text("Send valid list.")
            return
        async with data_lock:
            perma = G.get("permanent_block", {}) or {}
            for uid, uname in entries:
                perma[str(uid)] = {"username": uname}
            G["permanent_block"] = perma
        await save_data()
        admin_state.pop(chat_id, None)
        await update.message.reply_text(f"✅ Permanent block saved. Total: {len(G.get('permanent_block',{}))}")
        return

    if state == "oldwinner_block":
        entries = parse_user_lines(msg)
        if not entries:
            await update.message.reply_text("Send valid list.")
            return
        async with data_lock:
            ow = G.get("old_winners", {}) or {}
            for uid, uname in entries:
                ow[str(uid)] = {"username": uname}
            G["old_winners"] = ow
        await save_data()
        admin_state.pop(chat_id, None)
        await update.message.reply_text(f"✅ Old winner block saved. Total: {len(G.get('old_winners',{}))}")
        return

    # giveaway setup steps
    if state == "title":
        async with data_lock:
            G["title"] = msg
        await save_data()
        admin_state[chat_id] = "prize"
        await update.message.reply_text("STEP 2 — Send Prize Text (multi-line allowed).")
        return

    if state == "prize":
        async with data_lock:
            G["prize"] = msg
        await save_data()
        admin_state[chat_id] = "winners"
        await update.message.reply_text("STEP 3 — Send total winners (number).")
        return

    if state == "winners":
        if not msg.isdigit():
            await update.message.reply_text("Send a valid number.")
            return
        async with data_lock:
            G["winner_count"] = max(1, min(1000000, int(msg)))
        await save_data()
        admin_state[chat_id] = "duration"
        await update.message.reply_text("STEP 4 — Send duration (e.g. 30 Second / 10 Minute / 1 Hour).")
        return

    if state == "duration":
        sec = parse_duration(msg)
        if sec <= 0:
            await update.message.reply_text("Invalid duration format.")
            return
        async with data_lock:
            G["duration_seconds"] = sec
        await save_data()
        admin_state[chat_id] = "rules"
        await update.message.reply_text("STEP 5 — Send Rules (multi-line).")
        return

    if state == "rules":
        async with data_lock:
            G["rules"] = msg
        await save_data()
        admin_state.pop(chat_id, None)
        await update.message.reply_text("✅ Rules saved! Preview below:")
        await update.message.reply_text(build_preview_text(), reply_markup=preview_markup())
        return

# =========================
# CALLBACKS (FAST POPUP FIX)
# =========================
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    user = q.from_user
    uid = str(user.id)
    data_key = q.data or ""

    # ALWAYS answer quickly to guarantee popup works
    try:
        await q.answer()
    except Exception:
        pass

    # --- admin toggles ---
    if data_key in ("autopost_on", "autopost_off"):
        if not is_admin(user.id):
            try:
                await q.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        async with data_lock:
            G["auto_winner_post"] = (data_key == "autopost_on")
        await save_data()
        status = "ON ✅" if G["auto_winner_post"] else "OFF ❌"
        await safe_edit(context.bot, q.message.chat_id, q.message.message_id, f"Auto Winner Post: {status}")
        return

    if data_key == "reset_confirm":
        if not is_admin(user.id):
            try:
                await q.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        await safe_edit(context.bot, q.message.chat_id, q.message.message_id, build_reset_text(0, SPINNER[0]))
        await start_reset_progress(context, q.message.chat_id, q.message.message_id)
        return

    if data_key == "reset_cancel":
        if not is_admin(user.id):
            return
        await safe_edit(context.bot, q.message.chat_id, q.message.message_id, "❌ Reset cancelled.")
        return

    if data_key == "preview_approve":
        if not is_admin(user.id):
            try:
                await q.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return

        # post to channel
        async with data_lock:
            dur = int(G.get("duration_seconds", 1) or 1)
            G["participants"] = {}
            G["winners"] = {}
            G["first_winner_id"] = None
            G["first_winner_username"] = ""
            G["first_winner_name"] = ""
            G["closed"] = False
            G["active"] = True
            G["start_ts"] = datetime.utcnow().timestamp()

        await save_data()

        m = await safe_send(context.bot, CHANNEL_ID, build_live_text(dur), reply_markup=join_markup())
        if not m:
            await safe_edit(context.bot, q.message.chat_id, q.message.message_id, "❌ Failed to post in channel. Make bot admin.")
            return

        async with data_lock:
            G["live_message_id"] = m.message_id
            G["closed_message_id"] = None
            G["winners_message_id"] = None
            G["draw_message_id"] = None
        await save_data()

        await start_live_job(context.application)
        await safe_edit(context.bot, q.message.chat_id, q.message.message_id, "✅ Giveaway approved & posted to channel!")
        return

    if data_key == "preview_reject":
        if not is_admin(user.id):
            return
        await safe_edit(context.bot, q.message.chat_id, q.message.message_id, "❌ Giveaway rejected.")
        return

    if data_key == "preview_edit":
        if not is_admin(user.id):
            return
        await safe_edit(context.bot, q.message.chat_id, q.message.message_id, "✏️ Start again with /newgiveaway")
        return

    if data_key == "end_confirm":
        if not is_admin(user.id):
            try:
                await q.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return

        async with data_lock:
            if not G.get("active"):
                await safe_edit(context.bot, q.message.chat_id, q.message.message_id, "No active giveaway.")
                return
            G["active"] = False
            G["closed"] = True
        await save_data()

        # delete live post
        live_mid = G.get("live_message_id")
        if live_mid:
            try:
                await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception:
                pass

        m = await safe_send(context.bot, CHANNEL_ID, build_closed_text_unique())
        async with data_lock:
            G["closed_message_id"] = m.message_id if m else None
        await save_data()

        if bool(G.get("auto_winner_post")):
            await start_draw_to_channel(context, auto_mode=True)

        await stop_live_job()
        await safe_edit(context.bot, q.message.chat_id, q.message.message_id, "✅ Giveaway closed.")
        return

    if data_key == "end_cancel":
        if not is_admin(user.id):
            return
        await safe_edit(context.bot, q.message.chat_id, q.message.message_id, "Cancelled. Giveaway continues.")
        return

    # --- USER BUTTONS (ONLY HERE we enforce blocks) ---
    if data_key == "join_giveaway":
        # if not active
        if not G.get("active"):
            try:
                await q.answer("This giveaway is not active right now.", show_alert=True)
            except Exception:
                pass
            return

        # PERMANENT BLOCK
        if uid in (G.get("permanent_block", {}) or {}):
            try:
                await q.answer(popup_permanent_blocked(), show_alert=True)
            except Exception:
                pass
            return

        # OLD WINNER BLOCK (always enforced)
        if uid in (G.get("old_winners", {}) or {}):
            try:
                await q.answer(popup_old_winner_blocked(), show_alert=True)
            except Exception:
                pass
            return

        # VERIFY CHECK
        ok = await verify_user_join(context.bot, int(uid))
        if not ok:
            try:
                await q.answer(popup_verify_required(), show_alert=True)
            except Exception:
                pass
            return

        # already joined?
        if uid in (G.get("participants", {}) or {}):
            try:
                await q.answer(popup_already_joined(), show_alert=True)
            except Exception:
                pass
            return

        # join success (store)
        uname = user_tag(user.username or "")
        full_name = (user.full_name or "").strip()

        async with data_lock:
            # first join winner
            if not G.get("first_winner_id"):
                G["first_winner_id"] = uid
                G["first_winner_username"] = uname
                G["first_winner_name"] = full_name

            G["participants"][uid] = {"username": uname, "name": full_name}
        await save_data()

        # update live post immediately
        live_mid = G.get("live_message_id")
        start_ts = G.get("start_ts")
        if live_mid and start_ts:
            dur = int(G.get("duration_seconds", 1) or 1)
            elapsed = int(datetime.utcnow().timestamp() - float(start_ts))
            remaining = max(0, dur - elapsed)
            await safe_edit(context.bot, CHANNEL_ID, live_mid, build_live_text(remaining), reply_markup=join_markup())

        # popup first winner or normal
        if str(G.get("first_winner_id")) == uid:
            try:
                await q.answer(popup_first_winner(uname or "@User", uid), show_alert=True)
            except Exception:
                pass
        else:
            try:
                await q.answer(popup_join_success(uname or "@User", uid), show_alert=True)
            except Exception:
                pass
        return

    if data_key == "claim_prize":
        # block checks apply here too
        if uid in (G.get("permanent_block", {}) or {}):
            try:
                await q.answer(popup_permanent_blocked(), show_alert=True)
            except Exception:
                pass
            return
        if uid in (G.get("old_winners", {}) or {}):
            # old winner can still claim if winner; so do not block claim
            pass

        winners = G.get("winners", {}) or {}
        if uid in winners:
            uname = (winners.get(uid, {}) or {}).get("username", "") or user_tag(user.username or "") or "@User"
            try:
                await q.answer(popup_claim_winner(uname, uid), show_alert=True)
            except Exception:
                pass
        else:
            try:
                await q.answer(popup_claim_not_winner(), show_alert=True)
            except Exception:
                pass
        return

# =========================
# MAIN
# =========================
async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing in .env")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))

    app.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    app.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    app.add_handler(CommandHandler("draw", cmd_draw))

    app.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))

    app.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    app.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    app.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    app.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))
    app.add_handler(CommandHandler("blocklist", cmd_blocklist))

    app.add_handler(CommandHandler("winnerlist", cmd_winnerlist))

    app.add_handler(CommandHandler("reset", cmd_reset))

    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_handler))

    # resume live job if active
    if G.get("active"):
        await start_live_job(app)

    print("Bot running (PTB v20) ...")
    await app.run_polling(close_loop=False)

if __name__ == "__main__":
    asyncio.run(main())
