from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from brain_boot_mcp.models import (
    Correction,
    DueItem,
    PackLine,
    Rule,
    iso,
    normalize_rule_text,
    parse_iso,
)
from brain_boot_mcp.pack import (
    build_lines,
    compute_pack_id,
    diff_lines,
    looks_like_pack_id,
)

T = datetime(2026, 1, 5, 14, 0, 0, tzinfo=timezone.utc)


def _rule(rule_id: int, version: int, text: str) -> Rule:
    return Rule(rule_id, version, "*", 100, text, "active", T)


def test_pack_id_matches_documented_algorithm():
    lines = [PackLine("rule:2:v1", "rule", "B"), PackLine("correction:1", "correction", "A")]
    payload = "correction:1\tA\nrule:2:v1\tB\n".encode()
    expected = "pk_" + hashlib.sha256(payload).hexdigest()[:16]
    assert compute_pack_id(lines) == expected
    assert looks_like_pack_id(expected)


def test_pack_id_golden_value_guards_against_silent_algorithm_changes():
    # If this fails, sql/002_functions.sql must change in the same commit.
    lines = [PackLine("rule:1:v1", "rule", "Be kind.")]
    assert compute_pack_id(lines) == "pk_" + hashlib.sha256(b"rule:1:v1\tBe kind.\n").hexdigest()[:16]
    assert compute_pack_id([]) == "pk_e3b0c44298fc1c14"


def test_pack_id_ignores_input_order_but_not_content():
    a = PackLine("rule:1:v1", "rule", "one")
    b = PackLine("rule:2:v1", "rule", "two")
    assert compute_pack_id([a, b]) == compute_pack_id([b, a])
    assert compute_pack_id([a, b]) != compute_pack_id([a])
    assert compute_pack_id([a]) != compute_pack_id([PackLine("rule:1:v1", "rule", "one!")])


def test_build_lines_covers_all_three_sections():
    lines = build_lines(
        [_rule(1, 3, "rule text")],
        [Correction(7, "*", "fix it", "human", T)],
        [DueItem(4, "*", "call back", T)],
    )
    assert [line.key for line in lines] == ["correction:7", "due:4", "rule:1:v3"]
    due = next(line for line in lines if line.kind == "due")
    assert due.text == "2026-01-05T14:00:00Z call back"


def test_diff_reports_rule_edit_as_remove_plus_add():
    old = build_lines([_rule(1, 1, "old"), _rule(2, 1, "same")], [], [])
    new = build_lines([_rule(1, 2, "new"), _rule(2, 1, "same")], [], [])
    added, removed = diff_lines(old, new)
    assert [line.key for line in added] == ["rule:1:v2"]
    assert [line.key for line in removed] == ["rule:1:v1"]


def test_diff_reports_rescheduled_due_item_on_both_sides():
    later = datetime(2026, 1, 6, 9, 0, 0, tzinfo=timezone.utc)
    old = build_lines([], [], [DueItem(4, "*", "call back", T)])
    new = build_lines([], [], [DueItem(4, "*", "call back", later)])
    added, removed = diff_lines(old, new)
    assert added[0].text.startswith("2026-01-06T09:00:00Z")
    assert removed[0].text.startswith("2026-01-05T14:00:00Z")


def test_diff_of_identical_packs_is_empty():
    lines = build_lines([_rule(1, 1, "x")], [], [])
    assert diff_lines(lines, lines) == ([], [])


def test_looks_like_pack_id_rejects_junk():
    for bad in ("", "pk_", "pk_XYZ", "pk_0123456789abcdeg", "xx_0123456789abcdef",
                "pk_0123456789abcdef0"):
        assert not looks_like_pack_id(bad)


def test_iso_round_trip_and_naive_input():
    assert iso(T) == "2026-01-05T14:00:00Z"
    assert parse_iso("2026-01-05T14:00:00Z") == T
    assert parse_iso("2026-01-05T09:00:00-05:00") == T
    assert iso(datetime(2026, 1, 5, 14, 0, 0, 999)) == "2026-01-05T14:00:00Z"


def test_normalize_rule_text():
    assert normalize_rule_text("  Always   Confirm\nthe date. ") == "always confirm the date"
    assert normalize_rule_text("Stop!!") == "stop"
