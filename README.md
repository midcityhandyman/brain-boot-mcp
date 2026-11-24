# brain-boot-mcp

Agents start every session with no memory of the rules a human gave them yesterday, and a long session never notices when those rules change underneath it.
This is an MCP server that hands any agent a boot pack at session start and can tell it, at any later moment, exactly which lines of that pack have changed.

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
pip install -e ".[dev]"
python -m brain_boot_mcp --db brain.db seed
python -m brain_boot_mcp --db brain.db serve
```

`serve` speaks MCP over stdio, so it waits silently for a client. To attach it to an MCP host, register it like any stdio server:

```json
{
  "mcpServers": {
    "brain-boot": {
      "command": "python",
      "args": ["-m", "brain_boot_mcp", "--db", "/absolute/path/to/brain.db", "serve"]
    }
  }
}
```

The human side, from another terminal on the same database:

```bash
python -m brain_boot_mcp --db brain.db correct "Offer the next open slot, never same day." --agent ops_agent
python -m brain_boot_mcp --db brain.db proposals --status pending
python -m brain_boot_mcp --db brain.db approve 1
python -m brain_boot_mcp --db brain.db boot ops_agent     # print a pack without an MCP client
```

Configuration is by environment variable and every value is optional; see `.env.example`. The default setup has no secrets.

### Postgres

```bash
pip install -e ".[postgres]"
psql "$BRAIN_BOOT_PG_DSN" -f sql/001_schema.sql -f sql/002_functions.sql
BRAIN_BOOT_BACKEND=postgres python -m brain_boot_mcp serve
```

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

One agent session, start to finish, with the real MCP server and the real MCP client in one process, an in-memory database, synthetic data and a pinned clock. No network and no keys. The captured output is in [`demo/output.txt`](demo/output.txt). The middle of it:

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
- **What was actually run.** Built and tested against MCP Python SDK 2.3.0 on Python 3.12 (Windows) and Postgres 16 in a local container. The CI workflow is included but has not been executed on a hosted runner, and Python 3.10 and 3.13 have not been tried locally. Only the stdio transport has been exercised.

License: MIT, see the LICENSE file.
