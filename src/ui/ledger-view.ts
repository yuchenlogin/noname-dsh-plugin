/**
 * NoName ledger view: preserve the offline artifact and launch the kernel's
 * live review UI. Both views use the kernel renderer and review semantics.
 *
 * The artifact is written to a FIXED path inside the workspace and overwritten
 * each render, so the db dir does not accumulate timestamped HTML files.
 */

import { readFile } from "node:fs/promises";
import { join } from "node:path";
import type { Context } from "@deepseek-ai/cordis";
import { ensureNonameInit, runNoname } from "../kernel.js";
import type { NonameConfig } from "../config.js";
import { ledgerTarget, sessionIdOf } from "../workspace.js";
import { openLedgerServer } from "./ledger-server.js";

export interface LedgerViewResult {
  /** Absolute snapshot path, or null when the optional export failed. */
  htmlPath: string | null;
  /** Raw HTML (for clients that render a string rather than a file). */
  html: string;
  /** Authenticated loopback URL with human review controls. */
  liveUrl: string;
  /** Export/read error; live reviews remain available. */
  snapshotError?: string;
}

/** Generate the offline artifact and return the live ledger's review URL. */
export async function buildLedgerView(
  ctx: Context,
  config: NonameConfig,
  exec?: unknown,
): Promise<LedgerViewResult> {
  const target = ledgerTarget(ctx, config, sessionIdOf(exec));
  const opts = {
    dbDir: target.dbDir,
    root: target.root,
    pythonPath: config.pythonPath,
    timeoutMs: config.timeoutMs,
    signal: (exec as { signal?: AbortSignal } | undefined)?.signal,
  };
  await ensureNonameInit(opts);
  // Browser review does not depend on exporting or reading a static file.
  const liveUrl = await openLedgerServer(ctx, opts);
  // ledger-html enforces the NoName workspace boundary: --out must live inside
  // the project root.  With workspace scoping that root IS the project, and
  // the ledger dir (<project>/.noname) sits inside it, so the HTML lands next
  // to its db.  A fixed name + --overwrite keeps the artifact bounded.
  const outPath = join(target.dbDir, "noname-ledger.html");
  try {
    await runNoname(["ledger-html", "--out", outPath, "--overwrite"], opts);
    const html = await readFile(outPath, "utf-8");
    return { htmlPath: outPath, html, liveUrl };
  } catch (error) {
    return {
      htmlPath: null, html: "", liveUrl,
      snapshotError: error instanceof Error ? error.message : String(error),
    };
  }
}
