"""RunDB: a local-first, single-file database for AI agent runs, traces and memory."""

from .client import (
    MEMORY_KINDS,
    RUN_STATUSES,
    SPAN_KINDS,
    Run,
    RunDB,
    RunDBError,
    connect,
)

__all__ = ["connect", "RunDB", "Run", "RunDBError", "RUN_STATUSES", "SPAN_KINDS", "MEMORY_KINDS"]
__version__ = "0.1.0"
