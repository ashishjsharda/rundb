-- RunDB schema, migration 002 (v0.2.0): tool versions, exact fix matching,
-- environment fingerprints. Canonical copy; python/ and ts/ copies must match.

-- Version of the tool a span called (e.g. "pytest 8.3.2").
ALTER TABLE spans ADD COLUMN version TEXT;
CREATE INDEX idx_spans_tool ON spans(name, version);

-- What a memory fixes, so what_failed() can match it exactly:
--   tool / tool_version   the tool the fix applies to
--   error_signature       normalized error text (see suggest.error_signature)
--   fixes_span_id         the failed span this memory resolves
--   env                   JSON fingerprint at save time: {"git_commit": ..., "lockfiles": {...}}
ALTER TABLE memories ADD COLUMN tool TEXT;
ALTER TABLE memories ADD COLUMN tool_version TEXT;
ALTER TABLE memories ADD COLUMN error_signature TEXT;
ALTER TABLE memories ADD COLUMN fixes_span_id TEXT REFERENCES spans(id);
ALTER TABLE memories ADD COLUMN env TEXT CHECK (env IS NULL OR json_valid(env));
CREATE INDEX idx_memories_signature ON memories(workspace_id, error_signature);
CREATE INDEX idx_memories_tool ON memories(workspace_id, tool);
