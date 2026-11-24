"""Pure functions that turn stored rows into pack lines, a pack id and a diff.

The pack id is content addressed: the same rules, corrections and due items
always hash to the same id, on SQLite and on Postgres alike. That is what lets
pack_check answer "has anything changed since you booted" without the server
keeping any per-session state beyond a receipt.

Hash input, exactly (sql/002_functions.sql mirrors it):

    for each line, sorted by key in byte order:  key + TAB + text + LF
    pack_id = "pk_" + first 16 hex chars of sha256(utf8 of the concatenation)
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

from .models import (
    KIND_CORRECTION,
    KIND_DUE,
    KIND_RULE,
    Correction,
    DueItem,
    PackLine,
    Rule,
    iso,
)

PACK_ID_PREFIX = "pk_"
PACK_ID_HEX_CHARS = 16


def rule_line(rule: Rule) -> PackLine:
    return PackLine(rule.key, KIND_RULE, rule.text)


def correction_line(correction: Correction) -> PackLine:
    return PackLine(correction.key, KIND_CORRECTION, correction.text)


def due_line(item: DueItem) -> PackLine:
    # The due time is part of the text so that rescheduling changes the pack.
    # "Overdue" is deliberately not: it flips with the clock, not with a human.
    return PackLine(item.key, KIND_DUE, f"{iso(item.due_at)} {item.title}")


def build_lines(
    rules: Iterable[Rule],
    corrections: Iterable[Correction],
    due_items: Iterable[DueItem],
) -> tuple[PackLine, ...]:
    lines = [rule_line(r) for r in rules]
    lines += [correction_line(c) for c in corrections]
    lines += [due_line(d) for d in due_items]
    return tuple(sorted(lines, key=lambda line: line.key.encode("utf-8")))


def compute_pack_id(lines: Sequence[PackLine]) -> str:
    ordered = sorted(lines, key=lambda line: line.key.encode("utf-8"))
    payload = "".join(f"{line.key}\t{line.text}\n" for line in ordered)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return PACK_ID_PREFIX + digest[:PACK_ID_HEX_CHARS]


def looks_like_pack_id(value: str) -> bool:
    if not value.startswith(PACK_ID_PREFIX):
        return False
    tail = value[len(PACK_ID_PREFIX):]
    return len(tail) == PACK_ID_HEX_CHARS and all(c in "0123456789abcdef" for c in tail)


def diff_lines(
    old: Sequence[PackLine], new: Sequence[PackLine]
) -> tuple[list[PackLine], list[PackLine]]:
    """Return (added, removed) between two packs.

    A rule edit shows up as one removed line (old version key) and one added
    line (new version key), because the version is part of the key.
    """
    old_by_key = {line.key: line for line in old}
    new_by_key = {line.key: line for line in new}
    added = [line for key, line in new_by_key.items() if key not in old_by_key]
    removed = [line for key, line in old_by_key.items() if key not in new_by_key]
    # Same key, different text can only happen for a rescheduled due item.
    for key, line in new_by_key.items():
        before = old_by_key.get(key)
        if before is not None and before.text != line.text:
            added.append(line)
            removed.append(before)
    added.sort(key=lambda line: line.key.encode("utf-8"))
    removed.sort(key=lambda line: line.key.encode("utf-8"))
    return added, removed
