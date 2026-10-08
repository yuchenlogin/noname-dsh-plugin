/**
 * Plugin configuration, validated by @deepseek-ai/schemastery at the
 * boundary.  The schema IS the contract: invalid values fail loudly at load
 * (schemastery ValidationError), never as a `mkdir("")` deep in the bridge.
 *
 * Defaults are chosen so a first install works with zero configuration given
 * a system python3.  `dbDir` is an OPTIONAL override: left empty, the ledger
 * is scoped to the DSH workspace (project) the call belongs to -- see
 * workspace.ts -- so one project's evidence never mixes with another's.
 */
import z from "@deepseek-ai/schemastery";
/** Ledger directory inside a project, matching the kernel's own quickstart. */
export declare const ledgerDirName = ".noname";
/** Legacy per-user ledger location: used only when no workspace resolves. */
export declare function defaultDbDir(): string;
export interface NonameConfig {
    /** Python interpreter used for the sidecar (default python3). */
    pythonPath: string;
    /**
     * Explicit ledger directory.  Empty (default) means "scope the ledger to
     * the session's DSH workspace": `<workspace>/.noname`.
     */
    dbDir: string;
    /** Automatically ingest DSH tool results into the NoName evidence stream. */
    autoIngest: boolean;
    /** Milliseconds before a sidecar call is killed (default 30s). */
    timeoutMs: number;
}
export declare const Config: z<Schemastery.ObjectS<NoInfer<{
    pythonPath: z<string, string, "defined">;
    dbDir: z<string, string, "defined">;
    autoIngest: z<boolean, boolean, "defined">;
    timeoutMs: z<number, number, "defined">;
}>>, Schemastery.ObjectT<NoInfer<{
    pythonPath: z<string, string, "defined">;
    dbDir: z<string, string, "defined">;
    autoIngest: z<boolean, boolean, "defined">;
    timeoutMs: z<number, number, "defined">;
}>>, "plain">;
/**
 * Resolve raw (partial, possibly empty) config into a validated, complete
 * NonameConfig.  An empty `dbDir` stays empty on purpose: it is the signal
 * for workspace-scoped resolution, which needs the call's session and cannot
 * be decided at load time.
 */
export declare function resolveConfig(partial?: Partial<NonameConfig>): NonameConfig;
