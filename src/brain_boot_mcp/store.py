"""The storage interface.

BrainService only ever talks to this class. Two implementations ship:
SqliteStore (the offline demo and the tests) and PostgresStore (the same
contract over the schema in sql/). Adding a third backend means implementing
these methods and nothing else.

Every method that depends on "now" takes it as an argument. Stores never read
the clock, which keeps packs reproducible in tests and demos.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime

from .models import Correction, DueItem, MemoryHit, Proposal, Receipt, Rule


class StoreError(Exception):
    """A storage level failure the caller can report (missing row and so on)."""


class BrainStore(ABC):
    # ---- lifecycle -------------------------------------------------------
    @abstractmethod
    def init_schema(self) -> None:
        """Create tables if they do not exist. Safe to call repeatedly."""

    @abstractmethod
    def close(self) -> None: ...

    # ---- pack inputs -----------------------------------------------------
    @abstractmethod
    def active_rules(self, agent_key: str) -> list[Rule]:
        """Active rules scoped to this agent or to every agent, by priority."""

    @abstractmethod
    def recent_corrections(
        self, agent_key: str, since: datetime, limit: int
    ) -> list[Correction]:
        """Corrections for this agent (or all agents) since a time, newest first."""

    @abstractmethod
    def due_items(self, agent_key: str, until: datetime, limit: int) -> list[DueItem]:
        """Open items due at or before a time, soonest first."""

    # ---- receipts --------------------------------------------------------
    @abstractmethod
    def save_receipt(self, receipt: Receipt) -> None: ...

    @abstractmethod
    def latest_receipt(self, pack_id: str, agent_key: str) -> Receipt | None: ...

    @abstractmethod
    def count_receipts(self, agent_key: str) -> int: ...

    # ---- recall ----------------------------------------------------------
    @abstractmethod
    def search(
        self, tokens: Sequence[str], limit: int, kinds: Sequence[str]
    ) -> list[MemoryHit]:
        """Rank stored text against lowercase alphanumeric tokens (OR match)."""

    # ---- proposals -------------------------------------------------------
    @abstractmethod
    def find_active_rule_by_norm(self, norm_text: str) -> Rule | None: ...

    @abstractmethod
    def find_pending_proposal_by_norm(self, norm_text: str) -> Proposal | None: ...

    @abstractmethod
    def insert_proposal(
        self,
        agent_key: str,
        text: str,
        norm_text: str,
        rationale: str,
        scope: str,
        now: datetime,
    ) -> Proposal: ...

    @abstractmethod
    def get_proposal(self, proposal_id: int) -> Proposal | None: ...

    @abstractmethod
    def list_proposals(self, status: str | None = None) -> list[Proposal]: ...

    @abstractmethod
    def resolve_proposal(
        self, proposal_id: int, status: str, now: datetime, rule_id: int | None
    ) -> Proposal: ...

    # ---- human side writes ----------------------------------------------
    @abstractmethod
    def add_rule(
        self, text: str, norm_text: str, scope: str, priority: int, now: datetime
    ) -> Rule: ...

    @abstractmethod
    def update_rule(
        self, rule_id: int, text: str, norm_text: str, now: datetime
    ) -> Rule:
        """Replace a rule's text and bump its version."""

    @abstractmethod
    def retire_rule(self, rule_id: int, now: datetime) -> None: ...

    @abstractmethod
    def add_correction(
        self, text: str, agent_key: str, source: str, now: datetime
    ) -> Correction: ...

    @abstractmethod
    def add_due_item(
        self, title: str, due_at: datetime, agent_key: str, now: datetime
    ) -> DueItem: ...

    @abstractmethod
    def complete_due_item(self, item_id: int, now: datetime) -> None: ...

    @abstractmethod
    def add_memory(self, text: str, topic: str, now: datetime) -> int: ...
