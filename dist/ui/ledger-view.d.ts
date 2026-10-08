/**
 * NoName ledger view: generates the five-view ledger HTML by delegating to
 * the kernel's `ledger-html`.  The HTML is produced by NoName itself (its own
 * design language, offline, single file), so the view stays faithful to the
 * source and never re-implements the renderer.
 *
 * The artifact is written to a FIXED path inside the workspace and overwritten
 * each render, so the db dir does not accumulate timestamped HTML files.
 */
import type { Context } from "@deepseek-ai/cordis";
import type { NonameConfig } from "../config.js";
export interface LedgerViewResult {
    /** Absolute path of the generated HTML. */
    htmlPath: string;
    /** Raw HTML (for clients that render a string rather than a file). */
    html: string;
}
/** Generate the ledger HTML via the kernel and return its path + contents. */
export declare function buildLedgerView(ctx: Context, config: NonameConfig, exec?: unknown): Promise<LedgerViewResult>;
