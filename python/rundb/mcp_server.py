"""RunDB MCP server: JSON-RPC 2.0 over stdio, no dependencies.

Run it with `rundb mcp` (or `rundb-mcp`). Configure with RUNDB_PATH (database file)
and RUNDB_WORKSPACE (default workspace name).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Callable, TextIO
from urllib.parse import unquote

from . import __version__
from .client import MEMORY_KINDS, SPAN_KINDS, TERMINAL_STATUSES, Run, RunDB, RunDBError

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = (
    "RunDB records your runs, steps and lessons in a local database. Call start_run when you begin "
    "a task, log_span after each tool call (put failures in `error`), and end_run when done. "
    "Before retrying something that failed, call what_failed. When you learn a fix or a rule, call "
    "remember so later runs find it with search."
)


def _s(type_: str, desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": type_, "description": desc, **extra}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "start_run",
        "description": "Start a run for a task. Returns the run; pass its id to log_span/end_run.",
        "inputSchema": _obj({
            "goal": _s("string", "What this run is trying to achieve."),
            "workspace": _s("string", "Workspace name (default from RUNDB_WORKSPACE or 'default')."),
            "model": _s("string", "Model name, for later comparison."),
            "parent_run_id": _s("string", "Run this continues from, if any."),
        }, ["goal"]),
    },
    {
        "name": "log_span",
        "description": "Record one step of a run (tool call, thought, retrieval...). Put failures in `error`.",
        "inputSchema": _obj({
            "run_id": _s("string", "Run id from start_run."),
            "kind": _s("string", "Step type.", enum=list(SPAN_KINDS)),
            "name": _s("string", "Short step name, e.g. the tool name."),
            "input": _s("string", "What went in (command, args, prompt)."),
            "output": _s("string", "What came out."),
            "error": _s("string", "Error message if the step failed."),
            "tokens_in": _s("integer", "Prompt tokens used."),
            "tokens_out": _s("integer", "Completion tokens used."),
            "parent_span_id": _s("string", "Enclosing span id, for nesting."),
        }, ["run_id", "kind", "name"]),
    },
    {
        "name": "end_run",
        "description": "Finish a run with a final status.",
        "inputSchema": _obj({
            "run_id": _s("string", "Run id."),
            "status": _s("string", "Final status.", enum=list(TERMINAL_STATUSES)),
            "summary": _s("string", "One-line outcome."),
        }, ["run_id", "status"]),
    },
    {
        "name": "fork_run",
        "description": "Retry on a new branch linked to a previous run. Old spans stay on the old run.",
        "inputSchema": _obj({
            "run_id": _s("string", "Run to branch from."),
            "goal": _s("string", "New goal; defaults to the parent's goal."),
        }, ["run_id"]),
    },
    {
        "name": "what_failed",
        "description": "Recent errors for a run (and its ancestors) or a workspace, plus a suggested next step. Call before retrying.",
        "inputSchema": _obj({
            "target": _s("string", "Run id or workspace name. Omit for the default workspace."),
        }, []),
    },
    {
        "name": "remember",
        "description": "Save a durable lesson (fix, rule, preference). Survives failed runs; same key overwrites.",
        "inputSchema": _obj({
            "key": _s("string", "Stable key, e.g. 'deploy.env.AWS_REGION'."),
            "value": _s("string", "The thing to remember."),
            "kind": _s("string", "Memory type.", enum=list(MEMORY_KINDS)),
            "workspace": _s("string", "Workspace name."),
            "source_run_id": _s("string", "Run where this was learned."),
            "confidence": _s("number", "0..1", minimum=0, maximum=1),
            "ttl": _s("string", "Expire after e.g. '12h' or '30d'. Omit to keep forever."),
        }, ["key", "value"]),
    },
    {
        "name": "recall",
        "description": "List current memories in a workspace, optionally by exact key or kind.",
        "inputSchema": _obj({
            "workspace": _s("string", "Workspace name."),
            "key": _s("string", "Exact key."),
            "kind": _s("string", "Memory type.", enum=list(MEMORY_KINDS)),
        }, []),
    },
    {
        "name": "search",
        "description": "Full-text search over past steps, errors, memories and artifacts. Returns run_id/span_id hits.",
        "inputSchema": _obj({
            "query": _s("string", "Words to find."),
            "workspace": _s("string", "Limit to a workspace."),
            "run_id": _s("string", "Limit to a run."),
            "kind": _s("string", "Span or memory kind, e.g. 'tool' or 'constraint'."),
            "source": _s("string", "What to search.", enum=["span", "memory", "chunk"]),
            "since": _s("string", "ISO time or age like '2h', '7d'."),
            "limit": _s("integer", "Max hits (default 10).", minimum=1, maximum=100),
        }, ["query"]),
    },
    {
        "name": "list_runs",
        "description": "Most recent runs, newest first.",
        "inputSchema": _obj({
            "workspace": _s("string", "Workspace name."),
            "status": _s("string", "Filter by status.", enum=["running", *TERMINAL_STATUSES]),
            "limit": _s("integer", "Max runs (default 10).", minimum=1, maximum=100),
        }, []),
    },
    {
        "name": "get_run",
        "description": "One run with its spans.",
        "inputSchema": _obj({"run_id": _s("string", "Run id.")}, ["run_id"]),
    },
    {
        "name": "sql",
        "description": "Read-only SQL over tables workspaces, runs, spans, artifacts, memories, chunks, events.",
        "inputSchema": _obj({
            "query": _s("string", "A SELECT statement. Use ? placeholders."),
            "params": {"type": "array", "description": "Values for ? placeholders.", "items": {}},
        }, ["query"]),
    },
]

RESOURCE_TEMPLATES = [
    {
        "uriTemplate": "rundb://workspace/{id}/latest-failures",
        "name": "latest-failures",
        "description": "Recent errors in a workspace (id or name) with a suggested next step.",
        "mimeType": "application/json",
    },
    {
        "uriTemplate": "rundb://run/{id}",
        "name": "run",
        "description": "A run with its spans and events.",
        "mimeType": "application/json",
    },
]


def _compact(obj: Any) -> Any:
    if isinstance(obj, Run):
        obj = obj.to_dict()
    if isinstance(obj, dict):
        return {k: _compact(v) for k, v in obj.items() if v is not None and v != "{}"}
    if isinstance(obj, list):
        return [_compact(v) for v in obj]
    return obj


class Server:
    def __init__(self, db: RunDB, default_workspace: str = "default"):
        self.db = db
        self.ws = default_workspace
        self.tools: dict[str, Callable[[dict[str, Any]], Any]] = {
            "start_run": self._start_run,
            "log_span": self._log_span,
            "end_run": lambda a: self.db.end_run(a["run_id"], a["status"], a.get("summary")),
            "fork_run": lambda a: self.db.fork_run(a["run_id"], a.get("goal")),
            "what_failed": lambda a: self.db.what_failed(a.get("target") or self.ws),
            "remember": self._remember,
            "recall": lambda a: self.db.recall(a.get("workspace") or self.ws, a.get("key"), kind=a.get("kind")),
            "search": self._search,
            "list_runs": lambda a: self.db.list_runs(a.get("workspace"), a.get("status"), a.get("limit", 10)),
            "get_run": lambda a: {"run": self.db.get_run(a["run_id"]), "spans": self.db.spans(a["run_id"])},
            "sql": lambda a: self.db.sql(a["query"], a.get("params") or [], readonly=True),
        }

    # -- tool handlers
    def _start_run(self, a: dict[str, Any]) -> Any:
        return self.db.start_run(a.get("workspace") or self.ws, a["goal"], a.get("model"), a.get("parent_run_id"))

    def _log_span(self, a: dict[str, Any]) -> Any:
        sid = self.db.log_span(
            a["run_id"], a["kind"], a["name"], a.get("input"), a.get("output"), a.get("error"),
            tokens_in=a.get("tokens_in"), tokens_out=a.get("tokens_out"), parent_span_id=a.get("parent_span_id"),
        )
        return {"span_id": sid}

    def _remember(self, a: dict[str, Any]) -> Any:
        mid = self.db.remember(
            a.get("workspace") or self.ws, a["key"], a["value"], a.get("kind", "fact"),
            a.get("source_run_id"), confidence=a.get("confidence"), ttl=a.get("ttl"),
        )
        return {"memory_id": mid}

    def _search(self, a: dict[str, Any]) -> Any:
        return self.db.search(
            a["query"], workspace=a.get("workspace"), run_id=a.get("run_id"), kind=a.get("kind"),
            source=a.get("source"), since=a.get("since"), limit=a.get("limit", 10),
        )

    # -- protocol
    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        mid = msg.get("id")
        method = msg.get("method")
        is_request = "id" in msg and method is not None
        try:
            result = self._dispatch(method, msg.get("params") or {})
        except _RpcError as exc:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": exc.code, "message": str(exc)}} if is_request else None
        except Exception as exc:  # noqa: BLE001
            return ({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"internal error: {exc}"}}
                    if is_request else None)
        return {"jsonrpc": "2.0", "id": mid, "result": result} if is_request else None

    def _dispatch(self, method: str | None, params: dict[str, Any]) -> Any:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                "serverInfo": {"name": "rundb", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping" or (method or "").startswith("notifications/"):
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._call_tool(params.get("name"), params.get("arguments") or {})
        if method == "resources/list":
            return {"resources": [
                {"uri": f"rundb://workspace/{w['name']}/latest-failures",
                 "name": f"{w['name']} latest failures", "mimeType": "application/json"}
                for w in self.db.workspaces()
            ]}
        if method == "resources/templates/list":
            return {"resourceTemplates": RESOURCE_TEMPLATES}
        if method == "resources/read":
            uri = params.get("uri", "")
            return {"contents": [{"uri": uri, "mimeType": "application/json",
                                  "text": json.dumps(_compact(self._read(uri)), ensure_ascii=False)}]}
        if method == "prompts/list":
            return {"prompts": []}
        raise _RpcError(-32601, f"method not found: {method}")

    def _call_tool(self, name: str | None, args: dict[str, Any]) -> dict[str, Any]:
        fn = self.tools.get(name or "")
        if fn is None:
            raise _RpcError(-32602, f"unknown tool: {name}")
        try:
            result = fn(args)
        except (RunDBError, KeyError, ValueError, TypeError) as exc:
            msg = f"missing argument: {exc}" if isinstance(exc, KeyError) else str(exc)
            return {"content": [{"type": "text", "text": msg}], "isError": True}
        except Exception as exc:  # noqa: BLE001  (e.g. sqlite errors from the sql tool)
            return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(_compact(result), ensure_ascii=False)}]}

    def _read(self, uri: str) -> Any:
        prefix = "rundb://"
        if not uri.startswith(prefix):
            raise _RpcError(-32002, f"resource not found: {uri}")
        parts = [unquote(p) for p in uri[len(prefix):].split("/")]
        if len(parts) == 3 and parts[0] == "workspace" and parts[2] == "latest-failures":
            try:
                return self.db.what_failed(parts[1])
            except RunDBError as exc:
                raise _RpcError(-32002, str(exc)) from exc
        if len(parts) == 2 and parts[0] == "run":
            try:
                run = self.db.get_run(parts[1])
            except RunDBError as exc:
                raise _RpcError(-32002, str(exc)) from exc
            return {"run": run, "spans": self.db.spans(run), "events": self.db.events(run)}
        raise _RpcError(-32002, f"resource not found: {uri}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def serve(path: str | None = None, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    if stdin is None:
        stdin = sys.stdin
        if hasattr(stdin, "reconfigure"):
            stdin.reconfigure(encoding="utf-8", errors="replace")
    if stdout is None:
        stdout = sys.stdout
        if hasattr(stdout, "reconfigure"):  # UTF-8 and bare \n even on Windows
            stdout.reconfigure(encoding="utf-8", newline="\n")
    db = RunDB(path)
    server = Server(db, os.environ.get("RUNDB_WORKSPACE", "default"))
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                resp: Any = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                if isinstance(msg, list):
                    resp = [r for r in (server.handle(m) for m in msg) if r is not None] or None
                else:
                    resp = server.handle(msg)
            if resp is not None:
                stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                stdout.flush()
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        raise
    finally:
        db.close()


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(prog="rundb-mcp", description="RunDB MCP server (stdio)")
    p.add_argument("--db", default=None, help="database file (default: $RUNDB_PATH or ./agent.db)")
    serve(p.parse_args().db)


if __name__ == "__main__":
    main()
