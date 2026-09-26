"""Environment fingerprints: detect when a saved fix may be stale.

A fingerprint records the git commit and the hashes of dependency lockfiles when a
memory is saved. Later, drift() compares it with the current environment:

- a changed or removed lockfile marks the memory as possibly stale (dependencies moved);
- a different git commit is reported but does not mark it stale on its own (code always moves).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any

# Keep in sync with ts/src/env.ts.
LOCKFILES = (
    "requirements.txt", "poetry.lock", "uv.lock", "Pipfile.lock", "pdm.lock",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
    "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock",
)

_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_TTL = 5.0


def _git(cwd: str, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=2,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def fingerprint(cwd: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    """{'git_commit': sha|None, 'lockfiles': {name: sha256[:16]}} or None if nothing found.
    Cached for a few seconds so bulk writes don't spawn a git process each time."""
    root = str(Path(cwd or os.getcwd()).resolve())
    hit = _CACHE.get(root)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]

    commit = _git(root, "rev-parse", "HEAD")
    top = _git(root, "rev-parse", "--show-toplevel")
    dirs = [root] + ([str(Path(top).resolve())] if top and str(Path(top).resolve()) != root else [])
    locks: dict[str, str] = {}
    for d in dirs:
        for name in LOCKFILES:
            p = Path(d, name)
            if name not in locks and p.is_file():
                try:
                    locks[name] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
                except OSError:
                    pass
    fp = {"git_commit": commit, "lockfiles": locks} if (commit or locks) else None
    _CACHE[root] = (time.monotonic(), fp)
    return fp


def clear_cache() -> None:
    _CACHE.clear()


def drift(saved: dict[str, Any] | None, current: dict[str, Any] | None) -> dict[str, Any]:
    """Compare a saved fingerprint with the current one.

    Returns {'stale': bool, 'reason': str|None, 'changes': [...]}.
    """
    changes: list[dict[str, Any]] = []
    if not saved or not current:
        return {"stale": False, "reason": None, "changes": changes}
    old_locks = saved.get("lockfiles") or {}
    new_locks = current.get("lockfiles") or {}
    for name, h in old_locks.items():
        if name not in new_locks:
            changes.append({"what": "lockfile", "name": name, "change": "removed"})
        elif new_locks[name] != h:
            changes.append({"what": "lockfile", "name": name, "change": "changed"})
    old_c, new_c = saved.get("git_commit"), current.get("git_commit")
    if old_c and new_c and old_c != new_c:
        changes.append({"what": "git_commit", "was": old_c[:12], "now": new_c[:12]})
    lock_changes = [c["name"] for c in changes if c["what"] == "lockfile"]
    stale = bool(lock_changes)
    reason = (", ".join(lock_changes) + " changed since it was saved") if stale else None
    return {"stale": stale, "reason": reason, "changes": changes}
