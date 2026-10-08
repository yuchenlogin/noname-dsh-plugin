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
  /**
   * Why this target was chosen.  Without it a fallback is indistinguishable
   * from a correct answer -- the exact blindness that let a global ledger look
   * like a working workspace ledger.
   */
  detail: {
    /** Session id read from the execution, when the host supplied one. */
    sessionId?: string;
    /** Was the workspace registry reachable from this context? */
    registry: boolean;
    /** How many workspaces it listed. */
    workspaces: number;
  };
}

/** One-line, honest description of a resolved target (shown by noname_verify). */
export function describeLedgerTarget(target: LedgerTarget): string {
  const { sessionId, registry, workspaces } = target.detail;
  const session = sessionId ? `session=${sessionId}` : "session=(none)";
  if (target.scope === "workspace") {
    return `scope=workspace title=${target.title ?? "?"} ${session} db=${target.dbDir}`;
  }
  if (target.scope === "config") {
    return `scope=config (explicit dbDir) ${session} db=${target.dbDir}`;
  }
  const why = registry ? `registry=yes workspaces=${workspaces}` : "registry=no";
  const reason = !registry
    ? "workspace registry unreachable"
    : sessionId
      ? "session groups under no workspace"
      : "no session id on the execution";
  return `scope=global (${reason}: ${why}) ${session} db=${target.dbDir}`;
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
        agent?: { sessionId?: unknown; session?: { id?: unknown } };
      }
    | undefined;
  const candidates = [e?.sessionId, e?.session?.id, e?.agent?.sessionId, e?.agent?.session?.id];
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.length > 0) return candidate;
  }
  return undefined;
}

/**
 * Resolve the workspace registry defensively: absent services are not fatal
 * (a headless composition has no workspace feature), and a provider that is
 * not active yet must not break a tool call.
 */
function registryOf(ctx: Context | undefined): RegistryLike | undefined {
  for (const strict of [true, false]) {
    try {
      const registry = ctx?.get?.("workspaceRegistry", strict) as RegistryLike | undefined;
      if (registry && typeof registry.list === "function") return registry;
    } catch {
      // A strict get throws while the provider fiber is inactive; retry below.
    }
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
): { workspace?: WorkspaceLike; registry: boolean; workspaces: number } {
  const registry = registryOf(ctx);
  if (!registry) return { registry: false, workspaces: 0 };
  let list: WorkspaceLike[];
  try {
    list = registry.list();
  } catch {
    return { registry: false, workspaces: 0 };
  }
  if (!Array.isArray(list)) return { registry: true, workspaces: 0 };
  if (sessionId) {
    const owner = list.find(
      (w) => Array.isArray(w?.sessionIds) && w.sessionIds!.includes(sessionId),
    );
    if (owner?.path) return { workspace: owner, registry: true, workspaces: list.length };
  }
  const usable = list.filter((w) => typeof w?.path === "string" && w.path.length > 0);
  return {
    workspace: usable.length === 1 ? usable[0] : undefined,
    registry: true,
    workspaces: list.length,
  };
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
    return {
      dbDir: config.dbDir,
      root: config.dbDir,
      scope: "config",
      detail: { sessionId, registry: false, workspaces: 0 },
    };
  }
  const { workspace, registry, workspaces } = resolveWorkspace(ctx, sessionId);
  if (workspace?.path) {
    return {
      dbDir: join(workspace.path, ledgerDirName),
      root: workspace.path,
      scope: "workspace",
      title: workspace.title,
      detail: { sessionId, registry, workspaces },
    };
  }
  const fallback = defaultDbDir();
  return {
    dbDir: fallback,
    root: fallback,
    scope: "global",
    detail: { sessionId, registry, workspaces },
  };
}
