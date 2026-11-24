# brain-boot-mcp

[![ci](https://github.com/midcityhandyman/brain-boot-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/midcityhandyman/brain-boot-mcp/actions/workflows/ci.yml)

**Agents stop starting from zero, and stop repeating mistakes a human already corrected.**

An AI agent begins every session with no memory of the instructions it was given yesterday. Correct it in the morning and it, or another agent on the same team, makes the same mistake in the afternoon. A long session never notices when the rules change underneath it.

brain-boot-mcp is a small Model Context Protocol (MCP) server that closes that gap for any MCP client, including Claude Code and Claude Desktop:

- **At session start** the agent calls `boot` and gets its boot pack: the standing rules, the recent human corrections, what is due, and a pack id.
- **Before anything hard to undo** it calls `pack_check` and is told exactly which lines were added or removed since it booted.
- **Instead of guessing** it can `recall` stored memory, and it can `propose_rule` when a correction should outlast the session. Only a human can approve a proposal.

It is for anyone running one or more agents on real work who is tired of repeating the same instructions. Python 3.10+, the official MCP Python SDK (v2), SQLite out of the box, Postgres included, 80 tests.

## What you will see

Real output from `python demo/run_demo.py` (synthetic data, pinned clock; the full capture is in [`demo/output.txt`](demo/output.txt)). The pack an agent gets at session start:

```text
== 2. Session start: boot ==
pack_id       pk_838c339e0d9dd2f8
generated_at  2026-01-05T14:00:00Z
standing_rules
  [rule:1:v1] (*) Never send a message to a customer until a human has approved the draft.
  [rule:2:v1] (*) Quote prices only from the current price sheet. If an item is missing, ask.
  [rule:3:v1] (*) Write dates in full (Tuesday 6 January), never as bare numbers.
  [rule:4:v1] (ops_agent) Before booking a visit, confirm the address back to the customer.
recent_corrections
  [correction:2] 2026-01-05T08:00:00Z The Tuesday slot for Alex Sample was double booked. Check the calendar before offering a time, not after.
  [correction:1] 2026-01-04T08:00:00Z Stop opening replies with an apology when nothing went wrong.
due_items
  [due:1] OVERDUE 2026-01-05T11:00:00Z Call back Jordan Example on 555-0142 about the gutter estimate
  [due:2] due 2026-01-05T19:00:00Z Send the weekly open jobs summary
```

Twenty minutes later a human adds a correction and tightens rule 1. The same agent, mid session, checks its pack:

```text
== 5. The next pack_check catches it mid session ==
status           CHANGED
current_pack_id  pk_cd4a559679c1f1a8
  + [correction:4] Do not promise a same day visit. Offer the next open slot instead.
  + [rule:1:v2] Never send a message to a customer or a supplier until a human has approved the draft.
  - [rule:1:v1] Never send a message to a customer until a human has approved the draft.
instruction      Your pack is out of date. Follow the added lines first, stop following the removed lines, then use current_pack_id for later checks.
```

All sample data is invented (placeholder people, 555-01xx numbers, example.com addresses).

## What it does

The server exposes four tools over the Model Context Protocol:

| Tool | What the agent gets |
| --- | --- |
| `boot(agent_key)` | The boot pack: standing rules (by priority, scoped to this agent or to all), recent human corrections, items that are due or overdue, and a `pack_id`. A receipt is written. |
| `pack_check(pack_id, agent_key)` | `UNCHANGED`, or `CHANGED` with the exact lines added and removed since that pack was issued, or `UNKNOWN_PACK` (boot again). |
| `recall(query, limit, kinds)` | Ranked full text search over stored memories, past corrections and active rules. |
| `propose_rule(agent_key, text, rationale, scope)` | Records a proposal. It is not a rule and reaches no pack until a human approves it. |

Three ideas carry the design:

- **The pack id is content addressed.** It is a hash of the pack's lines, so "has my pack changed" is a string comparison and the server keeps no session state. Rule keys carry a version (`rule:1:v2`), so an edited rule shows up as one line removed and one added.
- **Receipts make the diff possible.** Every pack handed out is stored with its lines. `pack_check` diffs the receipt against the current pack and says what to start and stop following, not just "something changed".
- **Agents can ask, only humans decide.** Approving, rejecting, correcting and editing rules live in the CLI and the service API. They are deliberately not MCP tools, so a connected agent cannot promote its own proposal.

Storage sits behind one interface (`BrainStore`). SQLite ships for the demo and tests and needs nothing but the standard library. Postgres ships as a second implementation plus plain SQL under `sql/`: the schema, and `brain_boot`, `brain_pack_check`, `brain_recall`, `brain_propose_rule` as database functions that produce the same pack ids as the Python path.

## Architecture

```mermaid
flowchart LR
    A[Agent host<br/>any MCP client] -- stdio --> S[server.py<br/>MCPServer: 4 tools]
    H[Human<br/>CLI: correct, approve, reject] --> V
    S --> V[service.py<br/>BrainService]
    V --> P[pack.py<br/>lines, pack id, diff]
    V --> I{{store.py<br/>BrainStore interface}}
    I --> Q[(store_sqlite.py<br/>SQLite + FTS5)]
    I --> G[(store_pg.py<br/>Postgres)]
    G -.->|same tables| F[sql/001_schema.sql<br/>sql/002_functions.sql<br/>brain_boot, brain_pack_check,<br/>brain_recall, brain_propose_rule]
    R[Other callers<br/>plain SQL / RPC] -.-> F
```

```mermaid
sequenceDiagram
    participant Agent
    participant Server as brain-boot-mcp
    participant Human
    Agent->>Server: boot(ops_agent)
    Server-->>Agent: rules, corrections, due items, pack_id pk_A (receipt saved)
    Human->>Server: correction added, rule 1 edited
    Agent->>Server: pack_check(pk_A) before a risky action
    Server-->>Agent: CHANGED, +correction, +rule:1:v2, -rule:1:v1, current pk_B
    Agent->>Server: propose_rule("...")
    Server-->>Agent: pending (not in any pack)
    Human->>Server: approve proposal
    Agent->>Server: pack_check(pk_B)
    Server-->>Agent: CHANGED, +rule:6:v1, current pk_C
```

## Run it

```bash
git clone https://github.com/midcityhandyman/brain-boot-mcp.git
cd brain-boot-mcp
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m brain_boot_mcp --db brain.db seed
python -m brain_boot_mcp --db brain.db boot ops_agent     # print a pack without an MCP client
python -m brain_boot_mcp --db brain.db serve
```

`serve` speaks MCP over stdio, so it waits silently for a client; stop it with Ctrl+C. In normal use you do not start it yourself: the host does.

### Connect it to Claude

Hosts start the server from their own working directory with a minimal environment, so use absolute paths and point at the interpreter inside the virtual environment (`.venv/bin/python`, or `.venv\Scripts\python.exe` on Windows).

Claude Code:

```bash
claude mcp add brain-boot -- /absolute/path/to/brain-boot-mcp/.venv/bin/python -m brain_boot_mcp --db /absolute/path/to/brain.db serve
claude mcp list
```

Everything after `--` is the server command. Add `--scope project` to write the entry to a shared `.mcp.json`, or `--scope user` for all your projects. Inside a session, `/mcp` shows the connection.

Claude Desktop: Settings, Developer, Edit Config opens `claude_desktop_config.json`. Add the entry, then quit and reopen the app. The same `mcpServers` block is what a project `.mcp.json` holds.

```json
{
  "mcpServers": {
    "brain-boot": {
      "command": "/absolute/path/to/brain-boot-mcp/.venv/bin/python",
      "args": ["-m", "brain_boot_mcp", "--db", "/absolute/path/to/brain.db", "serve"]
    }
  }
}
```

On Windows, write the paths with doubled backslashes, for example `"C:\\path\\to\\brain-boot-mcp\\.venv\\Scripts\\python.exe"`.

The server sends instructions telling the agent to boot first and check before risky actions, but a host is free to ignore them. It is more reliable to also put one line in the agent's own instructions (for Claude Code, `CLAUDE.md`): "Call the brain-boot `boot` tool at the start of every session and follow the pack."

### The human side

From another terminal, on the same database:

```bash
python -m brain_boot_mcp --db brain.db correct "Offer the next open slot, never same day." --agent ops_agent
python -m brain_boot_mcp --db brain.db proposals --status pending
python -m brain_boot_mcp --db brain.db approve 1
```

`proposals` prints nothing and `approve 1` reports "proposal 1 does not exist" until an agent has called `propose_rule`; the seed data contains no proposals.

Configuration is by environment variable and every value is optional; see `.env.example`. The default setup has no secrets. A host does not pass your shell's variables to the server, so set any you need in the `env` field of the server entry.

### Postgres

```bash
pip install -e ".[postgres]"
psql "$BRAIN_BOOT_PG_DSN" -f sql/001_schema.sql -f sql/002_functions.sql
BRAIN_BOOT_BACKEND=postgres python -m brain_boot_mcp serve
```

`BRAIN_BOOT_PG_DSN` is a normal Postgres connection string (see `.env.example`).

The functions also work with no Python at all:

```sql
select brain_boot('ops_agent');
select brain_pack_check('pk_838c339e0d9dd2f8', 'ops_agent');
select * from brain_recall('invoice reminders');
select brain_propose_rule('ops_agent', 'Send every estimate as a PDF attachment.', 'asked twice');
select brain_approve_proposal(1);   -- human side; do not grant to the agents' role
```

## Tests

```bash
python -m pytest
```

71 tests run offline against SQLite: the pack id algorithm and diff, every tool's behaviour and validation, both search paths (FTS5 and the plain scan fallback), the CLI, the MCP layer through the SDK's real client, a stdio subprocess round trip, and the demo. 9 more cover the Postgres store and the SQL functions, including parity checks that SQL and Python produce identical pack ids, sections and diffs. Those are skipped unless you point them at a throwaway database:

```bash
docker run -d --rm -e POSTGRES_PASSWORD=test -p 54329:5432 postgres:16
BRAIN_BOOT_TEST_PG_DSN=postgresql://postgres:test@localhost:54329/postgres python -m pytest
```

CI (`.github/workflows/ci.yml`) runs the offline suite and the demo on Python 3.10, 3.12 and 3.13, and the Postgres suite against a Postgres 16 service container.

## Demo

```bash
python demo/run_demo.py
```

One agent session, start to finish, with the real MCP server and the real MCP client in one process, an in-memory database, synthetic data and a pinned clock. No network and no keys. It prints ten steps; the excerpts under "What you will see" are steps 2 and 5, and the full capture is in [`demo/output.txt`](demo/output.txt). Step 10 sends bad input on purpose, so the SDK logs one `Tool 'pack_check' failed` line to stderr. That line is expected.

## How the pack id is computed

```text
for each pack line, sorted by key in byte order:   key + TAB + text + LF
pack_id = "pk_" + first 16 hex characters of sha256(utf8 of the concatenation)
```

Keys are `rule:<id>:v<version>`, `correction:<id>` and `due:<id>`. A due item's text includes its due time, so rescheduling changes the pack; whether it is overdue does not, because that flips with the clock and not with a decision. `pack.py` and `sql/002_functions.sql` implement the same steps, and a test fails if they drift apart.

## Layout

```text
src/brain_boot_mcp/
  models.py        records and the canonical time and text formats
  pack.py          pack lines, pack id, diff (pure functions)
  store.py         BrainStore, the storage interface
  store_sqlite.py  SQLite implementation (FTS5 with a fallback)
  store_pg.py      Postgres implementation (optional dependency)
  service.py       boot, pack_check, recall, propose_rule and the human side
  server.py        the MCP tools
  config.py        environment variables
  seed.py          synthetic sample data
  __main__.py      CLI
sql/               Postgres schema and functions
demo/              offline demo and its captured output
tests/
```

## Where this pattern runs

This is a clean rewrite of a pattern drawn from systems that run two small service companies day to day. There, several agents on different hosts share one memory, each one boots from it before doing anything, and each checks its pack again before any message leaves the building. The pattern earned its place the ordinary way: a human corrected one agent in the morning and a different agent repeated the mistake in the afternoon. None of that system's data, rules or configuration is in this repository.

## Limitations

- **No authentication.** `agent_key` is a label the caller supplies, not an identity. Over stdio that is fine because the host starts the process. Do not expose this over a network transport without adding auth in front of it. In Postgres, grant the agents' role only what it needs and keep `brain_approve_proposal` and direct table writes for humans; this repository does not set up roles for you.
- **Compliance is not enforced.** The server can tell an agent its pack changed. It cannot make the agent call `pack_check` or follow the lines. Gating real actions on a fresh check belongs in the host or in the tool that performs the action.
- **Recall is keyword search.** OR matching on whole words, ranked by BM25 on SQLite and `ts_rank` on Postgres, with no stemming and no embeddings. "estimate" will not find "estimates". Scores are not comparable across backends, and ordering can differ between them on close matches.
- **The pack is bounded, not summarised.** Corrections older than the window (14 days by default) or beyond the limit (10) silently drop out. Something that should last needs to become a rule.
- **Time can change a pack.** An item falling due or a correction ageing out changes the pack id with no human action. That is intended, but it means `CHANGED` does not always mean "someone edited something".
- **Receipts grow forever.** There is no pruning job. One row per boot is small, but a busy deployment will want a retention policy.
- **Duplicate detection is literal.** Proposals are matched on case, whitespace and trailing punctuation only. Two differently worded versions of the same rule are both recorded.
- **Single writer assumptions on SQLite.** One connection guarded by a lock; fine for one server process and a human at the CLI, not for many concurrent writers. Use Postgres for that.
- **Timestamps are whole seconds, UTC.** The Postgres defaults truncate to the second to match. Rows written by other tools with sub second times can land on the other side of a window boundary than the Python path expects.
- **What was actually run.** Installed into a fresh virtual environment and tested against MCP Python SDK 2.3.0 on Python 3.12 (Windows), with Postgres 16 in a local container. CI runs the offline suite and the demo on Python 3.10, 3.12 and 3.13 and the Postgres suite on Ubuntu. Only the stdio transport has been exercised, and the stdio round trip was made with the SDK's own client. The Claude Code and Claude Desktop entries above follow the current documentation for those hosts; no test in this repository drives either app.

License: MIT, see the LICENSE file.
