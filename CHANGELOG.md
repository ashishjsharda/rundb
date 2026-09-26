# Changelog

## 0.2.0

Built from early user feedback.

- **Exact fix matching.** `remember(..., fixes=<failed span id>)` links a memory to the failure it solves and stores the error signature, tool and tool version. `what_failed()` matches fixes by signature first (same tool preferred; fixes still apply across tools) before falling back to full-text search. Each fix reports `match: "exact" | "text"`.
- **Stale-fix detection.** Memories are stamped with the git commit and dependency lockfile hashes when saved. `recall()` and `what_failed()` flag a fix as `stale` when a lockfile has changed since then; a new commit alone is reported in `env_changes` but not treated as stale. Set `RUNDB_FINGERPRINT=0` to disable.
- **Better repeat grouping.** Error signatures now fold UUIDs and runs of 8+ hex characters (commit SHAs, temp names) before digits.
- **Tool and version filters.** `log_span(..., version=)`, `search(tool=, version=)`, `what_failed(tool=, version=)` and `recall(tool=)`, plus matching CLI flags (`--tool`, `--tool-version`, `--fixes`) and MCP arguments.
- Migration 002 upgrades existing v0.1 databases in place.
- "How `what_failed()` works" section in the README.

## 0.1.1

- Rewrote the PyPI page: badges, feature list, MCP setup, API table.
- Added Python version classifiers and project links.

## 0.1.0

- First release: the SQLite schema (7 tables, FTS5 search, append-only guards), the Python SDK, the `rundb` CLI, the stdio MCP server with tools plus `latest-failures` and `run` resources, the TypeScript SDK on `node:sqlite`, a coding-agent retry example and a benchmark.
