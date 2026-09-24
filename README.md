# RunDB

**A local-first database for AI agents.** One SQLite file holds every run, retry, fork, trace and lesson learned. Python SDK, TypeScript SDK, CLI and MCP server. Zero dependencies. Apache-2.0.

[![PyPI](https://img.shields.io/pypi/v/rundb-ai?color=blue&label=pypi)](https://pypi.org/project/rundb-ai/)
[![Downloads](https://static.pepy.tech/badge/rundb-ai)](https://pepy.tech/project/rundb-ai)
[![Python](https://img.shields.io/pypi/pyversions/rundb-ai)](https://pypi.org/project/rundb-ai/)
[![CI](https://github.com/ashishjsharda/rundb/actions/workflows/ci.yml/badge.svg)](https://github.com/ashishjsharda/rundb/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)

## The problem

Agents don't use databases the way people do. A single task turns into dozens of short-lived runs: call a tool, fail, retry, branch, fail differently, succeed. Then the next agent starts from zero and makes the same mistake again.

Most stacks keep that history in log files, which are hard to query, or in a hosted tracing product, which needs an account and a network connection. Neither gives the agent a way to ask *"has this failed before, and what fixed it?"*

RunDB gives agents that memory. It lives in a single file next to your code, works offline, and every table can be queried with plain SQL.

## 30-second quickstart

```bash
pip install rundb-ai              # zero dependencies; Python 3.10+
git clone https://github.com/ashishjsharda/rundb && cd rundb
python examples/coding_agent_retry.py
rundb --db demo.db failed my-repo
```

In the demo, a simulated agent runs `pytest` five times and fails each time. RunDB tells it to stop retrying, the agent forks and fixes the problem, and it saves the fix as a memory. A second agent then finds that memory and passes on its first try.

## Python in 15 lines

```python
from rundb import connect

db = connect("agent.db")                    # created + migrated on first open
run = db.start_run("my-repo", goal="make tests pass", model="claude-sonnet-5")

db.log_span(run, "tool", "pytest", input="pytest -q",
            error="ModuleNotFoundError: No module named 'requests'")
print(db.what_failed(run)["suggested_next_step"])

retry = db.fork_run(run, goal="install deps first")   # new branch; old trace kept
db.log_span(retry, "tool", "pip", input="pip install -r requirements.txt", output="ok")
db.remember("my-repo", "tests.setup", "pip install -r requirements.txt before pytest",
            kind="constraint", source_run_id=retry)   # outlives every run
db.end_run(retry, "succeeded")

for hit in db.search("ModuleNotFoundError"):
    print(hit["source"], hit["run_id"], hit["snippet"])
```

| Call | What it does |
|---|---|
| `connect(path="agent.db")` | Open the file. It is created and migrated if needed. `$RUNDB_PATH` overrides the default path. |
| `start_run(workspace, goal, model=None, parent_run_id=None)` | Start a run and return a `Run` (`str(run)` is its id). |
| `log_span(run_id, kind, name, input=None, output=None, error=None, **kw)` | Append a step. `kind` is one of `thought`, `tool`, `retrieve`, `write`, `eval` or `human`. Values that aren't strings are stored as JSON. |
| `end_run(run_id, status, summary=None)` | Finish a run with `succeeded`, `failed`, `aborted` or `forked`. |
| `fork_run(run_id, goal=None)` | Start a new run linked by `parent_run_id`. Spans are not copied. A parent that is still running is closed as `forked`. |
| `remember(workspace, key, value, kind="fact", source_run_id=None)` | Save a durable memory (`fact`, `preference`, `decision` or `constraint`). A newer value for the same key replaces the old one, and the old one stays in history. Also accepts `ttl="7d"` and `confidence`. |
| `search(query, workspace=, run_id=, kind=, source=, since="2h", limit=)` | Ranked full-text search across spans, memories and artifact text. Every hit carries `run_id` and `span_id`. |
| `what_failed(run_id or workspace)` | Returns recent errors, repeated failures, related memories and forks that succeeded, plus a **suggested next step**. |
| `sql(query, params)` | Plain SQL, for power users. |

Also available: `recall`, `forget`, `add_artifact`, `add_chunk` (with an optional embedding), `similar`, `lineage`, `spans`, `events`, `abort_run`, `abort_stale`, `list_runs`, `batch()`.

## Use it from your coding agent (MCP)

```bash
claude mcp add rundb -- rundb mcp        # Claude Code (uses ./agent.db)
```

```jsonc
// Cursor / Windsurf / any MCP client
{ "mcpServers": { "rundb": { "command": "rundb", "args": ["mcp"], "env": { "RUNDB_PATH": "agent.db" } } } }
```

**Tools:** `start_run`, `log_span`, `end_run`, `fork_run`, `what_failed`, `remember`, `recall`, `search`, `list_runs`, `get_run`, `sql` (read-only). Each one has a strict JSON schema and a one-line description, so an agent can call it without reading any docs.

**Resources:** `rundb://workspace/{id}/latest-failures` and `rundb://run/{id}`.

The server uses stdio JSON-RPC and was tested against the official MCP Python client.

## CLI

```
rundb init [path]                 create or migrate a database file
rundb runs [-w ws] [--status s]   recent runs
rundb show <run_id>               one run and its spans
rundb tail [run_id] [-f]          event log for the latest (or a given) run, optionally followed
rundb search "..." [--since 2h]   full-text search
rundb failed [run_id|workspace]   errors, repeats and the suggested next step
rundb sql "select ..."            read-only unless --write
rundb abort <run_id> | --stale 1h clean up runs whose process died
rundb start/span/end/fork/remember/recall   drive it from shell scripts
rundb mcp                         MCP server on stdio
```

Every command accepts `--db PATH` and `--json`.

## TypeScript

Same file format and same operations, in camelCase. It uses the built-in `node:sqlite`, so there's nothing native to compile. Requires Node 22.13 or newer.

```ts
import { connect } from "rundb";

const db = connect("agent.db");
const run = db.startRun("my-repo", "make tests pass");
db.logSpan(run, "tool", "pytest", { error: "ModuleNotFoundError: requests" });
const retry = db.forkRun(run, "install deps first");
db.remember("my-repo", "tests.setup", "pip install -r requirements.txt first", { kind: "constraint" });
db.endRun(retry, "succeeded");
console.log(db.whatFailed("my-repo").suggested_next_step);
```

A Python agent and a TypeScript agent can share the same `agent.db`, and CI tests both directions.

## Data model

Seven tables, defined in [`schema/001_init.sql`](schema/001_init.sql):

```
workspaces(id, name, created_at, meta)
runs(id, workspace_id, parent_run_id, branch_name, status, goal, model, started_at, ended_at, meta)
spans(id, run_id, parent_span_id, kind, name, input, output, error, tokens_in, tokens_out, started_at, ended_at, meta)
artifacts(id, run_id, span_id, type, path_or_uri, mime, sha256, text_preview, meta)
memories(id, workspace_id, source_run_id, kind, key, value, confidence, expires_at, created_at)
chunks(id, workspace_id, run_id, memory_id, artifact_id, text, embedding)
events(id, run_id, ts, level, message, payload)
```

The schema enforces a few rules:

- **Append-heavy.** A trigger rejects any update to a run except `status`, `ended_at` and `meta`. `events` rejects all updates and deletes.
- **Forks link, they don't copy.** Walk `parent_run_id` (or call `lineage()`) to see how a run got to where it is.
- **Memories outlive runs.** Failed runs keep their traces, and memories are never tied to a run's lifetime.
- **Crash-safe.** If a process dies mid-run, its committed spans survive and the run stays `running`. `rundb abort --stale 1h` or `abort_stale()` closes it.
- **It's just SQLite.** Open it with `sqlite3`, DBeaver or Datasette:

```sql
SELECT r.model, count(*) AS runs, avg(s.tokens_in + s.tokens_out) AS avg_tokens
FROM runs r JOIN spans s ON s.run_id = r.id
WHERE r.status = 'failed' GROUP BY r.model;
```

**Search** uses a single SQLite FTS5 index (porter stemming) over span name, input, output and error, memory key and value, and chunk text. Queries are tokenized and quoted, so agent text like `./deploy.sh: ENOENT` works. RunDB first tries a match on all terms and falls back to any term. Superseded and expired memories are hidden. Embeddings are optional: pass `embedding=[...]` to `add_chunk` and use `similar()`. v0 has no vector index, only a brute-force cosine search that is fine up to about 100k chunks.

## Why not...

**Postgres?** Postgres is great at the current state of your data. Agents need the *attempts*: every failed branch, in order, next to the fix that worked. They also need it with zero setup, inside a sandbox, offline. A single file you can commit, copy or delete beats a server here.

**Chroma or another vector DB?** Those store chunks, not runs. They have no model of retries, forks, errors or run status, and "what failed last time?" is a filter-and-sort question, not a nearest-neighbour one. RunDB does keyword search first and leaves embeddings optional.

**A tracing SaaS (LangSmith, Langfuse and similar)?** Those are good dashboards for humans. RunDB is built for the agent itself to read its own history mid-task. It works offline, has no account and needs no network. You can use both.

## Benchmark

`python bench/bench.py` on a 2-vCPU cloud VM (Xeon 2.1 GHz, Python 3.11, SQLite 3.45). Each span has about 60 words of text plus tokens, and 10% have errors:

| | |
|---|---|
| 10k spans, one commit per span (default) | 2.9 s, about 3,500 spans/s |
| 10k spans inside `db.batch()` | 1.2 s, about 8,000 spans/s |
| File size | 17 MB (about 1.7 KB/span, including the FTS index and event log) |
| `search()` one term, top 20 | p50 19 ms, p95 21 ms |
| `search()` filtered to one run | p50 12 ms |
| `what_failed(workspace)` | 23 ms |

Caveats, to keep this honest:

- The benchmark vocabulary is tiny (40 words), so each query term matches about a quarter of all rows. That is close to the worst case for ranking, and real traces are sparser.
- Every span write also writes an event row and an FTS row.
- It's one process on one machine. SQLite allows a single writer at a time. Concurrent agents on the same file queue behind a 10 s busy timeout, which is fine for dozens of agents but not for thousands.
- Run it on your own hardware before trusting these numbers.

## Non-goals

No custom storage engine, WAL, cluster, cloud control plane, new SQL dialect or home-grown vector DB. RunDB is a schema, a few hundred lines of client code per language and one SQLite file.

## Roadmap

- `rundb ui`: a local web view of run trees and failures
- OpenTelemetry and LangChain/LlamaIndex callback importers
- An optional `sqlite-vec` backend for large embedding sets
- Optional sync of a workspace's memories across machines and teammates

Ideas and issues are welcome.

## Development

```bash
pip install -e "python[dev]" && pytest python/tests      # Python: 30 tests
cd ts && npm install && npm test                          # TypeScript: 9 tests, incl. Python interop
python bench/bench.py
```

`schema/001_init.sql` is the canonical schema. Its copies in `python/rundb/migrations/` and `ts/migrations/` must stay byte-identical, and tests enforce that. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache-2.0](LICENSE)
