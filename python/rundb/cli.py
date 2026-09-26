"""`rundb` command line. Human-readable by default, --json for machines."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Any, Iterable, Sequence

from . import __version__
from ._util import clip
from .client import MEMORY_KINDS, SPAN_KINDS, TERMINAL_STATUSES, Run, RunDB, RunDBError


def _common(parser: argparse.ArgumentParser, suppress: bool) -> None:
    d = argparse.SUPPRESS if suppress else None
    parser.add_argument("--db", default=d, metavar="PATH",
                        help="database file (default: $RUNDB_PATH or ./agent.db)")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS if suppress else False,
                        help="print JSON instead of tables")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rundb", description="RunDB: local-first database for AI agent runs.")
    p.add_argument("--version", action="version", version=f"rundb {__version__}")
    _common(p, suppress=False)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def cmd(name: str, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help, description=help)
        _common(sp, suppress=True)
        return sp

    c = cmd("init", "create (or migrate) a database file")
    c.add_argument("path", nargs="?", help="file to create (default: ./agent.db)")

    c = cmd("runs", "list recent runs")
    c.add_argument("--workspace", "-w")
    c.add_argument("--status", choices=["running", *TERMINAL_STATUSES])
    c.add_argument("--limit", "-n", type=int, default=20)

    c = cmd("show", "show one run with its spans")
    c.add_argument("run_id")

    c = cmd("tail", "print the event log (latest run if no id); -f to follow")
    c.add_argument("run_id", nargs="?")
    c.add_argument("--all", action="store_true", help="events from every run")
    c.add_argument("-n", type=int, default=30, help="number of past events to show")
    c.add_argument("-f", "--follow", action="store_true")

    c = cmd("search", "full-text search over spans, memories and chunks")
    c.add_argument("query")
    c.add_argument("--workspace", "-w")
    c.add_argument("--run")
    c.add_argument("--kind")
    c.add_argument("--source", choices=["span", "memory", "chunk"])
    c.add_argument("--since", help="ISO time or relative age like 30m, 2h, 7d")
    c.add_argument("--tool", help="only this tool (span name / memory tool)")
    c.add_argument("--tool-version", help="only this tool version")
    c.add_argument("--limit", "-n", type=int, default=20)

    c = cmd("sql", "run SQL (read-only unless --write)")
    c.add_argument("query")
    c.add_argument("--write", action="store_true")

    c = cmd("failed", "recent errors and a suggested next step")
    c.add_argument("target", nargs="?", help="run id or workspace (default: everything)")
    c.add_argument("--tool", help="only failures of this tool")
    c.add_argument("--tool-version", help="only failures of this tool version")

    c = cmd("abort", "abort a run, or every stale running run")
    c.add_argument("run_id", nargs="?")
    c.add_argument("--stale", metavar="AGE", help="abort runs idle longer than AGE (e.g. 1h)")
    c.add_argument("--reason")

    c = cmd("start", "start a run and print its id")
    c.add_argument("goal")
    c.add_argument("--workspace", "-w", default="default")
    c.add_argument("--model")
    c.add_argument("--parent")

    c = cmd("span", "log a span on a run")
    c.add_argument("run_id")
    c.add_argument("--kind", default="tool", choices=SPAN_KINDS)
    c.add_argument("--name", required=True)
    c.add_argument("--input")
    c.add_argument("--output")
    c.add_argument("--error")
    c.add_argument("--tool-version", help="version of the tool called")

    c = cmd("end", "finish a run")
    c.add_argument("run_id")
    c.add_argument("--status", default="succeeded", choices=TERMINAL_STATUSES)
    c.add_argument("--summary")

    c = cmd("fork", "branch a run and print the new run id")
    c.add_argument("run_id")
    c.add_argument("--goal")

    c = cmd("remember", "store a durable memory")
    c.add_argument("key")
    c.add_argument("value")
    c.add_argument("--workspace", "-w", default="default")
    c.add_argument("--kind", default="fact", choices=MEMORY_KINDS)
    c.add_argument("--run", help="source run id")
    c.add_argument("--fixes", metavar="SPAN_ID", help="failed span this fixes (enables exact matching)")
    c.add_argument("--tool", help="tool this applies to")

    c = cmd("recall", "list current memories (STALE = dependencies changed since saved)")
    c.add_argument("key", nargs="?")
    c.add_argument("--workspace", "-w", default="default")
    c.add_argument("--kind", choices=MEMORY_KINDS)
    c.add_argument("--tool")

    cmd("mcp", "run the MCP server on stdio")
    return p


# ---------------------------------------------------------------- output helpers

def _table(rows: Sequence[dict[str, Any]], cols: Sequence[str], widths: dict[str, int] | None = None) -> str:
    if not rows:
        return "(none)"
    widths = widths or {}
    cells = [[clip("" if r.get(c) is None else str(r.get(c)).replace("\n", " "), widths.get(c, 60)) or ""
              for c in cols] for r in rows]
    w = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    lines = ["  ".join(c.upper().ljust(w[i]) for i, c in enumerate(cols)).rstrip()]
    lines += ["  ".join(row[i].ljust(w[i]) for i in range(len(cols))).rstrip() for row in cells]
    return "\n".join(lines)


def _plain(obj: Any) -> Any:
    if isinstance(obj, Run):
        return obj.to_dict()
    if isinstance(obj, list):
        return [_plain(o) for o in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return obj


def _out(args: argparse.Namespace, data: Any, human: str | None = None) -> None:
    if args.json or human is None:
        print(json.dumps(_plain(data), indent=2, ensure_ascii=False))
    else:
        print(human)


def _fmt_event(e: dict[str, Any]) -> str:
    return f"{e['ts']}  {e['level'].upper():5}  {e['run_id'] or '-':26}  {e['message']}"


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # never crash on odd characters in agent output
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    if args.cmd == "mcp":
        from .mcp_server import serve
        serve(args.db)
        return 0

    path = args.path if args.cmd == "init" and args.path else args.db
    try:
        db = RunDB(path)
    except Exception as exc:  # noqa: BLE001
        print(f"rundb: cannot open database: {exc}", file=sys.stderr)
        return 1
    try:
        return _dispatch(db, args) or 0
    except RunDBError as exc:
        print(f"rundb: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"rundb: sql error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        db.close()


def _dispatch(db: RunDB, args: argparse.Namespace) -> int | None:
    c = args.cmd
    if c == "init":
        db.workspace("default")
        _out(args, {"path": os.path.abspath(db.path), "schema_version": db.schema_version},
             f"initialized {os.path.abspath(db.path)} (schema v{db.schema_version})")

    elif c == "runs":
        runs = db.list_runs(args.workspace, args.status, args.limit)
        rows = [r.to_dict() for r in runs]
        _out(args, rows, _table(rows, ["id", "status", "branch_name", "started_at", "goal"], {"goal": 50}))

    elif c == "show":
        run = db.get_run(args.run_id)
        spans = db.spans(run)
        human = (
            f"run      {run.id}\nstatus   {run.status}\ngoal     {run.goal or ''}\n"
            f"branch   {run.branch_name}   parent {run.parent_run_id or '-'}\n"
            f"started  {run.started_at}   ended {run.ended_at or '-'}\n\n"
            + _table(spans, ["id", "kind", "name", "error", "output"], {"error": 50, "output": 40})
        )
        _out(args, {"run": run, "spans": spans}, human)

    elif c == "tail":
        _tail(db, args)

    elif c == "search":
        hits = db.search(args.query, workspace=args.workspace, run_id=args.run, kind=args.kind,
                         source=args.source, since=args.since, tool=args.tool,
                         version=args.tool_version, limit=args.limit)
        _out(args, hits, _table(hits, ["source", "run_id", "span_id", "title", "snippet"],
                                {"snippet": 70, "title": 24}))

    elif c == "sql":
        rows = db.sql(args.query, readonly=not args.write)
        cols = list(rows[0].keys()) if rows else []
        _out(args, rows, _table(rows, cols) if rows else "(no rows)")

    elif c == "failed":
        report = db.what_failed(args.target, tool=args.tool, version=args.tool_version)
        human = (
            _table(report["errors"], ["run_id", "span_id", "name", "error"], {"error": 70})
            + (("\n\nrepeated:\n" + _table(report["repeated"], ["count", "name", "error"], {"error": 70}))
               if report["repeated"] else "")
            + f"\n\nnext step: {report['suggested_next_step']}"
        )
        _out(args, report, human)

    elif c == "abort":
        if args.stale:
            ids = db.abort_stale(args.stale)
            _out(args, ids, "\n".join(ids) if ids else "no stale runs")
        elif args.run_id:
            run = db.abort_run(args.run_id, args.reason)
            _out(args, run, f"{run.id} aborted")
        else:
            print("rundb abort: give a run id or --stale AGE", file=sys.stderr)
            return 2

    elif c == "start":
        run = db.start_run(args.workspace, goal=args.goal, model=args.model, parent_run_id=args.parent)
        _out(args, run, run.id)

    elif c == "span":
        sid = db.log_span(args.run_id, args.kind, args.name, input=args.input, output=args.output,
                          error=args.error, version=args.tool_version)
        _out(args, {"span_id": sid}, sid)

    elif c == "end":
        run = db.end_run(args.run_id, args.status, args.summary)
        _out(args, run, f"{run.id} {run.status}")

    elif c == "fork":
        run = db.fork_run(args.run_id, args.goal)
        _out(args, run, run.id)

    elif c == "remember":
        mid = db.remember(args.workspace, args.key, args.value, kind=args.kind, source_run_id=args.run,
                          fixes=args.fixes, tool=args.tool)
        _out(args, {"memory_id": mid}, mid)

    elif c == "recall":
        rows = db.recall(args.workspace, args.key, kind=args.kind, tool=args.tool)
        for r in rows:
            r["state"] = "STALE" if r["stale"] else ""
        _out(args, rows, _table(rows, ["key", "kind", "tool", "value", "state"], {"value": 60}))
    return None


def _tail(db: RunDB, args: argparse.Namespace) -> None:
    run_id = args.run_id
    if not run_id and not args.all:
        latest = db.list_runs(limit=1)
        if not latest:
            print("(no runs yet)")
            if not args.follow:
                return
        else:
            run_id = latest[0].id
    scope = "run_id = ?" if run_id else "1 = 1"
    params: list[Any] = [run_id] if run_id else []
    past = db.sql(f"SELECT * FROM (SELECT * FROM events WHERE {scope} ORDER BY id DESC LIMIT ?) ORDER BY id",
                  (*params, args.n))
    _emit(args, past)
    last = past[-1]["id"] if past else 0
    while args.follow:
        time.sleep(0.5)
        new = db.events(run_id, after_id=last, limit=500)
        _emit(args, new)
        if new:
            last = new[-1]["id"]


def _emit(args: argparse.Namespace, events: Iterable[dict[str, Any]]) -> None:
    for e in events:
        print(json.dumps(e, ensure_ascii=False) if args.json else _fmt_event(e), flush=True)


if __name__ == "__main__":
    sys.exit(main())
