/**
 * NoName ledger access.
 *
 * REAL-HOST FINDING (verified against a live DeepSeek Harness 0.2.0-rc.2 web
 * profile): the `sidebarRightTabs` service is NOT present on the host side of
 * the plugin runtime -- it lives on the client-UI side, which a tool/service
 * plugin cannot reach.  A sidebar panel therefore cannot be registered from
 * this plugin today, and claiming one would be a lie.
 *
 * The honest form of the ledger in DSH is therefore a TOOL: `noname_ledger`
 * returns the five-view HTML the kernel generates, which the model (and the
 * user, by saving it) can open.  This is verified to load and run on the real
 * host.  Should DSH later expose a host-reachable panel seam, the same
 * buildLedgerView() output plugs straight into it.
 */
import { buildLedgerView } from "./ledger-view.js";
/**
 * Kept for API compatibility: historically this registered a sidebar tab.
 * On the real host there is no reachable sidebar seam, so this is a no-op
 * (the ledger is exposed as the `noname_ledger` tool instead).  It never
 * touches the inject gate, so it can never break plugin load.
 */
export function registerLedgerTab(_ctx, _config) {
    // Intentionally a no-op: see the module docstring for the real-host finding.
}
export { buildLedgerView };
