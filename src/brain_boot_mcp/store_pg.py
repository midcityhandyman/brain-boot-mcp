"""Postgres implementation of BrainStore.

Works against the tables in sql/001_schema.sql. Needs the optional dependency
(pip install "brain-boot-mcp[postgres]"). The pack is assembled in Python by
BrainService exactly as for SQLite; sql/002_functions.sql offers the same
behaviour as database functions for callers that prefer plain RPC, and
tests/test_postgres.py checks that both paths produce the same pack id.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    ANY_AGENT,
    Correction,
    DueItem,
    MemoryHit,
    PackLine,
    Proposal,
    Receipt,
    Rule,
    to_utc,
)
from .store import BrainStore, StoreError

SQL_FILES = ("001_schema.sql", "002_functions.sql")


def default_sql_dir() -> Path:
    """The sql/ folder of a source checkout (not present in an installed wheel)."""
    return Path(__file__).resolve().parents[2] / "sql"


class PostgresStore(BrainStore):
    def __init__(self, dsn: str, sql_dir: Path | None = None) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise StoreError(
                'the Postgres backend needs psycopg: pip install "brain-boot-mcp[postgres]"'
            ) from exc
        self._conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self._lock = threading.RLock()
        self._sql_dir = sql_dir or default_sql_dir()

    # ---- lifecycle -------------------------------------------------------
    def init_schema(self) -> None:
        for name in SQL_FILES:
            path = self._sql_dir / name
            if not path.exists():
                raise StoreError(f"cannot find {path}; apply the files in sql/ with psql instead")
            with self._lock:
                self._conn.execute(path.read_text(encoding="utf-8"))  # type: ignore[arg-type]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- helpers ---------------------------------------------------------
    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()  # type: ignore[arg-type]

    def _one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()  # type: ignore[arg-type]

    @staticmethod
    def _rule(row: dict[str, Any]) -> Rule:
        return Rule(
            id=row["id"],
            version=row["version"],
            scope=row["scope"],
            priority=row["priority"],
            text=row["text"],
            status=row["status"],
            updated_at=to_utc(row["updated_at"]),
        )

    @staticmethod
    def _proposal(row: dict[str, Any]) -> Proposal:
        return Proposal(
            id=row["id"],
            agent_key=row["agent_key"],
            text=row["text"],
            rationale=row["rationale"],
            scope=row["scope"],
            status=row["status"],
            created_at=to_utc(row["created_at"]),
            resolved_at=to_utc(row["resolved_at"]) if row["resolved_at"] else None,
            rule_id=row["rule_id"],
        )

    # ---- pack inputs -----------------------------------------------------
    def active_rules(self, agent_key: str) -> list[Rule]:
        rows = self._all(
            "select * from brain_rules where status = 'active' and scope in (%s, %s) "
            "order by priority, id",
            (ANY_AGENT, agent_key),
        )
        return [self._rule(r) for r in rows]

    def recent_corrections(
        self, agent_key: str, since: datetime, limit: int
    ) -> list[Correction]:
        rows = self._all(
            "select * from brain_corrections where agent_key in (%s, %s) and created_at >= %s "
            "order by created_at desc, id desc limit %s",
            (ANY_AGENT, agent_key, to_utc(since), limit),
        )
        return [
            Correction(r["id"], r["agent_key"], r["text"], r["source"], to_utc(r["created_at"]))
            for r in rows
        ]

    def due_items(self, agent_key: str, until: datetime, limit: int) -> list[DueItem]:
        rows = self._all(
            "select * from brain_due_items where done_at is null and agent_key in (%s, %s) "
            "and due_at <= %s order by due_at, id limit %s",
            (ANY_AGENT, agent_key, to_utc(until), limit),
        )
        return [DueItem(r["id"], r["agent_key"], r["title"], to_utc(r["due_at"])) for r in rows]

    # ---- receipts --------------------------------------------------------
    def save_receipt(self, receipt: Receipt) -> None:
        self._all(
            "insert into brain_boot_receipts (pack_id, agent_key, source, created_at, lines) "
            "values (%s, %s, %s, %s, %s::jsonb) returning id",
            (
                receipt.pack_id,
                receipt.agent_key,
                receipt.source,
                to_utc(receipt.created_at),
                json.dumps([line.as_dict() for line in receipt.lines]),
            ),
        )

    def latest_receipt(self, pack_id: str, agent_key: str) -> Receipt | None:
        row = self._one(
            "select * from brain_boot_receipts where pack_id = %s and agent_key = %s "
            "order by id desc limit 1",
            (pack_id, agent_key),
        )
        if row is None:
            return None
        lines = tuple(PackLine(i["key"], i["kind"], i["text"]) for i in row["lines"])
        return Receipt(
            row["pack_id"], row["agent_key"], row["source"], to_utc(row["created_at"]), lines
        )

    def count_receipts(self, agent_key: str) -> int:
        row = self._one(
            "select count(*) as n from brain_boot_receipts where agent_key = %s", (agent_key,)
        )
        return int(row["n"]) if row else 0

    # ---- recall ----------------------------------------------------------
    def search(
        self, tokens: Sequence[str], limit: int, kinds: Sequence[str]
    ) -> list[MemoryHit]:
        if not tokens or not kinds:
            return []
        rows = self._all(
            "select * from brain_recall(%s, %s, %s::text[])",
            (" ".join(tokens), limit, list(kinds)),
        )
        return [
            MemoryHit(r["kind"], int(r["ref_id"]), r["body"], float(r["score"]),
                      to_utc(r["created_at"]))
            for r in rows
        ]

    # ---- proposals -------------------------------------------------------
    def find_active_rule_by_norm(self, norm_text: str) -> Rule | None:
        row = self._one(
            "select * from brain_rules where status = 'active' and norm_text = %s "
            "order by id limit 1",
            (norm_text,),
        )
        return self._rule(row) if row else None

    def find_pending_proposal_by_norm(self, norm_text: str) -> Proposal | None:
        row = self._one(
            "select * from brain_rule_proposals where status = 'pending' and norm_text = %s "
            "order by id limit 1",
            (norm_text,),
        )
        return self._proposal(row) if row else None

    def insert_proposal(
        self,
        agent_key: str,
        text: str,
        norm_text: str,
        rationale: str,
        scope: str,
        now: datetime,
    ) -> Proposal:
        row = self._one(
            "insert into brain_rule_proposals (agent_key, text, norm_text, rationale, scope, "
            "created_at) values (%s, %s, %s, %s, %s, %s) returning *",
            (agent_key, text, norm_text, rationale, scope, to_utc(now)),
        )
        assert row is not None
        return self._proposal(row)

    def get_proposal(self, proposal_id: int) -> Proposal | None:
        row = self._one("select * from brain_rule_proposals where id = %s", (proposal_id,))
        return self._proposal(row) if row else None

    def list_proposals(self, status: str | None = None) -> list[Proposal]:
        if status is None:
            rows = self._all("select * from brain_rule_proposals order by id")
        else:
            rows = self._all(
                "select * from brain_rule_proposals where status = %s order by id", (status,)
            )
        return [self._proposal(r) for r in rows]

    def resolve_proposal(
        self, proposal_id: int, status: str, now: datetime, rule_id: int | None
    ) -> Proposal:
        row = self._one(
            "update brain_rule_proposals set status = %s, resolved_at = %s, rule_id = %s "
            "where id = %s and status = 'pending' returning *",
            (status, to_utc(now), rule_id, proposal_id),
        )
        if row is None:
            raise StoreError(f"proposal {proposal_id} is not pending")
        return self._proposal(row)

    # ---- human side writes ----------------------------------------------
    def add_rule(
        self, text: str, norm_text: str, scope: str, priority: int, now: datetime
    ) -> Rule:
        row = self._one(
            "insert into brain_rules (scope, priority, text, norm_text, created_at, updated_at) "
            "values (%s, %s, %s, %s, %s, %s) returning *",
            (scope, priority, text, norm_text, to_utc(now), to_utc(now)),
        )
        assert row is not None
        return self._rule(row)

    def update_rule(self, rule_id: int, text: str, norm_text: str, now: datetime) -> Rule:
        row = self._one(
            "update brain_rules set text = %s, norm_text = %s, version = version + 1, "
            "updated_at = %s where id = %s and status = 'active' returning *",
            (text, norm_text, to_utc(now), rule_id),
        )
        if row is None:
            raise StoreError(f"rule {rule_id} does not exist or is retired")
        return self._rule(row)

    def retire_rule(self, rule_id: int, now: datetime) -> None:
        row = self._one(
            "update brain_rules set status = 'retired', updated_at = %s where id = %s returning id",
            (to_utc(now), rule_id),
        )
        if row is None:
            raise StoreError(f"rule {rule_id} does not exist")

    def add_correction(
        self, text: str, agent_key: str, source: str, now: datetime
    ) -> Correction:
        row = self._one(
            "insert into brain_corrections (agent_key, text, source, created_at) "
            "values (%s, %s, %s, %s) returning *",
            (agent_key, text, source, to_utc(now)),
        )
        assert row is not None
        return Correction(
            row["id"], row["agent_key"], row["text"], row["source"], to_utc(row["created_at"])
        )

    def add_due_item(
        self, title: str, due_at: datetime, agent_key: str, now: datetime
    ) -> DueItem:
        row = self._one(
            "insert into brain_due_items (agent_key, title, due_at, created_at) "
            "values (%s, %s, %s, %s) returning *",
            (agent_key, title, to_utc(due_at), to_utc(now)),
        )
        assert row is not None
        return DueItem(row["id"], row["agent_key"], row["title"], to_utc(row["due_at"]))

    def complete_due_item(self, item_id: int, now: datetime) -> None:
        row = self._one(
            "update brain_due_items set done_at = %s where id = %s and done_at is null "
            "returning id",
            (to_utc(now), item_id),
        )
        if row is None:
            raise StoreError(f"due item {item_id} is not open")

    def add_memory(self, text: str, topic: str, now: datetime) -> int:
        row = self._one(
            "insert into brain_memories (topic, text, created_at) values (%s, %s, %s) returning id",
            (topic, text, to_utc(now)),
        )
        assert row is not None
        return int(row["id"])
