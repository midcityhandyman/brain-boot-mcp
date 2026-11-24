from __future__ import annotations

import pytest

from brain_boot_mcp.seed import seed
from brain_boot_mcp.service import BrainService
from brain_boot_mcp.store import BrainStore, StoreError
from brain_boot_mcp.store_sqlite import SqliteStore


def _service(clock, use_fts: bool | None, path: str = ":memory:") -> BrainService:
    store = SqliteStore(path, use_fts=use_fts)
    store.init_schema()
    return BrainService(store, clock=clock)


def test_sqlite_store_implements_the_interface():
    assert issubclass(SqliteStore, BrainStore)
    assert not getattr(SqliteStore, "__abstractmethods__", None)


def test_init_schema_is_idempotent(clock):
    service = _service(clock, None)
    service.store.init_schema()
    service.store.init_schema()
    assert service.boot("ops_agent")["counts"]["standing_rules"] == 0


@pytest.mark.parametrize("use_fts", [True, False])
def test_search_backends_agree_on_the_top_hit(clock, use_fts: bool):
    service = _service(clock, use_fts)
    assert service.store.fts_enabled is use_fts
    seed(service)
    top = service.recall("invoice reminders due date")["hits"][0]
    assert top["kind"] == "memory"
    assert "Invoice reminders" in top["text"]
    assert top["score"] > 0
    assert service.recall("zeppelin")["hits"] == []


@pytest.mark.parametrize("use_fts", [True, False])
def test_retired_and_edited_rules_leave_the_search_index(clock, use_fts: bool):
    service = _service(clock, use_fts)
    rule = service.add_rule("Label every crate with a heliotrope sticker.")
    assert service.recall("heliotrope")["count"] == 1
    service.update_rule(rule.id, "Label every crate with a vermilion sticker.")
    assert service.recall("heliotrope")["count"] == 0
    assert service.recall("vermilion")["count"] == 1
    service.retire_rule(rule.id)
    assert service.recall("vermilion")["count"] == 0


def test_data_and_receipts_survive_reopening_the_file(clock, tmp_path):
    path = str(tmp_path / "brain.db")
    first = _service(clock, None, path)
    seed(first)
    pack = first.boot("ops_agent")
    first.store.close()

    second = _service(clock, None, path)
    assert second.pack_check(pack["pack_id"], "ops_agent")["status"] == "UNCHANGED"
    assert second.recall("gutter")["count"] >= 1
    second.store.close()


def test_store_errors_for_missing_rows(clock):
    service = _service(clock, None)
    with pytest.raises(StoreError):
        service.update_rule(42, "nope")
    with pytest.raises(StoreError):
        service.retire_rule(42)
    with pytest.raises(StoreError):
        service.complete_due_item(42)
    rule = service.add_rule("Temporary.")
    service.retire_rule(rule.id)
    with pytest.raises(StoreError):
        service.update_rule(rule.id, "Cannot edit a retired rule.")


def test_latest_receipt_round_trips_lines(clock):
    service = _service(clock, None)
    seed(service)
    pack = service.boot("ops_agent")
    receipt = service.store.latest_receipt(pack["pack_id"], "ops_agent")
    assert receipt is not None and receipt.source == "boot"
    assert len(receipt.lines) == 4 + 2 + 2
    assert service.store.latest_receipt(pack["pack_id"], "someone_else") is None
