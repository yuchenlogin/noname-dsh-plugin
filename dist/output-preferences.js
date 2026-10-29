import { readFileSync } from "node:fs";
import { join } from "node:path";
import { resolveKernelRoot } from "./kernel.js";
export const OUTPUT_PREFERENCE_SECTION = "noname:output-preferences";
/** Read the same versioned asset Python uses, from the pinned shipped kernel. */
export function readWritingPolicy() {
    const root = resolveKernelRoot();
    if (!root)
        throw new Error("NoName writing policy requires the bundled kernel snapshot");
    const policy = readFileSync(join(root, "noname_harness", "policies", "direct-writing.md"), "utf8").trim();
    if (!policy.startsWith('<noname-output-preferences version="1">') || !policy.endsWith("</noname-output-preferences>")) {
        throw new Error("NoName writing policy asset is invalid; sync the kernel snapshot");
    }
    return policy;
}
export function registerOutputPreferences(ctx, config) {
    if (config.writingStyle === "off")
        return;
    // Cordis owns this section's effect in the plugin context, including reload
    // and disposal. Static text stays prefix-stable across model/effort changes.
    ctx.systemPrompt.section({
        name: OUTPUT_PREFERENCE_SECTION,
        order: 9500,
        text: readWritingPolicy(),
        interpolate: false,
    });
}
