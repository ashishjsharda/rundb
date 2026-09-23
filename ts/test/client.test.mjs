import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync, spawnSync } from "node:child_process";
import { connect, RunDBError } from "../dist/index.js";

const here = dirname(fileURLToPath(import.meta.url));
const tmp = () => join(mkdtempSync(join(tmpdir(), "rundb-")), "agent.db");

test("creates file, migrates, schema copy matches canonical", () => {
  const p = join(mkdtempSync(join(tmpdir(), "rundb-")), "a", "b", "agent.db");
  const db = connect(p);
  assert.ok(existsSync(p));
  assert.equal(db.schemaVersion, 1);
  db.close();
  assert.equal(connect(p).schemaVersion, 1);
  const canonical = readFileSync(join(here, "..", "..", "schema", "001_init.sql"));
  assert.deepEqual(readFileSync(join(here, "..", "migrations", "001_init.sql")), canonical);
});

test("run lifecycle and validation", () => {
  const db = connect(tmp());
  const run = db.startRun("app", "ship it", { model: "m1" });
  assert.equal(run.status, "running");
  db.logSpan(run, "tool", "pytest", { input: { args: ["-q"] }, output: "ok" });
  assert.equal(db.spans(run)[0].input, '{"args":["-q"]}');
  const done = db.endRun(run, "succeeded", "all green");
  assert.equal(JSON.parse(done.meta).summary, "all green");
  assert.throws(() => db.endRun(run, "failed"), RunDBError);
  assert.throws(() => db.logSpan(run, "banana", "x"), RunDBError);
  assert.throws(() => db.logSpan("run_missing", "tool", "x"), RunDBError);
  assert.throws(() => db.sql("UPDATE runs SET goal = 'x'"), /append-only/);
  assert.throws(() => db.sql("DELETE FROM events"), /append-only/);
});

test("fork links without copying spans", () => {
  const db = connect(tmp());
  const parent = db.startRun("app", "deploy", { model: "m1" });
  db.logSpan(parent, "tool", "deploy", { error: "missing AWS_REGION" });
  const child = db.forkRun(parent, "set region");
  assert.equal(child.parent_run_id, parent.id);
  assert.equal(child.branch_name, "fork-1");
  assert.equal(child.model, "m1");
  assert.deepEqual(db.spans(child), []);
  assert.equal(db.getRun(parent).status, "forked");
  const gc = db.forkRun(child);
  assert.deepEqual(db.lineage(gc).map((r) => r.id), [parent.id, child.id, gc.id]);
});

test("search spans, memories, chunks with filters", () => {
  const db = connect(tmp());
  const run = db.startRun("app", "deploy");
  const sid = db.logSpan(run, "tool", "run_deploy", { input: "./deploy.sh", error: "ENOENT: missing AWS_REGION" });
  db.remember("app", "deploy.env", "AWS_REGION must be us-east-1", { kind: "constraint", sourceRunId: run });
  db.addArtifact(run, "notes.md", { content: "The AWS_REGION variable lives in .env.production" });
  const hits = db.search("AWS_REGION");
  assert.deepEqual(new Set(hits.map((h) => h.source)), new Set(["span", "memory", "chunk"]));
  const span = hits.find((h) => h.source === "span");
  assert.equal(span.span_id, sid);
  assert.equal(span.run_id, run.id);
  assert.ok(db.search("./deploy.sh: ENOENT (missing)").length);
  assert.deepEqual(db.search("AWS_REGION", { workspace: "nope" }), []);
  assert.equal(db.search("AWS_REGION", { source: "memory" }).length, 1);
  assert.ok(db.search("AWS", { since: "1h" }).length);
  assert.ok(db.search("webpack region").length, "falls back to any-term");
  assert.throws(() => db.search('"oops', { raw: true }), RunDBError);
});

test("remember supersedes, expires, forget", () => {
  const db = connect(tmp());
  const run = db.startRun("app");
  db.remember("app", "db.port", "5432", { sourceRunId: run });
  db.endRun(run, "failed");
  db.remember("app", "db.port", "6543");
  db.remember("app", "tmp", "old", { expiresAt: "2000-01-01T00:00:00.000Z" });
  assert.deepEqual(db.recall("app").map((m) => m.value), ["6543"]);
  assert.equal(db.recall("app", "db.port", { includeHistory: true }).length, 2);
  assert.equal(db.forget("app", "db.port"), 2);
  assert.deepEqual(db.search("port"), []);
});

test("whatFailed: repeats, memories, resolved forks", () => {
  const db = connect(tmp());
  const run = db.startRun("app", "deploy");
  for (let i = 0; i < 3; i++) db.logSpan(run, "tool", "deploy", { error: `timeout after ${30 + i}s` });
  db.endRun(run, "failed");
  let r = db.whatFailed(run);
  assert.equal(r.repeated[0].count, 3);
  assert.match(r.suggested_next_step, /Stop retrying 'deploy'/);
  const fix = db.forkRun(run);
  db.endRun(fix, "succeeded");
  db.remember("app", "deploy.timeout", "use --timeout 120", { kind: "constraint" });
  r = db.whatFailed("app");
  assert.equal(r.resolved_by[0].id, fix.id);
  assert.match(r.suggested_next_step, /--timeout 120/);
  assert.throws(() => db.whatFailed("missing-ws"), RunDBError);
});

test("crash mid-run leaves run 'running', then abortStale aborts it", () => {
  const p = tmp();
  const script = `
    import { connect } from ${JSON.stringify(new URL("../dist/index.js", import.meta.url).href)};
    const db = connect(${JSON.stringify(p)});
    const run = db.startRun("app", "will crash");
    db.logSpan(run, "tool", "step1", { output: "ok" });
    console.log(run.id);
    process.exit(137);`;
  const res = spawnSync(process.execPath, ["--no-warnings", "--input-type=module", "-e", script], { encoding: "utf8" });
  assert.equal(res.status, 137, res.stderr);
  const id = res.stdout.trim();
  const db = connect(p);
  assert.equal(db.getRun(id).status, "running");
  assert.equal(db.spans(id).length, 1);
  assert.deepEqual(db.abortStale("1h"), []);
  assert.deepEqual(db.abortStale(0), [id]);
  assert.equal(db.getRun(id).status, "aborted");
});

test("batch rolls back, readonly sql, vectors", () => {
  const db = connect(tmp());
  const run = db.startRun("app");
  assert.throws(() => db.batch(() => { db.logSpan(run, "tool", "a"); throw new Error("x"); }));
  assert.deepEqual(db.spans(run), []);
  assert.throws(() => db.sql("DELETE FROM runs", [], { readonly: true }));
  db.startRun("app");
  db.addChunk("app", "north", { embedding: [1, 0] });
  db.addChunk("app", "east", { embedding: [0, 1] });
  assert.equal(db.similar([0.9, 0.1], { k: 1 })[0].text, "north");
});

test("interop: file written by Python is readable from TS and vice versa", (t) => {
  const PY = ["python3", "python"].find((c) => spawnSync(c, ["--version"]).status === 0);
  if (!PY) return t.skip("python not available");
  const p = tmp();
  const pyPath = join(here, "..", "..", "python");
  const out = spawnSync(PY, ["-c", `
import sys; sys.path.insert(0, ${JSON.stringify(pyPath)})
from rundb import connect
db = connect(${JSON.stringify(p)})
r = db.start_run("shared", goal="from python")
db.log_span(r, "tool", "pyspan", error="boom from python")
db.remember("shared", "lang", "python wrote this")
print(r.id)`], { encoding: "utf8" });
  assert.equal(out.status, 0, out.stderr);
  const db = connect(p);
  const id = out.stdout.trim();
  assert.equal(db.getRun(id).goal, "from python");
  assert.equal(db.search("boom", { workspace: "shared" })[0].span_id, db.spans(id)[0].id);
  const child = db.forkRun(id, "from ts");
  db.close();
  const back = spawnSync(PY, ["-c", `
import sys; sys.path.insert(0, ${JSON.stringify(pyPath)})
from rundb import connect
db = connect(${JSON.stringify(p)})
print(db.get_run(${JSON.stringify(child.id)}).parent_run_id)`], { encoding: "utf8" });
  assert.equal(back.stdout.trim(), id, back.stderr);
});
