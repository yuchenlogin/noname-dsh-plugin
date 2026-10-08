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
import type { NonameConfig } from "./config.js";
export declare function registerIngestion(ctx: Context, config: NonameConfig): void;
/** Wait for every in-flight ingest to settle (used by tests). */
export declare function drainIngests(): Promise<void>;
