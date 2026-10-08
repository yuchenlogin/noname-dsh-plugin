/**
 * NoName Agent Harness as a DeepSeek Harness plugin.
 *
 * Context is an asset, not a consumable: this plugin brings NoName's
 * append-only evidence stream, versioned memory, first-class taste and
 * audited approvals into DSH -- without re-implementing any of it.  The
 * Python kernel (vendor/noname-harness) stays the single source of truth;
 * this layer registers model-visible tools, ingests DSH session events into
 * the evidence stream, and renders the ledger panel.
 */

import type { Context } from "@deepseek-ai/cordis";
import { resolveConfig, type NonameConfig } from "./config.js";
import { registerNonameTools } from "./tools.js";
import { registerIngestion } from "./ingest.js";
import { registerLedgerTab } from "./ui/ledger-tab.js";
import { pingKernel } from "./kernel.js";

export const name = "noname-harness";
export const inject = ["tools"];

export interface Config extends Partial<NonameConfig> {}

export function apply(ctx: Context, config?: Config): void {
  const resolved = resolveConfig(config);

  // Register capabilities first so the plugin is useful even if the sidecar
  // is not yet reachable (tools surface the bridge error on call).
  registerNonameTools(ctx, resolved);
  registerIngestion(ctx, resolved);
  registerLedgerTab(ctx, resolved);

  // Detect the kernel at load and guide setup if missing.  A failed ping is
  // reported, not fatal: the user may still be installing the submodule.
  ctx.effect(() => {
    void (async () => {
      const ok = await pingKernel({ dbDir: resolved.dbDir ?? "", pythonPath: resolved.pythonPath });
      if (!ok) {
        console.warn(
          "[noname-harness] kernel not reachable. Ensure python3 is installed " +
            "and vendor/noname-harness is present (git submodule update --init).",
        );
      } else {
        console.log("[noname-harness] kernel ready: context is an asset.");
      }
    })();
    return () => {};
  });
}

export default { name, inject, apply };
