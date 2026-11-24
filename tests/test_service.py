from __future__ import annotations

from datetime import timedelta

import pytest

from brain_boot_mcp.service import BrainError, BrainService, tokenize


# ---- boot ---------------------------------------------------------------
def test_boot_returns_scoped_rules_in_priority_order(seeded: BrainService):
    pack = seeded.boot("ops_agent")
    texts = [r["text"] for r in pack["standing_rules"]]
    assert len(texts) == 4
    assert texts[0].startswith("Never send a message")
    assert any("confirm the address" in t for t in texts)
    assert not any("refund" in t for t in texts), "billing rule leaked into ops pack"
    assert [r["priority"] for r in pack["standing_rules"]] == [10, 20, 30, 40]


def test_boot_for_other_agent_gets_its_own_rules_and_id(seeded: BrainService):
    ops = seeded.boot("ops_agent")
    billing = seeded.boot("billing_agent")
    assert any("refund" in r["text"] for r in billing["standing_rules"])
    assert ops["pack_id"] != billing["pack_id"]


def test_boot_includes_only_recent_corrections_newest_first(seeded: BrainService):
    pack = seeded.boot("ops_agent")
    texts = [c["text"] for c in pack["recent_corrections"]]
    assert len(texts) == 2
    assert "double booked" in texts[0]
    assert "apology" in texts[1]
    assert not any("Old correction" in t for t in texts)


def test_boot_includes_overdue_and_soon_due_but_not_far_future(seeded: BrainService):
    due = seeded.boot("ops_agent")["due_items"]
    assert [d["overdue"] for d in due] == [True, False]
    assert "Jordan Example" in due[0]["title"]
    assert not any("rental" in d["title"] for d in due)


def test_boot_counts_and_shape(seeded: BrainService):
    pack = seeded.boot("ops_agent")
    assert pack["counts"] == {"standing_rules": 4, "recent_corrections": 2, "due_items": 2}
    assert pack["generated_at"] == "2026-01-05T14:00:00Z"
    assert pack["pack_id"].startswith("pk_")
    assert "pack_check" in pack["instructions"]


def test_boot_is_stable_and_writes_a_receipt_each_time(seeded: BrainService, clock):
    first = seeded.boot("ops_agent")
    clock.advance(minutes=5)
    second = seeded.boot("ops_agent")
    assert first["pack_id"] == second["pack_id"]
    assert seeded.store.count_receipts("ops_agent") == 2


def test_boot_on_empty_store_still_works(service: BrainService):
    pack = service.boot("fresh_agent")
    assert pack["counts"] == {"standing_rules": 0, "recent_corrections": 0, "due_items": 0}
    assert pack["pack_id"] == "pk_e3b0c44298fc1c14"


@pytest.mark.parametrize("bad", ["", "   ", "has space", "-leading", "x" * 64, "semi;colon"])
def test_boot_rejects_bad_agent_keys(service: BrainService, bad: str):
    with pytest.raises(BrainError):
        service.boot(bad)


def test_agent_key_is_case_folded(seeded: BrainService):
    assert seeded.boot("OPS_Agent")["agent_key"] == "ops_agent"


# ---- pack_check ---------------------------------------------------------
def test_pack_check_unchanged(seeded: BrainService):
    pack = seeded.boot("ops_agent")
    result = seeded.pack_check(pack["pack_id"], "ops_agent")
    assert result["status"] == "UNCHANGED"
    assert result["current_pack_id"] == pack["pack_id"]
    assert result["added"] == [] and result["removed"] == []


def test_pack_check_reports_new_correction(seeded: BrainService, clock):
    pack = seeded.boot("ops_agent")
    clock.advance(minutes=10)
    seeded.add_correction("Use the customer's first name only.", agent_key="ops_agent")
    result = seeded.pack_check(pack["pack_id"], "ops_agent")
    assert result["status"] == "CHANGED"
    assert [line["kind"] for line in result["added"]] == ["correction"]
    assert result["added"][0]["text"] == "Use the customer's first name only."
    assert result["removed"] == []
    assert result["current_pack_id"] != pack["pack_id"]


def test_pack_check_reports_rule_edit_and_retirement(seeded: BrainService):
    pack = seeded.boot("ops_agent")
    first = seeded.store.active_rules("ops_agent")[0]
    last = seeded.store.active_rules("ops_agent")[-1]
    seeded.update_rule(first.id, "Never send anything to a customer without approval.")
    seeded.retire_rule(last.id)
    result = seeded.pack_check(pack["pack_id"], "ops_agent")
    assert result["status"] == "CHANGED"
    assert [line["key"] for line in result["added"]] == [f"rule:{first.id}:v2"]
    assert sorted(line["key"] for line in result["removed"]) == sorted(
        [f"rule:{first.id}:v1", f"rule:{last.id}:v1"]
    )


def test_pack_check_chain_uses_current_pack_id(seeded: BrainService):
    pack = seeded.boot("ops_agent")
    seeded.add_correction("First change.")
    step1 = seeded.pack_check(pack["pack_id"], "ops_agent")
    assert seeded.pack_check(step1["current_pack_id"], "ops_agent")["status"] == "UNCHANGED"
    seeded.add_correction("Second change.")
    step2 = seeded.pack_check(step1["current_pack_id"], "ops_agent")
    assert step2["status"] == "CHANGED"
    assert [line["text"] for line in step2["added"]] == ["Second change."]


def test_pack_check_ignores_changes_scoped_to_another_agent(seeded: BrainService):
    pack = seeded.boot("ops_agent")
    seeded.add_correction("Only for billing.", agent_key="billing_agent")
    seeded.add_rule("Billing only rule.", scope="billing_agent")
    assert seeded.pack_check(pack["pack_id"], "ops_agent")["status"] == "UNCHANGED"


def test_pack_check_unknown_pack(seeded: BrainService):
    result = seeded.pack_check("pk_0123456789abcdef", "ops_agent")
    assert result["status"] == "UNKNOWN_PACK"
    assert "boot" in result["instruction"]
    assert result["current_pack_id"].startswith("pk_")


def test_pack_check_does_not_accept_another_agents_receipt(seeded: BrainService):
    billing = seeded.boot("billing_agent")
    assert seeded.pack_check(billing["pack_id"], "ops_agent")["status"] == "UNKNOWN_PACK"


def test_pack_check_rejects_malformed_pack_id(seeded: BrainService):
    with pytest.raises(BrainError):
        seeded.pack_check("not-a-pack", "ops_agent")


def test_time_alone_changes_the_pack_when_an_item_falls_due(service: BrainService, clock):
    service.add_due_item("File the report", clock() + timedelta(hours=30))
    pack = service.boot("ops_agent")
    assert pack["due_items"] == []
    clock.advance(hours=8)
    result = service.pack_check(pack["pack_id"], "ops_agent")
    assert result["status"] == "CHANGED"
    assert result["added"][0]["text"].endswith("File the report")


def test_completing_a_due_item_removes_it(seeded: BrainService):
    pack = seeded.boot("ops_agent")
    item_id = int(pack["due_items"][0]["key"].split(":")[1])
    seeded.complete_due_item(item_id)
    result = seeded.pack_check(pack["pack_id"], "ops_agent")
    assert [line["key"] for line in result["removed"]] == [f"due:{item_id}"]


def test_correction_ages_out_of_the_window(service: BrainService, clock):
    service.add_correction("Short lived.")
    assert len(service.boot("ops_agent")["recent_corrections"]) == 1
    clock.advance(days=15)
    assert service.boot("ops_agent")["recent_corrections"] == []


# ---- recall -------------------------------------------------------------
def test_recall_finds_memory_by_keyword(seeded: BrainService):
    result = seeded.recall("gutter pricing per metre")
    assert result["count"] >= 1
    assert result["hits"][0]["kind"] == "memory"
    assert "per metre" in result["hits"][0]["text"]


def test_recall_searches_rules_and_corrections_too(seeded: BrainService):
    kinds = {hit["kind"] for hit in seeded.recall("customer invoice", limit=25)["hits"]}
    assert {"rule", "memory"} <= kinds
    only = seeded.recall("apology", kinds=["correction"])
    assert [hit["kind"] for hit in only["hits"]] == ["correction"]


def test_recall_respects_limit_and_handles_no_match(seeded: BrainService):
    assert len(seeded.recall("customer", limit=1)["hits"]) == 1
    assert seeded.recall("zeppelin")["hits"] == []


def test_recall_survives_query_syntax_characters(seeded: BrainService):
    result = seeded.recall('invoice" OR (reminders* NEAR -date')
    assert result["count"] >= 1


def test_recall_validation(seeded: BrainService):
    with pytest.raises(BrainError):
        seeded.recall("the and of")
    with pytest.raises(BrainError):
        seeded.recall("invoice", limit=0)
    with pytest.raises(BrainError):
        seeded.recall("invoice", kinds=["secrets"])


def test_tokenize_drops_stopwords_and_duplicates():
    assert tokenize("The invoice, the INVOICE and a reminder!") == ["invoice", "reminder"]


# ---- propose_rule -------------------------------------------------------
def test_proposal_is_not_a_rule_until_approved(seeded: BrainService):
    before = seeded.boot("ops_agent")
    result = seeded.propose_rule(
        "ops_agent", "Always send the estimate as a PDF.", "Customer asked twice."
    )
    assert result["status"] == "pending"
    assert seeded.pack_check(before["pack_id"], "ops_agent")["status"] == "UNCHANGED"

    rule = seeded.approve_proposal(result["proposal_id"])
    after = seeded.pack_check(before["pack_id"], "ops_agent")
    assert after["status"] == "CHANGED"
    assert after["added"][0]["key"] == rule.key
    stored = seeded.store.get_proposal(result["proposal_id"])
    assert stored.status == "approved" and stored.rule_id == rule.id


def test_rejected_proposal_never_reaches_a_pack(seeded: BrainService):
    before = seeded.boot("ops_agent")
    result = seeded.propose_rule("ops_agent", "Skip approvals on Fridays.")
    seeded.reject_proposal(result["proposal_id"])
    assert seeded.pack_check(before["pack_id"], "ops_agent")["status"] == "UNCHANGED"
    with pytest.raises(BrainError):
        seeded.approve_proposal(result["proposal_id"])


def test_duplicate_of_existing_rule_is_not_recorded(seeded: BrainService):
    result = seeded.propose_rule(
        "ops_agent", "  never send a message to a customer until a human has approved the DRAFT"
    )
    assert result["status"] == "duplicate_of_rule"
    assert result["rule_key"].startswith("rule:")
    assert seeded.list_proposals() == []


def test_same_proposal_twice_is_reported_as_pending(seeded: BrainService):
    first = seeded.propose_rule("ops_agent", "Always send the estimate as a PDF.")
    second = seeded.propose_rule("billing_agent", "always send the estimate as a pdf")
    assert second["status"] == "already_pending"
    assert second["proposal_id"] == first["proposal_id"]
    assert len(seeded.list_proposals("pending")) == 1


def test_scoped_proposal_becomes_scoped_rule(seeded: BrainService):
    result = seeded.propose_rule("ops_agent", "Log every callback.", scope="ops_agent")
    seeded.approve_proposal(result["proposal_id"])
    assert any("callback" in r["text"] for r in seeded.boot("ops_agent")["standing_rules"])
    assert not any("callback" in r["text"] for r in seeded.boot("billing_agent")["standing_rules"])


def test_proposal_validation(seeded: BrainService):
    with pytest.raises(BrainError):
        seeded.propose_rule("ops_agent", "   ")
    with pytest.raises(BrainError):
        seeded.propose_rule("ops_agent", "x" * 501)
    with pytest.raises(BrainError):
        seeded.propose_rule("ops_agent", "fine", rationale="y" * 1001)
    with pytest.raises(BrainError):
        seeded.propose_rule("ops_agent", "fine", scope="not a key")
    with pytest.raises(BrainError):
        seeded.approve_proposal(999)
