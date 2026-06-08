"""Shared data shapes between mac agent and cloud backend."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ActivityAggregate:
    category: str
    app: str
    domain: str
    minutes: float


@dataclass
class AmbiguousItem:
    app: str
    domain: str
    title: str
    minutes: float


@dataclass
class CoverageFlag:
    type: str
    message: str


@dataclass
class Coverage:
    """Reconciliation totals from the AFK-anchored merge pipeline (tracking-algorithm.md §6)."""
    active_minutes: float
    idle_minutes: float
    untracked_minutes: float
    flags: list[CoverageFlag] = field(default_factory=list)


@dataclass
class IngestPayload:
    date: str  # YYYY-MM-DD
    aggregates: list[ActivityAggregate]
    ambiguous: list[AmbiguousItem]
    coverage: Optional[Coverage] = None


@dataclass
class Directive:
    focus_block_active: bool
    block_domains: list[str]
    block_until: Optional[str]  # ISO 8601 UTC or None


@dataclass
class CategoryRule:
    match_type: str   # domain | app | regex
    match_value: str
    category: str
    productive: bool
