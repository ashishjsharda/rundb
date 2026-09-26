// RunDB TypeScript client. Zero dependencies: uses the built-in node:sqlite (Node >= 22.13).
// Same file format and same operations as the Python client.

import { createHash, randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";
import { drift, fingerprint, type Fingerprint } from "./env.js";
import { errorSignature, suggest } from "./suggest.js";

export { errorSignature, hintFor, suggest } from "./suggest.js";
export { clearFingerprintCache, drift, fingerprint, type Drift, type Fingerprint } from "./env.js";

export const RUN_STATUSES = ["running", "succeeded", "failed", "aborted", "forked"] as const;
export const TERMINAL_STATUSES = ["succeeded", "failed", "aborted", "forked"] as const;
export const SPAN_KINDS = ["thought", "tool", "retrieve", "write", "eval", "human"] as const;
export const MEMORY_KINDS = ["fact", "preference", "decision", "constraint"] as const;
export const EVENT_LEVELS = ["debug", "info", "warn", "error"] as const;

export type RunStatus = (typeof RUN_STATUSES)[number];
export type TerminalStatus = (typeof TERMINAL_STATUSES)[number];
export type SpanKind = (typeof SPAN_KINDS)[number];
export type MemoryKind = (typeof MEMORY_KINDS)[number];
export type EventLevel = (typeof EVENT_LEVELS)[number];
export type Row = Record<string, any>;

export interface Run {
  id: string;
  workspace_id: string;
  parent_run_id: string | null;
  branch_name: string;
  status: RunStatus;
  goal: string | null;
  model: string | null;
  started_at: string;
  ended_at: string | null;
  meta: string;
}
export type RunLike = string | { id: string };

export interface SearchHit {
  source: "span" | "memory" | "chunk";
  title: string | null;
  kind: string | null;
  workspace_id: string;
  run_id: string | null;
  span_id: string | null;
  memory_id: string | null;
  chunk_id: string | null;
  ts: string;
  snippet: string;
  score: number;
}

export class RunDBError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RunDBError";
  }
}

const MIGRATIONS_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..", "migrations");
const STOPWORDS = new Set([
  "the", "and", "for", "with", "error", "errors", "failed", "fail", "failure", "exception",
  "was", "not", "are", "this", "that", "from", "line", "file", "none", "null", "true", "false",
]);
const TERM = /[\p{L}\p{N}_]+/gu;
const UNITS: Record<string, number> = { "": 1, s: 1, m: 60, h: 3600, d: 86400, w: 604800 };

export const nowIso = (): string => new Date().toISOString();
export const newId = (prefix: string): string =>
  `${prefix}_${Date.now().toString(16).padStart(12, "0")}${randomBytes(5).toString("hex")}`;
const rid = (r: RunLike | null | undefined): string | null =>
  r == null ? null : typeof r === "string" ? r : r.id;
const terms = (text: string | null | undefined): string[] => (text ?? "").match(TERM) ?? [];
const clip = (t: string | null | undefined, n: number): string | null =>
  t == null ? null : t.length <= n ? t : t.slice(0, n - 1) + "…";
const toText = (v: unknown): string | null =>
  v === undefined || v === null ? null : typeof v === "string" ? v : JSON.stringify(v);
const toJson = (v: unknown): string => {
  if (v === undefined || v === null) return "{}";
  if (typeof v === "string") { JSON.parse(v); return v; }
  return JSON.stringify(v);
};
const nn = (v: unknown): any => (v === undefined ? null : v);
const plain = (row: any): Row => ({ ...row });

function seconds(text: string | number): number {
  if (typeof text === "number") return text;
  const m = /^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$/.exec(text);
  if (!m) throw new RunDBError(`bad duration ${JSON.stringify(text)}; use seconds or e.g. '30m', '12h', '7d'`);
  return parseFloat(m[1]) * UNITS[m[2]];
}

/** Accept a Date, ISO string, seconds ago (number) or a relative age like '30m', '2h', '7d'. */
export function parseSince(v: Date | string | number | null | undefined): string | null {
  if (v === undefined || v === null || v === "") return null;
  if (v instanceof Date) return v.toISOString();
  if (typeof v === "number" || /^\s*\d+\s*[smhdw]\s*$/.test(v)) {
    return new Date(Date.now() - seconds(v) * 1000).toISOString();
  }
  return v;
}

export function ftsQuery(text: string, mode: "and" | "or" = "and"): string | null {
  const t = terms(text);
  if (!t.length) return null;
  return t.map((x) => `"${x.replace(/"/g, '""')}"`).join(mode === "and" ? " AND " : " OR ");
}

function splitText(text: string, size = 2000): string[] {
  if (text.length <= size) return [text];
  const out: string[] = [];
  let start = 0;
  while (start < text.length) {
    let end = Math.min(text.length, start + size);
    if (end < text.length) {
      const nl = text.lastIndexOf("\n", end - 1);
      if (nl > start + size / 2) end = nl;
    }
    out.push(text.slice(start, end));
    start = end;
  }
  return out;
}

export interface StartRunOptions { model?: string | null; parentRunId?: RunLike | null; branchName?: string; meta?: unknown }
export interface LogSpanOptions {
  input?: unknown; output?: unknown; error?: unknown; tokensIn?: number; tokensOut?: number;
  parentSpanId?: string; startedAt?: string; endedAt?: string; meta?: unknown;
  /** Version of the tool called, e.g. "pytest 8.3". */
  version?: string;
}
export interface RememberOptions {
  kind?: MemoryKind; sourceRunId?: RunLike | null; confidence?: number; ttl?: string | number; expiresAt?: string;
  /** Id of the failed span this memory fixes: copies tool, version, error signature and run. */
  fixes?: string;
  tool?: string; toolVersion?: string; error?: string;
  /** Environment fingerprint override; false to skip stamping. */
  env?: Fingerprint | boolean | null;
}
export interface SearchOptions {
  workspace?: string; runId?: RunLike; kind?: string; source?: "span" | "memory" | "chunk";
  since?: Date | string | number; limit?: number; raw?: boolean;
  /** Span name / memory tool. */
  tool?: string;
  /** Span version / memory tool_version. */
  version?: string;
}
export interface RunDBOptions {
  timeoutMs?: number;
  /** Stamp memories with git commit + lockfile hashes (default on; RUNDB_FINGERPRINT=0 disables). */
  fingerprint?: boolean;
  /** Directory whose git repo and lockfiles are fingerprinted (default: process.cwd()). */
  cwd?: string;
}

// SQL fragment: memory row `m` is the latest version of its key.
const CURRENT_MEMORY = "NOT EXISTS (SELECT 1 FROM memories n WHERE n.workspace_id = m.workspace_id " +
  "AND n.key = m.key AND n.rowid > m.rowid)";

export class RunDB {
  readonly path: string;
  readonly db: DatabaseSync;
  readonly fingerprint: boolean;
  readonly cwd: string | null;
  private depth = 0;

  constructor(path?: string, opts: RunDBOptions = {}) {
    this.path = path ?? process.env.RUNDB_PATH ?? "agent.db";
    this.fingerprint = opts.fingerprint ??
      !["0", "false", "no", "off"].includes((process.env.RUNDB_FINGERPRINT ?? "1").toLowerCase());
    this.cwd = opts.cwd ?? null;
    if (this.path !== ":memory:") mkdirSync(dirname(resolve(this.path)), { recursive: true });
    this.db = new DatabaseSync(this.path);
    assertFts5(this.db);
    this.db.exec(`PRAGMA busy_timeout=${opts.timeoutMs ?? 10000}`);
    if (this.path !== ":memory:") this.db.exec("PRAGMA journal_mode=WAL");
    this.db.exec("PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;");
    this.migrate();
  }

  // ---------------------------------------------------------------- plumbing

  get schemaVersion(): number {
    return Number((this.db.prepare("PRAGMA user_version").get() as Row).user_version);
  }

  private migrate(): void {
    const files = readdirSync(MIGRATIONS_DIR).filter((f) => /^\d{3}.*\.sql$/.test(f)).sort();
    const pending = files.filter((f) => parseInt(f.slice(0, 3), 10) > this.schemaVersion);
    if (!pending.length) return;
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const current = this.schemaVersion;
      for (const f of pending) {
        const v = parseInt(f.slice(0, 3), 10);
        if (v <= current) continue;
        this.db.exec(readFileSync(resolve(MIGRATIONS_DIR, f), "utf8"));
        this.db.exec(`PRAGMA user_version=${v}`);
      }
      this.db.exec("COMMIT");
    } catch (e) {
      this.db.exec("ROLLBACK");
      throw e;
    }
  }

  /** Run fn inside one transaction (nestable). Much faster for bulk writes. */
  batch<T>(fn: (db: this) => T): T {
    const outer = this.depth === 0;
    if (outer) this.db.exec("BEGIN IMMEDIATE");
    this.depth++;
    try {
      const out = fn(this);
      this.depth--;
      if (outer) this.db.exec("COMMIT");
      return out;
    } catch (e) {
      this.depth--;
      if (outer) this.db.exec("ROLLBACK");
      throw e;
    }
  }

  private insert(table: string, cols: Record<string, unknown>): void {
    const keys = Object.keys(cols);
    this.db.prepare(`INSERT INTO ${table} (${keys.join(", ")}) VALUES (${keys.map(() => "?").join(", ")})`)
      .run(...keys.map((k) => nn(cols[k])));
  }
  private one(q: string, params: unknown[] = []): Row | undefined {
    const r = this.db.prepare(q).get(...params.map(nn));
    return r ? plain(r) : undefined;
  }
  private all(q: string, params: unknown[] = []): Row[] {
    return this.db.prepare(q).all(...params.map(nn)).map(plain);
  }

  close(): void {
    this.db.close();
  }

  // ---------------------------------------------------------------- workspaces

  workspace(nameOrId = "default", create = true): string | null {
    const row = this.one("SELECT id FROM workspaces WHERE id = ? OR name = ? LIMIT 1", [nameOrId, nameOrId]);
    if (row) return row.id;
    if (!create) return null;
    const id = newId("ws");
    try {
      this.batch(() => this.insert("workspaces", { id, name: nameOrId, created_at: nowIso() }));
    } catch (e: any) {
      if (/UNIQUE/.test(String(e?.message))) return this.workspace(nameOrId, false);
      throw e;
    }
    return id;
  }

  workspaces(): Row[] {
    return this.all("SELECT * FROM workspaces ORDER BY created_at");
  }

  // ---------------------------------------------------------------- runs

  startRun(workspace = "default", goal: string | null = null, opts: StartRunOptions = {}): Run {
    const id = newId("run");
    this.batch(() => {
      const wid = this.workspace(workspace)!;
      this.insert("runs", {
        id, workspace_id: wid, parent_run_id: rid(opts.parentRunId), branch_name: opts.branchName ?? "main",
        status: "running", goal, model: opts.model ?? null, started_at: nowIso(), meta: toJson(opts.meta),
      });
      this.event(id, "info", "run started", { goal, model: opts.model ?? null });
    });
    return this.getRun(id);
  }

  getRun(runId: RunLike): Run {
    const row = this.one("SELECT * FROM runs WHERE id = ?", [rid(runId)]);
    if (!row) throw new RunDBError(`run not found: ${rid(runId)}`);
    return row as Run;
  }

  listRuns(opts: { workspace?: string; status?: RunStatus; limit?: number } = {}): Run[] {
    const where: string[] = [];
    const params: unknown[] = [];
    if (opts.workspace) {
      const wid = this.workspace(opts.workspace, false);
      if (!wid) return [];
      where.push("workspace_id = ?"); params.push(wid);
    }
    if (opts.status) { where.push("status = ?"); params.push(opts.status); }
    const clause = where.length ? `WHERE ${where.join(" AND ")}` : "";
    return this.all(`SELECT * FROM runs ${clause} ORDER BY started_at DESC, rowid DESC LIMIT ?`,
      [...params, opts.limit ?? 20]) as Run[];
  }

  endRun(runId: RunLike, status: TerminalStatus = "succeeded", summary: string | null = null): Run {
    if (!(TERMINAL_STATUSES as readonly string[]).includes(status)) {
      throw new RunDBError(`status must be one of ${TERMINAL_STATUSES.join(", ")}, got ${status}`);
    }
    const id = rid(runId)!;
    this.batch(() => {
      const run = this.getRun(id);
      if (run.status !== "running") throw new RunDBError(`run ${id} already ended with status '${run.status}'`);
      if (summary != null) {
        this.db.prepare("UPDATE runs SET status = ?, ended_at = ?, meta = json_set(meta, '$.summary', ?) WHERE id = ?")
          .run(status, nowIso(), summary, id);
      } else {
        this.db.prepare("UPDATE runs SET status = ?, ended_at = ? WHERE id = ?").run(status, nowIso(), id);
      }
      const level = status === "failed" ? "error" : status === "aborted" ? "warn" : "info";
      this.event(id, level, `run ${status}` + (summary ? `: ${summary}` : ""), { status, summary });
    });
    return this.getRun(id);
  }

  abortRun(runId: RunLike, reason?: string): Run {
    return this.endRun(runId, "aborted", reason ?? "aborted");
  }

  /** Abort 'running' runs with no activity since `olderThan` (seconds or '30m', '1h'...). */
  abortStale(olderThan: string | number = "1h", workspace?: string): string[] {
    const cutoff = new Date(Date.now() - seconds(olderThan) * 1000).toISOString();
    const where = ["r.status = 'running'"];
    const params: unknown[] = [];
    if (workspace) {
      const wid = this.workspace(workspace, false);
      if (!wid) return [];
      where.push("r.workspace_id = ?"); params.push(wid);
    }
    const ids = this.all(
      `SELECT r.id FROM runs r WHERE ${where.join(" AND ")}
         AND max(r.started_at,
                 coalesce((SELECT max(coalesce(s.ended_at, s.started_at)) FROM spans s WHERE s.run_id = r.id), ''),
                 coalesce((SELECT max(e.ts) FROM events e WHERE e.run_id = r.id), '')) < ?`,
      [...params, cutoff]).map((r) => r.id as string);
    for (const id of ids) this.endRun(id, "aborted", `stale: no activity since ${cutoff}`);
    return ids;
  }

  /** New run linked by parent_run_id. Spans are not copied. A running parent is closed as 'forked'. */
  forkRun(runId: RunLike, goal?: string | null, opts: { branchName?: string; model?: string } = {}): Run {
    const parent = this.getRun(runId);
    return this.batch(() => {
      const n = Number(this.one("SELECT count(*) AS n FROM runs WHERE parent_run_id = ?", [parent.id])!.n);
      const child = this.startRun(parent.workspace_id, goal ?? parent.goal, {
        model: opts.model ?? parent.model, parentRunId: parent.id, branchName: opts.branchName ?? `fork-${n + 1}`,
      });
      if (parent.status === "running") this.endRun(parent.id, "forked", `forked into ${child.id}`);
      else this.event(parent.id, "info", `forked into ${child.id}`, { child: child.id });
      return child;
    });
  }

  /** The run and its ancestors, oldest first. */
  lineage(runId: RunLike): Run[] {
    return this.all(
      `WITH RECURSIVE chain(id, depth) AS (
         SELECT ?, 0
         UNION ALL
         SELECT r.parent_run_id, c.depth + 1 FROM runs r JOIN chain c ON r.id = c.id
         WHERE r.parent_run_id IS NOT NULL AND c.depth < 1000)
       SELECT runs.* FROM chain JOIN runs ON runs.id = chain.id ORDER BY chain.depth DESC`, [rid(runId)]) as Run[];
  }

  // ---------------------------------------------------------------- spans, events

  logSpan(runId: RunLike, kind: SpanKind, name: string, opts: LogSpanOptions = {}): string {
    if (!(SPAN_KINDS as readonly string[]).includes(kind)) {
      throw new RunDBError(`kind must be one of ${SPAN_KINDS.join(", ")}, got ${kind}`);
    }
    const id = newId("spn");
    const r = rid(runId)!;
    const ts = nowIso();
    const err = toText(opts.error);
    this.batch(() => {
      try {
        this.insert("spans", {
          id, run_id: r, parent_span_id: opts.parentSpanId, kind, name, version: opts.version,
          input: toText(opts.input), output: toText(opts.output), error: err,
          tokens_in: opts.tokensIn, tokens_out: opts.tokensOut,
          started_at: opts.startedAt ?? ts, ended_at: opts.endedAt ?? ts, meta: toJson(opts.meta),
        });
      } catch (e: any) {
        if (/FOREIGN KEY/.test(String(e?.message))) throw new RunDBError(`run or parent span not found: ${r}`);
        throw e;
      }
      if (err) this.event(r, "error", `${kind} ${name}: ${clip(err, 500)}`, { span_id: id });
      else {
        const out = toText(opts.output);
        this.event(r, "info", `${kind} ${name}` + (out != null ? ` -> ${clip(out, 120)}` : ""), { span_id: id });
      }
    });
    return id;
  }

  spans(runId: RunLike, opts: { errorsOnly?: boolean; limit?: number } = {}): Row[] {
    let q = "SELECT * FROM spans WHERE run_id = ?" + (opts.errorsOnly ? " AND error IS NOT NULL" : "");
    q += " ORDER BY started_at, rowid";
    const params: unknown[] = [rid(runId)];
    if (opts.limit) { q += " LIMIT ?"; params.push(opts.limit); }
    return this.all(q, params);
  }

  private event(runId: string | null, level: EventLevel, message: string, payload?: unknown): number {
    const res = this.db.prepare("INSERT INTO events (run_id, ts, level, message, payload) VALUES (?, ?, ?, ?, ?)")
      .run(runId, nowIso(), level, message, toJson(payload));
    return Number(res.lastInsertRowid);
  }

  logEvent(runId: RunLike | null, message: string, level: EventLevel = "info", payload?: unknown): number {
    if (!(EVENT_LEVELS as readonly string[]).includes(level)) throw new RunDBError(`bad level ${level}`);
    return this.batch(() => this.event(rid(runId), level, message, payload));
  }

  events(runId?: RunLike | null, opts: { afterId?: number; limit?: number } = {}): Row[] {
    if (runId) {
      return this.all("SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
        [rid(runId), opts.afterId ?? 0, opts.limit ?? 100]);
    }
    return this.all("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", [opts.afterId ?? 0, opts.limit ?? 100]);
  }

  // ---------------------------------------------------------------- artifacts, chunks

  addArtifact(runId: RunLike, pathOrUri: string, opts: {
    type?: string; spanId?: string; mime?: string; content?: string | Uint8Array; meta?: unknown; index?: boolean;
  } = {}): string {
    const run = this.getRun(runId);
    let data: Buffer | null = null;
    if (typeof opts.content === "string") data = Buffer.from(opts.content, "utf8");
    else if (opts.content) data = Buffer.from(opts.content);
    else if (existsSync(pathOrUri) && statSync(pathOrUri).isFile() && statSync(pathOrUri).size <= 5 * 1024 * 1024) {
      data = readFileSync(pathOrUri);
    }
    let text: string | null = null;
    let sha: string | null = null;
    if (data) {
      sha = createHash("sha256").update(data).digest("hex");
      try { text = new TextDecoder("utf-8", { fatal: true }).decode(data); } catch { text = null; }
    }
    const id = newId("art");
    this.batch(() => {
      this.insert("artifacts", {
        id, run_id: run.id, span_id: opts.spanId, type: opts.type ?? "file", path_or_uri: pathOrUri,
        mime: opts.mime ?? guessMime(pathOrUri), sha256: sha, text_preview: clip(text, 500), meta: toJson(opts.meta),
      });
      if ((opts.index ?? true) && text) {
        for (const piece of splitText(text)) this.addChunk(run.workspace_id, piece, { runId: run.id, artifactId: id });
      }
    });
    return id;
  }

  artifacts(runId: RunLike): Row[] {
    return this.all("SELECT * FROM artifacts WHERE run_id = ? ORDER BY rowid", [rid(runId)]);
  }

  addChunk(workspace: string, text: string, opts: {
    runId?: RunLike; memoryId?: string; artifactId?: string; embedding?: number[] | Float32Array;
  } = {}): string {
    const id = newId("chk");
    this.batch(() => {
      const wid = this.workspace(workspace)!;
      this.insert("chunks", {
        id, workspace_id: wid, run_id: rid(opts.runId), memory_id: opts.memoryId, artifact_id: opts.artifactId, text,
        embedding: opts.embedding ? new Uint8Array(Float32Array.from(opts.embedding).buffer) : null,
      });
    });
    return id;
  }

  /** Brute-force cosine similarity over chunks with embeddings. */
  similar(embedding: number[], opts: { workspace?: string; k?: number } = {}): Row[] {
    const where = ["embedding IS NOT NULL"];
    const params: unknown[] = [];
    if (opts.workspace) {
      const wid = this.workspace(opts.workspace, false);
      if (!wid) return [];
      where.push("workspace_id = ?"); params.push(wid);
    }
    const qn = Math.hypot(...embedding) || 1;
    const scored: Row[] = [];
    for (const row of this.all(
      `SELECT id, workspace_id, run_id, memory_id, artifact_id, text, embedding FROM chunks WHERE ${where.join(" AND ")}`,
      params)) {
      const buf = row.embedding as Uint8Array;
      const v = new Float32Array(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
      if (v.length !== embedding.length) continue;
      let dot = 0, vn = 0;
      for (let i = 0; i < v.length; i++) { dot += v[i] * embedding[i]; vn += v[i] * v[i]; }
      const { embedding: _drop, ...rest } = row;
      scored.push({ ...rest, score: dot / (qn * (Math.sqrt(vn) || 1)) });
    }
    return scored.sort((a, b) => b.score - a.score).slice(0, opts.k ?? 10);
  }

  // ---------------------------------------------------------------- memories

  remember(workspace: string, key: string, value: unknown, opts: RememberOptions = {}): string {
    const kind = opts.kind ?? "fact";
    if (!(MEMORY_KINDS as readonly string[]).includes(kind)) {
      throw new RunDBError(`kind must be one of ${MEMORY_KINDS.join(", ")}, got ${kind}`);
    }
    let expiresAt = opts.expiresAt ?? null;
    if (opts.ttl != null && !expiresAt) expiresAt = new Date(Date.now() + seconds(opts.ttl) * 1000).toISOString();
    let tool = opts.tool ?? null;
    let toolVersion = opts.toolVersion ?? null;
    let error = opts.error ?? null;
    let sourceRunId = rid(opts.sourceRunId);
    if (opts.fixes) {
      const span = this.one("SELECT run_id, name, version, error FROM spans WHERE id = ?", [opts.fixes]);
      if (!span) throw new RunDBError(`span not found: ${opts.fixes}`);
      tool = tool ?? span.name;
      toolVersion = toolVersion ?? span.version;
      error = error ?? span.error;
      sourceRunId = sourceRunId ?? span.run_id;
    }
    let env: Fingerprint | null = null;
    if (opts.env === undefined || opts.env === null || opts.env === true) {
      env = this.fingerprint || opts.env === true ? fingerprint(this.cwd) : null;
    } else if (opts.env !== false) {
      env = opts.env;
    }
    const id = newId("mem");
    this.batch(() => {
      const wid = this.workspace(workspace)!;
      this.insert("memories", {
        id, workspace_id: wid, source_run_id: sourceRunId, kind, key, value: toText(value),
        confidence: opts.confidence, expires_at: expiresAt, created_at: nowIso(),
        tool, tool_version: toolVersion, error_signature: error ? errorSignature(error) : null,
        fixes_span_id: opts.fixes ?? null, env: env ? JSON.stringify(env) : null,
      });
    });
    return id;
  }

  /** Current memories; each row gets stale / stale_reason / env_changes. */
  recall(workspace: string, key?: string | null, opts: {
    kind?: MemoryKind; tool?: string; limit?: number; includeHistory?: boolean;
  } = {}): Row[] {
    const wid = this.workspace(workspace, false);
    if (!wid) return [];
    const where = ["m.workspace_id = ?", "(m.expires_at IS NULL OR m.expires_at > ?)"];
    const params: unknown[] = [wid, nowIso()];
    if (key) { where.push("m.key = ?"); params.push(key); }
    if (opts.kind) { where.push("m.kind = ?"); params.push(opts.kind); }
    if (opts.tool) { where.push("m.tool = ?"); params.push(opts.tool); }
    if (!opts.includeHistory) where.push(CURRENT_MEMORY);
    return this.annotate(this.all(
      `SELECT m.* FROM memories m WHERE ${where.join(" AND ")} ORDER BY m.rowid DESC LIMIT ?`,
      [...params, opts.limit ?? 50]));
  }

  private annotate(memories: Row[]): Row[] {
    const current = this.fingerprint && memories.length ? fingerprint(this.cwd) : null;
    for (const m of memories) {
      const d = drift(m.env ? JSON.parse(m.env) : null, current);
      m.stale = d.stale;
      m.stale_reason = d.reason;
      m.env_changes = d.changes;
    }
    return memories;
  }

  forget(workspace: string, key: string): number {
    const wid = this.workspace(workspace, false);
    if (!wid) return 0;
    return this.batch(() => {
      this.db.prepare("UPDATE chunks SET memory_id = NULL WHERE memory_id IN (SELECT id FROM memories WHERE workspace_id = ? AND key = ?)")
        .run(wid, key);
      return Number(this.db.prepare("DELETE FROM memories WHERE workspace_id = ? AND key = ?").run(wid, key).changes);
    });
  }

  // ---------------------------------------------------------------- search

  /** Ranked full-text search over spans, memories and chunks. All terms first, then any term. */
  search(query: string, opts: SearchOptions = {}): SearchHit[] {
    const filters: string[] = [];
    const params: unknown[] = [];
    if (opts.workspace) {
      const wid = this.workspace(opts.workspace, false);
      if (!wid) return [];
      filters.push("search_index.workspace_id = ?"); params.push(wid);
    }
    if (opts.runId) { filters.push("search_index.run_id = ?"); params.push(rid(opts.runId)); }
    if (opts.kind) { filters.push("search_index.kind = ?"); params.push(opts.kind); }
    if (opts.source) { filters.push("search_index.source = ?"); params.push(opts.source); }
    const cutoff = parseSince(opts.since);
    if (cutoff) { filters.push("search_index.ts >= ?"); params.push(cutoff); }
    if (opts.tool || opts.version) {
      const sw: string[] = [], mw: string[] = [], sp: unknown[] = [], mp: unknown[] = [];
      if (opts.tool) { sw.push("name = ?"); sp.push(opts.tool); mw.push("tool = ?"); mp.push(opts.tool); }
      if (opts.version) { sw.push("version = ?"); sp.push(opts.version); mw.push("tool_version = ?"); mp.push(opts.version); }
      filters.push(`((search_index.source = 'span' AND search_index.span_id IN (SELECT id FROM spans WHERE ${sw.join(" AND ")})) OR ` +
        `(search_index.source = 'memory' AND search_index.memory_id IN (SELECT id FROM memories WHERE ${mw.join(" AND ")})))`);
      params.push(...sp, ...mp);
    }

    const queries: string[] = [];
    if (opts.raw) queries.push(query);
    else {
      const q = ftsQuery(query, "and");
      if (q) queries.push(q);
      if (terms(query).length > 1) queries.push(ftsQuery(query, "or")!);
    }
    const extra = filters.map((f) => ` AND ${f}`).join("");
    const sql = `
      SELECT search_index.source, search_index.title, search_index.kind,
             search_index.workspace_id, search_index.run_id, search_index.span_id,
             search_index.memory_id, search_index.chunk_id, search_index.ts,
             snippet(search_index, 0, '[', ']', '…', 16) AS snippet,
             -bm25(search_index) AS score
      FROM search_index
      WHERE search_index MATCH ?${extra}
        AND (search_index.source != 'memory' OR search_index.memory_id IN (
             SELECT m.id FROM memories m
             WHERE (m.expires_at IS NULL OR m.expires_at > ?)
               AND NOT EXISTS (SELECT 1 FROM memories n WHERE n.workspace_id = m.workspace_id
                               AND n.key = m.key AND n.rowid > m.rowid)))
      ORDER BY bm25(search_index) LIMIT ?`;
    const now = nowIso();
    for (const q of queries) {
      let rows: Row[];
      try {
        rows = this.all(sql, [q, ...params, now, opts.limit ?? 20]);
      } catch (e: any) {
        if (opts.raw) throw new RunDBError(`invalid FTS5 query: ${e?.message}`);
        throw e;
      }
      if (rows.length) return rows.map((r) => ({ ...r, score: Math.round(r.score * 1e6) / 1e6 })) as SearchHit[];
    }
    return [];
  }

  // ---------------------------------------------------------------- diagnosis

  /** Recent failures for a run (+ ancestors) or a workspace, with a deterministic next step. */
  /**
   * Recent failures for a run (+ ancestors) or a workspace, known fixes, and a next step.
   * Fixes whose error signature exactly matches come first (same tool preferred, other tools
   * still apply); otherwise full-text search. Each fix is flagged if possibly stale.
   */
  whatFailed(target?: RunLike | null, opts: { limit?: number; tool?: string; version?: string } = {}): Row {
    const limit = opts.limit ?? 10;
    const tid = rid(target);
    let scope: Row;
    let runIds: string[] | null = null;
    let wid: string | null = null;
    if (tid && this.one("SELECT 1 AS x FROM runs WHERE id = ?", [tid])) {
      const chain = this.lineage(tid);
      runIds = chain.map((r) => r.id);
      wid = chain[chain.length - 1].workspace_id;
      scope = { type: "run", id: tid, workspace_id: wid, lineage: runIds };
    } else if (tid) {
      wid = this.workspace(tid, false);
      if (!wid) throw new RunDBError(`no run or workspace named '${tid}'`);
      scope = { type: "workspace", id: wid };
    } else {
      scope = { type: "all" };
    }

    let runFilter = "1 = 1";
    let rparams: unknown[] = [];
    if (runIds) { runFilter = `r.id IN (${runIds.map(() => "?").join(",")})`; rparams = runIds; }
    else if (wid) { runFilter = "r.workspace_id = ?"; rparams = [wid]; }

    let spanFilter = "";
    const sparams: unknown[] = [];
    if (opts.tool) { spanFilter += " AND s.name = ?"; sparams.push(opts.tool); }
    if (opts.version) { spanFilter += " AND s.version = ?"; sparams.push(opts.version); }
    const errors = this.all(
      `SELECT s.run_id, s.id AS span_id, s.kind, s.name, s.version, s.error, s.input, s.started_at AS at
       FROM spans s JOIN runs r ON r.id = s.run_id
       WHERE ${runFilter} AND s.error IS NOT NULL${spanFilter}
       ORDER BY s.started_at DESC, s.rowid DESC LIMIT 200`, [...rparams, ...sparams])
      .map((e): Row => ({
        ...e, signature: errorSignature(e.error), input: clip(e.input, 300), error: clip(e.error, 1000),
      }));

    const groups = new Map<string, Row>();
    for (const e of errors) {
      const k = `${e.name}\u0000${e.signature}`;
      const g = groups.get(k) ?? { name: e.name, error: e.error, signature: e.signature, count: 0, run_ids: [] as string[] };
      g.count++;
      if (!g.run_ids.includes(e.run_id)) g.run_ids.push(e.run_id);
      groups.set(k, g);
    }
    const repeated = [...groups.values()].filter((g) => g.count >= 2).sort((a, b) => b.count - a.count);

    const failedRuns = this.all(
      `SELECT r.id, r.goal, r.status, r.branch_name, r.parent_run_id, r.ended_at,
              json_extract(r.meta, '$.summary') AS summary
       FROM runs r WHERE ${runFilter} AND r.status IN ('failed', 'aborted')
       ORDER BY r.started_at DESC LIMIT ?`, [...rparams, limit]);
    const running = this.all(
      `SELECT r.id, r.goal, r.started_at FROM runs r WHERE ${runFilter} AND r.status = 'running'
       ORDER BY r.started_at DESC LIMIT 5`, rparams);

    let resolvedBy: Row[] = [];
    const failedIds = failedRuns.length ? failedRuns.map((r) => r.id) : errors.length ? [errors[0].run_id] : [];
    if (failedIds.length) {
      resolvedBy = this.all(
        `WITH RECURSIVE d(id) AS (
           SELECT id FROM runs WHERE parent_run_id IN (${failedIds.map(() => "?").join(",")})
           UNION SELECT r.id FROM runs r JOIN d ON r.parent_run_id = d.id)
         SELECT runs.id, runs.goal, runs.ended_at FROM runs JOIN d ON d.id = runs.id
         WHERE runs.status = 'succeeded' ORDER BY runs.ended_at DESC LIMIT 3`, failedIds);
    }

    const memories = errors.length ? this.relatedFixes(errors, repeated, wid) : [];
    if (opts.tool) scope.tool = opts.tool;
    if (opts.version) scope.version = opts.version;

    return {
      scope,
      failed_runs: failedRuns,
      errors: errors.slice(0, limit),
      repeated: repeated.slice(0, 5),
      related_memories: memories,
      resolved_by: resolvedBy,
      running,
      suggested_next_step: suggest(errors, repeated, memories, resolvedBy, running),
    };
  }

  private relatedFixes(errors: Row[], repeated: Row[], wid: string | null): Row[] {
    const latest = errors[0];
    const sigs = [...new Set([latest.signature, ...repeated.slice(0, 3).map((g) => g.signature)])];
    const where = [`m.error_signature IN (${sigs.map(() => "?").join(",")})`,
      "(m.expires_at IS NULL OR m.expires_at > ?)", CURRENT_MEMORY];
    const params: unknown[] = [...sigs, nowIso()];
    if (wid) { where.push("m.workspace_id = ?"); params.push(wid); }
    let rows = this.all(
      `SELECT m.* FROM memories m WHERE ${where.join(" AND ")}
       ORDER BY (m.error_signature = ?) DESC, (m.tool IS ?) DESC, m.rowid DESC LIMIT 3`,
      [...params, latest.signature, latest.name]);
    let match = "exact";
    if (!rows.length) {
      match = "text";
      const words = terms([latest.name, latest.error].filter(Boolean).join(" "))
        .filter((t) => t.length >= 3 && !STOPWORDS.has(t.toLowerCase()) && !/^\d+$/.test(t));
      const hits = words.length
        ? this.search([...new Set(words)].join(" "), { workspace: wid ?? undefined, source: "memory", limit: 3 })
        : [];
      if (hits.length) {
        const ids = hits.map((h) => h.memory_id);
        const byId = new Map(this.all(
          `SELECT * FROM memories WHERE id IN (${ids.map(() => "?").join(",")})`, ids).map((m) => [m.id, m]));
        rows = ids.filter((i) => byId.has(i)).map((i) => byId.get(i)!);
      }
    }
    this.annotate(rows);
    const keep = ["id", "key", "value", "kind", "confidence", "source_run_id", "tool", "tool_version",
      "fixes_span_id", "created_at", "stale", "stale_reason", "env_changes"];
    const out: Row[] = rows.map((m) => ({ ...Object.fromEntries(keep.map((k) => [k, m[k] ?? null])), match }));
    // stable sort: fresh fixes first, ranking otherwise preserved
    return out.map((m, i) => [m, i] as const)
      .sort((a, b) => Number(a[0].stale) - Number(b[0].stale) || a[1] - b[1]).map(([m]) => m);
  }

  // ---------------------------------------------------------------- raw SQL

  sql(query: string, params: unknown[] = [], opts: { readonly?: boolean } = {}): Row[] {
    if (opts.readonly) this.db.exec("PRAGMA query_only = ON");
    try {
      return this.all(query, params);
    } finally {
      if (opts.readonly) this.db.exec("PRAGMA query_only = OFF");
    }
  }
}

function guessMime(p: string): string | null {
  const ext = p.toLowerCase().split(".").pop() ?? "";
  const map: Record<string, string> = {
    txt: "text/plain", md: "text/markdown", json: "application/json", html: "text/html", csv: "text/csv",
    py: "text/x-python", ts: "text/typescript", js: "text/javascript", png: "image/png", jpg: "image/jpeg",
    jpeg: "image/jpeg", pdf: "application/pdf", log: "text/plain", yaml: "application/yaml", yml: "application/yaml",
  };
  return map[ext] ?? null;
}

/** node:sqlite only ships FTS5 from Node 22.16 / 24.0; fail early with a clear message. */
function assertFts5(db: DatabaseSync): void {
  try {
    db.exec("CREATE VIRTUAL TABLE temp.rundb_fts5_probe USING fts5(x); DROP TABLE temp.rundb_fts5_probe;");
  } catch {
    db.close();
    throw new RunDBError(
      `RunDB needs SQLite full-text search (FTS5), which Node.js ${process.version} does not include. ` +
      "Upgrade to Node.js 22.16+ (LTS) or 24+.");
  }
}

/** Open (and create/migrate if needed) a RunDB file. Default: $RUNDB_PATH or ./agent.db. */
export function connect(path?: string): RunDB {
  return new RunDB(path);
}

export default connect;
