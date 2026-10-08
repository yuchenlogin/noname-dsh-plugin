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
import { ensureNonameInit, runNoname } from "./kernel.js";
import { ledgerTarget, sessionIdOf } from "./workspace.js";
/**
 * The session id for one ingested event.
 *
 * REAL-HOST FINDING (DSH 0.2.0-rc.2): `ToolExecution` carries
 * `name`/`callId`/`agent` and no top-level `sessionId`, so an
 * `sessionId ?? session.id` probe silently fell through to the shared
 * `dsh-unknown` bucket -- exactly the provenance loss this module forbids.
 * The id lives on `exec.agent.sessionId`; workspace.ts reads every known
 * shape.  `dsh-unknown` stays as the honest, visible last resort.
 */
function ingestSessionId(exec) {
    return sessionIdOf(exec) ?? "dsh-unknown";
}
export function registerIngestion(ctx, config) {
    if (!config.autoIngest)
        return;
    // Dedup within this process: each tool call is ingested at most once.
    // Bounded to avoid unbounded growth on long sessions; the kernel's
    // append-only ledger is the durable record, this is only a short-term
    // in-flight guard.
    const seen = new Set();
    const SEEN_CAP = 4096;
    let failedIngests = 0;
    // Serialize writes: SQLite allows one writer at a time, so concurrent
    // ingests would otherwise race into "database is locked".  A promise chain
    // keeps the evidence stream ordered and loss-free under bursts.
    let queue = Promise.resolve();
    async function ingest(eventType, session, key, summary) {
        if (seen.has(key))
            return;
        seen.add(key);
        if (seen.size > SEEN_CAP) {
            const first = seen.values().next().value;
            seen.delete(first);
        }
        try {
            // Scope the write to the session's workspace: evidence from two
            // projects must never land in one ledger just because one host process
            // served both.
            const target = ledgerTarget(ctx, config, session);
            const opts = {
                dbDir: target.dbDir,
                root: target.root,
                pythonPath: config.pythonPath,
                timeoutMs: config.timeoutMs,
            };
            await ensureNonameInit(opts);
            await runNoname(["event", "--session", session, "--type", eventType, "--payload", JSON.stringify({ text: summary })], opts);
        }
        catch (err) {
            // "No silent loss": never break the host session, but never hide that we
            // dropped evidence either.  Re-queue so a transient sidecar failure does
            // not permanently mark this event as seen.
            seen.delete(key);
            failedIngests += 1;
            console.warn(`[noname-harness] failed to ingest evidence (${failedIngests} so far): ` +
                `${err.message?.slice(0, 160) ?? err}`);
        }
    }
    ctx.on("tools/result", (exec, result) => {
        const full = (result?.content ?? [])
            .map((b) => (b.type === "text" ? b.text ?? "" : ""))
            .join("");
        // Honest sampling: the ledger stores a bounded excerpt, but the reader must
        // KNOW it is truncated (NoName: compression may be lossy, presentation may
        // not lie).  The full output stays in the DSH transcript (the host of record).
        const truncated = full.length > 500;
        const text = full.slice(0, 500) + (truncated ? " … [truncated, full in DSH transcript]" : "");
        // A tool that produced nothing carries nothing.  Writing a row anyway would
        // spend a seq and a sidecar call on an event that can never be recalled --
        // and the kernel now refuses an empty event outright, so this would surface
        // as a permanent failed-ingest warning.  Skipping is not silent loss: there
        // is no content to lose.
        if (!text.trim())
            return;
        const session = ingestSessionId(exec);
        // Dedup key: the correlation id when present, else a content fingerprint
        // scoped to the session so identical short outputs in different sessions
        // do not collide (and identical ones in the same session do).
        const key = exec?.callId ?? `${session}:${exec?.name}:${text.slice(0, 64)}`;
        queue = queue.then(() => ingest("dsh.tool.completed", session, key, `tool ${exec?.name ?? "?"} -> ${text}`));
        const p = queue;
        pending.add(p);
        void p.finally(() => pending.delete(p));
    });
    /** Test/debug hook: wait for all in-flight ingests to settle. */
    async function drain() {
        await Promise.all([...pending]);
    }
    registerIngestion.__drain = drain;
}
const pending = new Set();
/** Wait for every in-flight ingest to settle (used by tests). */
export async function drainIngests() {
    await Promise.all([...pending]);
}
