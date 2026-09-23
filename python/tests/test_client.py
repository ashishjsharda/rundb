import json
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import rundb
from rundb import RunDBError, connect

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def db(tmp_path):
    d = connect(tmp_path / "agent.db")
    yield d
    d.close()


# ---------------------------------------------------------------- open / migrate

def test_creates_file_and_migrates(tmp_path):
    path = tmp_path / "nested" / "dir" / "agent.db"
    assert not path.exists()
    d = connect(path)
    assert path.exists()
    assert d.schema_version == 1
    tables = {r["name"] for r in d.sql("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("workspaces", "runs", "spans", "artifacts", "memories", "chunks", "events", "search_index"):
        assert t in tables
    d.close()
    # re-open is a no-op migration
    d2 = connect(path)
    assert d2.schema_version == 1
    d2.close()


def test_default_path_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNDB_PATH", str(tmp_path / "env.db"))
    d = connect()
    assert d.path.endswith("env.db")
    d.close()


def test_schema_copies_identical():
    canonical = (REPO / "schema" / "001_init.sql").read_bytes()
    assert (REPO / "python" / "rundb" / "migrations" / "001_init.sql").read_bytes() == canonical
    ts_copy = REPO / "ts" / "migrations" / "001_init.sql"
    if ts_copy.exists():
        assert ts_copy.read_bytes() == canonical


# ---------------------------------------------------------------- runs & spans

def test_run_lifecycle(db):
    run = db.start_run("app", goal="ship it", model="m1")
    assert run.status == "running" and run.branch_name == "main"
    sid = db.log_span(run, "tool", "pytest", input={"args": ["-q"]}, output="ok", tokens_in=10)
    spans = db.spans(run)
    assert spans[0]["id"] == sid and spans[0]["input"] == '{"args": ["-q"]}'
    done = db.end_run(run, "succeeded", summary="all green")
    assert done.status == "succeeded" and done.ended_at
    assert json.loads(done.meta)["summary"] == "all green"
    with pytest.raises(RunDBError):
        db.end_run(run, "failed")  # cannot end twice


def test_validation(db):
    run = db.start_run("app")
    with pytest.raises(RunDBError):
        db.log_span(run, "banana", "x")
    with pytest.raises(RunDBError):
        db.end_run(run, "running")
    with pytest.raises(RunDBError):
        db.log_span("run_missing", "tool", "x")
    with pytest.raises(RunDBError):
        db.remember("app", "k", "v", kind="opinion")


def test_runs_are_append_only(db):
    run = db.start_run("app", goal="original")
    with pytest.raises(sqlite3.IntegrityError):
        db.sql("UPDATE runs SET goal = 'rewritten' WHERE id = ?", (run.id,))
    ev = db.events(run)
    assert ev and ev[0]["message"] == "run started"
    with pytest.raises(sqlite3.IntegrityError):
        db.sql("DELETE FROM events")


# ---------------------------------------------------------------- fork

def test_fork_links_and_does_not_copy_spans(db):
    parent = db.start_run("app", goal="deploy", model="m1")
    db.log_span(parent, "tool", "deploy", error="missing AWS_REGION")
    child = db.fork_run(parent, goal="set region then deploy")
    assert child.parent_run_id == parent.id
    assert child.goal == "set region then deploy" and child.model == "m1"
    assert child.branch_name == "fork-1"
    assert db.spans(child) == []  # spans are not copied
    assert db.get_run(parent).status == "forked"  # running parent is closed as forked
    second = db.fork_run(parent)
    assert second.branch_name == "fork-2" and second.goal == "deploy"
    grandchild = db.fork_run(child)
    assert [r.id for r in db.lineage(grandchild)] == [parent.id, child.id, grandchild.id]


def test_fork_of_failed_run_keeps_failed_status(db):
    parent = db.start_run("app", goal="x")
    db.end_run(parent, "failed")
    db.fork_run(parent)
    assert db.get_run(parent).status == "failed"


# ---------------------------------------------------------------- search

def test_search_spans_memories_chunks(db):
    run = db.start_run("app", goal="deploy")
    sid = db.log_span(run, "tool", "run_deploy", input="./deploy.sh", error="ENOENT: missing AWS_REGION")
    db.remember("app", "deploy.env", "AWS_REGION must be us-east-1", kind="constraint", source_run_id=run)
    db.add_artifact(run, "notes.md", content="The AWS_REGION variable lives in .env.production")

    hits = db.search("AWS_REGION")
    sources = {h["source"] for h in hits}
    assert sources == {"span", "memory", "chunk"}
    span_hit = next(h for h in hits if h["source"] == "span")
    assert span_hit["run_id"] == run.id and span_hit["span_id"] == sid
    assert "[" in span_hit["snippet"]
    assert all(h["score"] >= 0 for h in hits)
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    # punctuation-heavy agent queries do not blow up
    assert db.search("./deploy.sh: ENOENT (missing)")
    assert db.search('"unbalanced quote') == []
    assert db.search("") == []


def test_search_filters_and_stemming(db):
    a = db.start_run("ws-a")
    b = db.start_run("ws-b")
    db.log_span(a, "tool", "fetch", error="connection timeout")
    db.log_span(b, "thought", "plan", output="consider timeouts")
    db.remember("ws-a", "net", "timeouts happen at 5pm")
    assert {h["run_id"] for h in db.search("timeout", workspace="ws-a", source="span")} == {a.id}
    assert [h["kind"] for h in db.search("timeout", kind="thought")] == ["thought"]  # stemmed match
    assert {h["run_id"] for h in db.search("timeout", run_id=b)} == {b.id}
    assert db.search("timeout", workspace="nope") == []
    assert db.search("timeout", since="1h")
    assert db.search("timeout", since="2999-01-01T00:00:00.000Z") == []


def test_search_falls_back_to_any_term(db):
    run = db.start_run("app")
    db.log_span(run, "tool", "build", error="webpack exploded")
    hits = db.search("webpack segfault")  # 'segfault' matches nothing
    assert hits and hits[0]["run_id"] == run.id


def test_raw_fts_query(db):
    run = db.start_run("app")
    db.log_span(run, "tool", "build", output="alpha beta")
    assert db.search("alp*", raw=True)
    with pytest.raises(RunDBError):
        db.search('"unterminated', raw=True)


# ---------------------------------------------------------------- memory

def test_remember_survives_failed_run_and_supersedes(db):
    run = db.start_run("app", goal="x")
    db.remember("app", "db.port", "5432", source_run_id=run)
    db.end_run(run, "failed")
    db.remember("app", "db.port", "6543", kind="decision")
    current = db.recall("app", "db.port")
    assert len(current) == 1 and current[0]["value"] == "6543"
    assert len(db.recall("app", "db.port", include_history=True)) == 2
    hits = db.search("port", source="memory")
    assert [h["title"] for h in hits] == ["db.port"] and len(hits) == 1  # superseded hidden
    assert db.forget("app", "db.port") == 2
    assert db.recall("app", "db.port") == [] and db.search("port") == []


def test_memory_expiry(db):
    db.remember("app", "tmp", "short lived", expires_at="2000-01-01T00:00:00.000Z")
    db.remember("app", "keep", "long lived", ttl="7d")
    assert [m["key"] for m in db.recall("app")] == ["keep"]
    assert db.search("lived", source="memory")[0]["title"] == "keep"
    assert len(db.search("lived", source="memory")) == 1


# ---------------------------------------------------------------- what_failed

def test_what_failed_run_scope(db):
    run = db.start_run("app", goal="deploy")
    for _ in range(3):
        db.log_span(run, "tool", "deploy", error="timeout after 30s")
    db.end_run(run, "failed")
    report = db.what_failed(run)
    assert report["scope"]["type"] == "run"
    assert report["repeated"][0]["count"] == 3
    assert "Stop retrying 'deploy'" in report["suggested_next_step"]
    assert report["errors"][0]["span_id"]


def test_what_failed_uses_memory_and_forks(db):
    run = db.start_run("app", goal="deploy")
    db.log_span(run, "tool", "deploy", error="ENOENT: missing AWS_REGION")
    db.end_run(run, "failed")
    assert "Missing configuration" in db.what_failed(run)["suggested_next_step"]

    fix = db.fork_run(run, goal="set region")
    db.log_span(fix, "tool", "deploy", output="ok")
    db.end_run(fix, "succeeded")
    db.remember("app", "deploy.AWS_REGION", "export AWS_REGION=us-east-1 first", kind="constraint")
    report = db.what_failed("app")
    assert report["scope"]["type"] == "workspace"
    assert report["resolved_by"][0]["id"] == fix.id
    assert report["related_memories"][0]["key"] == "deploy.AWS_REGION"
    assert "export AWS_REGION=us-east-1" in report["suggested_next_step"]


def test_what_failed_clean_and_unknown(db):
    db.start_run("app")
    assert "No errors" in db.what_failed("app")["suggested_next_step"]
    with pytest.raises(RunDBError):
        db.what_failed("does-not-exist")


# ---------------------------------------------------------------- crash mid-run

def test_crash_mid_run_then_abort(tmp_path):
    path = tmp_path / "crash.db"
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(REPO / 'python')!r})
        from rundb import connect
        db = connect({str(path)!r})
        run = db.start_run("app", goal="will crash")
        db.log_span(run, "tool", "step1", output="ok")
        print(run.id, flush=True)
        os._exit(137)  # hard crash: no end_run, no close
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 137, proc.stderr
    run_id = proc.stdout.strip()

    d = connect(path)
    run = d.get_run(run_id)
    assert run.status == "running" and run.ended_at is None
    assert len(d.spans(run)) == 1  # committed work survived the crash
    assert d.abort_stale(older_than="1h") == []  # still recent: not stale
    assert d.abort_stale(older_than=0) == [run_id]
    assert d.get_run(run_id).status == "aborted"
    d.close()


def test_abort_run(db):
    run = db.start_run("app")
    assert db.abort_run(run, "user cancelled").status == "aborted"


# ---------------------------------------------------------------- misc

def test_batch_rolls_back(db):
    run = db.start_run("app")
    with pytest.raises(ZeroDivisionError):
        with db.batch():
            db.log_span(run, "tool", "a")
            1 / 0
    assert db.spans(run) == []


def test_sql_readonly(db):
    db.start_run("app")
    assert db.sql("SELECT count(*) AS n FROM runs")[0]["n"] == 1
    with pytest.raises(sqlite3.OperationalError):
        db.sql("DELETE FROM runs", readonly=True)
    db.start_run("app")  # connection still writable afterwards


def test_artifact_hash_and_vectors(db, tmp_path):
    run = db.start_run("app")
    f = tmp_path / "out.txt"
    f.write_text("hello artifact")
    aid = db.add_artifact(run, str(f))
    art = db.artifacts(run)[0]
    assert art["id"] == aid and len(art["sha256"]) == 64 and art["mime"] == "text/plain"
    db.add_chunk("app", "north", embedding=[1.0, 0.0])
    db.add_chunk("app", "east", embedding=[0.0, 1.0])
    assert db.similar([0.9, 0.1], k=1)[0]["text"] == "north"


def test_multiple_connections(tmp_path):
    path = tmp_path / "shared.db"
    a, b = connect(path), connect(path)
    run = a.start_run("app")
    b.log_span(run.id, "tool", "from-b")
    assert len(a.spans(run)) == 1
    a.close()
    b.close()


def test_version():
    assert rundb.__version__
