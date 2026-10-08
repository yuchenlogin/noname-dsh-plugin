/**
 * Python sidecar bridge to the NoName Harness kernel.
 *
 * The kernel (vendor/noname-harness, pure Python stdlib + SQLite) is the
 * single source of truth for evidence, memory, taste and approvals.  This
 * bridge shells out to `python -m noname_harness <cmd>` and parses stdout
 * JSON -- the same contract the CLI uses, so the bridge is as stable as the
 * kernel itself.  Nothing in the DSH layer re-implements kernel logic.
 */
/**
 * Resolve the kernel root NOW, not at module load.
 *
 * A load-time constant is a trap: when this module is imported from a path
 * that later moves (a staging directory during install, a re-installed
 * profile), the frozen value points at a directory that no longer exists --
 * and `spawn(..., { cwd })` then fails with a bare `ENOENT` that names the
 * python binary and hides the real cause.  Resolving per call costs four
 * stat()s and turns that failure class into an explicit, named error.
 *
 * @returns the kernel root, or null when no candidate holds the package.
 */
export declare function resolveKernelRoot(): string | null;
export interface NonameRunOptions {
    /** Override the db path (defaults to <dbDir>/harness.db). */
    dbPath?: string;
    /** Directory holding the NoName db (created on demand). */
    dbDir: string;
    /**
     * Workspace root the kernel treats as the project boundary.  Used by
     * `init` (and therefore by ensureNonameInit); every later command reads it
     * from the stored project row, so carrying it on all calls is harmless.
     */
    root?: string;
    /** Python interpreter (defaults to python3). */
    pythonPath?: string;
    /** Abort a long-running call. */
    signal?: AbortSignal;
    /** Milliseconds before the call is killed (default 30s). */
    timeoutMs?: number;
    /**
     * Return raw stdout instead of parsing JSON.  Required for commands whose
     * output is not JSON (e.g. `package` emits markdown, `ledger` a table);
     * assuming JSON for those would falsely report a bad_json bridge error.
     */
    raw?: boolean;
}
export declare class NonameBridgeError extends Error {
    readonly code: "spawn_failed" | "kernel_missing" | "nonzero_exit" | "bad_json" | "timeout" | "aborted";
    readonly stderr: string;
    readonly exitCode: number | null;
    constructor(message: string, code: "spawn_failed" | "kernel_missing" | "nonzero_exit" | "bad_json" | "timeout" | "aborted", stderr?: string, exitCode?: number | null);
}
declare function dbFile(opts: NonameRunOptions): string;
/** Run `python -m noname_harness <args>` and return parsed stdout JSON. */
export declare function runNoname<T = unknown>(args: string[], opts: NonameRunOptions): Promise<T>;
/**
 * Initialize a NoName project at the db dir (idempotent, non-destructive).
 *
 * Always runs `init`: the kernel's init is idempotent and never overwrites an
 * existing project row (verified against the kernel).  Short-circuiting on
 * "the db file exists" was wrong, because every kernel command opens -- and
 * therefore creates -- the db file: a `verify`/`state`/`search` against a
 * fresh profile left an empty db behind, after which this function believed
 * the project was initialized and the next `record`/`package`/`extract`/
 * `ledger` call failed with "project is not initialized".
 */
export declare function ensureNonameInit(opts: NonameRunOptions & {
    name?: string;
    root?: string;
}): Promise<{
    created: boolean;
}>;
/** Verify the kernel is importable and the bridge works end to end. */
export declare function pingKernel(opts: NonameRunOptions): Promise<boolean>;
export declare const paths: {
    /** Resolved fresh on every read -- see resolveKernelRoot(). */
    readonly KERNEL_ROOT: string;
    dbFile: typeof dbFile;
};
export {};
