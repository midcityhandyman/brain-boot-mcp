"""Offline demo: one agent session against brain-boot-mcp, start to finish.

Runs the real MCP server and the real MCP client in one process, backed by an
in-memory SQLite database with synthetic data and a pinned clock, so the output
is the same on every run. No network, no keys.

    python demo/run_demo.py            print to the terminal
    python demo/run_demo.py --save     also write demo/output.txt
"""

from __future__ import annotations

import asyncio
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp import Client  # noqa: E402

from brain_boot_mcp.seed import seed  # noqa: E402
from brain_boot_mcp.server import build_server  # noqa: E402
from brain_boot_mcp.service import BrainService  # noqa: E402
from brain_boot_mcp.store_sqlite import SqliteStore  # noqa: E402

AGENT = "ops_agent"


class DemoClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 5, 14, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, minutes: int) -> None:
        self.current += timedelta(minutes=minutes)


OUT = io.StringIO()


def say(text: str = "") -> None:
    print(text)
    OUT.write(text + "\n")


def step(number: int, title: str) -> None:
    say()
    say(f"== {number}. {title} ==")


def show_pack(pack: dict[str, Any]) -> None:
    say(f"pack_id       {pack['pack_id']}")
    say(f"generated_at  {pack['generated_at']}")
    say("standing_rules")
    for rule in pack["standing_rules"]:
        say(f"  [{rule['key']}] ({rule['scope']}) {rule['text']}")
    say("recent_corrections")
    for item in pack["recent_corrections"]:
        say(f"  [{item['key']}] {item['created_at']} {item['text']}")
    say("due_items")
    for item in pack["due_items"]:
        flag = "OVERDUE" if item["overdue"] else "due"
        say(f"  [{item['key']}] {flag} {item['due_at']} {item['title']}")


def show_check(result: dict[str, Any]) -> None:
    say(f"status           {result['status']}")
    say(f"current_pack_id  {result['current_pack_id']}")
    for line in result["added"]:
        say(f"  + [{line['key']}] {line['text']}")
    for line in result["removed"]:
        say(f"  - [{line['key']}] {line['text']}")
    say(f"instruction      {result['instruction']}")


async def run() -> None:
    clock = DemoClock()
    store = SqliteStore(":memory:")
    store.init_schema()
    service = BrainService(store, clock=clock)
    counts = seed(service)

    say("brain-boot-mcp demo (in-memory SQLite, synthetic data, pinned clock)")
    say(f"seeded: {counts}")
    say(f"full text search: {'FTS5' if store.fts_enabled else 'plain scan fallback'}")

    async with Client(build_server(service)) as agent:
        step(1, "The agent connects and lists the tools")
        for tool in (await agent.list_tools()).tools:
            say(f"  {tool.name}: {(tool.description or '').splitlines()[0]}")

        step(2, "Session start: boot")
        pack = (await agent.call_tool("boot", {"agent_key": AGENT})).structured_content
        show_pack(pack)
        say("note: the billing only rule, the 40 day old correction and the item due in")
        say("      6 days are in the database but correctly left out of this pack.")

        step(3, "Before an outbound action: pack_check (nothing has changed)")
        check = await agent.call_tool("pack_check", {"pack_id": pack["pack_id"], "agent_key": AGENT})
        show_check(check.structured_content)

        step(4, "Meanwhile a human corrects the agent and tightens a rule")
        clock.advance(20)
        service.add_correction(
            "Do not promise a same day visit. Offer the next open slot instead.", agent_key=AGENT
        )
        first_rule = service.store.active_rules(AGENT)[0]
        service.update_rule(
            first_rule.id,
            "Never send a message to a customer or a supplier until a human has approved the draft.",
        )
        say("  human: added a correction for ops_agent")
        say(f"  human: edited rule {first_rule.id} (now version 2)")

        step(5, "The next pack_check catches it mid session")
        check = await agent.call_tool("pack_check", {"pack_id": pack["pack_id"], "agent_key": AGENT})
        changed = check.structured_content
        show_check(changed)

        step(6, "The agent adopts the new pack id and checks again")
        check = await agent.call_tool(
            "pack_check", {"pack_id": changed["current_pack_id"], "agent_key": AGENT}
        )
        show_check(check.structured_content)

        step(7, "recall: look it up instead of guessing")
        for query in ("how is gutter cleaning priced", "when do invoice reminders go out"):
            found = (await agent.call_tool("recall", {"query": query, "limit": 2})).structured_content
            say(f"query: {query!r} -> tokens {found['tokens']}")
            for hit in found["hits"]:
                say(f"  {hit['ref']:<14} score {hit['score']:<8} {hit['text']}")

        step(8, "propose_rule: the agent asks, it cannot decide")
        args = {
            "agent_key": AGENT,
            "text": "Send every estimate as a PDF attachment.",
            "rationale": "Jordan Example asked for PDF twice in one week.",
        }
        proposed = (await agent.call_tool("propose_rule", args)).structured_content
        say(f"status {proposed['status']}, proposal_id {proposed['proposal_id']}")
        say(f"message: {proposed['message']}")
        again = (await agent.call_tool("propose_rule", args)).structured_content
        say(f"same proposal again -> status {again['status']}")
        existing = (
            await agent.call_tool(
                "propose_rule",
                {"agent_key": AGENT, "text": "write dates in full (Tuesday 6 January), "
                                             "never as bare numbers"},
            )
        ).structured_content
        say(f"an existing rule, reworded only by case -> status {existing['status']} "
            f"({existing['rule_key']})")
        still = await agent.call_tool(
            "pack_check", {"pack_id": changed["current_pack_id"], "agent_key": AGENT}
        )
        say(f"pack_check after proposing -> {still.structured_content['status']} "
            "(a proposal is not a rule)")

        step(9, "A human approves the proposal; the next check delivers the rule")
        clock.advance(30)
        rule = service.approve_proposal(proposed["proposal_id"], priority=50)
        say(f"  human: approved proposal {proposed['proposal_id']} as {rule.key}")
        check = await agent.call_tool(
            "pack_check", {"pack_id": changed["current_pack_id"], "agent_key": AGENT}
        )
        show_check(check.structured_content)

        step(10, "Bad input is a tool error, not a crash")
        bad = await agent.call_tool("pack_check", {"pack_id": "made-up", "agent_key": AGENT})
        say(f"is_error {bad.is_error}: {bad.content[0].text}")
        stale = await agent.call_tool(
            "pack_check", {"pack_id": "pk_0123456789abcdef", "agent_key": AGENT}
        )
        say(f"unknown but well formed id -> {stale.structured_content['status']}")

    say()
    say(f"receipts written for {AGENT}: {store.count_receipts(AGENT)}")
    say("demo complete")
    store.close()


def main() -> int:
    asyncio.run(run())
    if "--save" in sys.argv[1:]:
        (ROOT / "demo" / "output.txt").write_text(OUT.getvalue(), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
