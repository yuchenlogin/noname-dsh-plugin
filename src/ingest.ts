/**
 * Automatic ingestion of DSH session/tool events into the NoName evidence
 * stream -- the "context is an asset" loop.  When enabled, work done in DSH
 * accumulates into NoName without any manual step, so a later session (or a
 * different model) can pick it up with `noname_package`.
 *
 * Ingestion is deliberately conservative and idempotent: each tool call is
 * recorded at most once (keyed by its correlation id), and the volume is
 * bounded by the configured ingest level.
 */

import type { Context } from "@deepseek-ai/cordis";
import { ensureNonameInit, runNoname } from "./kernel.js";
import type { NonameConfig } from "./config.js";

const INGEST_SESSION = "dsh";

export function registerIngestion(ctx: Context, config: NonameConfig): void {
  if (!config.autoIngest) return;

  const seen = new Set<string>();

  async function ingest(eventType: string, key: string, summary: string): Promise<void> {
    if (seen.has(key)) return;
    seen.add(key);
    try {
      const opts = { dbDir: config.dbDir ?? "", pythonPath: config.pythonPath };
      await ensureNonameInit(opts);
      await runNoname(
        ["event", "--session", INGEST_SESSION, "--type", eventType, "--payload", JSON.stringify({ text: summary })],
        opts,
      );
    } catch {
      // Ingestion must never break the host session: a sidecar failure is
      // logged by the bridge, swallowed here, and simply means this one event
      // is not persisted.  The ledger stays consistent (append-only).
      seen.delete(key);
    }
  }

  // Tool results: the highest-value evidence (what the agent actually did).
  ctx.on("tools/result", (exec: { name?: string; callId?: string }, result: { content?: { type: string; text?: string }[] }) => {
    if (config.ingestLevel === "minimal" || config.ingestLevel === "verbose") {
      const text = (result?.content ?? [])
        .map((b) => (b.type === "text" ? b.text ?? "" : ""))
        .join("")
        .slice(0, 500);
      const key = exec?.callId ?? `${exec?.name}:${text.slice(0, 40)}`;
      void ingest("dsh.tool.completed", key, `tool ${exec?.name ?? "?"} -> ${text}`);
    }
  });
}
