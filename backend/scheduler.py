"""
APScheduler jobs: evening planning prompt, morning confirm, weekly report,
and dynamic focus-block start/end nudges.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_scheduler: AsyncIOScheduler | None = None
_send_fn = None   # stored so schedule_block_nudges can be called after startup


def start(send_message_fn) -> AsyncIOScheduler:
    global _scheduler, _send_fn
    _send_fn = send_message_fn
    _scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

    from backend import db
    evening_time = db.get_config("nudge_evening", "21:00")
    morning_time = db.get_config("nudge_morning", "08:30")
    evening_h, evening_m = map(int, evening_time.split(":"))
    morning_h, morning_m = map(int, morning_time.split(":"))

    _scheduler.add_job(
        _evening_nudge, CronTrigger(hour=evening_h, minute=evening_m),
        id="evening_nudge", replace_existing=True, args=[send_message_fn],
    )
    _scheduler.add_job(
        _morning_nudge, CronTrigger(hour=morning_h, minute=morning_m),
        id="morning_nudge", replace_existing=True, args=[send_message_fn],
    )
    _scheduler.add_job(
        _weekly_report,
        CronTrigger(day_of_week="sun", hour=18, minute=0, timezone="Asia/Kolkata"),
        id="weekly_report", replace_existing=True, args=[send_message_fn],
    )

    _scheduler.start()
    log.info("Scheduler started.")

    # Re-arm any future focus blocks from today/tomorrow that survived a restart
    from backend.rules import today_date, tomorrow_date
    schedule_block_nudges(today_date())
    schedule_block_nudges(tomorrow_date())

    return _scheduler


def schedule_block_nudges(date: str) -> None:
    """
    Schedule start/end nudge jobs for every focus block on `date`.
    Safe to call multiple times — existing jobs are replaced.
    Only schedules jobs that are still in the future.
    """
    if _scheduler is None or _send_fn is None:
        return

    from backend import db
    blocks = db.get_time_blocks_for_date(date)
    now_ist = datetime.now(IST)

    for block in blocks:
        if block["kind"] != "focus":
            continue

        block_id = block["id"]
        start_dt = datetime.strptime(f"{date} {block['start']}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        end_dt   = datetime.strptime(f"{date} {block['end']}",   "%Y-%m-%d %H:%M").replace(tzinfo=IST)

        if start_dt > now_ist:
            _scheduler.add_job(
                _focus_start_nudge,
                DateTrigger(run_date=start_dt),
                id=f"focus_start_{block_id}",
                replace_existing=True,
                args=[_send_fn, dict(block)],
            )
            log.info("Scheduled focus-start nudge for block %d at %s IST", block_id, block["start"])

        if end_dt > now_ist:
            _scheduler.add_job(
                _focus_end_nudge,
                DateTrigger(run_date=end_dt),
                id=f"focus_end_{block_id}",
                replace_existing=True,
                args=[_send_fn, dict(block), date],
            )
            log.info("Scheduled focus-end nudge for block %d at %s IST", block_id, block["end"])


def cancel_block_nudges(block_id: int) -> None:
    """Cancel start/end nudge jobs for a block (called when /shift moves it)."""
    if _scheduler is None:
        return
    for job_id in (f"focus_start_{block_id}", f"focus_end_{block_id}"):
        job = _scheduler.get_job(job_id)
        if job:
            job.remove()


# ── Nudge handlers ────────────────────────────────────────────────────────────

async def _focus_start_nudge(send, block: dict) -> None:
    import json
    domains = json.loads(block.get("block_domains") or "[]")
    domain_str = ", ".join(domains) if domains else "none"
    await send(
        f"🔒 *Focus block starting!*\n"
        f"`{block['start']}–{block['end']}` — {block['label']}\n"
        f"Blocking: {domain_str}\n\n"
        f"Use /shift focus +30 to push it back.",
        "Markdown",
    )


async def _focus_end_nudge(send, block: dict, date: str) -> None:
    from backend import db
    from backend.rules import _fmt_min

    await send(
        f"✅ *Focus block complete!* ({block['label']})\n"
        f"Take a break — you've earned it.",
        "Markdown",
    )

    # Show next block if there is one
    blocks = db.get_time_blocks_for_date(date)
    for i, b in enumerate(blocks):
        if b["id"] == block["id"] and i + 1 < len(blocks):
            nxt = blocks[i + 1]
            await send(
                f"Next up: `{nxt['start']}–{nxt['end']}` {nxt['label']}",
                "Markdown",
            )
            break


async def _evening_nudge(send) -> None:
    from backend.rules import format_plan_prompt, tomorrow_date
    await send(format_plan_prompt(tomorrow_date()), "Markdown")


async def _morning_nudge(send) -> None:
    from backend import db
    from backend.rules import format_morning_confirm, today_date
    today = today_date()
    plan = db.get_plan(today)
    if plan:
        await send(format_morning_confirm(today, plan["raw"]), "Markdown")
    else:
        await send(
            f"☀️ Good morning! No plan for today ({today}) yet.\n"
            "Send me your plan when you're ready.",
            "Markdown",
        )


async def _weekly_report(send) -> None:
    from backend import db
    from backend.rules import ist_now
    from backend.scoring import format_weekly_report

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
    await send(format_weekly_report(days, deep_target, streak_target), "Markdown")
