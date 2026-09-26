// Deterministic next-step heuristics. Keep in sync with python/rundb/suggest.py.

export const ERROR_HINTS: Array<[RegExp, string]> = [
  [/rate.?limit|too many requests|\b429\b/i,
    "Rate limited: back off (exponential delay) before retrying."],
  [/unauthori[sz]ed|forbidden|permission denied|eacces|access denied|\b401\b|\b403\b|invalid api key/i,
    "Auth or permission failure: retrying will not help. Fix credentials or scope first."],
  [/timed? ?out|etimedout|deadline exceeded/i,
    "Timed out: retry once with backoff or a smaller input, then change approach."],
  [/env(ironment)? var|not set|missing \w*_\w*|undefined variable/i,
    "Missing configuration: set the value, fork_run, and retry once."],
  [/enoent|no such file|not found|modulenotfound|cannot find module|does not exist|404/i,
    "Something referenced does not exist: verify the path, module or resource before retrying."],
  [/syntax ?error|parse error|unexpected token|invalid json|jsondecodeerror/i,
    "Malformed input: fix what you passed to the tool instead of retrying it."],
  [/assert|tests? failed|\d+ failed|expected .* (got|but)/i,
    "Tests are failing: read the failing assertion in the span output before editing code."],
  [/connection refused|econnrefused|econnreset|network/i,
    "Network or service unavailable: check the service is up before retrying."],
];

// Order matters: UUIDs and hex runs are folded before digits become '#'.
const UUID = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/g;
const HEX = /\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b/g; // 8+ hex chars with at least one digit

/** Normalize an error so variants of the same failure group together. Same as Python. */
export function errorSignature(error: string | null | undefined): string {
  return (error ?? "").toLowerCase()
    .replace(UUID, "<uuid>")
    .replace(HEX, "<hex>")
    .replace(/\d+/g, "#")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 200);
}

export function hintFor(error: string | null | undefined): string | null {
  if (!error) return null;
  for (const [rx, hint] of ERROR_HINTS) if (rx.test(error)) return hint;
  return null;
}

type Row = Record<string, any>;

export function suggest(errors: Row[], repeated: Row[], memories: Row[], resolvedBy: Row[], running: Row[]): string {
  if (errors.length === 0) {
    if (running.length) {
      const r = running[0];
      return `No errors recorded. Run ${r.id} is still 'running'; if its process died, ` +
        `end it with end_run(status='aborted') or fork_run to continue.`;
    }
    return "No errors recorded. Nothing to fix.";
  }
  const latest = errors[0];
  const parts: string[] = [];
  if (resolvedBy.length) {
    const r = resolvedBy[0];
    parts.push(`Fork ${r.id} already succeeded after this failure; reuse its approach (search with run_id=${r.id}).`);
  }
  if (memories.length) {
    const m = memories[0];
    if (m.stale) {
      parts.push(`Possible fix in memory '${m.key}': ${m.value}. It may be stale ` +
        `(${m.stale_reason}), so verify it before applying.`);
    } else {
      parts.push(`Known fix in memory '${m.key}': ${m.value}. Apply it before retrying.`);
    }
  }
  if (repeated.length && repeated[0].count >= 3 && !parts.length) {
    const rep = repeated[0];
    parts.push(`Stop retrying '${rep.name}' unchanged: it failed ${rep.count}x with the same error. ` +
      `Change the input or approach, then fork_run.`);
  }
  if (!parts.length) {
    parts.push(hintFor(latest.error) ??
      `Inspect span ${latest.span_id} ('${latest.name}') input and error, then fork_run with a changed plan.`);
  }
  if (!memories.length) parts.push("Once fixed, call remember() with the fix so future runs skip this failure.");
  return parts.join(" ");
}
