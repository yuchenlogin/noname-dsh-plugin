/**
 * Automatic ingestion of DSH tool results into the NoName evidence stream --
 * the "context is an asset" loop.  When enabled, work done in DSH accumulates
 * into NoName so a later session (or a different model) can pick it up with
 * `noname_package`.
 *
 * Three honesty rules, inherited from NoName's ledger philosophy:
 *  - PROVENANCE: every event is recorded under the real DSH session id (from
 *    the exec context), never a single shared bucket, so `package --session`
 *    can tell one piece of work from another.
 *  - NO SILENT LOSS: a failed ingest is warned (with a running counter), so
 *    "we did not persist this" is never invisible.  Ingestion still never
 *    breaks the host session.
 *  - NO OVER-CLAIM: only `tools/result` is observed (the highest-signal
 *    evidence).  There is no pretend "verbose" tier.
 */

import type { Context } from "@deepseek-ai/cordis";
import { ensureNonameInit, runNoname } from "./kernel.js";
import type { NonameConfig } from "./config.js";

/** Shape of the tool-result event payload we rely on (minimal, defensive). */
interface ToolExec {
  name?: string;
  callId?: string;
  sessionId?: string;
  session?: { id?: string };
}
interface ToolResult {
  content?: { type: string; text?: string }[];
}

/** Extract the real DSH session id from the exec context, best-effort. */
function sessionIdOf(exec: ToolExec): string {
  return exec.sessionId ?? exec.session?.id ?? "dsh-unknown";
}

export function registerIngestion(ctx: Context, config: NonameConfig): void {
  if (!config.autoIngest) return;

  // Dedup within this process: each tool call is ingested at most once.
  // Bounded to avoid unbounded growth on long sessions; the kernel's
  // append-only ledger is the durable record, this is only a short-term
  // in-flight guard.
  const seen = new Set<string>();
  const SEEN_CAP = 4096;
  let failedIngests = 0;

  async function ingest(eventType: string, session: string, key: string, summary: string): Promise<void> {
    if (seen.has(key)) return;
    seen.add(key);
    if (seen.size > SEEN_CAP) {
      const first = seen.values().next().value as string;
      seen.delete(first);
    }
    try {
      const opts = { dbDir: config.dbDir, pythonPath: config.pythonPath, timeoutMs: config.timeoutMs };
      await ensureNonameInit(opts);
      await runNoname(
        ["event", "--session", session, "--type", eventType, "--payload", JSON.stringify({ text: summary })],
        opts,
      );
    } catch (err) {
      // "No silent loss": never break the host session, but never hide that we
      // dropped evidence either.  Re-queue so a transient sidecar failure does
      // not permanently mark this event as seen.
      seen.delete(key);
      failedIngests += 1;
      console.warn(
        `[noname-harness] failed to ingest evidence (${failedIngests} so far): ` +
          `${(err as Error).message?.slice(0, 160) ?? err}`,
      );
    }
  }

  ctx.on("tools/result", (exec: ToolExec, result: ToolResult) => {
    const text = (result?.content ?? [])
      .map((b) => (b.type === "text" ? b.text ?? "" : ""))
      .join("")
      .slice(0, 500);
    const session = sessionIdOf(exec);
    // Dedup key: the correlation id when present, else a content fingerprint
    // scoped to the session so identical short outputs in different sessions
    // do not collide (and identical ones in the same session do).
    const key = exec?.callId ?? `${session}:${exec?.name}:${text.slice(0, 64)}`;
    void ingest("dsh.tool.completed", session, key, `tool ${exec?.name ?? "?"} -> ${text}`);
  });
}
