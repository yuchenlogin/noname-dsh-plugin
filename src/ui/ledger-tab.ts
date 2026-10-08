/**
 * Sidebar tab registration for the NoName ledger panel.
 *
 * The tab type is registered once (its kind opens `noname://ledger`
 * addresses); a keyed slot provides the body.  The body renders the HTML
 * produced by buildLedgerView inside an iframe so the ledger keeps NoName's
 * own five-view design (克制 dark, progressive disclosure) unchanged.
 */

import type { Context } from "@deepseek-ai/cordis";
import { buildLedgerView } from "./ledger-view.js";
import type { NonameConfig } from "../config.js";

export const LEDGER_TAB_KIND = "noname-ledger";
export const LEDGER_ADDRESS = "noname://ledger";

export function registerLedgerTab(ctx: Context, config: NonameConfig): void {
  const sidebar = (ctx as unknown as {
    sidebarRightTabs?: { register?: (def: unknown) => void };
  }).sidebarRightTabs;

  // Register the tab type defensively: the exact sidebar API surface may vary
  // across DSH versions, so a missing registry degrades to "no panel" rather
  // than breaking plugin load.  The tools and ingestion still work.
  sidebar?.register?.({
    kind: LEDGER_TAB_KIND,
    title: "NoName 账本",
    address: LEDGER_ADDRESS,
    async render(): Promise<{ html: string }> {
      const view = await buildLedgerView(config);
      return { html: view.html };
    },
  });
}
