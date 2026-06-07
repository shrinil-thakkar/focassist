"""
FastAPI endpoints for the Mac agent.
  POST /ingest   — receive aggregates + ambiguous queue
  GET  /directive — return current focus-block directive
  GET  /rules     — return Tier-1 ruleset
"""
import os
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import backend.db as db

app = FastAPI(title="FocAssist Backend API")

BEARER_TOKEN = os.environ.get("FOCASSIST_TOKEN", "")


def require_auth(request: Request) -> None:
    if not BEARER_TOKEN:
        raise HTTPException(500, "Server token not configured")
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {BEARER_TOKEN}":
        raise HTTPException(401, "Unauthorized")


# --- Pydantic models ---

class Aggregate(BaseModel):
    category: str
    app: str
    domain: str
    minutes: float


class AmbiguousItem(BaseModel):
    app: str
    domain: str
    title: str
    minutes: float


class IngestPayload(BaseModel):
    date: str
    aggregates: list[Aggregate]
    ambiguous: list[AmbiguousItem]


# --- Routes ---

@app.post("/ingest")
def ingest(payload: IngestPayload, _=Depends(require_auth)):
    db.upsert_activity(payload.date, [a.model_dump() for a in payload.aggregates])
    db.insert_ambiguous([a.model_dump() for a in payload.ambiguous])
    return {"status": "ok", "aggregates": len(payload.aggregates), "ambiguous": len(payload.ambiguous)}


@app.get("/directive")
def directive(_=Depends(require_auth)):
    return db.get_active_directive()


@app.get("/rules")
def rules(_=Depends(require_auth)):
    rows = db.get_rules()
    return [
        {
            "match_type": r["match_type"],
            "match_value": r["match_value"],
            "category": r["category"],
            "productive": bool(r["productive"]),
        }
        for r in rows
    ]


@app.get("/health")
def health():
    return {"status": "ok"}
