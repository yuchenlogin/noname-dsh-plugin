/**
 * DSH model-visible tools backed by the NoName kernel.
 *
 * Each tool is a thin adapter: defineTool declares the model-facing schema,
 * execute() bridges to `python -m noname_harness <cmd>`, and output.render
 * converts the kernel's JSON into model-readable text.  No kernel logic is
 * re-implemented here -- approval gates, sandbox boundaries and the ledger
 * all stay inside NoName.
 *
 * The tool set deliberately spans NoName's two taste tracks (authored AND
 * adopted) and its review lifecycle, because taste is a first-class citizen
 * in NoName, not a single record primitive.
 */
import type { Context } from "@deepseek-ai/cordis";
import type { NonameConfig } from "./config.js";
export declare function registerNonameTools(ctx: Context, config: NonameConfig): void;
export declare const NONAME_TOOL_NAMES: readonly ["noname_record", "noname_package", "noname_search", "noname_state", "noname_inbox", "noname_taste_add", "noname_taste_propose", "noname_taste_review", "noname_card_queue", "noname_ledger", "noname_verify", "noname_extract"];
