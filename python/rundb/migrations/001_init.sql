-- RunDB schema, migration 001.
-- Canonical copy. python/rundb/migrations/ and ts/migrations/ must stay byte-identical
-- (enforced by tests). Timestamps are ISO-8601 UTC strings with milliseconds.
-- JSON columns are TEXT validated with json_valid().

CREATE TABLE workspaces (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  meta        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta))
);

CREATE TABLE runs (
  id             TEXT PRIMARY KEY,
  workspace_id   TEXT NOT NULL REFERENCES workspaces(id),
  parent_run_id  TEXT REFERENCES runs(id),
  branch_name    TEXT NOT NULL DEFAULT 'main',
  status         TEXT NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'succeeded', 'failed', 'aborted', 'forked')),
  goal           TEXT,
  model          TEXT,
  started_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  ended_at       TEXT,
  meta           TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta))
);
CREATE INDEX idx_runs_workspace ON runs(workspace_id, started_at);
CREATE INDEX idx_runs_parent    ON runs(parent_run_id);
CREATE INDEX idx_runs_status    ON runs(workspace_id, status, started_at);

-- Runs are append-only apart from status, ended_at and meta.
CREATE TRIGGER runs_append_only
BEFORE UPDATE OF id, workspace_id, parent_run_id, branch_name, goal, model, started_at ON runs
BEGIN
  SELECT RAISE(ABORT, 'runs are append-only: only status, ended_at and meta may change');
END;

CREATE TABLE spans (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES runs(id),
  parent_span_id  TEXT REFERENCES spans(id),
  kind            TEXT NOT NULL
                  CHECK (kind IN ('thought', 'tool', 'retrieve', 'write', 'eval', 'human')),
  name            TEXT NOT NULL,
  input           TEXT,
  output          TEXT,
  error           TEXT,
  tokens_in       INTEGER,
  tokens_out      INTEGER,
  started_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  ended_at        TEXT,
  meta            TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta))
);
CREATE INDEX idx_spans_run     ON spans(run_id, started_at);
CREATE INDEX idx_spans_parent  ON spans(parent_span_id);
CREATE INDEX idx_spans_errors  ON spans(run_id, started_at) WHERE error IS NOT NULL;

CREATE TABLE artifacts (
  id            TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES runs(id),
  span_id       TEXT REFERENCES spans(id),
  type          TEXT NOT NULL DEFAULT 'file',
  path_or_uri   TEXT NOT NULL,
  mime          TEXT,
  sha256        TEXT,
  text_preview  TEXT,
  meta          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta))
);
CREATE INDEX idx_artifacts_run  ON artifacts(run_id);
CREATE INDEX idx_artifacts_span ON artifacts(span_id);
CREATE INDEX idx_artifacts_sha  ON artifacts(sha256);

CREATE TABLE memories (
  id             TEXT PRIMARY KEY,
  workspace_id   TEXT NOT NULL REFERENCES workspaces(id),
  source_run_id  TEXT REFERENCES runs(id),
  kind           TEXT NOT NULL DEFAULT 'fact'
                 CHECK (kind IN ('fact', 'preference', 'decision', 'constraint')),
  key            TEXT NOT NULL,
  value          TEXT NOT NULL,
  confidence     REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  expires_at     TEXT,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX idx_memories_key    ON memories(workspace_id, key);
CREATE INDEX idx_memories_kind   ON memories(workspace_id, kind);
CREATE INDEX idx_memories_source ON memories(source_run_id);

CREATE TABLE chunks (
  id            TEXT PRIMARY KEY,
  workspace_id  TEXT NOT NULL REFERENCES workspaces(id),
  run_id        TEXT REFERENCES runs(id),
  memory_id     TEXT REFERENCES memories(id),
  artifact_id   TEXT REFERENCES artifacts(id),
  text          TEXT NOT NULL,
  embedding     BLOB
);
CREATE INDEX idx_chunks_workspace ON chunks(workspace_id);
CREATE INDEX idx_chunks_run       ON chunks(run_id);
CREATE INDEX idx_chunks_memory    ON chunks(memory_id);
CREATE INDEX idx_chunks_artifact  ON chunks(artifact_id);

-- Append-only log. Integer ids make tailing trivial.
CREATE TABLE events (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id   TEXT REFERENCES runs(id),
  ts       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  level    TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('debug', 'info', 'warn', 'error')),
  message  TEXT NOT NULL,
  payload  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload))
);
CREATE INDEX idx_events_run ON events(run_id, id);

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

-- Full-text search over spans, memories and chunks, in one index so ranks are comparable.
-- source is 'span' | 'memory' | 'chunk'.
CREATE VIRTUAL TABLE search_index USING fts5(
  body,
  title         UNINDEXED,
  source        UNINDEXED,
  workspace_id  UNINDEXED,
  run_id        UNINDEXED,
  span_id       UNINDEXED,
  memory_id     UNINDEXED,
  chunk_id      UNINDEXED,
  kind          UNINDEXED,
  ts            UNINDEXED,
  tokenize = 'porter unicode61'
);

CREATE TRIGGER spans_fts_insert AFTER INSERT ON spans BEGIN
  INSERT INTO search_index (body, title, source, workspace_id, run_id, span_id, kind, ts)
  VALUES (
    new.name || ' ' || coalesce(new.input, '') || ' ' || coalesce(new.output, '') || ' ' || coalesce(new.error, ''),
    new.name, 'span', (SELECT workspace_id FROM runs WHERE id = new.run_id),
    new.run_id, new.id, new.kind, new.started_at);
END;
CREATE TRIGGER spans_fts_delete AFTER DELETE ON spans BEGIN
  DELETE FROM search_index WHERE source = 'span' AND span_id = old.id;
END;
CREATE TRIGGER spans_fts_update AFTER UPDATE OF name, input, output, error ON spans BEGIN
  DELETE FROM search_index WHERE source = 'span' AND span_id = old.id;
  INSERT INTO search_index (body, title, source, workspace_id, run_id, span_id, kind, ts)
  VALUES (
    new.name || ' ' || coalesce(new.input, '') || ' ' || coalesce(new.output, '') || ' ' || coalesce(new.error, ''),
    new.name, 'span', (SELECT workspace_id FROM runs WHERE id = new.run_id),
    new.run_id, new.id, new.kind, new.started_at);
END;

CREATE TRIGGER memories_fts_insert AFTER INSERT ON memories BEGIN
  INSERT INTO search_index (body, title, source, workspace_id, run_id, memory_id, kind, ts)
  VALUES (new.key || ' ' || new.value, new.key, 'memory', new.workspace_id,
          new.source_run_id, new.id, new.kind, new.created_at);
END;
CREATE TRIGGER memories_fts_delete AFTER DELETE ON memories BEGIN
  DELETE FROM search_index WHERE source = 'memory' AND memory_id = old.id;
END;

CREATE TRIGGER chunks_fts_insert AFTER INSERT ON chunks BEGIN
  INSERT INTO search_index (body, title, source, workspace_id, run_id, chunk_id, memory_id, kind, ts)
  VALUES (new.text, NULL, 'chunk', new.workspace_id, new.run_id, new.id, new.memory_id, NULL,
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END;
CREATE TRIGGER chunks_fts_delete AFTER DELETE ON chunks BEGIN
  DELETE FROM search_index WHERE source = 'chunk' AND chunk_id = old.id;
END;
