"""Postgres backend and SQL function tests.

Skipped unless BRAIN_BOOT_TEST_PG_DSN points at a throwaway database, for example

    docker run -d --rm -e POSTGRES_PASSWORD=test -p 54329:5432 postgres:16
    BRAIN_BOOT_TEST_PG_DSN=postgresql://postgres:test@localhost:54329/postgres pytest

Every test drops and recreates the brain_ objects, so never point this at a
database you care about.
"""

from __future__ import annotations

import os

import pytest

from brain_boot_mcp.seed import seed
from brain_boot_mcp.service import BrainService

DSN = os.environ.get("BRAIN_BOOT_TEST_PG_DSN", "")
psycopg = pytest.importorskip("psycopg") if DSN else None
pytestmark = pytest.mark.skipif(not DSN, reason="BRAIN_BOOT_TEST_PG_DSN is not set")

DROP = """
drop view if exists brain_search_docs;
drop table if exists brain_rule_proposals, brain_boot_receipts, brain_memories,
    brain_due_items, brain_corrections, brain_rules cascade;
"""


@pytest.fixture
def pg(clock):
    from brain_boot_mcp.store_pg import PostgresStore

    store = PostgresStore(DSN)
    store._conn.execute(DROP)
    store.init_schema()
    store.init_schema()  # applying twice must be harmless
    service = BrainService(store, clock=clock)
    seed(service)
    yield service
    store.close()


def _sql(service: BrainService, query: str, *params):
    row = service.store._conn.execute(query, params).fetchone()
    return next(iter(row.values()))


def test_service_contract_on_postgres(pg: BrainService, clock):
    pack = pg.boot("ops_agent")
    assert pack["counts"] == {"standing_rules": 4, "recent_corrections": 2, "due_items": 2}
    assert [d["overdue"] for d in pack["due_items"]] == [True, False]
    assert pg.pack_check(pack["pack_id"], "ops_agent")["status"] == "UNCHANGED"

    clock.advance(minutes=1)
    pg.add_correction("Postgres side correction.", agent_key="ops_agent")
    first = pg.store.active_rules("ops_agent")[0]
    pg.update_rule(first.id, "Edited rule text.")
    changed = pg.pack_check(pack["pack_id"], "ops_agent")
    assert changed["status"] == "CHANGED"
    assert {line["kind"] for line in changed["added"]} == {"correction", "rule"}
    assert [line["key"] for line in changed["removed"]] == [f"rule:{first.id}:v1"]
    assert pg.pack_check("pk_0123456789abcdef", "ops_agent")["status"] == "UNKNOWN_PACK"


def test_recall_and_proposals_on_postgres(pg: BrainService):
    top = pg.recall("invoice reminders due date")["hits"][0]
    assert top["kind"] == "memory" and "Invoice reminders" in top["text"]
    assert pg.recall("zeppelin")["hits"] == []
    assert [h["kind"] for h in pg.recall("apology", kinds=["correction"])["hits"]] == ["correction"]

    proposed = pg.propose_rule("ops_agent", "Always send the estimate as a PDF.", "asked twice")
    assert proposed["status"] == "pending"
    assert pg.propose_rule("ops_agent", "always send the estimate as a pdf")["status"] == (
        "already_pending"
    )
    rule = pg.approve_proposal(proposed["proposal_id"])
    assert any(r["key"] == rule.key for r in pg.boot("ops_agent")["standing_rules"])
    assert pg.propose_rule("ops_agent", "Always send the estimate as a PDF")["status"] == (
        "duplicate_of_rule"
    )


@pytest.mark.parametrize("agent", ["ops_agent", "billing_agent", "nobody_yet"])
def test_sql_brain_boot_matches_python_pack(pg: BrainService, clock, agent: str):
    python_pack = pg.boot(agent)
    sql_pack = _sql(pg, "select brain_boot(%s, %s)", agent, clock())
    assert sql_pack["pack_id"] == python_pack["pack_id"]
    for section in ("standing_rules", "recent_corrections", "due_items", "counts"):
        assert sql_pack[section] == python_pack[section], section
    assert sql_pack["generated_at"] == python_pack["generated_at"]
    assert sql_pack["instructions"] == python_pack["instructions"]


def test_sql_and_python_pack_check_understand_each_others_receipts(pg: BrainService, clock):
    from_python = pg.boot("ops_agent")["pack_id"]
    clock.advance(minutes=2)
    pg.add_correction("Seen by both paths.")

    sql_check = _sql(pg, "select brain_pack_check(%s, %s, %s)", from_python, "ops_agent", clock())
    python_check = pg.pack_check(from_python, "ops_agent")
    assert sql_check["status"] == python_check["status"] == "CHANGED"
    assert sql_check["added"] == python_check["added"]
    assert sql_check["removed"] == python_check["removed"]
    assert sql_check["current_pack_id"] == python_check["current_pack_id"]

    # The receipt written by the SQL check is good enough for the Python path.
    assert pg.pack_check(sql_check["current_pack_id"], "ops_agent")["status"] == "UNCHANGED"
    unknown = _sql(
        pg, "select brain_pack_check(%s, %s, %s)", "pk_0123456789abcdef", "ops_agent", clock()
    )
    assert unknown["status"] == "UNKNOWN_PACK"


def test_sql_propose_and_approve(pg: BrainService, clock):
    before = pg.boot("ops_agent")["pack_id"]
    proposed = _sql(
        pg, "select brain_propose_rule(%s, %s, %s)", "ops_agent", "Log every callback.", "why"
    )
    assert proposed["status"] == "pending"
    again = _sql(pg, "select brain_propose_rule(%s, %s)", "ops_agent", "log every  callback")
    assert again["status"] == "already_pending"
    assert pg.pack_check(before, "ops_agent")["status"] == "UNCHANGED"

    rule_id = _sql(pg, "select brain_approve_proposal(%s)", proposed["proposal_id"])
    changed = pg.pack_check(before, "ops_agent")
    assert [line["key"] for line in changed["added"]] == [f"rule:{rule_id}:v1"]
    duplicate = _sql(pg, "select brain_propose_rule(%s, %s)", "ops_agent", "Log every callback")
    assert duplicate["status"] == "duplicate_of_rule"


def test_sql_functions_reject_bad_input(pg: BrainService):
    import psycopg

    for query, params in [
        ("select brain_boot(%s)", ("not a key",)),
        ("select brain_pack_check(%s, %s)", ("garbage", "ops_agent")),
        ("select brain_propose_rule(%s, %s)", ("ops_agent", "   ")),
        ("select brain_approve_proposal(%s)", (999,)),
    ]:
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            pg.store._conn.execute(query, params)


def test_brain_norm_matches_python(pg: BrainService):
    from brain_boot_mcp.models import normalize_rule_text

    for text in ["  Always   Confirm\nthe date. ", "Stop!!", "plain", "Trailing space .  "]:
        assert _sql(pg, "select brain_norm(%s)", text) == normalize_rule_text(text)
