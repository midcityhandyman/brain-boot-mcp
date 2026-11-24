"""BrainService: the behaviour behind the four MCP tools.

Agent side (exposed as tools):   boot, pack_check, recall, propose_rule
Human side (CLI and tests only): add_rule, update_rule, retire_rule,
                                 add_correction, add_due_item, complete_due_item,
                                 remember, approve_proposal, reject_proposal

The split is the point of the design. An agent can read its rules and can ask
for a new one, but only a human action turns a proposal into a standing rule.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import (
    ANY_AGENT,
    PROPOSAL_APPROVED,
    PROPOSAL_REJECTED,
    SEARCH_KINDS,
    BootPack,
    Correction,
    DueItem,
    PackSettings,
    Proposal,
    Receipt,
    Rule,
    iso,
    normalize_rule_text,
    to_utc,
)
from .pack import build_lines, compute_pack_id, diff_lines, looks_like_pack_id
from .store import BrainStore

STATUS_UNCHANGED = "UNCHANGED"
STATUS_CHANGED = "CHANGED"
STATUS_UNKNOWN = "UNKNOWN_PACK"

MAX_RULE_CHARS = 500
MAX_RATIONALE_CHARS = 1000
MAX_RECALL_LIMIT = 25
MAX_QUERY_TOKENS = 12

_AGENT_KEY = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by do for from how in is it of on or that the this to was "
    "we what when where which who why with".split()
)

BOOT_INSTRUCTIONS = (
    "Follow every line in standing_rules for this whole session. "
    "recent_corrections are things a human told an agent to do differently; "
    "treat them as rules. Handle or mention due_items. "
    "Keep pack_id and call pack_check with it before any action that is hard to undo."
)


class BrainError(ValueError):
    """Bad input from a caller. The message is safe to show to the agent."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def tokenize(query: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopwords and duplicates removed."""
    seen: list[str] = []
    for token in _TOKEN.findall(query.lower()):
        if len(token) < 2 or token in _STOPWORDS or token in seen:
            continue
        seen.append(token)
    return seen[:MAX_QUERY_TOKENS]


def check_agent_key(agent_key: str) -> str:
    key = (agent_key or "").strip().lower()
    if not _AGENT_KEY.match(key):
        raise BrainError(
            "agent_key must be 1 to 63 characters of a-z, 0-9, dot, dash or underscore"
        )
    return key


class BrainService:
    def __init__(
        self,
        store: BrainStore,
        settings: PackSettings | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.settings = settings or PackSettings()
        self._clock = clock

    def now(self) -> datetime:
        return to_utc(self._clock())

    # ---- pack assembly ---------------------------------------------------
    def build_pack(self, agent_key: str, now: datetime | None = None) -> BootPack:
        """Assemble the current pack without writing a receipt."""
        key = check_agent_key(agent_key)
        moment = to_utc(now) if now else self.now()
        cfg = self.settings
        rules = self.store.active_rules(key)
        corrections = self.store.recent_corrections(
            key, moment - timedelta(days=cfg.correction_window_days), cfg.correction_limit
        )
        due = self.store.due_items(
            key, moment + timedelta(hours=cfg.due_horizon_hours), cfg.due_limit
        )
        lines = build_lines(rules, corrections, due)
        return BootPack(
            pack_id=compute_pack_id(lines),
            agent_key=key,
            generated_at=moment,
            rules=rules,
            corrections=corrections,
            due_items=due,
            lines=lines,
        )

    # ---- tool: boot ------------------------------------------------------
    def boot(self, agent_key: str) -> dict[str, Any]:
        pack = self.build_pack(agent_key)
        self.store.save_receipt(
            Receipt(pack.pack_id, pack.agent_key, "boot", pack.generated_at, pack.lines)
        )
        return {
            "pack_id": pack.pack_id,
            "agent_key": pack.agent_key,
            "generated_at": iso(pack.generated_at),
            "standing_rules": [_rule_dict(r) for r in pack.rules],
            "recent_corrections": [_correction_dict(c) for c in pack.corrections],
            "due_items": [_due_dict(d, pack.generated_at) for d in pack.due_items],
            "counts": {
                "standing_rules": len(pack.rules),
                "recent_corrections": len(pack.corrections),
                "due_items": len(pack.due_items),
            },
            "instructions": BOOT_INSTRUCTIONS,
        }

    # ---- tool: pack_check ------------------------------------------------
    def pack_check(self, pack_id: str, agent_key: str) -> dict[str, Any]:
        key = check_agent_key(agent_key)
        claimed = (pack_id or "").strip()
        if not looks_like_pack_id(claimed):
            raise BrainError("pack_id must look like pk_ followed by 16 hex characters")
        current = self.build_pack(key)
        base: dict[str, Any] = {
            "pack_id": claimed,
            "current_pack_id": current.pack_id,
            "agent_key": key,
            "checked_at": iso(current.generated_at),
            "added": [],
            "removed": [],
        }
        if claimed == current.pack_id:
            return {
                **base,
                "status": STATUS_UNCHANGED,
                "instruction": "Nothing changed since this pack was issued. Carry on.",
            }
        # The agent is about to be told the current pack id, so record that it
        # was handed over. The next check with current_pack_id then has a base.
        self.store.save_receipt(
            Receipt(current.pack_id, key, "pack_check", current.generated_at, current.lines)
        )
        receipt = self.store.latest_receipt(claimed, key)
        if receipt is None:
            return {
                **base,
                "status": STATUS_UNKNOWN,
                "instruction": (
                    "No receipt exists for this pack id and agent. Call boot again and "
                    "follow the pack it returns before doing anything else."
                ),
            }
        added, removed = diff_lines(receipt.lines, current.lines)
        return {
            **base,
            "status": STATUS_CHANGED,
            "added": [line.as_dict() for line in added],
            "removed": [line.as_dict() for line in removed],
            "instruction": (
                "Your pack is out of date. Follow the added lines first, stop following "
                "the removed lines, then use current_pack_id for later checks."
            ),
        }

    # ---- tool: recall ----------------------------------------------------
    def recall(
        self, query: str, limit: int = 5, kinds: Sequence[str] | None = None
    ) -> dict[str, Any]:
        wanted = tuple(kinds) if kinds else SEARCH_KINDS
        unknown = [k for k in wanted if k not in SEARCH_KINDS]
        if unknown:
            raise BrainError(f"unknown kind {unknown[0]!r}; use any of {list(SEARCH_KINDS)}")
        if not 1 <= limit <= MAX_RECALL_LIMIT:
            raise BrainError(f"limit must be between 1 and {MAX_RECALL_LIMIT}")
        tokens = tokenize(query or "")
        if not tokens:
            raise BrainError("query has no searchable words")
        hits = self.store.search(tokens, limit, wanted)
        return {
            "query": query,
            "tokens": tokens,
            "count": len(hits),
            "hits": [hit.as_dict() for hit in hits],
        }

    # ---- tool: propose_rule ----------------------------------------------
    def propose_rule(
        self, agent_key: str, text: str, rationale: str = "", scope: str = ANY_AGENT
    ) -> dict[str, Any]:
        key = check_agent_key(agent_key)
        body = " ".join((text or "").split())
        why = (rationale or "").strip()
        if not body:
            raise BrainError("rule text is empty")
        if len(body) > MAX_RULE_CHARS:
            raise BrainError(f"rule text is longer than {MAX_RULE_CHARS} characters")
        if len(why) > MAX_RATIONALE_CHARS:
            raise BrainError(f"rationale is longer than {MAX_RATIONALE_CHARS} characters")
        target = ANY_AGENT if scope in ("", ANY_AGENT) else check_agent_key(scope)
        norm = normalize_rule_text(body)
        existing = self.store.find_active_rule_by_norm(norm)
        if existing is not None:
            return {
                "status": "duplicate_of_rule",
                "rule_key": existing.key,
                "proposal_id": None,
                "message": "This is already a standing rule. Nothing was recorded.",
            }
        pending = self.store.find_pending_proposal_by_norm(norm)
        if pending is not None:
            return {
                "status": "already_pending",
                "rule_key": None,
                "proposal_id": pending.id,
                "message": "The same proposal is already waiting for a human decision.",
            }
        proposal = self.store.insert_proposal(key, body, norm, why, target, self.now())
        return {
            "status": "pending",
            "rule_key": None,
            "proposal_id": proposal.id,
            "message": (
                "Recorded as a proposal. It is not a rule and will not appear in any "
                "pack until a human approves it."
            ),
        }

    # ---- human side ------------------------------------------------------
    def add_rule(self, text: str, scope: str = ANY_AGENT, priority: int = 100) -> Rule:
        body = " ".join(text.split())
        if not body:
            raise BrainError("rule text is empty")
        return self.store.add_rule(body, normalize_rule_text(body), scope, priority, self.now())

    def update_rule(self, rule_id: int, text: str) -> Rule:
        body = " ".join(text.split())
        if not body:
            raise BrainError("rule text is empty")
        return self.store.update_rule(rule_id, body, normalize_rule_text(body), self.now())

    def retire_rule(self, rule_id: int) -> None:
        self.store.retire_rule(rule_id, self.now())

    def add_correction(
        self, text: str, agent_key: str = ANY_AGENT, source: str = "human"
    ) -> Correction:
        return self.store.add_correction(text.strip(), agent_key, source, self.now())

    def add_due_item(
        self, title: str, due_at: datetime, agent_key: str = ANY_AGENT
    ) -> DueItem:
        return self.store.add_due_item(title.strip(), to_utc(due_at), agent_key, self.now())

    def complete_due_item(self, item_id: int) -> None:
        self.store.complete_due_item(item_id, self.now())

    def remember(self, text: str, topic: str = "") -> int:
        return self.store.add_memory(text.strip(), topic, self.now())

    def list_proposals(self, status: str | None = None) -> list[Proposal]:
        return self.store.list_proposals(status)

    def approve_proposal(self, proposal_id: int, priority: int = 100) -> Rule:
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            raise BrainError(f"proposal {proposal_id} does not exist")
        if proposal.status != "pending":
            raise BrainError(f"proposal {proposal_id} is already {proposal.status}")
        rule = self.add_rule(proposal.text, proposal.scope, priority)
        self.store.resolve_proposal(proposal_id, PROPOSAL_APPROVED, self.now(), rule.id)
        return rule

    def reject_proposal(self, proposal_id: int) -> Proposal:
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            raise BrainError(f"proposal {proposal_id} does not exist")
        if proposal.status != "pending":
            raise BrainError(f"proposal {proposal_id} is already {proposal.status}")
        return self.store.resolve_proposal(proposal_id, PROPOSAL_REJECTED, self.now(), None)


def _rule_dict(rule: Rule) -> dict[str, Any]:
    return {
        "key": rule.key,
        "scope": rule.scope,
        "priority": rule.priority,
        "text": rule.text,
    }


def _correction_dict(correction: Correction) -> dict[str, Any]:
    return {
        "key": correction.key,
        "text": correction.text,
        "source": correction.source,
        "created_at": iso(correction.created_at),
    }


def _due_dict(item: DueItem, now: datetime) -> dict[str, Any]:
    return {
        "key": item.key,
        "title": item.title,
        "due_at": iso(item.due_at),
        "overdue": item.due_at < now,
    }
