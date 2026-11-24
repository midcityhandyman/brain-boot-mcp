"""Plain data records shared by every storage backend.

Nothing in this module talks to a database or to MCP. Timestamps are always
timezone-aware UTC datetimes in memory and a fixed ISO-8601 string on the wire.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

ANY_AGENT = "*"

PROPOSAL_PENDING = "pending"
PROPOSAL_APPROVED = "approved"
PROPOSAL_REJECTED = "rejected"

KIND_RULE = "rule"
KIND_CORRECTION = "correction"
KIND_DUE = "due"
KIND_MEMORY = "memory"
SEARCH_KINDS = (KIND_MEMORY, KIND_CORRECTION, KIND_RULE)


def to_utc(value: datetime) -> datetime:
    """Return value as an aware UTC datetime with microseconds dropped."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def iso(value: datetime) -> str:
    """Canonical wire format, for example 2026-01-05T14:00:00Z."""
    return to_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    """Parse the canonical wire format (and plain ISO-8601) back to UTC."""
    cleaned = text.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    return to_utc(datetime.fromisoformat(cleaned))


_WS = re.compile(r"\s+")


def normalize_rule_text(text: str) -> str:
    """Normal form used to spot duplicate rules and proposals.

    Lowercase, whitespace collapsed, trailing full stops and exclamation marks
    removed. The Postgres function brain_norm() implements the same steps.
    """
    return _WS.sub(" ", text).strip().lower().rstrip(".!").strip()


@dataclass(frozen=True)
class Rule:
    id: int
    version: int
    scope: str
    priority: int
    text: str
    status: str
    updated_at: datetime

    @property
    def key(self) -> str:
        return f"rule:{self.id}:v{self.version}"


@dataclass(frozen=True)
class Correction:
    id: int
    agent_key: str
    text: str
    source: str
    created_at: datetime

    @property
    def key(self) -> str:
        return f"correction:{self.id}"


@dataclass(frozen=True)
class DueItem:
    id: int
    agent_key: str
    title: str
    due_at: datetime
    done_at: datetime | None = None

    @property
    def key(self) -> str:
        return f"due:{self.id}"


@dataclass(frozen=True)
class PackLine:
    """One line of a boot pack. The pack id is a hash over (key, text)."""

    key: str
    kind: str
    text: str

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "kind": self.kind, "text": self.text}


@dataclass(frozen=True)
class Receipt:
    """Proof that an agent was handed a specific pack, with its lines."""

    pack_id: str
    agent_key: str
    source: str
    created_at: datetime
    lines: tuple[PackLine, ...]


@dataclass(frozen=True)
class MemoryHit:
    kind: str
    ref_id: int
    text: str
    score: float
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": f"{self.kind}:{self.ref_id}",
            "text": self.text,
            "score": round(self.score, 4),
            "created_at": iso(self.created_at),
        }


@dataclass(frozen=True)
class Proposal:
    id: int
    agent_key: str
    text: str
    rationale: str
    scope: str
    status: str
    created_at: datetime
    resolved_at: datetime | None = None
    rule_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.id,
            "agent_key": self.agent_key,
            "text": self.text,
            "rationale": self.rationale,
            "scope": self.scope,
            "status": self.status,
            "created_at": iso(self.created_at),
            "resolved_at": iso(self.resolved_at) if self.resolved_at else None,
            "rule_id": self.rule_id,
        }


@dataclass(frozen=True)
class PackSettings:
    """How much goes into a pack. The SQL functions take the same knobs."""

    correction_window_days: int = 14
    correction_limit: int = 10
    due_horizon_hours: int = 24
    due_limit: int = 20


@dataclass
class BootPack:
    pack_id: str
    agent_key: str
    generated_at: datetime
    rules: list[Rule] = field(default_factory=list)
    corrections: list[Correction] = field(default_factory=list)
    due_items: list[DueItem] = field(default_factory=list)
    lines: tuple[PackLine, ...] = ()
