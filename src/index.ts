/**
 * NoName Agent Harness as a DeepSeek Harness plugin.
 *
 * Context is an asset, not a consumable: this plugin brings NoName's
 * append-only evidence stream, versioned memory, first-class taste and
 * audited approvals into DSH -- without re-implementing any of it.  The
 * Python kernel (vendor/noname-harness) stays the single source of truth;
 * this layer registers model-visible tools, ingests DSH tool results into
 * the evidence stream, and renders the ledger panel.
 */

import type { Context } from "@deepseek-ai/cordis";
import { resolveConfig, type NonameConfig } from "./config.js";
import { registerNonameTools } from "./tools.js";
import { registerIngestion } from "./ingest.js";
import { registerLedgerTab } from "./ui/ledger-tab.js";
import { pingKernel } from "./kernel.js";

export const name = "noname-harness";
// Declared dependencies: Cordis readies `tools` before apply; the sidebar
// registry is optional (accessed defensively inside an effect, not injected,
// so its absence never fails plugin load).
export const inject = ["tools"];

export interface Config extends Partial<NonameConfig> {}

export function apply(ctx: Context, config?: Config): void {
  const resolved = resolveConfig(config);

  // Central cancellation for in-flight sidecar calls: on unload (HMR/disable)
  // every tracked call is aborted so no python process outlives the plugin.
  const inFlight = new Set<AbortController>();
  const track = () => {
    const c = new AbortController();
    inFlight.add(c);
    return c;
  };

  // Register capabilities first so the plugin is useful even if the sidecar
  // is not yet reachable (a tool call then surfaces the bridge error).
  registerNonameTools(ctx, resolved);
  registerIngestion(ctx, resolved);
  registerLedgerTab(ctx, resolved);

  // Detect the kernel at load and guide setup if missing.  A failed ping is
  // a warning, not fatal: the user may still be installing the submodule.
  ctx.effect(() => {
    void (async () => {
      const ok = await pingKernel(resolved);
      if (!ok) {
        console.warn(
          "[noname-harness] kernel not reachable. Ensure python3 is installed " +
            "and vendor/noname-harness is present (git submodule update --init).",
        );
      } else {
        console.log("[noname-harness] kernel ready: context is an asset.");
      }
    })();
    return () => {
      // Unload: abort every in-flight sidecar call, then let each bridge call
      // clean up its own process via its abort path.
      for (const c of inFlight) c.abort();
      inFlight.clear();
    };
  });
}

export default { name, inject, apply };
