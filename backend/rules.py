"""
v1 rule engine: parse Telegram commands and build nudge text.
"""
from datetime import date, timedelta


def parse_command(text: str) -> dict:
    """
    Parse a Telegram message into a command dict.
    Returns {"cmd": ..., "args": ...} or {"cmd": "unknown"}.
    """
    text = text.strip()
    parts = text.split()
    if not parts:
        return {"cmd": "unknown", "args": []}
    cmd = parts[0].lower()
    args = parts[1:]
    return {"cmd": cmd, "args": args}


def format_activity_summary(rows: list, for_date: str) -> str:
    """Turn activity rows into a human-readable Telegram message."""
    if not rows:
        return f"No activity recorded for {for_date} yet."

    total = sum(r["minutes"] for r in rows)
    productive = sum(r["minutes"] for r in rows if r["category"] not in ("social", "video"))

    lines = [f"📊 *Activity for {for_date}* (total: {_fmt_min(total)})"]
    lines.append(f"✅ Productive: {_fmt_min(productive)}  |  😴 Other: {_fmt_min(total - productive)}\n")

    by_cat: dict[str, float] = {}
    for r in rows:
        by_cat.setdefault(r["category"], 0)
        by_cat[r["category"]] += r["minutes"]

    for cat, mins in sorted(by_cat.items(), key=lambda x: -x[1]):
        bar = "█" * min(20, int(mins / max(total, 1) * 20))
        lines.append(f"`{cat:<15}` {bar} {_fmt_min(mins)}")

    return "\n".join(lines)


def _fmt_min(mins: float) -> str:
    h = int(mins) // 60
    m = int(mins) % 60
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def format_plan_prompt(tomorrow: str) -> str:
    return (
        f"🌙 *Evening check-in* — time to plan tomorrow ({tomorrow})!\n\n"
        "Send me your plan in any format. Examples:\n"
        "• `9-11 deep work (coding)`\n"
        "• `11-12 email + slack`\n"
        "• `14-16 focus block [youtube.com, reddit.com]`\n\n"
        "Or just describe your day in plain text — I'll parse it."
    )


def format_morning_confirm(today: str, plan_raw: str) -> str:
    return (
        f"☀️ *Good morning!* Here's your plan for today ({today}):\n\n"
        f"{plan_raw}\n\n"
        "Reply `ok` to confirm, or send an updated plan."
    )


def tomorrow_date() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


def today_date() -> str:
    return date.today().isoformat()
