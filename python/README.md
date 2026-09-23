# rundb (Python)

Python SDK, CLI and MCP server for [RunDB](https://github.com/ashishjsharda/rundb): a local-first, single-file database for AI agent runs, retries, traces and memory. It depends only on the standard library.

```bash
pip install rundb-ai        # installs the `rundb` package and the `rundb` CLI
```

```python
from rundb import connect

db = connect("agent.db")
run = db.start_run("my-repo", goal="make tests pass")
db.log_span(run, "tool", "pytest", error="ModuleNotFoundError: requests")
retry = db.fork_run(run, goal="install deps first")
db.remember("my-repo", "tests.setup", "pip install -r requirements.txt first", kind="constraint")
db.end_run(retry, "succeeded")
print(db.what_failed("my-repo")["suggested_next_step"])
```

MCP server: `rundb mcp` (stdio). The full docs are in the [main README](https://github.com/ashishjsharda/rundb#readme).
