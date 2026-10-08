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
import { ledgerTarget, sessionIdOf } from "../workspace.js";
/** Generate the ledger HTML via the kernel and return its path + contents. */
export async function buildLedgerView(ctx, config, exec) {
    const target = ledgerTarget(ctx, config, sessionIdOf(exec));
    const opts = {
        dbDir: target.dbDir,
        root: target.root,
        pythonPath: config.pythonPath,
        timeoutMs: config.timeoutMs,
    };
    await ensureNonameInit(opts);
    // ledger-html enforces the NoName workspace boundary: --out must live inside
    // the project root.  With workspace scoping that root IS the project, and
    // the ledger dir (<project>/.noname) sits inside it, so the HTML lands next
    // to its db.  A fixed name + --overwrite keeps the artifact bounded.
    const outPath = join(target.dbDir, "noname-ledger.html");
    await runNoname(["ledger-html", "--out", outPath, "--overwrite"], opts);
    const html = await readFile(outPath, "utf-8");
    return { htmlPath: outPath, html };
}
