/**
 * End-to-end integration: the full "context is an asset" loop through the
 * plugin surface against the real kernel -- record evidence, extract memory
 * candidates, assemble a handoff package, and render the ledger panel.
 */

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ensureNonameInit, runNoname } from "../src/kernel.js";
import { buildLedgerView } from "../src/ui/ledger-view.js";
import { resolveConfig } from "../src/config.js";

let dir: string;
let opts: { dbDir: string };

beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "noname-e2e-"));
  opts = { dbDir: dir };
});
afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
});

describe("context-as-asset loop", () => {
  it("record -> extract -> propose -> package survives a session switch", async () => {
    await ensureNonameInit(opts);

    // Work happens in session A.
    const evt = await runNoname<{ id: string }>(
      ["event", "--session", "A", "--type", "user.message", "--payload", JSON.stringify({ text: "We prefer explicit state machines and append-only logs." })],
      opts,
    );

    // Extraction surfaces candidates (never auto-confirmed).
    const extraction = await runNoname<{ scanned_events: number }>(["extract", "--session", "A"], opts);
    expect(extraction.scanned_events).toBeGreaterThan(0);

    // A human-approved canon entry (the constitution).
    await runNoname(
      ["propose", "--layer", "high", "--key", "project.constraint", "--content", JSON.stringify({ rule: "append-only is the source of truth" }), "--source-event", evt.id, "--by", "test", "--confidence", "0.9", "--reason", "stated"],
      opts,
    );
    const proposals = await runNoname<{ id: string }[]>(["proposals"], opts);
    await runNoname(["review", "--proposal", proposals[0].id, "--action", "accept", "--reviewer", "human"], opts);

    // A NEW session assembles the handoff package -- the whole point.
    const pkg = await runNoname<string>(["package", "--task", "continue work", "--session", "B"], { ...opts, raw: true });
    expect(String(pkg)).toContain("append-only");
  }, 30_000);

  it("ledger panel renders the five views as non-empty HTML", async () => {
    await ensureNonameInit(opts);
    await runNoname(
      ["event", "--session", "A", "--type", "user.message", "--payload", JSON.stringify({ text: "context is an asset" })],
      opts,
    );
    const view = await buildLedgerView(
      {} as Parameters<typeof buildLedgerView>[0],
      resolveConfig({ dbDir: dir }),
    );
    expect(view.html.length).toBeGreaterThan(1000);
    for (const label of ["收件箱", "状态", "版本演进", "因果图", "时间线"]) {
      expect(view.html).toContain(label);
    }
  }, 30_000);

  it("verify passes after a realistic mixed workload", async () => {
    await ensureNonameInit(opts);
    for (let i = 0; i < 5; i++) {
      await runNoname(
        ["event", "--session", "work", "--type", "finding", "--payload", JSON.stringify({ text: `finding ${i}` })],
        opts,
      );
    }
    const r = await runNoname<{ ok: boolean }>(["verify"], opts);
    expect(r.ok).toBe(true);
  }, 30_000);
});
