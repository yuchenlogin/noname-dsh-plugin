/**
 * Ledger scope: one ledger per DSH workspace (project), not per session and
 * not one global bucket for every project.
 *
 * The user's mental model is the sidebar's: a workspace groups sessions, and
 * the ledger is the project's asset, shared by every session that runs in it.
 * DSH already owns that mapping (`ctx.workspaceRegistry`: workspace -> path ->
 * sessionIds), so this module reads it instead of inventing a second one:
 *
 *   session id --(registry)--> workspace path --(convention)--> <path>/.noname
 *
 * `<project>/.noname/` is the kernel's own quickstart convention
 * (`init --db .noname/harness.db --root .`), keeps the kernel's workspace
 * boundary pointing at the real project (so `snapshot` reads the project's
 * git state and `package` prints the project boundary), and both the NoName
 * and plugin repos already gitignore it.
 *
 * Kernels rule that decides the layout: `validate_workspace_path` refuses any
 * generated file outside the project row's `workspace_root` -- so the ledger
 * HTML cannot live next to a centralized db while the root is the project.
 *
 * The registry is optional: when it is absent or the session is ungrouped,
 * the target falls back to the legacy per-user directory, so the plugin still
 * works under a bare `dsh headless` composition.
 */

import { join } from "node:path";
import type { Context } from "@deepseek-ai/cordis";
import { defaultDbDir, ledgerDirName, type NonameConfig } from "./config.js";

/** Minimal structural view of one DSH workspace entity (defensive). */
interface WorkspaceLike {
  id?: string;
  path?: string;
  title?: string;
  sessionIds?: readonly string[];
}

/** Minimal structural view of the DSH workspace registry service. */
interface RegistryLike {
  list(): WorkspaceLike[];
}

/** Where one ledger lives, and which directory the kernel treats as the project. */
export interface LedgerTarget {
  /** Directory holding this ledger's db (created on demand). */
  dbDir: string;
  /** Workspace root the kernel treats as the project boundary. */
  root: string;
  /** Which rule produced this target. */
  scope: "config" | "workspace" | "global";
  /** Workspace display title, when the target came from a workspace. */
  title?: string;
}

/**
 * Read the session id out of a host execution object.
 *
 * Accepts `unknown` on purpose: the host type differs per DSH version
 * (`sessionId` on some, `agent.sessionId` on 0.2.0-rc.2), and a narrower
 * parameter type only produced casts at every call site.
 */
export function sessionIdOf(exec: unknown): string | undefined {
  const e = exec as
    | {
        sessionId?: unknown;
        session?: { id?: unknown };
        agent?: { sessionId?: unknown };
      }
    | undefined;
  const candidates = [e?.sessionId, e?.session?.id, e?.agent?.sessionId];
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.length > 0) return candidate;
  }
  return undefined;
}

/** Resolve the workspace registry defensively: absent services are not fatal. */
function registryOf(ctx: Context | undefined): RegistryLike | undefined {
  try {
    const registry = ctx?.get?.("workspaceRegistry") as RegistryLike | undefined;
    if (registry && typeof registry.list === "function") return registry;
  } catch {
    // A missing/not-yet-ready service must never break a tool call.
  }
  return undefined;
}

/**
 * Find the workspace a session ran in, or the only workspace when no session
 * context exists (load-time ping).  With several workspaces and no session we
 * refuse to guess: the caller falls back to the legacy directory instead of
 * writing one project's evidence into another's ledger.
 */
export function resolveWorkspace(
  ctx: Context | undefined,
  sessionId?: string,
): WorkspaceLike | undefined {
  const registry = registryOf(ctx);
  if (!registry) return undefined;
  let list: WorkspaceLike[];
  try {
    list = registry.list();
  } catch {
    return undefined;
  }
  if (!Array.isArray(list)) return undefined;
  if (sessionId) {
    const owner = list.find(
      (w) => Array.isArray(w?.sessionIds) && w.sessionIds!.includes(sessionId),
    );
    if (owner?.path) return owner;
  }
  const usable = list.filter((w) => typeof w?.path === "string" && w.path.length > 0);
  return usable.length === 1 ? usable[0] : undefined;
}

/**
 * Resolve the ledger a call belongs to.
 *
 * Precedence: an explicit `dbDir` config wins (tests, custom layouts); else
 * the session's workspace; else the legacy per-user directory.
 */
export function ledgerTarget(
  ctx: Context | undefined,
  config: NonameConfig,
  sessionId?: string,
): LedgerTarget {
  if (config.dbDir) {
    return { dbDir: config.dbDir, root: config.dbDir, scope: "config" };
  }
  const workspace = resolveWorkspace(ctx, sessionId);
  if (workspace?.path) {
    return {
      dbDir: join(workspace.path, ledgerDirName),
      root: workspace.path,
      scope: "workspace",
      title: workspace.title,
    };
  }
  const fallback = defaultDbDir();
  return { dbDir: fallback, root: fallback, scope: "global" };
}
