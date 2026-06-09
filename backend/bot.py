"""
Telegram bot: handles commands, sends nudges, answers data queries.
Commands (v1):
  /plan   — plan tomorrow (or today)
  /day [yesterday|YYYY-MM-DD]  — daily time breakdown
  /hour <h>  — detailed app/site breakdown for a given hour (e.g. /hour 14)
  /report — latest weekly report
  /shift <block> <±mins>  — shift a time block
  /block_now <mins>       — start ad-hoc focus block
  /sensitive <app|domain> — mark as never-send-to-LLM
  free text               — store as plan reply during planning flow
"""
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import backend.db as db
from backend.rules import (
    format_plan_prompt,
    format_morning_confirm,
    tomorrow_date,
    today_date,
)
from backend import scheduler

log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# State: track which users are in planning flow
_awaiting_plan: dict[int, str] = {}  # chat_id -> date being planned


# --- Command handlers ---

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    name = update.effective_chat.first_name or "there"
    # Auto-save chat_id to config on first /start
    if not db.get_config("telegram_chat_id"):
        db.set_config("telegram_chat_id", str(chat_id))
        log.info("Auto-saved telegram_chat_id: %s", chat_id)
    await update.message.reply_text(
        f"👋 Hi {name}! FocAssist is connected.\n\n"
        f"Your chat ID: `{chat_id}`\n\n"
        "Commands:\n"
        "/plan — plan tomorrow\n"
        "/day — today's activity  (or /day yesterday)\n"
        "/hour <h> — hour breakdown (e.g. /hour 14)\n"
        "/report — weekly report\n"
        "/block\\_now <mins> — start a focus block\n"
        "/shift <label> <±mins> — move a time block\n"
        "/sensitive <app|domain> — never send to LLM",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*FocAssist commands*\n\n"
        "/plan — plan tomorrow\n"
        "/day — today's activity breakdown\n"
        "/day `yesterday` or `/day 2026-06-09` — past day\n"
        "/hour `<h>` — hour breakdown (e.g. /hour 14)\n"
        "/report — weekly report\n"
        "/block\\_now `<mins>` `[domain ...]` — start a focus block\n"
        "/shift `<label>` `<±mins>` — move a time block\n"
        "/sensitive `<app|domain>` — never send to LLM\n"
        "/help — show this menu",
        parse_mode="Markdown",
    )


async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    tomorrow = tomorrow_date()
    _awaiting_plan[chat_id] = tomorrow
    await update.message.reply_text(format_plan_prompt(tomorrow), parse_mode="Markdown")


async def cmd_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from backend.scoring import format_daily_report, compute_score
    from backend.rules import ist_now
    args = ctx.args or []
    if args:
        arg = args[0].lower()
        if arg == "yesterday":
            target = (ist_now().date() - timedelta(days=1)).isoformat()
        else:
            try:
                date.fromisoformat(args[0])
                target = args[0]
            except ValueError:
                await update.message.reply_text("Usage: /day  or  /day yesterday  or  /day 2026-06-09")
                return
    else:
        target = today_date()

    aggregates = [dict(r) for r in db.get_activity_for_date(target)]
    sessions   = [dict(r) for r in db.get_sessions_for_date(target)]
    timeline   = db.get_timeline_for_date(target)
    coverage   = db.get_coverage(target)

    # Previous day's score for trend arrow
    prev_date  = (date.fromisoformat(target) - timedelta(days=1)).isoformat()
    prev_aggs  = [dict(r) for r in db.get_activity_for_date(prev_date)]
    prev_sess  = [dict(r) for r in db.get_sessions_for_date(prev_date)]
    prev_score = compute_score(prev_aggs, prev_sess)["score"] if (prev_aggs or prev_sess) else None

    deep_target   = float(db.get_config("score_deep_target_min",   "240"))
    streak_target = float(db.get_config("score_streak_target_min", "90"))
    msg = format_daily_report(target, aggregates, sessions, timeline,
                               prev_score, deep_target, streak_target, coverage)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_reprocess(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from backend.rules import ist_now
    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            "Usage: `/reprocess yesterday`  or  `/reprocess 2026-06-09`",
            parse_mode="Markdown",
        )
        return
    arg = args[0].lower()
    if arg == "yesterday":
        target = (ist_now().date() - timedelta(days=1)).isoformat()
    else:
        try:
            date.fromisoformat(args[0])
            target = args[0]
        except ValueError:
            await update.message.reply_text(
                "Usage: `/reprocess yesterday`  or  `/reprocess 2026-06-09`",
                parse_mode="Markdown",
            )
            return
    db.add_reprocess_job(target)
    await update.message.reply_text(
        f"Queued reprocess for `{target}`. The Mac agent will pick it up within ~5 min.\n"
        f"Then run `/day {target}` to see the updated report.",
        parse_mode="Markdown",
    )


_HOUR_RE = re.compile(r"^(\d{1,2})\s*(am|pm)?$", re.IGNORECASE)


def _parse_hour(text: str) -> int | None:
    """Parse '14', '2pm', '9am', '9' → 0-23, or None if invalid."""
    m = _HOUR_RE.match(text.strip())
    if not m:
        return None
    h = int(m.group(1))
    period = (m.group(2) or "").lower()
    if period:
        if not (1 <= h <= 12):
            return None
        if period == "am":
            return 0 if h == 12 else h
        return 12 if h == 12 else h + 12
    return h if 0 <= h <= 23 else None


async def cmd_hour(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from backend.scoring import format_hour_report
    from backend.rules import ist_now
    now = ist_now()
    args = ctx.args or []

    if not args or args[0].lower() == "now":
        hour = now.hour
    else:
        hour = _parse_hour(args[0])
        if hour is None:
            await update.message.reply_text("Give me an hour 0–23 (or like `2pm`).", parse_mode="Markdown")
            return

    if hour > now.hour:
        await update.message.reply_text("That hour hasn't happened yet.")
        return

    today = today_date()
    items     = [dict(r) for r in db.get_hourly_activity(today, hour)]
    timeline  = db.get_timeline_for_date(today)
    sessions  = [dict(r) for r in db.get_sessions_for_date(today)]

    elapsed = (now.minute + now.second / 60.0) if hour == now.hour else None
    msg = format_hour_report(today, hour, items, timeline, sessions, elapsed)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from backend.scoring import format_weekly_report
    from backend.rules import ist_now
    today = ist_now().date()
    days = []
    for offset in range(6, -1, -1):
        d = (today - timedelta(days=offset)).isoformat()
        days.append({
            "date":       d,
            "aggregates": [dict(r) for r in db.get_activity_for_date(d)],
            "sessions":   [dict(r) for r in db.get_sessions_for_date(d)],
        })
    deep_target   = float(db.get_config("score_deep_target_min",   "240"))
    streak_target = float(db.get_config("score_streak_target_min", "90"))
    msg = format_weekly_report(days, deep_target, streak_target)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_shift(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # /shift <label> <+/-mins>   e.g. /shift focus +30
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /shift <block-label> <±minutes>  e.g. /shift focus +30")
        return

    label = args[0].lower()
    try:
        delta = int(args[1].replace("+", ""))
    except ValueError:
        await update.message.reply_text("Minutes must be a number, e.g. +30 or -15")
        return

    today = today_date()
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM time_blocks WHERE date=? AND lower(label)=? ORDER BY start LIMIT 1",
            (today, label),
        ).fetchone()

    if not row:
        await update.message.reply_text(f"No block labeled '{label}' found for today.")
        return

    def shift_time(t: str, mins: int) -> str:
        dt = datetime.strptime(t, "%H:%M") + timedelta(minutes=mins)
        return dt.strftime("%H:%M")

    new_start = shift_time(row["start"], delta)
    new_end = shift_time(row["end"], delta)

    with db.get_db() as conn:
        conn.execute(
            "UPDATE time_blocks SET start=?, end=? WHERE id=?",
            (new_start, new_end, row["id"]),
        )

    # Reschedule nudge jobs for the moved block
    from backend import scheduler
    scheduler.cancel_block_nudges(row["id"])
    scheduler.schedule_block_nudges(today)

    sign = "+" if delta >= 0 else ""
    await update.message.reply_text(
        f"✅ Shifted '{label}' by {sign}{delta} min → {new_start}–{new_end}"
    )


DEFAULT_BLOCK_DOMAINS = [
    "youtube.com", "reddit.com", "twitter.com", "x.com",
    "instagram.com", "primevideo.com",
]


async def cmd_block_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # /block_now <mins> [domain1 domain2 ...]
    # e.g. /block_now 45 youtube.com reddit.com
    #      /block_now 25          ← uses default list
    args = ctx.args or []
    mins = 25
    domains = []

    if args:
        # First arg is duration if it's a number; otherwise treat everything as domains
        if args[0].isdigit():
            mins = int(args[0])
            domains = args[1:]
        else:
            domains = args

    if not domains:
        domains = DEFAULT_BLOCK_DOMAINS

    import json
    from zoneinfo import ZoneInfo
    today = today_date()
    now = datetime.now(ZoneInfo("Asia/Kolkata"))   # store times in IST
    start_str = now.strftime("%H:%M")
    end_dt = now + timedelta(minutes=mins)
    end_str = end_dt.strftime("%H:%M")

    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO time_blocks (date, start, end, label, kind, block_domains)
               VALUES (?, ?, ?, 'ad-hoc focus', 'focus', ?)""",
            (today, start_str, end_str, json.dumps(domains)),
        )

    domain_list = "\n".join(f"  • {d}" for d in domains)
    await update.message.reply_text(
        f"🔒 Focus block: {start_str}–{end_str} ({mins} min)\n\n"
        f"Blocking:\n{domain_list}"
    )


async def cmd_sensitive(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    import json
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /sensitive <app-or-domain>")
        return
    item = args[0]
    current = json.loads(db.get_config("sensitive_apps", "[]"))
    if item not in current:
        current.append(item)
        db.set_config("sensitive_apps", json.dumps(current))
    await update.message.reply_text(f"✅ '{item}' added to the never-send-to-LLM list.")


# --- Free-text handler ---

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = update.message.text or ""

    if chat_id in _awaiting_plan:
        plan_date = _awaiting_plan.pop(chat_id)
        db.save_plan(plan_date, text)

        # Parse into structured time blocks
        from backend.plan_parser import parse_plan, format_blocks_confirmation
        from backend import scheduler
        blocks = parse_plan(text, plan_date)
        if blocks:
            db.save_time_blocks(plan_date, blocks)
            scheduler.schedule_block_nudges(plan_date)

        await update.message.reply_text(
            format_blocks_confirmation(blocks),
            parse_mode="Markdown",
        )
        return

    # Not in a flow — give a hint
    await update.message.reply_text(
        "I didn't understand that. Try:\n"
        "/plan — plan tomorrow\n"
        "/day — today's activity\n"
        "/report — weekly report\n"
        "/block_now <mins> — start a focus block\n"
        "/shift <label> <±mins> — shift a time block"
    )


# --- Bot setup ---

def build_app() -> Application:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set.")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CommandHandler("reprocess", cmd_reprocess))
    app.add_handler(CommandHandler("hour", cmd_hour))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("shift", cmd_shift))
    app.add_handler(CommandHandler("block_now", cmd_block_now))
    app.add_handler(CommandHandler("sensitive", cmd_sensitive))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


async def send_to_owner(bot, text: str, parse_mode: str = "Markdown") -> None:
    """Send a message to the configured owner chat id."""
    chat_id = db.get_config("telegram_chat_id")
    if not chat_id:
        log.warning("telegram_chat_id not set in config — skipping send.")
        return
    await bot.send_message(chat_id=int(chat_id), text=text, parse_mode=parse_mode)
