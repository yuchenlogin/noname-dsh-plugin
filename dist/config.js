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
import { homedir } from "node:os";
import { join } from "node:path";
import z from "@deepseek-ai/schemastery";
/** Ledger directory inside a project, matching the kernel's own quickstart. */
export const ledgerDirName = ".noname";
/** Legacy per-user ledger location: used only when no workspace resolves. */
export function defaultDbDir() {
    return process.env.DSH_HOME
        ? join(process.env.DSH_HOME, "noname")
        : join(homedir(), ".dsh", "noname");
}
export const Config = z.object({
    pythonPath: z.string().default("python3"),
    dbDir: z.string().default(""),
    autoIngest: z.boolean().default(true),
    timeoutMs: z.natural().default(30_000),
});
/**
 * Resolve raw (partial, possibly empty) config into a validated, complete
 * NonameConfig.  An empty `dbDir` stays empty on purpose: it is the signal
 * for workspace-scoped resolution, which needs the call's session and cannot
 * be decided at load time.
 */
export function resolveConfig(partial) {
    const raw = {
        pythonPath: partial?.pythonPath ?? "python3",
        dbDir: partial?.dbDir ?? "",
        autoIngest: partial?.autoIngest ?? true,
        timeoutMs: partial?.timeoutMs ?? 30_000,
    };
    // schemastery validates the shape; the result is complete and safe to use.
    return Config(raw);
}
