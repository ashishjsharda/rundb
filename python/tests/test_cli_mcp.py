import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rundb import connect
from rundb.cli import main as cli_main
from rundb.mcp_server import TOOLS, Server

PY = Path(__file__).resolve().parents[1]


def run_cli(db_path, *args, capsys):
    code = cli_main(["--db", str(db_path), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_end_to_end(tmp_path, capsys):
    db = tmp_path / "cli.db"
    code, out, _ = run_cli(db, "init", capsys=capsys)
    assert code == 0 and "initialized" in out
    _, run_id, _ = run_cli(db, "start", "fix the deploy", "-w", "ops", capsys=capsys)
    run_id = run_id.strip()
    run_cli(db, "span", run_id, "--name", "deploy", "--error", "missing AWS_REGION", capsys=capsys)
    _, fork_id, _ = run_cli(db, "fork", run_id, capsys=capsys)
    run_cli(db, "remember", "deploy.env", "AWS_REGION=us-east-1", "-w", "ops", capsys=capsys)
    run_cli(db, "end", fork_id.strip(), capsys=capsys)

    _, out, _ = run_cli(db, "runs", capsys=capsys)
    assert run_id in out and "forked" in out and "succeeded" in out
    _, out, _ = run_cli(db, "search", "AWS_REGION", "--json", capsys=capsys)
    hits = json.loads(out)
    assert {h["source"] for h in hits} == {"span", "memory"}
    _, out, _ = run_cli(db, "failed", "ops", capsys=capsys)
    assert "next step:" in out and "AWS_REGION=us-east-1" in out
    _, out, _ = run_cli(db, "tail", run_id, capsys=capsys)
    assert "run started" in out and "tool deploy: missing AWS_REGION" in out
    _, out, _ = run_cli(db, "show", run_id, capsys=capsys)
    assert "deploy" in out
    _, out, _ = run_cli(db, "sql", "select count(*) as n from runs", "--json", capsys=capsys)
    assert json.loads(out) == [{"n": 2}]
    code, _, err = run_cli(db, "sql", "delete from runs", capsys=capsys)
    assert code == 1 and "readonly" in err.lower()


def test_cli_init_positional_and_errors(tmp_path, capsys):
    target = tmp_path / "x" / "my.db"
    code, out, _ = run_cli(tmp_path / "ignored.db", "init", str(target), capsys=capsys)
    assert code == 0 and target.exists()
    code, _, err = run_cli(target, "end", "run_nope", capsys=capsys)
    assert code == 2 and "not found" in err
    code, _, _ = run_cli(target, "abort", capsys=capsys)
    assert code == 2


def test_cli_abort_stale(tmp_path, capsys):
    path = tmp_path / "s.db"
    d = connect(path)
    run = d.start_run("app")
    d.close()
    _, out, _ = run_cli(path, "abort", "--stale", "0s", capsys=capsys)
    assert run.id in out


def test_console_script_module(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(PY)}
    out = subprocess.run([sys.executable, "-m", "rundb", "--db", str(tmp_path / "m.db"), "init"],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr


# ---------------------------------------------------------------- MCP (in-process)

def call(server, method, params=None, mid=1):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def tool(server, _tool, **args):
    resp = call(server, "tools/call", {"name": _tool, "arguments": args})
    res = resp["result"]
    text = res["content"][0]["text"]
    return (json.loads(text) if not res.get("isError") else text), res.get("isError", False)


def test_mcp_protocol(tmp_path):
    s = Server(connect(tmp_path / "mcp.db"))
    init = call(s, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "0"}})["result"]
    assert init["protocolVersion"] == "2025-03-26" and init["serverInfo"]["name"] == "rundb"
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in call(s, "tools/list")["result"]["tools"]}
    assert {"start_run", "log_span", "end_run", "remember", "search", "fork_run", "what_failed", "sql"} <= names
    for t in TOOLS:
        assert t["inputSchema"]["type"] == "object" and len(t["description"]) < 130
    assert call(s, "nope")["error"]["code"] == -32601

    run, _ = tool(s, "start_run", goal="deploy", workspace="ops")
    tool(s, "log_span", run_id=run["id"], kind="tool", name="deploy", error="timeout after 30s")
    _, is_err = tool(s, "log_span", run_id=run["id"], kind="bogus", name="x")
    assert is_err
    _, is_err = tool(s, "log_span", run_id=run["id"])
    assert is_err
    tool(s, "remember", key="deploy.timeout", value="use --timeout 120", workspace="ops", kind="constraint")
    hits, _ = tool(s, "search", query="timeout", workspace="ops")
    assert hits and all("run_id" in h or h["source"] == "memory" for h in hits)
    report, _ = tool(s, "what_failed", target=run["id"])
    assert "--timeout 120" in report["suggested_next_step"]
    child, _ = tool(s, "fork_run", run_id=run["id"])
    assert child["parent_run_id"] == run["id"]
    ended, _ = tool(s, "end_run", run_id=child["id"], status="succeeded")
    assert ended["status"] == "succeeded"
    rows, _ = tool(s, "sql", query="select count(*) n from runs where status = ?", params=["succeeded"])
    assert rows == [{"n": 1}]
    msg, is_err = tool(s, "sql", query="delete from runs")
    assert is_err

    templates = call(s, "resources/templates/list")["result"]["resourceTemplates"]
    assert templates[0]["uriTemplate"] == "rundb://workspace/{id}/latest-failures"
    uris = [r["uri"] for r in call(s, "resources/list")["result"]["resources"]]
    assert "rundb://workspace/ops/latest-failures" in uris
    content = call(s, "resources/read", {"uri": "rundb://workspace/ops/latest-failures"})["result"]["contents"][0]
    assert json.loads(content["text"])["errors"][0]["name"] == "deploy"
    run_res = call(s, "resources/read", {"uri": f"rundb://run/{run['id']}"})["result"]["contents"][0]
    assert json.loads(run_res["text"])["spans"]
    assert "error" in call(s, "resources/read", {"uri": "rundb://workspace/nope/latest-failures"})


def test_mcp_stdio_subprocess(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(PY), "RUNDB_PATH": str(tmp_path / "stdio.db")}
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "start_run", "arguments": {"goal": "hello"}}},
    ]
    proc = subprocess.run([sys.executable, "-m", "rundb", "mcp"], input="\n".join(json.dumps(m) for m in msgs) + "\nnot json\n",
                          capture_output=True, text=True, env=env, timeout=30)
    lines = [json.loads(l) for l in proc.stdout.splitlines()]
    assert [l.get("id") for l in lines] == [1, 2, None]
    assert lines[2]["error"]["code"] == -32700
    assert json.loads(lines[1]["result"]["content"][0]["text"])["status"] == "running"
