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
import type { Context } from "@deepseek-ai/cordis";
import { type NonameConfig } from "./config.js";
/** Minimal structural view of one DSH workspace entity (defensive). */
interface WorkspaceLike {
    id?: string;
    path?: string;
    title?: string;
    sessionIds?: readonly string[];
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
export declare function describeLedgerTarget(target: LedgerTarget): string;
/**
 * Read the session id out of a host execution object.
 *
 * Accepts `unknown` on purpose: the host type differs per DSH version
 * (`sessionId` on some, `agent.sessionId` on 0.2.0-rc.2), and a narrower
 * parameter type only produced casts at every call site.
 */
export declare function sessionIdOf(exec: unknown): string | undefined;
/**
 * Find the workspace a session ran in, or the only workspace when no session
 * context exists (load-time ping).  With several workspaces and no session we
 * refuse to guess: the caller falls back to the legacy directory instead of
 * writing one project's evidence into another's ledger.
 */
export declare function resolveWorkspace(ctx: Context | undefined, sessionId?: string): {
    workspace?: WorkspaceLike;
    registry: boolean;
    workspaces: number;
};
/**
 * Resolve the ledger a call belongs to.
 *
 * Precedence: an explicit `dbDir` config wins (tests, custom layouts); else
 * the session's workspace; else the legacy per-user directory.
 */
export declare function ledgerTarget(ctx: Context | undefined, config: NonameConfig, sessionId?: string): LedgerTarget;
export {};
