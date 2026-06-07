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
class IngestPayload:
    date: str  # YYYY-MM-DD
    aggregates: list[ActivityAggregate]
    ambiguous: list[AmbiguousItem]


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
