# rundb (TypeScript)

TypeScript SDK for [RunDB](https://github.com/ashishjsharda/rundb): a local-first, single-file database for AI agent runs, retries, traces and memory. It has no dependencies because it uses the built-in `node:sqlite`, and it needs Node 22.16+ or 24+ (earlier versions of `node:sqlite` lack full-text search).

```bash
npm install rundb
```

```ts
import { connect } from "rundb";

const db = connect("agent.db");
const run = db.startRun("my-repo", "make tests pass");
db.logSpan(run, "tool", "pytest", { error: "ModuleNotFoundError: requests" });
const retry = db.forkRun(run, "install deps first");
db.remember("my-repo", "tests.setup", "pip install -r requirements.txt first", { kind: "constraint" });
db.endRun(retry, "succeeded");
console.log(db.whatFailed("my-repo").suggested_next_step);
console.log(db.search("ModuleNotFoundError"));
```

Files are interchangeable with the Python client, the `rundb` CLI and the MCP server. On Node 22 you'll see a one-line `ExperimentalWarning` for `node:sqlite`. Pass `--no-warnings=ExperimentalWarning` to hide it. The full docs are in the [main README](https://github.com/ashishjsharda/rundb#readme).
