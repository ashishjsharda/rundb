"""Deterministic 'what should the agent do next' heuristics for what_failed().

No LLM involved: rules are ordered, cheap, and identical in the TS client.
"""

from __future__ import annotations

import re
from typing import Any

# (pattern, hint). First match wins. Keep in sync with ts/src/suggest.ts.
ERROR_HINTS: list[tuple[str, str]] = [
    (r"rate.?limit|too many requests|\b429\b",
     "Rate limited: back off (exponential delay) before retrying."),
    (r"unauthori[sz]ed|forbidden|permission denied|eacces|access denied|\b401\b|\b403\b|invalid api key",
     "Auth or permission failure: retrying will not help. Fix credentials or scope first."),
    (r"timed? ?out|etimedout|deadline exceeded",
     "Timed out: retry once with backoff or a smaller input, then change approach."),
    (r"env(ironment)? var|not set|missing \w*_\w*|undefined variable",
     "Missing configuration: set the value, fork_run, and retry once."),
    (r"enoent|no such file|not found|modulenotfound|cannot find module|does not exist|404",
     "Something referenced does not exist: verify the path, module or resource before retrying."),
    (r"syntax ?error|parse error|unexpected token|invalid json|jsondecodeerror",
     "Malformed input: fix what you passed to the tool instead of retrying it."),
    (r"assert|tests? failed|\d+ failed|expected .* (got|but)",
     "Tests are failing: read the failing assertion in the span output before editing code."),
    (r"connection refused|econnrefused|econnreset|network",
     "Network or service unavailable: check the service is up before retrying."),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), h) for p, h in ERROR_HINTS]

# Order matters: UUIDs and hex runs must be folded before digits become '#',
# otherwise a SHA like a3f9c21e turns into a#f#c#e first and never matches.
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_HEX = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b")  # 8+ hex chars with at least one digit
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def error_signature(error: str | None) -> str:
    """Normalize an error so variants of the same failure group together.

    'timeout after 31s' == 'timeout after 30s'; commit SHAs, UUIDs and hex temp names fold to
    <hex>/<uuid>. Identical in ts/src/suggest.ts.
    """
    text = (error or "").lower()
    text = _UUID.sub("<uuid>", text)
    text = _HEX.sub("<hex>", text)
    text = _NUM.sub("#", text)
    return _WS.sub(" ", text).strip()[:200]


def hint_for(error: str | None) -> str | None:
    for rx, hint in _COMPILED:
        if error and rx.search(error):
            return hint
    return None


def suggest(
    errors: list[dict[str, Any]],
    repeated: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    resolved_by: list[dict[str, Any]],
    running: list[dict[str, Any]],
) -> str:
    if not errors:
        if running:
            r = running[0]
            return (f"No errors recorded. Run {r['id']} is still 'running'; if its process died, "
                    f"end it with end_run(status='aborted') or fork_run to continue.")
        return "No errors recorded. Nothing to fix."

    latest = errors[0]
    parts: list[str] = []

    if resolved_by:
        r = resolved_by[0]
        parts.append(f"Fork {r['id']} already succeeded after this failure; reuse its approach "
                     f"(search with run_id={r['id']}).")
    if memories:
        m = memories[0]
        if m.get("stale"):
            parts.append(f"Possible fix in memory '{m['key']}': {m['value']}. It may be stale "
                         f"({m.get('stale_reason')}), so verify it before applying.")
        else:
            parts.append(f"Known fix in memory '{m['key']}': {m['value']}. Apply it before retrying.")
    if repeated and repeated[0]["count"] >= 3 and not parts:
        rep = repeated[0]
        parts.append(f"Stop retrying '{rep['name']}' unchanged: it failed {rep['count']}x with the "
                     f"same error. Change the input or approach, then fork_run.")

    if not parts:
        hint = hint_for(latest.get("error"))
        parts.append(hint or (f"Inspect span {latest['span_id']} ('{latest['name']}') input and error, "
                              f"then fork_run with a changed plan."))

    if not memories:
        parts.append("Once fixed, call remember() with the fix so future runs skip this failure.")
    return " ".join(parts)
