"""Command line entry point.

    python -m brain_boot_mcp serve                 run the MCP server over stdio
    python -m brain_boot_mcp seed                  load synthetic sample data
    python -m brain_boot_mcp boot ops_agent        print a pack (writes a receipt)
    python -m brain_boot_mcp correct "text"        record a human correction
    python -m brain_boot_mcp proposals             list rule proposals
    python -m brain_boot_mcp approve 1             turn a proposal into a rule
    python -m brain_boot_mcp reject 1

The last four are the human side of the loop. They are deliberately not MCP
tools: an agent connected to the server cannot approve its own proposal.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Sequence

from .config import load_config, open_store
from .seed import seed
from .service import BrainError, BrainService
from .store import StoreError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain_boot_mcp", description=__doc__.split("\n")[0])
    parser.add_argument("--db", help="SQLite file path (overrides BRAIN_BOOT_DB)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the MCP server over stdio")
    sub.add_parser("seed", help="load synthetic sample data")

    boot = sub.add_parser("boot", help="print the pack for an agent")
    boot.add_argument("agent_key")

    correct = sub.add_parser("correct", help="record a human correction")
    correct.add_argument("text")
    correct.add_argument("--agent", default="*", help="agent_key it applies to (default all)")

    proposals = sub.add_parser("proposals", help="list rule proposals")
    proposals.add_argument("--status", choices=("pending", "approved", "rejected"))

    approve = sub.add_parser("approve", help="approve a proposal")
    approve.add_argument("proposal_id", type=int)
    approve.add_argument("--priority", type=int, default=100)

    reject = sub.add_parser("reject", help="reject a proposal")
    reject.add_argument("proposal_id", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config()
        if args.db:
            config = dataclasses.replace(config, backend="sqlite", sqlite_path=args.db)
        store = open_store(config)
        store.init_schema()
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    service = BrainService(store, config.pack)
    try:
        return _run(args, service)
    except (BrainError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


def _run(args: argparse.Namespace, service: BrainService) -> int:
    if args.command == "serve":
        # Imported here so the admin commands work without the MCP SDK loaded.
        from .server import build_server

        # stdout belongs to the protocol from here on; never print to it.
        build_server(service).run(transport="stdio")
        return 0
    if args.command == "seed":
        if service.store.active_rules("ops_agent"):
            print("database already has rules; not seeding twice")
            return 0
        print(json.dumps(seed(service)))
        return 0
    if args.command == "boot":
        print(json.dumps(service.boot(args.agent_key), indent=2))
        return 0
    if args.command == "correct":
        correction = service.add_correction(args.text, agent_key=args.agent)
        print(f"recorded {correction.key}")
        return 0
    if args.command == "proposals":
        for proposal in service.list_proposals(args.status):
            print(json.dumps(proposal.as_dict()))
        return 0
    if args.command == "approve":
        rule = service.approve_proposal(args.proposal_id, priority=args.priority)
        print(f"approved proposal {args.proposal_id} as {rule.key}")
        return 0
    if args.command == "reject":
        service.reject_proposal(args.proposal_id)
        print(f"rejected proposal {args.proposal_id}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
