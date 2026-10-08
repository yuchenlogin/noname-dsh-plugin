/**
 * Python sidecar bridge to the NoName Harness kernel.
 *
 * The kernel (vendor/noname-harness, pure Python stdlib + SQLite) is the
 * single source of truth for evidence, memory, taste and approvals.  This
 * bridge shells out to `python -m noname_harness <cmd>` and parses stdout
 * JSON -- the same contract the CLI uses, so the bridge is as stable as the
 * kernel itself.  Nothing in the DSH layer re-implements kernel logic.
 */
export interface NonameRunOptions {
    /** Override the db path (defaults to <dbDir>/harness.db). */
    dbPath?: string;
    /** Directory holding the NoName db (created on demand). */
    dbDir: string;
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
    readonly code: "spawn_failed" | "nonzero_exit" | "bad_json" | "timeout" | "aborted";
    readonly stderr: string;
    readonly exitCode: number | null;
    constructor(message: string, code: "spawn_failed" | "nonzero_exit" | "bad_json" | "timeout" | "aborted", stderr?: string, exitCode?: number | null);
}
declare function dbFile(opts: NonameRunOptions): string;
/** Run `python -m noname_harness <args>` and return parsed stdout JSON. */
export declare function runNoname<T = unknown>(args: string[], opts: NonameRunOptions): Promise<T>;
/** Initialize a NoName project at the db dir if not already present (idempotent). */
export declare function ensureNonameInit(opts: NonameRunOptions & {
    name?: string;
    root?: string;
}): Promise<{
    created: boolean;
}>;
/** Verify the kernel is importable and the bridge works end to end. */
export declare function pingKernel(opts: NonameRunOptions): Promise<boolean>;
export declare const paths: {
    KERNEL_ROOT: string;
    dbFile: typeof dbFile;
};
export {};
