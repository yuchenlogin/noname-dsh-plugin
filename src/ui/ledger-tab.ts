/**
 * Sidebar tab registration for the NoName ledger panel -- honest best-effort.
 *
 * The real DSH sidebar contract (reference/subsystems/sidebar-right) is a
 * TWO-part registration: a static definition in `ctx.sidebarRightTabs` plus a
 * keyed slot that provides the body, with `useTabInfo()` injected and the
 * framework owning the `sidebar://<kind>` address.  That full surface cannot
 * be exercised without a live DSH host, so this module:
 *
 *  1. registers the static definition through the documented
 *     `ctx.sidebarRightTabs.register(...)` shape (in an effect, so it is
 *     disposed with the plugin), and
 *  2. exposes the ledger HTML builder separately so the panel body (and any
 *     future settings card) renders real content.
 *
 * It does NOT claim a working keyed-slot body: that requires a live host to
 * verify, and is marked as such rather than faked.  If the sidebar service is
 * absent, the plugin still loads and every tool keeps working.
 */

import type { Context } from "@deepseek-ai/cordis";
import { buildLedgerView } from "./ledger-view.js";
import type { NonameConfig } from "../config.js";

export const LEDGER_TAB_KIND = "noname-ledger";

interface SidebarTabsRegistry {
  register?: (definition: Record<string, unknown>) => (() => void) | void;
}

export function registerLedgerTab(ctx: Context, config: NonameConfig): void {
  const registry = (ctx as unknown as { sidebarRightTabs?: SidebarTabsRegistry })
    .sidebarRightTabs;

  ctx.effect(() => {
    let dispose: (() => void) | void;
    if (typeof registry?.register === "function") {
      try {
        // Static definition per the documented shape: an id, the kind of
        // address it opens, a localized title and a loading hint.  The body is
        // provided separately (see buildLedgerView) and wired to a keyed slot
        // once verified against a live host.
        dispose = registry.register({
          id: LEDGER_TAB_KIND,
          kind: LEDGER_TAB_KIND,
          title: () => "NoName 账本",
          loading: () => "正在生成账本…",
          // The renderer delegates to the kernel; a live host calls this to
          // obtain the five-view HTML.
          render: async () => ({ html: (await buildLedgerView(config)).html }),
        });
      } catch (err) {
        console.warn(
          `[noname-harness] sidebar tab registration failed (panel disabled): ${(err as Error).message}`,
        );
      }
    } else {
      console.warn(
        "[noname-harness] sidebar registry not present; ledger panel disabled, tools unaffected",
      );
    }
    return () => {
      if (typeof dispose === "function") dispose();
    };
  });
}
