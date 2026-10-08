/**
 * NoName ledger view: generates the five-view ledger HTML by delegating to
 * the kernel's `ledger-html`.  The HTML is produced by NoName itself (its own
 * design language, offline, single file), so the view stays faithful to the
 * source and never re-implements the renderer.
 *
 * The artifact is written to a FIXED path inside the workspace and overwritten
 * each render, so the db dir does not accumulate timestamped HTML files.
 */

import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { ensureNonameInit, runNoname } from "../kernel.js";
import type { NonameConfig } from "../config.js";

export interface LedgerViewResult {
  /** Absolute path of the generated HTML. */
  htmlPath: string;
  /** Raw HTML (for clients that render a string rather than a file). */
  html: string;
}

/** Generate the ledger HTML via the kernel and return its path + contents. */
export async function buildLedgerView(config: NonameConfig): Promise<LedgerViewResult> {
  const opts = { dbDir: config.dbDir, pythonPath: config.pythonPath, timeoutMs: config.timeoutMs };
  await ensureNonameInit({ ...opts, root: config.dbDir });
  // ledger-html enforces the NoName workspace boundary: --out must live inside
  // the project root (the db dir here).  A fixed name + --overwrite keeps the
  // artifact bounded (one file, replaced each render).
  const outPath = join(config.dbDir, "noname-ledger.html");
  await runNoname(["ledger-html", "--out", outPath, "--overwrite"], opts);
  const html = await readFile(outPath, "utf-8");
  return { htmlPath: outPath, html };
}
