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
import { resolveConfig } from "./config.js";
import { registerNonameTools } from "./tools.js";
import { registerIngestion } from "./ingest.js";
import { registerLedgerTab } from "./ui/ledger-tab.js";
import { pingKernel } from "./kernel.js";
import { ledgerTarget } from "./workspace.js";
export const name = "noname-harness";
// Declared dependencies: Cordis readies `tools` before apply; the sidebar
// registry is optional (accessed defensively inside an effect, not injected,
// so its absence never fails plugin load).
export const inject = ["tools"];
export function apply(ctx, config) {
    const resolved = resolveConfig(config);
    // Register capabilities first so the plugin is useful even if the sidecar
    // is not yet reachable (a tool call then surfaces the bridge error).
    registerNonameTools(ctx, resolved);
    registerIngestion(ctx, resolved);
    registerLedgerTab(ctx, resolved);
    // Detect the kernel at load and guide setup if missing.  A failed ping is
    // a warning, not fatal: the user may still be installing the submodule.
    // The ping has no session, so it resolves the target without one (the only
    // workspace, else the legacy directory) -- it never decides where evidence
    // goes, it only reports whether the sidecar answers.
    ctx.effect(() => {
        void (async () => {
            const target = ledgerTarget(ctx, resolved);
            const ok = await pingKernel({
                dbDir: target.dbDir,
                root: target.root,
                pythonPath: resolved.pythonPath,
                timeoutMs: resolved.timeoutMs,
            });
            if (!ok) {
                console.warn("[noname-harness] kernel not reachable. Ensure python3 is installed " +
                    "and vendor/noname-harness is present (git submodule update --init).");
            }
            else {
                console.log(`[noname-harness] kernel ready (ledger: ${target.dbDir}): context is an asset.`);
            }
        })();
        return () => {
            // Unload: sidecar calls are short-lived (bounded by timeoutMs) and each
            // bridge call already owns its process + abort path, so there is nothing
            // to force-kill here.  (A previous AbortController pool was removed: it
            // was never wired to any call -- claiming it aborted anything was a lie.)
        };
    });
}
export default { name, inject, apply };
