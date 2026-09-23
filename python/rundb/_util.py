"""Small helpers shared by the client, CLI and MCP server."""

from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

_TERM = re.compile(r"\w+", re.UNICODE)
_REL = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def now_iso() -> str:
    """UTC timestamp, millisecond precision, same format as JS Date.toISOString()."""
    return iso(datetime.now(timezone.utc))


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def new_id(prefix: str) -> str:
    """Time-sortable id: <prefix>_<12 hex ms><10 hex random>."""
    return f"{prefix}_{int(time.time() * 1000):012x}{secrets.token_hex(5)}"


def parse_since(value: Any) -> str | None:
    """Accept None, a datetime, an ISO string, or a relative age like '30m', '2h', '7d'."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, (int, float)):
        return iso(datetime.now(timezone.utc) - timedelta(seconds=float(value)))
    text = str(value)
    m = _REL.match(text)
    if m:
        seconds = int(m.group(1)) * _UNITS[m.group(2)]
        return iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))
    return text


def to_text(value: Any) -> str | None:
    """Store strings as-is; anything else as JSON."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def to_json(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        json.loads(value)  # validate
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def terms(text: str) -> list[str]:
    return _TERM.findall(text or "")


def fts_query(text: str, mode: str = "and") -> str | None:
    """Turn arbitrary agent text into a safe FTS5 query (every term quoted)."""
    found = terms(text)
    if not found:
        return None
    joiner = " AND " if mode == "and" else " OR "
    return joiner.join('"' + t.replace('"', '""') + '"' for t in found)


def clip(text: str | None, n: int) -> str | None:
    if text is None or len(text) <= n:
        return text
    return text[: n - 1] + "…"
