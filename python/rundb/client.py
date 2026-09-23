"""RunDB Python client. Standard library only (sqlite3 + FTS5)."""

from __future__ import annotations

import hashlib
import math
import mimetypes
import os
import re
import sqlite3
import struct
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Iterator, Sequence, Union

from ._util import clip, fts_query, iso, new_id, now_iso, parse_since, terms, to_json, to_text
from .suggest import error_signature, suggest

RUN_STATUSES = ("running", "succeeded", "failed", "aborted", "forked")
TERMINAL_STATUSES = ("succeeded", "failed", "aborted", "forked")
SPAN_KINDS = ("thought", "tool", "retrieve", "write", "eval", "human")
MEMORY_KINDS = ("fact", "preference", "decision", "constraint")
EVENT_LEVELS = ("debug", "info", "warn", "error")
DEFAULT_PATH = "agent.db"

_STOPWORDS = {
    "the", "and", "for", "with", "error", "errors", "failed", "fail", "failure", "exception",
    "was", "not", "are", "this", "that", "from", "line", "file", "none", "null", "true", "false",
}


class RunDBError(Exception):
    """Raised for invalid arguments or state transitions."""


@dataclass
class Run:
    id: str
    workspace_id: str
    parent_run_id: str | None
    branch_name: str
    status: str
    goal: str | None
    model: str | None
    started_at: str
    ended_at: str | None
    meta: str = field(default="{}")

    def __str__(self) -> str:  # lets a Run be passed anywhere a run id is expected
        return self.id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


RunLike = Union[str, Run]


def _rid(run: RunLike | None) -> str | None:
    if run is None:
        return None
    return run.id if isinstance(run, Run) else str(run)


def _migrations() -> list[tuple[int, str, str]]:
    out = []
    for entry in resources.files("rundb").joinpath("migrations").iterdir():
        name = entry.name
        if name.endswith(".sql") and name[:3].isdigit():
            out.append((int(name[:3]), name, entry.read_text(encoding="utf-8")))
    return sorted(out)


def _statements(script: str) -> Iterator[str]:
    """Split a SQL script into complete statements (handles trigger bodies)."""
    buf = ""
    for line in script.splitlines(keepends=True):
        if not buf and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            yield buf.strip()
            buf = ""
    if buf.strip():
        raise RunDBError(f"incomplete SQL statement in migration: {buf[:80]!r}")


def connect(path: str | os.PathLike[str] | None = None) -> "RunDB":
    """Open (and create/migrate if needed) a RunDB file. Default: $RUNDB_PATH or ./agent.db."""
    return RunDB(path)


class RunDB:
    """One SQLite file holding runs, spans, artifacts, memories, chunks and events."""

    def __init__(self, path: str | os.PathLike[str] | None = None, *, timeout: float = 10.0):
        path = path or os.environ.get("RUNDB_PATH") or DEFAULT_PATH
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self.conn = sqlite3.connect(
            self.path, timeout=timeout, isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # ------------------------------------------------------------------ plumbing

    def _migrate(self) -> None:
        pending = [m for m in _migrations() if m[0] > self.schema_version]
        if not pending:
            return
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                current = self.schema_version  # re-check under the write lock
                for version, _name, sql in pending:
                    if version <= current:
                        continue
                    for stmt in _statements(sql):
                        self.conn.execute(stmt)
                    self.conn.execute(f"PRAGMA user_version={version}")
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    @contextmanager
    def batch(self) -> Iterator["RunDB"]:
        """Group writes into one transaction (much faster for bulk inserts). Nestable."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self.conn.execute("COMMIT")

    def _insert(self, table: str, cols: dict[str, Any]) -> None:
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        self.conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})", list(cols.values()))

    def _one(self, query: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        row = self.conn.execute(query, params).fetchone()
        return dict(row) if row else None

    def _all(self, query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "RunDB":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ workspaces

    def workspace(self, name_or_id: str = "default", *, create: bool = True) -> str | None:
        """Resolve a workspace by id or name; create it by name if missing."""
        row = self._one("SELECT id FROM workspaces WHERE id = ? OR name = ? LIMIT 1",
                        (name_or_id, name_or_id))
        if row:
            return row["id"]
        if not create:
            return None
        wid = new_id("ws")
        with self.batch():
            try:
                self._insert("workspaces", {"id": wid, "name": name_or_id, "created_at": now_iso()})
            except sqlite3.IntegrityError:  # created concurrently by another process
                return self.workspace(name_or_id, create=False)
        return wid

    def workspaces(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM workspaces ORDER BY created_at")

    # ------------------------------------------------------------------ runs

    def start_run(
        self,
        workspace: str = "default",
        goal: str | None = None,
        model: str | None = None,
        parent_run_id: RunLike | None = None,
        *,
        branch_name: str | None = None,
        meta: Any = None,
    ) -> Run:
        """Start a run. Returns a Run (str(run) is its id)."""
        with self.batch():
            wid = self.workspace(workspace)
            rid = new_id("run")
            ts = now_iso()
            self._insert("runs", {
                "id": rid, "workspace_id": wid, "parent_run_id": _rid(parent_run_id),
                "branch_name": branch_name or "main", "status": "running", "goal": goal,
                "model": model, "started_at": ts, "meta": to_json(meta),
            })
            self._event(rid, "info", "run started", {"goal": goal, "model": model})
        return self.get_run(rid)

    def get_run(self, run_id: RunLike) -> Run:
        row = self._one("SELECT * FROM runs WHERE id = ?", (_rid(run_id),))
        if not row:
            raise RunDBError(f"run not found: {_rid(run_id)}")
        return Run(**row)

    def list_runs(
        self,
        workspace: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[Run]:
        where, params = [], []
        if workspace:
            wid = self.workspace(workspace, create=False)
            if not wid:
                return []
            where.append("workspace_id = ?")
            params.append(wid)
        if status:
            where.append("status = ?")
            params.append(status)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self._all(f"SELECT * FROM runs {clause} ORDER BY started_at DESC, rowid DESC LIMIT ?",
                         (*params, limit))
        return [Run(**r) for r in rows]

    def end_run(self, run_id: RunLike, status: str = "succeeded", summary: str | None = None) -> Run:
        """Finish a run. Only 'running' runs can be ended."""
        if status not in TERMINAL_STATUSES:
            raise RunDBError(f"status must be one of {TERMINAL_STATUSES}, got {status!r}")
        rid = _rid(run_id)
        with self.batch():
            run = self.get_run(rid)
            if run.status != "running":
                raise RunDBError(f"run {rid} already ended with status {run.status!r}")
            meta_sql = "json_set(meta, '$.summary', ?)" if summary is not None else "meta"
            params: list[Any] = [status, now_iso()]
            if summary is not None:
                params.append(summary)
            self.conn.execute(
                f"UPDATE runs SET status = ?, ended_at = ?, meta = {meta_sql} WHERE id = ?",
                (*params, rid),
            )
            level = "error" if status == "failed" else "warn" if status == "aborted" else "info"
            self._event(rid, level, f"run {status}" + (f": {summary}" if summary else ""),
                        {"status": status, "summary": summary})
        return self.get_run(rid)

    def abort_run(self, run_id: RunLike, reason: str | None = None) -> Run:
        return self.end_run(run_id, "aborted", reason or "aborted")

    def abort_stale(self, older_than: float | str = "1h", workspace: str | None = None) -> list[str]:
        """Abort 'running' runs with no span/event activity since `older_than` (seconds or '30m')."""
        cutoff = parse_since(older_than)
        where, params = ["r.status = 'running'"], []
        if workspace:
            wid = self.workspace(workspace, create=False)
            if not wid:
                return []
            where.append("r.workspace_id = ?")
            params.append(wid)
        rows = self._all(
            f"""SELECT r.id FROM runs r
                WHERE {' AND '.join(where)}
                  AND max(r.started_at,
                          coalesce((SELECT max(coalesce(s.ended_at, s.started_at)) FROM spans s WHERE s.run_id = r.id), ''),
                          coalesce((SELECT max(e.ts) FROM events e WHERE e.run_id = r.id), '')) < ?""",
            (*params, cutoff),
        )
        ids = [r["id"] for r in rows]
        for rid in ids:
            self.end_run(rid, "aborted", f"stale: no activity since {cutoff}")
        return ids

    def fork_run(
        self,
        run_id: RunLike,
        goal: str | None = None,
        *,
        branch_name: str | None = None,
        model: str | None = None,
    ) -> Run:
        """Branch a run: new run linked by parent_run_id. Spans are NOT copied.

        If the parent is still running it is closed with status 'forked'.
        """
        parent = self.get_run(run_id)
        with self.batch():
            n = self.conn.execute("SELECT count(*) FROM runs WHERE parent_run_id = ?",
                                  (parent.id,)).fetchone()[0]
            child = self.start_run(
                parent.workspace_id,
                goal=goal or parent.goal,
                model=model or parent.model,
                parent_run_id=parent.id,
                branch_name=branch_name or f"fork-{n + 1}",
            )
            if parent.status == "running":
                self.end_run(parent.id, "forked", f"forked into {child.id}")
            else:
                self._event(parent.id, "info", f"forked into {child.id}", {"child": child.id})
        return child

    def lineage(self, run_id: RunLike) -> list[Run]:
        """The run and its ancestors, oldest first."""
        rows = self._all(
            """WITH RECURSIVE chain(id, depth) AS (
                 SELECT ?, 0
                 UNION ALL
                 SELECT r.parent_run_id, c.depth + 1 FROM runs r JOIN chain c ON r.id = c.id
                 WHERE r.parent_run_id IS NOT NULL AND c.depth < 1000)
               SELECT runs.* FROM chain JOIN runs ON runs.id = chain.id ORDER BY chain.depth DESC""",
            (_rid(run_id),),
        )
        return [Run(**r) for r in rows]

    # ------------------------------------------------------------------ spans, events

    def log_span(
        self,
        run_id: RunLike,
        kind: str,
        name: str,
        input: Any = None,
        output: Any = None,
        error: Any = None,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        parent_span_id: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        meta: Any = None,
    ) -> str:
        """Append a span. Non-string input/output/error are stored as JSON. Returns span id."""
        if kind not in SPAN_KINDS:
            raise RunDBError(f"kind must be one of {SPAN_KINDS}, got {kind!r}")
        rid = _rid(run_id)
        sid = new_id("spn")
        ts = now_iso()
        err = to_text(error)
        with self.batch():
            try:
                self._insert("spans", {
                    "id": sid, "run_id": rid, "parent_span_id": parent_span_id, "kind": kind,
                    "name": name, "input": to_text(input), "output": to_text(output), "error": err,
                    "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "started_at": started_at or ts, "ended_at": ended_at or ts, "meta": to_json(meta),
                })
            except sqlite3.IntegrityError as exc:
                if "FOREIGN KEY" in str(exc):
                    raise RunDBError(f"run or parent span not found: {rid}") from exc
                raise
            if err:
                self._event(rid, "error", f"{kind} {name}: {clip(err, 500)}", {"span_id": sid})
            else:
                self._event(rid, "info", f"{kind} {name}" + (f" -> {clip(to_text(output), 120)}" if output is not None else ""),
                            {"span_id": sid})
        return sid

    def spans(self, run_id: RunLike, *, errors_only: bool = False, limit: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM spans WHERE run_id = ?" + (" AND error IS NOT NULL" if errors_only else "")
        q += " ORDER BY started_at, rowid"
        params: list[Any] = [_rid(run_id)]
        if limit:
            q += " LIMIT ?"
            params.append(limit)
        return self._all(q, params)

    def _event(self, run_id: str | None, level: str, message: str, payload: Any = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (run_id, ts, level, message, payload) VALUES (?, ?, ?, ?, ?)",
            (run_id, now_iso(), level, message, to_json(payload)),
        )
        return int(cur.lastrowid)

    def log_event(self, run_id: RunLike | None, message: str, level: str = "info", payload: Any = None) -> int:
        if level not in EVENT_LEVELS:
            raise RunDBError(f"level must be one of {EVENT_LEVELS}, got {level!r}")
        with self.batch():
            return self._event(_rid(run_id), level, message, payload)

    def events(self, run_id: RunLike | None = None, *, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if run_id:
            return self._all("SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
                             (_rid(run_id), after_id, limit))
        return self._all("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit))

    # ------------------------------------------------------------------ artifacts, chunks

    def add_artifact(
        self,
        run_id: RunLike,
        path_or_uri: str,
        *,
        type: str = "file",
        span_id: str | None = None,
        mime: str | None = None,
        content: bytes | str | None = None,
        meta: Any = None,
        index: bool = True,
    ) -> str:
        """Record an artifact. If content is given (or path is a small local file) it is hashed,
        previewed and, when it is text, indexed for search."""
        rid = _rid(run_id)
        run = self.get_run(rid)
        data: bytes | None
        if isinstance(content, str):
            data = content.encode("utf-8")
        else:
            data = content
        if data is None:
            p = Path(path_or_uri)
            if p.is_file() and p.stat().st_size <= 5 * 1024 * 1024:
                data = p.read_bytes()
        text: str | None = None
        sha = None
        if data is not None:
            sha = hashlib.sha256(data).hexdigest()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        aid = new_id("art")
        with self.batch():
            self._insert("artifacts", {
                "id": aid, "run_id": rid, "span_id": span_id, "type": type,
                "path_or_uri": path_or_uri, "mime": mime or mimetypes.guess_type(path_or_uri)[0],
                "sha256": sha, "text_preview": clip(text, 500), "meta": to_json(meta),
            })
            if index and text:
                for piece in _split(text):
                    self.add_chunk(run.workspace_id, piece, run_id=rid, artifact_id=aid)
        return aid

    def artifacts(self, run_id: RunLike) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM artifacts WHERE run_id = ? ORDER BY rowid", (_rid(run_id),))

    def add_chunk(
        self,
        workspace: str,
        text: str,
        *,
        run_id: RunLike | None = None,
        memory_id: str | None = None,
        artifact_id: str | None = None,
        embedding: Sequence[float] | None = None,
    ) -> str:
        """Add searchable text. Embeddings are optional (stored as float32 little-endian)."""
        cid = new_id("chk")
        with self.batch():
            wid = self.workspace(workspace)
            self._insert("chunks", {
                "id": cid, "workspace_id": wid, "run_id": _rid(run_id), "memory_id": memory_id,
                "artifact_id": artifact_id, "text": text,
                "embedding": _pack(embedding) if embedding is not None else None,
            })
        return cid

    def similar(self, embedding: Sequence[float], *, workspace: str | None = None, k: int = 10) -> list[dict[str, Any]]:
        """Brute-force cosine similarity over chunks that have embeddings. Fine for ~100k chunks."""
        where, params = ["embedding IS NOT NULL"], []
        if workspace:
            wid = self.workspace(workspace, create=False)
            if not wid:
                return []
            where.append("workspace_id = ?")
            params.append(wid)
        q = list(embedding)
        qn = math.sqrt(sum(x * x for x in q)) or 1.0
        scored = []
        for row in self.conn.execute(
            f"SELECT id, workspace_id, run_id, memory_id, artifact_id, text, embedding FROM chunks WHERE {' AND '.join(where)}",
            params,
        ):
            v = _unpack(row["embedding"])
            if len(v) != len(q):
                continue
            vn = math.sqrt(sum(x * x for x in v)) or 1.0
            score = sum(a * b for a, b in zip(q, v)) / (qn * vn)
            d = dict(row)
            d.pop("embedding")
            d["score"] = score
            scored.append(d)
        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[:k]

    # ------------------------------------------------------------------ memories

    def remember(
        self,
        workspace: str,
        key: str,
        value: Any,
        kind: str = "fact",
        source_run_id: RunLike | None = None,
        *,
        confidence: float | None = None,
        ttl: float | str | None = None,
        expires_at: str | None = None,
    ) -> str:
        """Store a durable memory. Memories outlive failed runs. A newer value for the same key
        supersedes the older one (history is kept)."""
        if kind not in MEMORY_KINDS:
            raise RunDBError(f"kind must be one of {MEMORY_KINDS}, got {kind!r}")
        if ttl is not None and expires_at is None:
            seconds = float(ttl) if not isinstance(ttl, str) else _ttl_seconds(ttl)
            expires_at = iso(datetime.now(timezone.utc) + timedelta(seconds=seconds))
        mid = new_id("mem")
        with self.batch():
            wid = self.workspace(workspace)
            self._insert("memories", {
                "id": mid, "workspace_id": wid, "source_run_id": _rid(source_run_id), "kind": kind,
                "key": key, "value": to_text(value), "confidence": confidence,
                "expires_at": expires_at, "created_at": now_iso(),
            })
        return mid

    def recall(
        self,
        workspace: str,
        key: str | None = None,
        *,
        kind: str | None = None,
        limit: int = 50,
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        """Current (latest, unexpired) memories, optionally filtered by exact key or kind."""
        wid = self.workspace(workspace, create=False)
        if not wid:
            return []
        where = ["m.workspace_id = ?", "(m.expires_at IS NULL OR m.expires_at > ?)"]
        params: list[Any] = [wid, now_iso()]
        if key:
            where.append("m.key = ?")
            params.append(key)
        if kind:
            where.append("m.kind = ?")
            params.append(kind)
        if not include_history:
            where.append("NOT EXISTS (SELECT 1 FROM memories n WHERE n.workspace_id = m.workspace_id "
                         "AND n.key = m.key AND n.rowid > m.rowid)")
        return self._all(
            f"SELECT m.* FROM memories m WHERE {' AND '.join(where)} ORDER BY m.rowid DESC LIMIT ?",
            (*params, limit),
        )

    def forget(self, workspace: str, key: str) -> int:
        """Delete every version of a memory key. Returns rows deleted."""
        wid = self.workspace(workspace, create=False)
        if not wid:
            return 0
        with self.batch():
            self.conn.execute(
                "UPDATE chunks SET memory_id = NULL WHERE memory_id IN "
                "(SELECT id FROM memories WHERE workspace_id = ? AND key = ?)", (wid, key))
            cur = self.conn.execute("DELETE FROM memories WHERE workspace_id = ? AND key = ?", (wid, key))
        return cur.rowcount

    # ------------------------------------------------------------------ search

    def search(
        self,
        query: str,
        *,
        workspace: str | None = None,
        run_id: RunLike | None = None,
        kind: str | None = None,
        source: str | None = None,
        since: Any = None,
        limit: int = 20,
        raw: bool = False,
    ) -> list[dict[str, Any]]:
        """Ranked full-text search over spans, memories and chunks.

        Filters: workspace (name or id), run_id, kind (span or memory kind), source
        ('span'|'memory'|'chunk'), since (ISO time or '2h'/'7d'). All terms must match;
        if nothing does, falls back to any-term matching. raw=True passes FTS5 syntax through.
        """
        filters, params = [], []
        if workspace:
            wid = self.workspace(workspace, create=False)
            if not wid:
                return []
            filters.append("search_index.workspace_id = ?")
            params.append(wid)
        if run_id:
            filters.append("search_index.run_id = ?")
            params.append(_rid(run_id))
        if kind:
            filters.append("search_index.kind = ?")
            params.append(kind)
        if source:
            filters.append("search_index.source = ?")
            params.append(source)
        cutoff = parse_since(since)
        if cutoff:
            filters.append("search_index.ts >= ?")
            params.append(cutoff)

        if raw:
            queries = [query]
        else:
            queries = [q for q in (fts_query(query, "and"),) if q]
            if len(terms(query)) > 1:
                queries.append(fts_query(query, "or"))
        extra = "".join(f" AND {f}" for f in filters)
        sql = f"""
            SELECT search_index.source, search_index.title, search_index.kind,
                   search_index.workspace_id, search_index.run_id, search_index.span_id,
                   search_index.memory_id, search_index.chunk_id, search_index.ts,
                   snippet(search_index, 0, '[', ']', '…', 16) AS snippet,
                   -bm25(search_index) AS score
            FROM search_index
            WHERE search_index MATCH ?{extra}
              AND (search_index.source != 'memory' OR search_index.memory_id IN (
                   SELECT m.id FROM memories m
                   WHERE (m.expires_at IS NULL OR m.expires_at > ?)
                     AND NOT EXISTS (SELECT 1 FROM memories n WHERE n.workspace_id = m.workspace_id
                                     AND n.key = m.key AND n.rowid > m.rowid)))
            ORDER BY bm25(search_index) LIMIT ?"""
        now = now_iso()
        for q in queries:
            try:
                rows = self._all(sql, (q, *params, now, limit))
            except sqlite3.OperationalError as exc:
                if raw:
                    raise RunDBError(f"invalid FTS5 query: {exc}") from exc
                raise
            if rows:
                for r in rows:
                    r["score"] = round(r["score"], 6)
                return rows
        return []

    # ------------------------------------------------------------------ diagnosis

    def what_failed(self, target: RunLike | None = None, *, limit: int = 10) -> dict[str, Any]:
        """Summarize recent failures for a run (and its ancestors) or a workspace, with a
        deterministic suggested next step. target: run id, workspace name/id, or None (all)."""
        tid = _rid(target)
        scope: dict[str, Any]
        run_ids: list[str] | None = None
        wid: str | None = None
        if tid and self._one("SELECT 1 FROM runs WHERE id = ?", (tid,)):
            chain = self.lineage(tid)
            run_ids = [r.id for r in chain]
            wid = chain[-1].workspace_id
            scope = {"type": "run", "id": tid, "workspace_id": wid, "lineage": run_ids}
        elif tid:
            wid = self.workspace(tid, create=False)
            if not wid:
                raise RunDBError(f"no run or workspace named {tid!r}")
            scope = {"type": "workspace", "id": wid}
        else:
            scope = {"type": "all"}

        if run_ids is not None:
            marks = ",".join("?" for _ in run_ids)
            run_filter, rparams = f"r.id IN ({marks})", list(run_ids)
        elif wid:
            run_filter, rparams = "r.workspace_id = ?", [wid]
        else:
            run_filter, rparams = "1 = 1", []

        errors = self._all(
            f"""SELECT s.run_id, s.id AS span_id, s.kind, s.name, s.error, s.input, s.started_at AS at
                FROM spans s JOIN runs r ON r.id = s.run_id
                WHERE {run_filter} AND s.error IS NOT NULL
                ORDER BY s.started_at DESC, s.rowid DESC LIMIT 200""",
            rparams,
        )
        for e in errors:
            e["input"] = clip(e["input"], 300)
            e["error"] = clip(e["error"], 1000)

        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for e in errors:
            k = (e["name"], error_signature(e["error"]))
            g = groups.setdefault(k, {"name": e["name"], "error": e["error"], "count": 0, "run_ids": []})
            g["count"] += 1
            if e["run_id"] not in g["run_ids"]:
                g["run_ids"].append(e["run_id"])
        repeated = sorted((g for g in groups.values() if g["count"] >= 2), key=lambda g: -g["count"])

        failed_runs = self._all(
            f"""SELECT r.id, r.goal, r.status, r.branch_name, r.parent_run_id, r.ended_at,
                       json_extract(r.meta, '$.summary') AS summary
                FROM runs r WHERE {run_filter} AND r.status IN ('failed', 'aborted')
                ORDER BY r.started_at DESC LIMIT ?""",
            (*rparams, limit),
        )
        running = self._all(
            f"SELECT r.id, r.goal, r.started_at FROM runs r WHERE {run_filter} AND r.status = 'running' "
            f"ORDER BY r.started_at DESC LIMIT 5", rparams)

        # Descendant runs of failed ones that succeeded.
        resolved_by: list[dict[str, Any]] = []
        failed_ids = [r["id"] for r in failed_runs] or ([errors[0]["run_id"]] if errors else [])
        if failed_ids:
            marks = ",".join("?" for _ in failed_ids)
            resolved_by = self._all(
                f"""WITH RECURSIVE d(id) AS (
                      SELECT id FROM runs WHERE parent_run_id IN ({marks})
                      UNION SELECT r.id FROM runs r JOIN d ON r.parent_run_id = d.id)
                    SELECT runs.id, runs.goal, runs.ended_at FROM runs JOIN d ON d.id = runs.id
                    WHERE runs.status = 'succeeded' ORDER BY runs.ended_at DESC LIMIT 3""",
                failed_ids,
            )

        memories: list[dict[str, Any]] = []
        if errors:
            words = [t for t in terms(" ".join(filter(None, [errors[0]["name"], errors[0]["error"]])))
                     if len(t) >= 3 and t.lower() not in _STOPWORDS and not t.isdigit()]
            if words:
                hits = self.search(" ".join(dict.fromkeys(words)), workspace=wid, source="memory", limit=3)
                if hits:
                    ids = [h["memory_id"] for h in hits]
                    marks = ",".join("?" for _ in ids)
                    by_id = {m["id"]: m for m in self._all(
                        f"SELECT id, key, value, kind, confidence, source_run_id FROM memories WHERE id IN ({marks})", ids)}
                    memories = [by_id[i] for i in ids if i in by_id]

        return {
            "scope": scope,
            "failed_runs": failed_runs,
            "errors": errors[:limit],
            "repeated": repeated[:5],
            "related_memories": memories,
            "resolved_by": resolved_by,
            "running": running,
            "suggested_next_step": suggest(errors, repeated, memories, resolved_by, running),
        }

    # ------------------------------------------------------------------ raw SQL

    def sql(self, query: str, params: Sequence[Any] | dict[str, Any] = (), *, readonly: bool = False) -> list[dict[str, Any]]:
        """Run any SQL. readonly=True rejects writes (used by the MCP server)."""
        with self._lock:
            if readonly:
                self.conn.execute("PRAGMA query_only = ON")
            try:
                return self._all(query, params)
            finally:
                if readonly:
                    self.conn.execute("PRAGMA query_only = OFF")


def _split(text: str, size: int = 2000) -> list[str]:
    if len(text) <= size:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            nl = text.rfind("\n", start + size // 2, end)
            if nl > start:
                end = nl
        out.append(text[start:end])
        start = end
    return out


def _pack(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _ttl_seconds(text: str) -> float:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", text)
    if not m:
        raise RunDBError(f"bad ttl {text!r}; use seconds or e.g. '30m', '12h', '7d'")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
