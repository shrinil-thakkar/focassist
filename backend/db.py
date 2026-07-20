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


# Old category → (tier, category) backfill mapping
_CATEGORY_REMAP: dict[str, tuple[str, str]] = {
    "dev":      ("deep",        "coding"),
    "design":   ("deep",        "design"),
    "ai":       ("deep",        "ai"),
    "docs":     ("deep",        "docs"),
    "work":     ("supporting",  "planning"),
    "comms":    ("supporting",  "comms"),
    "meetings": ("supporting",  "meetings"),
    "planning": ("supporting",  "planning"),
    "video":    ("distraction", "video"),
    "social":   ("distraction", "social"),
    "browsing": ("distraction", "browsing"),
    "other":    ("distraction", "browsing"),
    "system":   ("neutral",     "system"),
}


def init_db() -> None:
    with get_db() as conn:
        # ── Static tables (no migrations needed) ─────────────────────────────
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS time_blocks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                date          TEXT NOT NULL,
                start         TEXT NOT NULL,
                end           TEXT NOT NULL,
                label         TEXT NOT NULL,
                kind          TEXT NOT NULL CHECK(kind IN ('productive','unproductive','focus')),
                block_domains TEXT DEFAULT '[]'
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
                date    TEXT NOT NULL UNIQUE,
                raw     TEXT NOT NULL,
                created TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                date             TEXT NOT NULL,
                start            TEXT NOT NULL,
                end              TEXT NOT NULL,
                deep_minutes     REAL NOT NULL,
                absorbed_minutes REAL NOT NULL,
                span_minutes     REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_timeline (
                date    TEXT PRIMARY KEY,
                buckets TEXT NOT NULL   -- JSON array of tier strings (15-min buckets)
            );

            CREATE TABLE IF NOT EXISTS daily_coverage (
                date              TEXT PRIMARY KEY,
                active_minutes    REAL NOT NULL DEFAULT 0,
                idle_minutes      REAL NOT NULL DEFAULT 0,
                untracked_minutes REAL NOT NULL DEFAULT 0,
                flags             TEXT NOT NULL DEFAULT '[]'   -- JSON array of {type, message, ...}
            );

            CREATE TABLE IF NOT EXISTS hourly_activity (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                date     TEXT NOT NULL,
                hour     INTEGER NOT NULL,
                tier     TEXT NOT NULL,
                category TEXT NOT NULL,
                app      TEXT NOT NULL,
                domain   TEXT NOT NULL,
                minutes  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reprocess_queue (
                date    TEXT PRIMARY KEY,
                status  TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','done')),
                created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS fetch_queue (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                days    INTEGER NOT NULL,
                status  TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','done')),
                created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ── rules table — recreate if old schema (no tier/url_match support) ──
        _migrate_rules(conn)

        # ── activity table — add tier column + backfill old rows ──────────────
        _migrate_activity(conn)

        # ── coverage table — add first_tracked_ist column ─────────────────────
        _migrate_coverage(conn)

        # ── Seed default config ───────────────────────────────────────────────
        defaults = {
            "nudge_evening":        "21:00",
            "nudge_morning":        "08:30",
            "nudge_weekly":         "Sunday 18:00",
            "sensitive_apps":       "[]",
            "working_hours_start":  "09:00",
            "working_hours_end":    "18:00",
            "telegram_chat_id":     "",
            "score_deep_target_min":    "240",
            "score_streak_target_min":  "90",
            "nudge_daily_report":       "19:30",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                (key, value),
            )


def _migrate_coverage(conn: sqlite3.Connection) -> None:
    """Add first_tracked_ist column to daily_coverage if absent."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_coverage)").fetchall()}
    if "first_tracked_ist" not in cols:
        conn.execute("ALTER TABLE daily_coverage ADD COLUMN first_tracked_ist TEXT")


def _migrate_rules(conn: sqlite3.Connection) -> None:
    """Recreate rules table with url_match support + tier column."""
    # Check if migration is needed by probing for the tier column
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rules)").fetchall()}
    if "tier" in cols:
        return  # already migrated

    # Capture existing user/llm rules before dropping the table
    old_rows = []
    try:
        old_rows = conn.execute(
            "SELECT match_type, match_value, category, productive, source FROM rules"
        ).fetchall()
    except Exception:
        pass

    conn.executescript("""
        DROP TABLE IF EXISTS rules;
        CREATE TABLE rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            match_type  TEXT NOT NULL
                        CHECK(match_type IN ('domain','app','url_match','regex')),
            match_value TEXT NOT NULL,
            tier        TEXT NOT NULL
                        CHECK(tier IN ('deep','supporting','neutral','distraction')),
            category    TEXT NOT NULL,
            productive  INTEGER NOT NULL DEFAULT 1,
            source      TEXT NOT NULL DEFAULT 'seed'
                        CHECK(source IN ('seed','user','llm')),
            UNIQUE(match_type, match_value)
        );
    """)

    # Backfill old user/llm rules using the category remap
    for row in old_rows:
        if row["source"] == "seed":
            continue  # seed rules come from the agent, don't persist them
        old_cat = row["category"]
        tier, new_cat = _CATEGORY_REMAP.get(old_cat, ("distraction", old_cat))
        mt = row["match_type"]
        if mt not in ("domain", "app", "url_match", "regex"):
            mt = "domain"
        try:
            conn.execute(
                """INSERT OR IGNORE INTO rules
                   (match_type, match_value, tier, category, productive, source)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (mt, row["match_value"], tier, new_cat,
                 int(tier in ("deep", "supporting", "neutral")), row["source"]),
            )
        except Exception:
            pass


def _migrate_activity(conn: sqlite3.Connection) -> None:
    """Add tier column to activity table and backfill old rows."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(activity)").fetchall()}

    if "activity" not in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        conn.execute("""
            CREATE TABLE activity (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                date     TEXT NOT NULL,
                tier     TEXT NOT NULL DEFAULT 'distraction',
                category TEXT NOT NULL,
                app      TEXT NOT NULL,
                domain   TEXT NOT NULL,
                minutes  REAL NOT NULL
            )
        """)
        return

    if "tier" not in cols:
        conn.execute("ALTER TABLE activity ADD COLUMN tier TEXT NOT NULL DEFAULT 'distraction'")
        # Backfill existing rows
        for old_cat, (tier, _) in _CATEGORY_REMAP.items():
            conn.execute(
                "UPDATE activity SET tier = ? WHERE category = ?",
                (tier, old_cat),
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
            """INSERT INTO activity (date, tier, category, app, domain, minutes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(date,
              a.get("tier", "distraction"),
              a.get("category", "other"),
              a["app"], a["domain"], a["minutes"]) for a in aggregates],
        )


def upsert_hourly_activity(date: str, hourly: list[dict]) -> None:
    """Replace today's hourly activity rollup with fresh data."""
    with get_db() as conn:
        conn.execute("DELETE FROM hourly_activity WHERE date = ?", (date,))
        conn.executemany(
            """INSERT INTO hourly_activity (date, hour, tier, category, app, domain, minutes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(date, h["hour"], h["tier"], h["category"], h["app"], h["domain"], h["minutes"])
             for h in hourly],
        )


def get_hourly_activity(date: str, hour: int) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM hourly_activity WHERE date = ? AND hour = ? ORDER BY minutes DESC",
            (date, hour),
        ).fetchall()


def upsert_sessions(date: str, sessions: list[dict]) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE date = ?", (date,))
        conn.executemany(
            """INSERT INTO sessions (date, start, end, deep_minutes, absorbed_minutes, span_minutes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(date, s["start"], s["end"], s["deep_minutes"],
              s["absorbed_minutes"], s["span_minutes"]) for s in sessions],
        )


def save_timeline(date: str, buckets: list[str]) -> None:
    import json
    with get_db() as conn:
        conn.execute(
            """INSERT INTO daily_timeline (date, buckets) VALUES (?, ?)
               ON CONFLICT(date) DO UPDATE SET buckets=excluded.buckets""",
            (date, json.dumps(buckets)),
        )


def get_sessions_for_date(date: str) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM sessions WHERE date = ? ORDER BY start",
            (date,),
        ).fetchall()


def get_timeline_for_date(date: str) -> list[str]:
    import json
    with get_db() as conn:
        row = conn.execute(
            "SELECT buckets FROM daily_timeline WHERE date = ?", (date,)
        ).fetchone()
    return json.loads(row["buckets"]) if row else []


def save_coverage(date: str, active_minutes: float, idle_minutes: float,
                  untracked_minutes: float, flags: list[dict],
                  first_tracked_ist: str | None = None) -> None:
    """
    Persist the day's active/idle/untracked reconciliation totals + health flags
    (tracking-algorithm.md §6, §5) — the trust check behind 'tracked X; idle Y;
    untracked Z' in the daily report.
    """
    import json
    with get_db() as conn:
        conn.execute(
            """INSERT INTO daily_coverage
                 (date, active_minutes, idle_minutes, untracked_minutes, flags, first_tracked_ist)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 active_minutes=excluded.active_minutes,
                 idle_minutes=excluded.idle_minutes,
                 untracked_minutes=excluded.untracked_minutes,
                 flags=excluded.flags,
                 first_tracked_ist=excluded.first_tracked_ist""",
            (date, active_minutes, idle_minutes, untracked_minutes,
             json.dumps(flags), first_tracked_ist),
        )


def get_coverage(date: str) -> dict | None:
    import json
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM daily_coverage WHERE date = ?", (date,)
        ).fetchone()
    if not row:
        return None
    return {
        "active_minutes": row["active_minutes"],
        "idle_minutes": row["idle_minutes"],
        "untracked_minutes": row["untracked_minutes"],
        "flags": json.loads(row["flags"]),
        "first_tracked_ist": row["first_tracked_ist"],
    }


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


def upsert_rule(match_type: str, match_value: str, tier: str, category: str,
                source: str = "user") -> None:
    productive = int(tier in ("deep", "supporting", "neutral"))
    with get_db() as conn:
        conn.execute(
            """INSERT INTO rules (match_type, match_value, tier, category, productive, source)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(match_type, match_value)
               DO UPDATE SET tier=excluded.tier,
                             category=excluded.category,
                             productive=excluded.productive,
                             source=excluded.source""",
            (match_type, match_value, tier, category, productive, source),
        )


def save_time_blocks(date: str, blocks: list[dict]) -> None:
    """Replace all time blocks for a date with the parsed plan."""
    with get_db() as conn:
        conn.execute("DELETE FROM time_blocks WHERE date = ?", (date,))
        conn.executemany(
            """INSERT INTO time_blocks (date, start, end, label, kind, block_domains)
               VALUES (:date, :start, :end, :label, :kind, :block_domains)""",
            blocks,
        )


def get_time_blocks_for_date(date: str) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM time_blocks WHERE date = ? ORDER BY start",
            (date,),
        ).fetchall()


def get_time_block_by_id(block_id: int) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM time_blocks WHERE id = ?", (block_id,)
        ).fetchone()


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


def add_reprocess_job(date: str) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO reprocess_queue (date, status) VALUES (?, 'pending')
               ON CONFLICT(date) DO UPDATE SET status='pending', created=CURRENT_TIMESTAMP""",
            (date,),
        )


def get_pending_reprocess_jobs() -> list[str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT date FROM reprocess_queue WHERE status='pending' ORDER BY created"
        ).fetchall()
    return [r["date"] for r in rows]


def mark_reprocess_done(date: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE reprocess_queue SET status='done' WHERE date=?", (date,)
        )


def add_fetch_job(days: int) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO fetch_queue (days, status) VALUES (?, 'pending')", (days,)
        )
        return cur.lastrowid


def get_pending_fetch_jobs() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, days FROM fetch_queue WHERE status='pending' ORDER BY created"
        ).fetchall()
    return [{"id": r["id"], "days": r["days"]} for r in rows]


def mark_fetch_job_done(job_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE fetch_queue SET status='done' WHERE id=?", (job_id,)
        )


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
