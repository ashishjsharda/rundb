"""v0.2.0: normalization, tool/version filters, exact fix matching, env fingerprints, upgrades."""

import sqlite3
import subprocess
from pathlib import Path

import pytest

from rundb import connect
from rundb._env import clear_cache, drift
from rundb.client import _statements
from rundb.suggest import error_signature

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def db(tmp_path):
    d = connect(tmp_path / "agent.db", fingerprint=False)
    yield d
    d.close()


# ---------------------------------------------------------------- normalization

@pytest.mark.parametrize("a,b", [
    ("timeout after 30s", "timeout after 31s"),
    ("fatal: bad object a3f9c21e7b0d", "fatal: bad object 0b1c2d3e4f5a"),
    ("lock /tmp/9f86d081-884c-4d63-a1b2-0123456789ab.lock held",
     "lock /tmp/1c3e5a7b-0000-4d63-a1b2-fedcba987654.lock held"),
    ("Error  in\nfoo", "error in foo"),
])
def test_signature_folds_variants(a, b):
    assert error_signature(a) == error_signature(b)


def test_signature_hex_runs_before_digits_and_keeps_words():
    assert error_signature("commit a3f9c21e not found") == "commit <hex> not found"
    assert error_signature("deadbeef cafebabe") == "deadbeef cafebabe"  # no digit: real words kept
    assert error_signature("short ab12 id") == "short ab# id"             # < 8 chars: digits only


def test_repeats_group_across_shas(db):
    run = db.start_run("app")
    for sha in ("a3f9c21e7b", "0b1c2d3e4f", "99aa88bb77"):
        db.log_span(run, "tool", "git", error=f"fatal: reference is not a tree: {sha}")
    report = db.what_failed(run)
    assert report["repeated"][0]["count"] == 3


# ---------------------------------------------------------------- tool / version

def test_search_and_what_failed_filter_by_tool_and_version(db):
    run = db.start_run("app")
    db.log_span(run, "tool", "pytest", error="ImportError boom", version="8.3")
    db.log_span(run, "tool", "pytest", error="ImportError boom", version="7.4")
    db.log_span(run, "tool", "ruff", error="ImportError boom")
    db.remember("app", "pytest.import", "pin pytest<8 for boom", tool="pytest", tool_version="7.4")
    db.remember("app", "general", "boom happens")

    assert {h["title"] for h in db.search("boom", tool="pytest", source="span")} == {"pytest"}
    assert len(db.search("boom", tool="pytest", source="span")) == 2
    v = db.search("boom", tool="pytest", version="7.4")
    assert {(h["source"], h["title"]) for h in v} == {("span", "pytest"), ("memory", "pytest.import")}
    assert db.search("boom", tool="nope") == []

    r = db.what_failed(run, tool="pytest", version="8.3")
    assert [e["version"] for e in r["errors"]] == ["8.3"]
    assert r["scope"]["tool"] == "pytest"
    assert {e["name"] for e in db.what_failed("app", tool="ruff")["errors"]} == {"ruff"}


# ---------------------------------------------------------------- exact fix matching

def test_fix_saved_with_fixes_matches_exactly_and_across_tools(db):
    run = db.start_run("app")
    bad = db.log_span(run, "tool", "pytest", version="8.3",
                      error="ModuleNotFoundError: No module named 'requests' (sha a3f9c21e7b)")
    mid = db.remember("app", "deps.requests", "pip install -r requirements.txt", fixes=bad,
                      kind="constraint")
    mem = db.recall("app", "deps.requests")[0]
    assert mem["id"] == mid and mem["tool"] == "pytest" and mem["tool_version"] == "8.3"
    assert mem["source_run_id"] == run.id and mem["fixes_span_id"] == bad
    assert mem["error_signature"] == error_signature(
        "ModuleNotFoundError: No module named 'requests' (sha 0000ffff11)")

    # a different tool, a different sha, and no shared keywords with the memory text
    other = db.start_run("app")
    db.log_span(other, "tool", "tox", error="ModuleNotFoundError: No module named 'requests' (sha 99aa88bb77)")
    report = db.what_failed(other)
    fix = report["related_memories"][0]
    assert fix["key"] == "deps.requests" and fix["match"] == "exact"
    assert "Known fix in memory 'deps.requests'" in report["suggested_next_step"]


def test_exact_match_prefers_same_tool(db):
    run = db.start_run("app")
    db.remember("app", "for.tox", "tox fix", tool="tox", error="E: boom 1")
    db.remember("app", "for.pytest", "pytest fix", tool="pytest", error="E: boom 2")
    db.log_span(run, "tool", "pytest", error="E: boom 3")
    fixes = db.what_failed(run)["related_memories"]
    assert [f["key"] for f in fixes] == ["for.pytest", "for.tox"]


def test_text_fallback_still_works(db):
    run = db.start_run("app")
    db.remember("app", "deploy.region", "set AWS_REGION before deploy")
    db.log_span(run, "tool", "deploy", error="missing AWS_REGION")
    assert db.what_failed(run)["related_memories"][0]["match"] == "text"


def test_remember_fixes_unknown_span(db):
    with pytest.raises(Exception):
        db.remember("app", "k", "v", fixes="spn_nope")


# ---------------------------------------------------------------- environment fingerprint

def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    try:
        _git(r, "init", "-q")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    (r / "requirements.txt").write_text("requests==2.31\n")
    (r / "app.py").write_text("print(1)\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    clear_cache()
    yield r
    clear_cache()


def test_fingerprint_flags_stale_fix_when_lockfile_changes(tmp_path, repo):
    d = connect(tmp_path / "fp.db", cwd=repo)
    run = d.start_run("app")
    bad = d.log_span(run, "tool", "pytest", error="ImportError: cannot import name 'x' from 'requests'")
    d.remember("app", "requests.x", "use requests.y instead", fixes=bad)
    env = d.recall("app")[0]
    assert env["stale"] is False and env["env_changes"] == []

    # new commit, same deps -> reported, not stale
    (repo / "app.py").write_text("print(2)\n")
    _git(repo, "commit", "-qam", "code")
    clear_cache()
    mem = d.recall("app")[0]
    assert mem["stale"] is False
    assert [c["what"] for c in mem["env_changes"]] == ["git_commit"]

    # dependency change -> stale, and what_failed says so
    (repo / "requirements.txt").write_text("requests==2.32\n")
    clear_cache()
    mem = d.recall("app")[0]
    assert mem["stale"] is True and "requirements.txt" in mem["stale_reason"]
    d.log_span(run, "tool", "pytest", error="ImportError: cannot import name 'x' from 'requests'")
    report = d.what_failed(run)
    assert report["related_memories"][0]["stale"] is True
    assert "may be stale" in report["suggested_next_step"]

    # re-saving the fix re-stamps it
    d.remember("app", "requests.x", "use requests.y instead", fixes=bad)
    clear_cache()
    assert d.recall("app")[0]["stale"] is False
    d.close()


def test_fingerprint_off_and_outside_git(tmp_path):
    clear_cache()
    d = connect(tmp_path / "a.db", cwd=tmp_path)  # not a repo, no lockfiles
    d.remember("app", "k", "v")
    assert d.recall("app")[0]["env"] is None
    d.close()
    d2 = connect(tmp_path / "b.db", fingerprint=False)
    d2.remember("app", "k", "v", env={"git_commit": "abc", "lockfiles": {}})
    assert d2.recall("app")[0]["stale"] is False  # fingerprinting off: never compared
    d2.close()


def test_drift_rules():
    saved = {"git_commit": "a" * 40, "lockfiles": {"uv.lock": "1", "package-lock.json": "2"}}
    same = drift(saved, saved)
    assert same == {"stale": False, "reason": None, "changes": []}
    moved = drift(saved, {"git_commit": "b" * 40, "lockfiles": {"uv.lock": "9"}})
    assert moved["stale"] is True
    assert {c.get("name") for c in moved["changes"] if c["what"] == "lockfile"} == {"uv.lock", "package-lock.json"}
    assert drift(None, saved)["stale"] is False


# ---------------------------------------------------------------- upgrade path

def test_v1_database_upgrades_in_place(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for stmt in _statements((REPO / "schema" / "001_init.sql").read_text()):
        conn.execute(stmt)
    conn.execute("PRAGMA user_version=1")
    conn.execute("INSERT INTO workspaces (id, name) VALUES ('ws_1', 'app')")
    conn.execute("INSERT INTO runs (id, workspace_id) VALUES ('run_1', 'ws_1')")
    conn.execute("INSERT INTO spans (id, run_id, kind, name, error) VALUES ('spn_1', 'run_1', 'tool', 'pytest', 'boom 42')")
    conn.execute("INSERT INTO memories (id, workspace_id, key, value) VALUES ('mem_1', 'ws_1', 'k', 'v')")
    conn.commit()
    conn.close()

    d = connect(path, fingerprint=False)
    assert d.schema_version == 2
    assert d.spans("run_1")[0]["version"] is None
    assert d.recall("app")[0]["key"] == "k"
    assert d.what_failed("run_1")["errors"][0]["span_id"] == "spn_1"
    d.close()
