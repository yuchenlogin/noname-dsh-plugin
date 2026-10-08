/**
 * Plugin configuration, validated by @deepseek-ai/schemastery at the
 * boundary.  The schema IS the contract: invalid values fail loudly at load
 * (schemastery ValidationError), never as a `mkdir("")` deep in the bridge.
 *
 * Defaults are chosen so a first install works with zero configuration given
 * a system python3; `dbDir` falls back to a per-user home location so the
 * ledger never lands in the host's CWD or the kernel submodule.
 */

import { homedir } from "node:os";
import { join } from "node:path";
import z from "@deepseek-ai/schemastery";

/** Default ledger location: per-user, never the host CWD or the submodule. */
export function defaultDbDir(): string {
  return process.env.DSH_HOME
    ? join(process.env.DSH_HOME, "noname")
    : join(homedir(), ".dsh", "noname");
}

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

export const Config = z.object({
  pythonPath: z.string().default("python3"),
  dbDir: z.string().default(""),
  autoIngest: z.boolean().default(true),
  timeoutMs: z.natural().default(30_000),
});

/**
 * Resolve raw (partial, possibly empty) config into a validated, complete
 * NonameConfig.  An empty dbDir is replaced by the per-user default here --
 * at the single boundary -- so downstream never sees `""`.
 */
export function resolveConfig(partial?: Partial<NonameConfig>): NonameConfig {
  const raw = {
    pythonPath: partial?.pythonPath ?? "python3",
    dbDir: partial?.dbDir && partial.dbDir.length > 0 ? partial.dbDir : defaultDbDir(),
    autoIngest: partial?.autoIngest ?? true,
    timeoutMs: partial?.timeoutMs ?? 30_000,
  };
  // schemastery validates the shape; the result is complete and safe to use.
  return Config(raw) as NonameConfig;
}
