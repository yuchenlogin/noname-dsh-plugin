/** Own the bounded, loopback ledger servers launched by this plugin. */
import type { Context } from "@deepseek-ai/cordis";
import { type NonameRunOptions } from "../kernel.js";
/** Plugin disposal and test teardown; no server is left unbounded. */
export declare function stopLedgerServers(ctx?: Context): Promise<void>;
/** Reuse a live server for this context's ledger, coalescing parallel opens. */
export declare function openLedgerServer(ctx: Context, opts: NonameRunOptions): Promise<string>;
