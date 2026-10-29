/** Register the shared kernel policy before DSH builds any model request. */
import type { Context } from "@deepseek-ai/cordis";
import type { NonameConfig } from "./config.js";
export declare const OUTPUT_PREFERENCE_SECTION = "noname:output-preferences";
/** Read the same versioned asset Python uses, from the pinned shipped kernel. */
export declare function readWritingPolicy(): string;
export declare function registerOutputPreferences(ctx: Context, config: NonameConfig): void;
