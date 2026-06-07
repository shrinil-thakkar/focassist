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


class IngestPayload(BaseModel):
    date: str
    aggregates: list[Aggregate]
    ambiguous: list[AmbiguousItem]
    sessions: list[Session] = []
    timeline: list[str] = []


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/ingest")
def ingest(payload: IngestPayload, _=Depends(require_auth)):
    db.upsert_activity(payload.date, [a.model_dump() for a in payload.aggregates])
    db.insert_ambiguous([a.model_dump() for a in payload.ambiguous])
    if payload.sessions:
        db.upsert_sessions(payload.date, [s.model_dump() for s in payload.sessions])
    if payload.timeline:
        db.save_timeline(payload.date, payload.timeline)
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
