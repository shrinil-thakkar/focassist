"""SQLite models and initialization."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get("FOCASSIST_DB", str(Path.home() / ".focassist" / "focassist.db"))


def _connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS time_blocks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT NOT NULL,          -- YYYY-MM-DD
                start        TEXT NOT NULL,          -- HH:MM
                end          TEXT NOT NULL,          -- HH:MM
                label        TEXT NOT NULL,
                kind         TEXT NOT NULL CHECK(kind IN ('productive','unproductive','focus')),
                block_domains TEXT DEFAULT '[]'      -- JSON list of domains to block
            );

            CREATE TABLE IF NOT EXISTS rules (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                match_type  TEXT NOT NULL CHECK(match_type IN ('domain','app','regex')),
                match_value TEXT NOT NULL,
                category    TEXT NOT NULL,
                productive  INTEGER NOT NULL DEFAULT 1,  -- 0/1
                source      TEXT NOT NULL DEFAULT 'seed' CHECK(source IN ('seed','user','llm')),
                UNIQUE(match_type, match_value)
            );

            CREATE TABLE IF NOT EXISTS activity (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                date     TEXT NOT NULL,
                category TEXT NOT NULL,
                app      TEXT NOT NULL,
                domain   TEXT NOT NULL,
                minutes  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ambiguous_queue (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                app     TEXT NOT NULL,
                domain  TEXT NOT NULL,
                title   TEXT NOT NULL,
                minutes REAL NOT NULL,
                status  TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','asked','resolved'))
            );

            CREATE TABLE IF NOT EXISTS plans (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                date    TEXT NOT NULL UNIQUE,    -- YYYY-MM-DD
                raw     TEXT NOT NULL,           -- raw text the user typed
                created TEXT NOT NULL            -- ISO timestamp
            );
        """)

        # Seed default config
        defaults = {
            "nudge_evening": "21:00",
            "nudge_morning": "08:30",
            "nudge_weekly": "Sunday 18:00",
            "sensitive_apps": "[]",
            "working_hours_start": "09:00",
            "working_hours_end": "18:00",
            "telegram_chat_id": "",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                (key, value),
            )


def get_config(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def upsert_activity(date: str, aggregates: list[dict]) -> None:
    """Replace today's activity records with fresh aggregates."""
    with get_db() as conn:
        conn.execute("DELETE FROM activity WHERE date = ?", (date,))
        conn.executemany(
            "INSERT INTO activity (date, category, app, domain, minutes) VALUES (?, ?, ?, ?, ?)",
            [(date, a["category"], a["app"], a["domain"], a["minutes"]) for a in aggregates],
        )


def insert_ambiguous(items: list[dict]) -> None:
    """Insert new ambiguous items; skip exact duplicates."""
    with get_db() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO ambiguous_queue (app, domain, title, minutes)
               SELECT ?, ?, ?, ?
               WHERE NOT EXISTS (
                 SELECT 1 FROM ambiguous_queue
                 WHERE app=? AND domain=? AND title=? AND status='pending'
               )""",
            [(i["app"], i["domain"], i["title"], i["minutes"],
              i["app"], i["domain"], i["title"]) for i in items],
        )


def get_activity_for_date(date: str) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM activity WHERE date = ? ORDER BY minutes DESC",
            (date,),
        ).fetchall()


def get_rules() -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute("SELECT * FROM rules ORDER BY source, id").fetchall()


def upsert_rule(match_type: str, match_value: str, category: str,
                productive: bool, source: str = "user") -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO rules (match_type, match_value, category, productive, source)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(match_type, match_value)
               DO UPDATE SET category=excluded.category,
                             productive=excluded.productive,
                             source=excluded.source""",
            (match_type, match_value, category, int(productive), source),
        )


def save_plan(date: str, raw_text: str) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO plans (date, raw, created) VALUES (?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET raw=excluded.raw, created=excluded.created""",
            (date, raw_text, now),
        )


def get_plan(date: str) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute("SELECT * FROM plans WHERE date = ?", (date,)).fetchone()


def get_active_directive() -> dict:
    """Return the current focus-block directive from time_blocks."""
    import json
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")

    # Time blocks are stored as IST HH:MM — compare in IST
    now_ist = datetime.now(IST)
    now_str = now_ist.strftime("%H:%M")
    today = now_ist.strftime("%Y-%m-%d")

    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM time_blocks
               WHERE date = ? AND kind = 'focus'
                 AND start <= ? AND end >= ?
               ORDER BY start LIMIT 1""",
            (today, now_str, now_str),
        ).fetchone()

    if not row:
        return {"focus_block_active": False, "block_domains": [], "block_until": None}

    # Treat block end as IST, convert to UTC for the Mac agent
    end_dt_ist = datetime.strptime(f"{today} {row['end']}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    end_dt_utc = end_dt_ist.astimezone(timezone.utc)
    return {
        "focus_block_active": True,
        "block_domains": json.loads(row["block_domains"] or "[]"),
        "block_until": end_dt_utc.isoformat(),
    }
