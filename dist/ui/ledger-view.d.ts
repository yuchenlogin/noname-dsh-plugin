/**
 * NoName ledger view: preserve the offline artifact and launch the kernel's
 * live review UI. Both views use the kernel renderer and review semantics.
 *
 * The artifact is written to a FIXED path inside the workspace and overwritten
 * each render, so the db dir does not accumulate timestamped HTML files.
 */
import type { Context } from "@deepseek-ai/cordis";
import type { NonameConfig } from "../config.js";
export interface LedgerViewResult {
    /** Absolute snapshot path, or null when the optional export failed. */
    htmlPath: string | null;
    /** Raw HTML (for clients that render a string rather than a file). */
    html: string;
    /** Authenticated loopback URL with human review controls. */
    liveUrl: string;
    /** Export/read error; live reviews remain available. */
    snapshotError?: string;
}
/** Generate the offline artifact and return the live ledger's review URL. */
export declare function buildLedgerView(ctx: Context, config: NonameConfig, exec?: unknown): Promise<LedgerViewResult>;
