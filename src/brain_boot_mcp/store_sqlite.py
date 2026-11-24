"""SQLite implementation of BrainStore.

Uses only the standard library. Full text search goes through FTS5 when the
SQLite build has it and falls back to a token-count scan when it does not, so
the demo runs on any Python.

Timestamps are stored as canonical ISO strings (see models.iso), which sort
correctly as text.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from .models import (
    ANY_AGENT,
    KIND_CORRECTION,
    KIND_MEMORY,
    KIND_RULE,
    PROPOSAL_PENDING,
    Correction,
    DueItem,
    MemoryHit,
    PackLine,
    Proposal,
    Receipt,
    Rule,
    iso,
    parse_iso,
)
from .store import BrainStore, StoreError

SCHEMA = """
create table if not exists rules (
    id          integer primary key autoincrement,
    version     integer not null default 1,
    scope       text    not null default '*',
    priority    integer not null default 100,
    text        text    not null,
    norm_text   text    not null,
    status      text    not null default 'active' check (status in ('active', 'retired')),
    created_at  text    not null,
    updated_at  text    not null
);
create index if not exists rules_active_idx on rules (status, scope, priority);

create table if not exists corrections (
    id          integer primary key autoincrement,
    agent_key   text not null default '*',
    text        text not null,
    source      text not null default 'human',
    created_at  text not null
);
create index if not exists corrections_recent_idx on corrections (created_at);

create table if not exists due_items (
    id          integer primary key autoincrement,
    agent_key   text not null default '*',
    title       text not null,
    due_at      text not null,
    done_at     text,
    created_at  text not null
);
create index if not exists due_items_open_idx on due_items (done_at, due_at);

create table if not exists memories (
    id          integer primary key autoincrement,
    topic       text not null default '',
    text        text not null,
    created_at  text not null
);

create table if not exists boot_receipts (
    id          integer primary key autoincrement,
    pack_id     text not null,
    agent_key   text not null,
    source      text not null,
    created_at  text not null,
    lines_json  text not null
);
create index if not exists boot_receipts_pack_idx on boot_receipts (pack_id, agent_key);

create table if not exists rule_proposals (
    id          integer primary key autoincrement,
    agent_key   text not null,
    text        text not null,
    norm_text   text not null,
    rationale   text not null default '',
    scope       text not null default '*',
    status      text not null default 'pending'
                check (status in ('pending', 'approved', 'rejected')),
    created_at  text not null,
    resolved_at text,
    rule_id     integer references rules (id)
);
"""

FTS_TABLE = (
    "create virtual table if not exists search_index using fts5("
    "body, kind unindexed, ref_id unindexed, created_at unindexed)"
)
PLAIN_TABLE = (
    "create table if not exists search_index ("
    "body text not null, kind text not null, ref_id integer not null, created_at text not null)"
)


class SqliteStore(BrainStore):
    def __init__(self, path: str = ":memory:", use_fts: bool | None = None) -> None:
        """Open (or create) a database file.

        use_fts: True forces FTS5, False forces the plain scan, None picks FTS5
        when available. The choice only matters the first time a file is
        created; after that the existing search_index table decides.
        """
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("pragma foreign_keys = on")
        self._lock = threading.RLock()
        self._want_fts = use_fts
        self.fts_enabled = False

    # ---- lifecycle -------------------------------------------------------
    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            existing = self._conn.execute(
                "select sql from sqlite_master where name = 'search_index'"
            ).fetchone()
            if existing is not None:
                self.fts_enabled = "fts5" in (existing["sql"] or "").lower()
                return
            if self._want_fts is False:
                self._conn.execute(PLAIN_TABLE)
                return
            try:
                self._conn.execute(FTS_TABLE)
                self.fts_enabled = True
            except sqlite3.OperationalError:
                if self._want_fts is True:
                    raise
                self._conn.execute(PLAIN_TABLE)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- helpers ---------------------------------------------------------
    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _insert(self, sql: str, params: Sequence[Any]) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def _index(self, kind: str, ref_id: int, body: str, created_at: str) -> None:
        with self._lock:
            self._unindex(kind, ref_id)
            self._conn.execute(
                "insert into search_index (body, kind, ref_id, created_at) values (?, ?, ?, ?)",
                (body, kind, ref_id, created_at),
            )

    def _unindex(self, kind: str, ref_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "delete from search_index where kind = ? and ref_id = ?", (kind, ref_id)
            )

    @staticmethod
    def _rule(row: sqlite3.Row) -> Rule:
        return Rule(
            id=row["id"],
            version=row["version"],
            scope=row["scope"],
            priority=row["priority"],
            text=row["text"],
            status=row["status"],
            updated_at=parse_iso(row["updated_at"]),
        )

    @staticmethod
    def _proposal(row: sqlite3.Row) -> Proposal:
        return Proposal(
            id=row["id"],
            agent_key=row["agent_key"],
            text=row["text"],
            rationale=row["rationale"],
            scope=row["scope"],
            status=row["status"],
            created_at=parse_iso(row["created_at"]),
            resolved_at=parse_iso(row["resolved_at"]) if row["resolved_at"] else None,
            rule_id=row["rule_id"],
        )

    # ---- pack inputs -----------------------------------------------------
    def active_rules(self, agent_key: str) -> list[Rule]:
        rows = self._all(
            "select * from rules where status = 'active' and scope in (?, ?) "
            "order by priority, id",
            (ANY_AGENT, agent_key),
        )
        return [self._rule(r) for r in rows]

    def recent_corrections(
        self, agent_key: str, since: datetime, limit: int
    ) -> list[Correction]:
        rows = self._all(
            "select * from corrections where agent_key in (?, ?) and created_at >= ? "
            "order by created_at desc, id desc limit ?",
            (ANY_AGENT, agent_key, iso(since), limit),
        )
        return [
            Correction(r["id"], r["agent_key"], r["text"], r["source"], parse_iso(r["created_at"]))
            for r in rows
        ]

    def due_items(self, agent_key: str, until: datetime, limit: int) -> list[DueItem]:
        rows = self._all(
            "select * from due_items where done_at is null and agent_key in (?, ?) "
            "and due_at <= ? order by due_at, id limit ?",
            (ANY_AGENT, agent_key, iso(until), limit),
        )
        return [
            DueItem(r["id"], r["agent_key"], r["title"], parse_iso(r["due_at"])) for r in rows
        ]

    # ---- receipts --------------------------------------------------------
    def save_receipt(self, receipt: Receipt) -> None:
        self._insert(
            "insert into boot_receipts (pack_id, agent_key, source, created_at, lines_json) "
            "values (?, ?, ?, ?, ?)",
            (
                receipt.pack_id,
                receipt.agent_key,
                receipt.source,
                iso(receipt.created_at),
                json.dumps([line.as_dict() for line in receipt.lines]),
            ),
        )

    def latest_receipt(self, pack_id: str, agent_key: str) -> Receipt | None:
        row = self._one(
            "select * from boot_receipts where pack_id = ? and agent_key = ? "
            "order by id desc limit 1",
            (pack_id, agent_key),
        )
        if row is None:
            return None
        lines = tuple(
            PackLine(item["key"], item["kind"], item["text"])
            for item in json.loads(row["lines_json"])
        )
        return Receipt(
            row["pack_id"], row["agent_key"], row["source"], parse_iso(row["created_at"]), lines
        )

    def count_receipts(self, agent_key: str) -> int:
        row = self._one(
            "select count(*) as n from boot_receipts where agent_key = ?", (agent_key,)
        )
        return int(row["n"]) if row else 0

    # ---- recall ----------------------------------------------------------
    def search(
        self, tokens: Sequence[str], limit: int, kinds: Sequence[str]
    ) -> list[MemoryHit]:
        if not tokens or not kinds:
            return []
        kind_marks = ", ".join("?" for _ in kinds)
        if self.fts_enabled:
            match = " OR ".join(f'"{token}"' for token in tokens)
            rows = self._all(
                "select kind, ref_id, body, created_at, bm25(search_index) as rank "
                f"from search_index where search_index match ? and kind in ({kind_marks}) "
                "order by rank, created_at desc limit ?",
                (match, *kinds, limit),
            )
            return [
                MemoryHit(r["kind"], int(r["ref_id"]), r["body"], -float(r["rank"]),
                          parse_iso(r["created_at"]))
                for r in rows
            ]
        rows = self._all(
            f"select kind, ref_id, body, created_at from search_index where kind in ({kind_marks})",
            tuple(kinds),
        )
        hits: list[MemoryHit] = []
        for r in rows:
            words = set(_words(r["body"]))
            matched = sum(1 for token in tokens if token in words)
            if matched:
                hits.append(
                    MemoryHit(r["kind"], int(r["ref_id"]), r["body"], matched / len(tokens),
                              parse_iso(r["created_at"]))
                )
        hits.sort(key=lambda h: (-h.score, -h.created_at.timestamp(), h.ref_id))
        return hits[:limit]

    # ---- proposals -------------------------------------------------------
    def find_active_rule_by_norm(self, norm_text: str) -> Rule | None:
        row = self._one(
            "select * from rules where status = 'active' and norm_text = ? order by id limit 1",
            (norm_text,),
        )
        return self._rule(row) if row else None

    def find_pending_proposal_by_norm(self, norm_text: str) -> Proposal | None:
        row = self._one(
            "select * from rule_proposals where status = 'pending' and norm_text = ? "
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
        new_id = self._insert(
            "insert into rule_proposals (agent_key, text, norm_text, rationale, scope, status, "
            "created_at) values (?, ?, ?, ?, ?, ?, ?)",
            (agent_key, text, norm_text, rationale, scope, PROPOSAL_PENDING, iso(now)),
        )
        proposal = self.get_proposal(new_id)
        assert proposal is not None
        return proposal

    def get_proposal(self, proposal_id: int) -> Proposal | None:
        row = self._one("select * from rule_proposals where id = ?", (proposal_id,))
        return self._proposal(row) if row else None

    def list_proposals(self, status: str | None = None) -> list[Proposal]:
        if status is None:
            rows = self._all("select * from rule_proposals order by id")
        else:
            rows = self._all(
                "select * from rule_proposals where status = ? order by id", (status,)
            )
        return [self._proposal(r) for r in rows]

    def resolve_proposal(
        self, proposal_id: int, status: str, now: datetime, rule_id: int | None
    ) -> Proposal:
        with self._lock:
            changed = self._conn.execute(
                "update rule_proposals set status = ?, resolved_at = ?, rule_id = ? "
                "where id = ? and status = 'pending'",
                (status, iso(now), rule_id, proposal_id),
            ).rowcount
        if changed != 1:
            raise StoreError(f"proposal {proposal_id} is not pending")
        proposal = self.get_proposal(proposal_id)
        assert proposal is not None
        return proposal

    # ---- human side writes ----------------------------------------------
    def _get_rule(self, rule_id: int) -> Rule:
        row = self._one("select * from rules where id = ?", (rule_id,))
        if row is None:
            raise StoreError(f"rule {rule_id} does not exist")
        return self._rule(row)

    def add_rule(
        self, text: str, norm_text: str, scope: str, priority: int, now: datetime
    ) -> Rule:
        stamp = iso(now)
        new_id = self._insert(
            "insert into rules (version, scope, priority, text, norm_text, status, created_at, "
            "updated_at) values (1, ?, ?, ?, ?, 'active', ?, ?)",
            (scope, priority, text, norm_text, stamp, stamp),
        )
        self._index(KIND_RULE, new_id, text, stamp)
        return self._get_rule(new_id)

    def update_rule(self, rule_id: int, text: str, norm_text: str, now: datetime) -> Rule:
        current = self._get_rule(rule_id)
        if current.status != "active":
            raise StoreError(f"rule {rule_id} is retired")
        stamp = iso(now)
        with self._lock:
            self._conn.execute(
                "update rules set text = ?, norm_text = ?, version = version + 1, updated_at = ? "
                "where id = ?",
                (text, norm_text, stamp, rule_id),
            )
        self._index(KIND_RULE, rule_id, text, stamp)
        return self._get_rule(rule_id)

    def retire_rule(self, rule_id: int, now: datetime) -> None:
        self._get_rule(rule_id)
        with self._lock:
            self._conn.execute(
                "update rules set status = 'retired', updated_at = ? where id = ?",
                (iso(now), rule_id),
            )
        self._unindex(KIND_RULE, rule_id)

    def add_correction(
        self, text: str, agent_key: str, source: str, now: datetime
    ) -> Correction:
        stamp = iso(now)
        new_id = self._insert(
            "insert into corrections (agent_key, text, source, created_at) values (?, ?, ?, ?)",
            (agent_key, text, source, stamp),
        )
        self._index(KIND_CORRECTION, new_id, text, stamp)
        return Correction(new_id, agent_key, text, source, parse_iso(stamp))

    def add_due_item(
        self, title: str, due_at: datetime, agent_key: str, now: datetime
    ) -> DueItem:
        new_id = self._insert(
            "insert into due_items (agent_key, title, due_at, created_at) values (?, ?, ?, ?)",
            (agent_key, title, iso(due_at), iso(now)),
        )
        return DueItem(new_id, agent_key, title, parse_iso(iso(due_at)))

    def complete_due_item(self, item_id: int, now: datetime) -> None:
        with self._lock:
            changed = self._conn.execute(
                "update due_items set done_at = ? where id = ? and done_at is null",
                (iso(now), item_id),
            ).rowcount
        if changed != 1:
            raise StoreError(f"due item {item_id} is not open")

    def add_memory(self, text: str, topic: str, now: datetime) -> int:
        stamp = iso(now)
        new_id = self._insert(
            "insert into memories (topic, text, created_at) values (?, ?, ?)",
            (topic, text, stamp),
        )
        self._index(KIND_MEMORY, new_id, text, stamp)
        return new_id


def _words(text: str) -> list[str]:
    out: list[str] = []
    word: list[str] = []
    for char in text.lower():
        if char.isalnum():
            word.append(char)
        elif word:
            out.append("".join(word))
            word = []
    if word:
        out.append("".join(word))
    return out
