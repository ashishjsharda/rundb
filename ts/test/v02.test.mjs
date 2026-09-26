import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync, spawnSync } from "node:child_process";
import { DatabaseSync } from "node:sqlite";
import { RunDB, errorSignature, clearFingerprintCache, drift } from "../dist/index.js";

const here = dirname(fileURLToPath(import.meta.url));
const tmp = () => join(mkdtempSync(join(tmpdir(), "rundb-")), "agent.db");
const open = (p = tmp(), opts = { fingerprint: false }) => new RunDB(p, opts);

test("signature folds digits, hex runs and uuids (same as Python)", () => {
  const pairs = [
    ["timeout after 30s", "timeout after 31s"],
    ["fatal: bad object a3f9c21e7b0d", "fatal: bad object 0b1c2d3e4f5a"],
    ["lock /tmp/9f86d081-884c-4d63-a1b2-0123456789ab.lock", "lock /tmp/1c3e5a7b-0000-4d63-a1b2-fedcba987654.lock"],
  ];
  for (const [a, b] of pairs) assert.equal(errorSignature(a), errorSignature(b));
  assert.equal(errorSignature("commit a3f9c21e not found"), "commit <hex> not found");
  assert.equal(errorSignature("deadbeef cafebabe"), "deadbeef cafebabe");
  assert.equal(errorSignature("short ab12 id"), "short ab# id");
});

test("signatures match the Python implementation byte for byte", (t) => {
  const PY = ["python3", "python"].find((c) => spawnSync(c, ["--version"]).status === 0);
  if (!PY) return t.skip("python not available");
  const samples = ["Timeout after 31s", "fatal: bad object A3F9C21E7B0D in 9f86d081-884c-4d63-a1b2-0123456789ab",
    "  ERESOLVE\n could not   resolve 0b1c2d3e4f  ", "deadbeef 12345678 x"];
  const out = spawnSync(PY, ["-c", `
import sys, json; sys.path.insert(0, ${JSON.stringify(join(here, "..", "..", "python"))})
from rundb.suggest import error_signature
print(json.dumps([error_signature(s) for s in json.loads(sys.argv[1])]))`, JSON.stringify(samples)], { encoding: "utf8" });
  assert.equal(out.status, 0, out.stderr);
  assert.deepEqual(JSON.parse(out.stdout), samples.map(errorSignature));
});

test("tool / version filters on search and whatFailed", () => {
  const db = open();
  const run = db.startRun("app");
  db.logSpan(run, "tool", "pytest", { error: "ImportError boom", version: "8.3" });
  db.logSpan(run, "tool", "pytest", { error: "ImportError boom", version: "7.4" });
  db.logSpan(run, "tool", "ruff", { error: "ImportError boom" });
  db.remember("app", "pytest.import", "pin pytest<8 for boom", { tool: "pytest", toolVersion: "7.4" });
  assert.equal(db.search("boom", { tool: "pytest", source: "span" }).length, 2);
  const v = db.search("boom", { tool: "pytest", version: "7.4" });
  assert.deepEqual(new Set(v.map((h) => `${h.source}:${h.title}`)), new Set(["span:pytest", "memory:pytest.import"]));
  assert.deepEqual(db.search("boom", { tool: "nope" }), []);
  const r = db.whatFailed(run, { tool: "pytest", version: "8.3" });
  assert.deepEqual(r.errors.map((e) => e.version), ["8.3"]);
  assert.equal(r.scope.tool, "pytest");
});

test("fixes= links a memory to a failure and matches exactly across tools", () => {
  const db = open();
  const run = db.startRun("app");
  const bad = db.logSpan(run, "tool", "pytest", { version: "8.3", error: "ModuleNotFoundError: requests (sha a3f9c21e7b)" });
  db.remember("app", "deps.requests", "pip install -r requirements.txt", { fixes: bad, kind: "constraint" });
  const mem = db.recall("app", "deps.requests")[0];
  assert.equal(mem.tool, "pytest");
  assert.equal(mem.tool_version, "8.3");
  assert.equal(mem.source_run_id, run.id);
  const other = db.startRun("app");
  db.logSpan(other, "tool", "tox", { error: "ModuleNotFoundError: requests (sha 99aa88bb77)" });
  const report = db.whatFailed(other);
  assert.equal(report.related_memories[0].key, "deps.requests");
  assert.equal(report.related_memories[0].match, "exact");
  assert.match(report.suggested_next_step, /Known fix in memory 'deps.requests'/);
  assert.throws(() => db.remember("app", "k", "v", { fixes: "spn_nope" }));
});

test("exact match prefers the same tool; text fallback still works", () => {
  const db = open();
  const run = db.startRun("app");
  db.remember("app", "for.tox", "tox fix", { tool: "tox", error: "E: boom 1" });
  db.remember("app", "for.pytest", "pytest fix", { tool: "pytest", error: "E: boom 2" });
  db.logSpan(run, "tool", "pytest", { error: "E: boom 3" });
  assert.deepEqual(db.whatFailed(run).related_memories.map((m) => m.key), ["for.pytest", "for.tox"]);
  const db2 = open();
  const r2 = db2.startRun("app");
  db2.remember("app", "deploy.region", "set AWS_REGION before deploy");
  db2.logSpan(r2, "tool", "deploy", { error: "missing AWS_REGION" });
  assert.equal(db2.whatFailed(r2).related_memories[0].match, "text");
});

test("environment fingerprint flags stale fixes when a lockfile changes", (t) => {
  const repo = join(mkdtempSync(join(tmpdir(), "rundb-repo-")), "proj");
  mkdirSync(repo);
  const git = (...a) => execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@t", ...a], { cwd: repo, stdio: "ignore" });
  try { git("init", "-q"); } catch { return t.skip("git not available"); }
  writeFileSync(join(repo, "package-lock.json"), '{"a":1}');
  writeFileSync(join(repo, "app.js"), "1");
  git("add", "-A"); git("commit", "-q", "-m", "init");
  clearFingerprintCache();
  const db = new RunDB(tmp(), { cwd: repo });
  const run = db.startRun("app");
  const bad = db.logSpan(run, "tool", "npm", { error: "ERESOLVE peer conflict" });
  db.remember("app", "npm.peer", "use --legacy-peer-deps", { fixes: bad });
  assert.equal(db.recall("app")[0].stale, false);

  writeFileSync(join(repo, "app.js"), "2");
  git("commit", "-qam", "code");
  clearFingerprintCache();
  let m = db.recall("app")[0];
  assert.equal(m.stale, false);
  assert.deepEqual(m.env_changes.map((c) => c.what), ["git_commit"]);

  writeFileSync(join(repo, "package-lock.json"), '{"a":2}');
  clearFingerprintCache();
  m = db.recall("app")[0];
  assert.equal(m.stale, true);
  assert.match(m.stale_reason, /package-lock.json/);
  db.logSpan(run, "tool", "npm", { error: "ERESOLVE peer conflict" });
  assert.match(db.whatFailed(run).suggested_next_step, /may be stale/);
  clearFingerprintCache();
});

test("drift rules", () => {
  const saved = { git_commit: "a".repeat(40), lockfiles: { "uv.lock": "1", "package-lock.json": "2" } };
  assert.deepEqual(drift(saved, saved), { stale: false, reason: null, changes: [] });
  const moved = drift(saved, { git_commit: "b".repeat(40), lockfiles: { "uv.lock": "9" } });
  assert.equal(moved.stale, true);
  assert.equal(drift(null, saved).stale, false);
});

test("v1 database upgrades in place", () => {
  const p = tmp();
  const raw = new DatabaseSync(p);
  raw.exec(readFileSync(join(here, "..", "migrations", "001_init.sql"), "utf8"));
  raw.exec("PRAGMA user_version=1");
  raw.exec("INSERT INTO workspaces (id, name) VALUES ('ws_1', 'app')");
  raw.exec("INSERT INTO runs (id, workspace_id) VALUES ('run_1', 'ws_1')");
  raw.exec("INSERT INTO spans (id, run_id, kind, name, error) VALUES ('spn_1', 'run_1', 'tool', 'pytest', 'boom 42')");
  raw.close();
  const db = open(p);
  assert.equal(db.schemaVersion, 2);
  assert.equal(db.spans("run_1")[0].version, null);
  assert.equal(db.whatFailed("run_1").errors[0].span_id, "spn_1");
});
