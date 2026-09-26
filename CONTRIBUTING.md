# Contributing

Thanks for helping. RunDB is meant to stay small, so please open an issue before starting a large change.

## Setup

```bash
pip install -e "python[dev]"
pytest python/tests

cd ts && npm install && npm test   # Node >= 22.16; also runs the Python interop test
```

## Rules of the road

- **The schema is the product.** `schema/001_init.sql` is canonical. Never edit a released migration: add `002_*.sql`, copy it byte-for-byte into `python/rundb/migrations/` and `ts/migrations/`, and let the tests confirm the copies match.
- **Python and TypeScript stay in step.** A new operation lands in both clients (snake_case in Python, camelCase in TS) along with a test. The `what_failed` hint table in `suggest.py` and `suggest.ts` must match.
- **No runtime dependencies.** Both clients use only their standard library, and the MCP server is hand-rolled.
- **Boring beats clever.** Anything a user can do with SQL should stay possible with SQL.
- **Keep benchmark claims honest.** If a change affects performance, include before and after numbers from `python bench/bench.py`.
