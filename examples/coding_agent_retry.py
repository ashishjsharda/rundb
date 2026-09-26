"""A simulated coding agent that retries a failing tool 5 times, then learns.

    python examples/coding_agent_retry.py

Run 1: `pytest` keeps failing. The agent retries blindly 5 times, asks RunDB
what_failed(), gets told to stop retrying, forks, fixes the environment, and
remember()s the fix.
Run 2 (a fresh agent, same workspace): searches memory before acting, applies
the fix up front, and passes on the first try.

No LLM or network needed; the "tools" are simulated.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))  # run from a clone without installing

from rundb import connect

DB_PATH = "demo.db"
WORKSPACE = "my-repo"


class FakeRepo:
    """Stand-in for a real environment: pytest fails until deps are installed."""

    def __init__(self) -> None:
        self.deps_installed = False

    def run(self, cmd: str) -> tuple[str | None, str | None]:
        if cmd == "pip install -r requirements.txt":
            self.deps_installed = True
            return "Successfully installed requests-2.32.3", None
        if cmd == "pytest -q":
            if not self.deps_installed:
                return None, "ModuleNotFoundError: No module named 'requests' (tests/test_api.py:1)"
            return "12 passed in 0.41s", None
        return None, f"unknown command: {cmd}"


def run_tool(db, run, repo: FakeRepo, cmd: str) -> tuple[bool, str]:
    output, error = repo.run(cmd)
    span_id = db.log_span(run, "tool", cmd.split()[0], input=cmd, output=output, error=error)
    return error is None, span_id


def first_agent(db) -> int:
    repo = FakeRepo()
    run = db.start_run(WORKSPACE, goal="make the test suite pass", model="sim-agent-1")
    db.log_span(run, "thought", "plan", output="run pytest, fix whatever breaks")

    attempts = 0
    failed_span = None
    for _ in range(5):  # a naive agent retrying the same thing
        attempts += 1
        ok, failed_span = run_tool(db, run, repo, "pytest -q")
        if ok:
            break

    report = db.what_failed(run)
    print("what_failed ->", report["suggested_next_step"], "\n")
    db.end_run(run, "failed", summary="pytest failed 5x with ModuleNotFoundError")

    fix = db.fork_run(run, goal="install deps, then run pytest")
    db.log_span(fix, "thought", "diagnose", output="requests is missing: install requirements first")
    attempts += 1
    run_tool(db, fix, repo, "pip install -r requirements.txt")
    attempts += 1
    ok, _ = run_tool(db, fix, repo, "pytest -q")
    db.remember(
        WORKSPACE,
        key="tests.setup",
        value="Run `pip install -r requirements.txt` before `pytest`, or it fails with ModuleNotFoundError",
        kind="constraint",
        source_run_id=fix,
        confidence=0.95,
        fixes=failed_span,  # links the fix to the exact failure, so it matches across tools
    )
    db.end_run(fix, "succeeded" if ok else "failed", summary="installed deps; 12 passed")
    return attempts


def second_agent(db) -> int:
    repo = FakeRepo()  # fresh environment, deps NOT installed
    run = db.start_run(WORKSPACE, goal="make the test suite pass", model="sim-agent-2")
    hits = db.search("pytest", workspace=WORKSPACE, source="memory")
    attempts = 0
    if hits:
        memory = db.recall(WORKSPACE, hits[0]["title"])[0]
        db.log_span(run, "retrieve", "memory", input="pytest", output=memory["value"])
        print("agent 2 found memory ->", memory["value"], "\n")
        attempts += 1
        run_tool(db, run, repo, "pip install -r requirements.txt")
    attempts += 1
    ok, _ = run_tool(db, run, repo, "pytest -q")
    db.end_run(run, "succeeded" if ok else "failed")
    return attempts


def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(DB_PATH + suffix):
            os.remove(DB_PATH + suffix)
    db = connect(DB_PATH)

    a1 = first_agent(db)
    a2 = second_agent(db)

    print(f"{'RUN':28} {'STATUS':10} {'BRANCH':8} GOAL")
    for r in reversed(db.list_runs(WORKSPACE)):
        print(f"{r.id:28} {r.status:10} {r.branch_name:8} {r.goal}")
    print(f"\ntool calls: agent 1 = {a1}, agent 2 = {a2}")
    print(f"\nTry:  rundb --db {DB_PATH} failed {WORKSPACE}")
    print(f"      rundb --db {DB_PATH} search ModuleNotFoundError")
    db.close()


if __name__ == "__main__":
    main()
