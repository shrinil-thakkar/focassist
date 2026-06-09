"""
FastAPI endpoints for the Mac agent.
  POST /ingest    — aggregates + ambiguous + sessions + timeline
  GET  /directive — current focus-block directive
  GET  /rules     — Tier-1 ruleset (tier + category + match fields)
"""
import os
from fastapi import FastAPI, Depends, HTTPException, Request
from pydantic import BaseModel
import backend.db as db

app = FastAPI(title="FocAssist Backend API")

BEARER_TOKEN = os.environ.get("FOCASSIST_TOKEN", "")


def require_auth(request: Request) -> None:
    if not BEARER_TOKEN:
        raise HTTPException(500, "Server token not configured")
    if request.headers.get("Authorization") != f"Bearer {BEARER_TOKEN}":
        raise HTTPException(401, "Unauthorized")


# ── Pydantic models ───────────────────────────────────────────────────────────

class Aggregate(BaseModel):
    tier: str = "distraction"
    category: str
    app: str
    domain: str
    minutes: float


class AmbiguousItem(BaseModel):
    app: str
    domain: str
    title: str
    minutes: float


class Session(BaseModel):
    start: str
    end: str
    deep_minutes: float
    absorbed_minutes: float
    span_minutes: float


class HourlyItem(BaseModel):
    hour: int
    tier: str
    category: str
    app: str
    domain: str
    minutes: float


class CoverageFlag(BaseModel):
    type: str
    message: str
    coverage: float | None = None            # chrome_unlabeled: URL-coverage fraction
    unlabeled_minutes: float | None = None   # chrome_unlabeled: unlabeled browser minutes
    minutes: float | None = None             # non_chrome_browser: minutes in other browser
    fraction: float | None = None            # high_untracked: untracked fraction


class Coverage(BaseModel):
    """Reconciliation totals from the AFK-anchored merge pipeline (tracking-algorithm.md §6)."""
    active_minutes: float = 0
    idle_minutes: float = 0
    untracked_minutes: float = 0
    flags: list[CoverageFlag] = []
    first_tracked_ist: str | None = None


class IngestPayload(BaseModel):
    date: str
    aggregates: list[Aggregate]
    ambiguous: list[AmbiguousItem]
    sessions: list[Session] = []
    timeline: list[str] = []
    hourly_activity: list[HourlyItem] = []
    coverage: Coverage | None = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/ingest")
def ingest(payload: IngestPayload, _=Depends(require_auth)):
    db.upsert_activity(payload.date, [a.model_dump() for a in payload.aggregates])
    db.insert_ambiguous([a.model_dump() for a in payload.ambiguous])
    if payload.sessions:
        db.upsert_sessions(payload.date, [s.model_dump() for s in payload.sessions])
    if payload.timeline:
        db.save_timeline(payload.date, payload.timeline)
    if payload.hourly_activity:
        db.upsert_hourly_activity(payload.date, [h.model_dump() for h in payload.hourly_activity])
    if payload.coverage is not None:
        c = payload.coverage
        db.save_coverage(payload.date, c.active_minutes, c.idle_minutes,
                         c.untracked_minutes,
                         [f.model_dump(exclude_none=True) for f in c.flags],
                         c.first_tracked_ist)
    return {
        "status": "ok",
        "aggregates": len(payload.aggregates),
        "sessions": len(payload.sessions),
    }


@app.get("/directive")
def directive(_=Depends(require_auth)):
    return db.get_active_directive()


@app.get("/rules")
def rules(_=Depends(require_auth)):
    rows = db.get_rules()
    return [
        {
            "match_type":  r["match_type"],
            "match_value": r["match_value"],
            "tier":        r["tier"],
            "category":    r["category"],
            "productive":  bool(r["productive"]),
        }
        for r in rows
    ]


@app.get("/health")
def health():
    return {"status": "ok"}
