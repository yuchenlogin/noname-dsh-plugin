/**
 * NoName Agent Harness as a DeepSeek Harness plugin.
 *
 * Context is an asset, not a consumable: this plugin brings NoName's
 * append-only evidence stream, versioned memory, first-class taste and
 * audited approvals into DSH -- without re-implementing any of it.  The
 * Python kernel (vendor/noname-harness) stays the single source of truth;
 * this layer registers model-visible tools (including the ledger as the
 * `noname_ledger` tool -- the host has no reachable sidebar seam, verified on
 * a live DSH web profile) and ingests DSH tool results into the evidence
 * stream.
 */
import type { Context } from "@deepseek-ai/cordis";
import { type NonameConfig } from "./config.js";
export declare const name = "noname-harness";
export declare const inject: string[];
export interface Config extends Partial<NonameConfig> {
}
export declare function apply(ctx: Context, config?: Config): void;
declare const _default: {
    name: string;
    inject: string[];
    apply: typeof apply;
};
export default _default;
