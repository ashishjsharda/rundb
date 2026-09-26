# RunDB

**The local-first database for AI agents.**
Every run, retry, fork, trace and lesson learned, in one SQLite file.

[![PyPI](https://img.shields.io/pypi/v/rundb-ai?color=blue&label=pypi)](https://pypi.org/project/rundb-ai/)
[![Downloads](https://img.shields.io/pepy/dt/rundb-ai?label=downloads)](https://pepy.tech/project/rundb-ai)
[![Monthly downloads](https://img.shields.io/pypi/dm/rundb-ai?label=downloads%2Fmonth)](https://pypistats.org/packages/rundb-ai)
[![Python](https://img.shields.io/pypi/pyversions/rundb-ai)](https://pypi.org/project/rundb-ai/)
[![CI](https://github.com/ashishjsharda/rundb/actions/workflows/ci.yml/badge.svg)](https://github.com/ashishjsharda/rundb/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](https://github.com/ashishjsharda/rundb/blob/main/LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/ashishjsharda/rundb?style=social)](https://github.com/ashishjsharda/rundb)

---

## Why RunDB?

Agents fail, retry and branch dozens of times per task, then the next agent
starts from zero and makes the same mistake again.

RunDB gives agents a memory they can query:

- 🗂 **One file, zero setup.** SQLite under the hood. Works offline. No server.
- 🔁 **Runs, retries and forks.** Every attempt is kept and linked to the one
  it came from.
- 🔎 **Full-text search** over every step, error and memory. Hits come back
  with `run_id` and `span_id`.
- 🧠 **Memories that outlive failed runs**, so the fix is found next time.
- 🩺 **`what_failed()`** returns recent errors, known fixes and a suggested next step.
- 🎯 **Exact fix matching.** Save a fix with `fixes=<failed span>` and it's found the
  next time the same error appears, even from a different tool.
- ⏳ **Stale-fix detection.** Fixes are stamped with your git commit and lockfile hashes,
  and flagged when your dependencies change.
- 🔌 **MCP server built in**, so Claude Code, Cursor and other agents can use
  it with no glue code.
- 📦 **Zero dependencies.** Pure Python standard library.

## Install

```bash
pip install rundb-ai
```

This installs the `rundb` Python package and the `rundb` command.
Python 3.10+.

## Quickstart

```python
from rundb import connect

db = connect("agent.db")  # created on first use
run = db.start_run("my-repo", goal="make tests pass")

bad = db.log_span(run, "tool", "pytest",
                  error="ModuleNotFoundError: No module named 'requests'")
print(db.what_failed(run)["suggested_next_step"])

retry = db.fork_run(run, goal="install deps first")
db.remember("my-repo", "tests.setup",
            "pip install -r requirements.txt before pytest",
            kind="constraint", fixes=bad)
db.end_run(retry, "succeeded")

for hit in db.search("ModuleNotFoundError"):
    print(hit["source"], hit["run_id"], hit["snippet"])
```

## Use it from your coding agent (MCP)

**Claude Code**

```bash
claude mcp add rundb -- rundb mcp
```

**Cursor, Windsurf or any MCP client**

```json
{
  "mcpServers": {
    "rundb": { "command": "rundb", "args": ["mcp"] }
  }
}
```

Tools: `start_run`, `log_span`, `end_run`, `fork_run`, `what_failed`,
`remember`, `recall`, `search`, `list_runs`, `get_run`, `sql` (read-only).

## CLI

```bash
rundb init                  # create agent.db
rundb runs                  # recent runs
rundb tail -f               # follow the latest run live
rundb search "timeout"      # full-text search
rundb failed my-repo        # errors plus a suggested next step
rundb sql "select * from runs limit 5"
```

## API at a glance

| Call | Does |
|---|---|
| `connect(path)` | Open or create the database file |
| `start_run(workspace, goal)` | Start a run |
| `log_span(run, kind, name, ...)` | Record a step (`tool`, `thought`, ...) |
| `end_run(run, status)` | Finish with `succeeded`, `failed`, ... |
| `fork_run(run, goal)` | Retry on a linked branch |
| `remember(workspace, key, value, fixes=span)` | Save a durable fix |
| `search(query, **filters)` | Ranked full-text search |
| `what_failed(run or workspace)` | Errors, known fixes, next step |
| `sql(query, params)` | Plain SQL |

## Also available

- **TypeScript SDK:** `npm install rundb`. It uses the same file format, so
  Python and Node agents can share one database.
- **Full docs, benchmarks and design notes:**
  [github.com/ashishjsharda/rundb](https://github.com/ashishjsharda/rundb)

If RunDB saves your agent a retry loop, a ⭐ on
[GitHub](https://github.com/ashishjsharda/rundb) helps others find it.

---

Apache-2.0 · Built by [Ashish Sharda](https://github.com/ashishjsharda)
