// Environment fingerprints: detect when a saved fix may be stale.
// Keep in sync with python/rundb/_env.py.

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

export const LOCKFILES = [
  "requirements.txt", "poetry.lock", "uv.lock", "Pipfile.lock", "pdm.lock",
  "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
  "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock",
];

export interface Fingerprint {
  git_commit: string | null;
  lockfiles: Record<string, string>;
}

export interface Drift {
  stale: boolean;
  reason: string | null;
  changes: Array<Record<string, string>>;
}

const cache = new Map<string, { at: number; fp: Fingerprint | null }>();
const TTL_MS = 5000;

function git(cwd: string, ...args: string[]): string | null {
  try {
    const out = execFileSync("git", args, { cwd, stdio: ["ignore", "pipe", "ignore"], timeout: 2000 })
      .toString().trim();
    return out || null;
  } catch {
    return null;
  }
}

function real(p: string): string {
  try { return realpathSync(p); } catch { return resolve(p); }
}

/** {git_commit, lockfiles: {name: sha256[:16]}} or null. Cached for a few seconds. */
export function fingerprint(cwd?: string | null): Fingerprint | null {
  const root = real(cwd ?? process.cwd());
  const hit = cache.get(root);
  if (hit && Date.now() - hit.at < TTL_MS) return hit.fp;
  const commit = git(root, "rev-parse", "HEAD");
  const top = git(root, "rev-parse", "--show-toplevel");
  const dirs = [root];
  if (top && real(top) !== root) dirs.push(real(top));
  const lockfiles: Record<string, string> = {};
  for (const d of dirs) {
    for (const name of LOCKFILES) {
      const p = join(d, name);
      if (!(name in lockfiles) && existsSync(p) && statSync(p).isFile()) {
        try { lockfiles[name] = createHash("sha256").update(readFileSync(p)).digest("hex").slice(0, 16); } catch { /* skip */ }
      }
    }
  }
  const fp = commit || Object.keys(lockfiles).length ? { git_commit: commit, lockfiles } : null;
  cache.set(root, { at: Date.now(), fp });
  return fp;
}

export function clearFingerprintCache(): void {
  cache.clear();
}

/** A changed/removed lockfile marks a fix stale; a new commit is reported but not stale by itself. */
export function drift(saved: Fingerprint | null | undefined, current: Fingerprint | null | undefined): Drift {
  const changes: Array<Record<string, string>> = [];
  if (!saved || !current) return { stale: false, reason: null, changes };
  const oldL = saved.lockfiles ?? {};
  const newL = current.lockfiles ?? {};
  for (const [name, h] of Object.entries(oldL)) {
    if (!(name in newL)) changes.push({ what: "lockfile", name, change: "removed" });
    else if (newL[name] !== h) changes.push({ what: "lockfile", name, change: "changed" });
  }
  if (saved.git_commit && current.git_commit && saved.git_commit !== current.git_commit) {
    changes.push({ what: "git_commit", was: saved.git_commit.slice(0, 12), now: current.git_commit.slice(0, 12) });
  }
  const locks = changes.filter((c) => c.what === "lockfile").map((c) => c.name);
  const stale = locks.length > 0;
  return { stale, reason: stale ? `${locks.join(", ")} changed since it was saved` : null, changes };
}
