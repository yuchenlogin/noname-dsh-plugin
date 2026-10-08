/**
 * NoName ledger panel: renders the five-view ledger (review inbox / state /
 * version evolution / causal map / timeline) by delegating generation to the
 * kernel's `ledger-html` and embedding the result.  The HTML is produced by
 * NoName itself (its own design language, offline, single file), so the panel
 * stays faithful to the source and never re-implements the renderer.
 */

import { join } from "node:path";
import { ensureNonameInit, runNoname } from "../kernel.js";
import type { NonameConfig } from "../config.js";

export interface LedgerViewResult {
  /** Absolute path of the generated HTML (open in an iframe / webview). */
  htmlPath: string;
  /** Raw HTML (for clients that render a string rather than a file). */
  html: string;
}

/** Generate the ledger HTML via the kernel and return its path + contents. */
export async function buildLedgerView(config: NonameConfig): Promise<LedgerViewResult> {
  const opts = { dbDir: config.dbDir ?? "", pythonPath: config.pythonPath };
  await ensureNonameInit({ ...opts, root: opts.dbDir });
  // ledger-html enforces the NoName workspace boundary: --out must live
  // inside the project root.  The db dir IS the workspace root here, so the
  // artifact is written under it (and cleaned up by the host's own policy).
  const outPath = join(opts.dbDir, `noname-ledger-${Date.now()}.html`);
  await runNoname(["ledger-html", "--out", outPath, "--overwrite"], opts);
  const { readFile } = await import("node:fs/promises");
  const html = await readFile(outPath, "utf-8");
  return { htmlPath: outPath, html };
}
