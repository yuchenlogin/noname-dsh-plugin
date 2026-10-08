/**
 * Plugin configuration schema (zod via @deepseek-ai/schemastery).
 *
 * All fields are editable through the DSH settings card; Volatile fields can
 * change at runtime.  Defaults are chosen so a first install works with zero
 * configuration given a system python3.
 */

export interface NonameConfig {
  /** Python interpreter used for the sidecar (default python3). */
  pythonPath?: string;
  /** Directory holding the NoName db (default <dsh-home>/noname). */
  dbDir?: string;
  /** Automatically ingest DSH session/tool events into the NoName evidence stream. */
  autoIngest: boolean;
  /** How much to ingest automatically: 'minimal' (tool results) or 'verbose' (all observable events). */
  ingestLevel: "minimal" | "verbose";
}

export const DEFAULT_CONFIG: NonameConfig = {
  autoIngest: true,
  ingestLevel: "minimal",
};

export function resolveConfig(partial?: Partial<NonameConfig>): NonameConfig {
  return { ...DEFAULT_CONFIG, ...(partial ?? {}) };
}
