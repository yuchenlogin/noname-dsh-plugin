/**
 * Plugin configuration, validated by @deepseek-ai/schemastery at the
 * boundary.  The schema IS the contract: invalid values fail loudly at load
 * (schemastery ValidationError), never as a `mkdir("")` deep in the bridge.
 *
 * Defaults are chosen so a first install works with zero configuration given
 * a system python3; `dbDir` falls back to a per-user home location so the
 * ledger never lands in the host's CWD or the kernel submodule.
 */
import z from "@deepseek-ai/schemastery";
/** Default ledger location: per-user, never the host CWD or the submodule. */
export declare function defaultDbDir(): string;
export interface NonameConfig {
    /** Python interpreter used for the sidecar (default python3). */
    pythonPath: string;
    /** Directory holding the NoName db (default <dsh-home>/noname or ~/.dsh/noname). */
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
 * NonameConfig.  An empty dbDir is replaced by the per-user default here --
 * at the single boundary -- so downstream never sees `""`.
 */
export declare function resolveConfig(partial?: Partial<NonameConfig>): NonameConfig;
