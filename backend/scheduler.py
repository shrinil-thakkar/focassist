"""
APScheduler jobs: evening planning prompt, morning confirm, weekly report.
"""
import logging
import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def start(send_message_fn) -> AsyncIOScheduler:
    """
    Start the scheduler. `send_message_fn` is an async callable that sends
    a Telegram message to the owner's chat id: send_message_fn(text, parse_mode).
    """
    global _scheduler
    # All nudge times are interpreted in IST (Asia/Kolkata, UTC+5:30)
    _scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

    # Read nudge times from DB config (lazy import to avoid circular)
    from backend import db

    evening_time = db.get_config("nudge_evening", "21:00")
    morning_time = db.get_config("nudge_morning", "08:30")
    evening_h, evening_m = map(int, evening_time.split(":"))
    morning_h, morning_m = map(int, morning_time.split(":"))

    _scheduler.add_job(
        _evening_nudge,
        CronTrigger(hour=evening_h, minute=evening_m),
        id="evening_nudge",
        replace_existing=True,
        args=[send_message_fn],
    )

    _scheduler.add_job(
        _morning_nudge,
        CronTrigger(hour=morning_h, minute=morning_m),
        id="morning_nudge",
        replace_existing=True,
        args=[send_message_fn],
    )

    _scheduler.add_job(
        _weekly_report,
        CronTrigger(day_of_week="sun", hour=18, minute=0, timezone="Asia/Kolkata"),
        id="weekly_report",
        replace_existing=True,
        args=[send_message_fn],
    )

    _scheduler.start()
    log.info("Scheduler started.")
    return _scheduler


async def _evening_nudge(send) -> None:
    from backend.rules import format_plan_prompt, tomorrow_date
    tomorrow = tomorrow_date()
    await send(format_plan_prompt(tomorrow), "Markdown")


async def _morning_nudge(send) -> None:
    from backend import db
    from backend.rules import format_morning_confirm, today_date
    today = today_date()
    plan = db.get_plan(today)
    if plan:
        await send(format_morning_confirm(today, plan["raw"]), "Markdown")
    else:
        await send(
            f"☀️ Good morning! You haven't planned today ({today}) yet.\n"
            "Send me your plan when you're ready.",
            "Markdown",
        )


async def _weekly_report(send) -> None:
    from backend import db
    from backend.rules import _fmt_min, ist_now
    from datetime import timedelta

    today = ist_now().date()
    lines = ["📅 *Weekly report*\n"]
    total_prod = 0.0
    total_all = 0.0

    for offset in range(6, -1, -1):
        d = (today - timedelta(days=offset)).isoformat()
        rows = db.get_activity_for_date(d)
        day_total = sum(r["minutes"] for r in rows)
        day_prod = sum(r["minutes"] for r in rows if r["category"] not in ("social", "video"))
        total_all += day_total
        total_prod += day_prod
        lines.append(f"`{d}`: {_fmt_min(day_prod)} productive / {_fmt_min(day_total)} total")

    if total_all > 0:
        pct = int(total_prod / total_all * 100)
        lines.append(f"\n🏆 Week total: {_fmt_min(total_prod)} productive ({pct}%)")
    else:
        lines.append("\nNo data recorded this week yet.")

    await send("\n".join(lines), "Markdown")
