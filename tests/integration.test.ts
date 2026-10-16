/**
 * End-to-end integration: the full "context is an asset" loop through the
 * plugin surface against the real kernel -- record evidence, extract memory
 * candidates, assemble a handoff package, and render the ledger panel.
 */

import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ensureNonameInit, runNoname } from "../src/kernel.js";
import { buildLedgerView } from "../src/ui/ledger-view.js";
import { stopLedgerServers } from "../src/ui/ledger-server.js";
import { resolveConfig } from "../src/config.js";

let dir: string;
let opts: { dbDir: string };

beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "noname-e2e-"));
  opts = { dbDir: dir };
});
afterEach(async () => {
  await stopLedgerServers();
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

  it("ledger returns an offline snapshot and a live page with the five reader views", async () => {
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
    for (const label of ["此刻", "等你判断", "品味与偏好", "来路与变化", "活动档案"]) {
      expect(view.html).toContain(label);
    }
    expect(new URL(view.liveUrl).hostname).toBe("127.0.0.1");
    const response = await fetch(view.liveUrl);
    expect(response.status).toBe(200);
    expect(await response.text()).toContain("活动档案");
  }, 30_000);

  it("browser review persists into the real project ledger and rejects replay", async () => {
    await ensureNonameInit(opts);
    const source = await runNoname<{ id: string }>([
      "event", "--session", "browser", "--type", "user.message",
      "--payload", JSON.stringify({ text: "approve this rule in the ledger browser" }),
    ], opts);
    const proposal = await runNoname<{ id: string }>([
      "propose", "--layer", "high", "--key", "browser.constraint",
      "--content", JSON.stringify({ rule: "approved from the ledger browser" }),
      "--source-event", source.id,
      "--by", "test", "--confidence", "0.9", "--reason", "browser integration",
    ], opts);
    const ctx = {} as Parameters<typeof buildLedgerView>[0];
    const view = await buildLedgerView(ctx, resolveConfig({ dbDir: dir }));
    const page = await (await fetch(view.liveUrl)).text();
    const version = page.match(new RegExp(`data-id="${proposal.id}" data-version="([a-f0-9]+)"`))?.[1];
    expect(version).toMatch(/^[a-f0-9]{64}$/);
    expect(page).toContain('data-expected-head-id=""');
    const url = new URL(view.liveUrl);
    const headers = {
      "Content-Type": "application/json",
      Origin: url.origin,
      "X-NoName-Token": url.pathname.split("/")[1],
    };
    const body = JSON.stringify({
      kind: "state_proposal", id: proposal.id, action: "accept",
      reviewer_id: "test-human", reason: "explicit browser approval", expected_version: version,
      expected_head_id: "",
    });
    const blocked = await fetch(`${view.liveUrl}api/review`, {
      method: "POST", headers: { ...headers, Origin: "https://example.org" }, body,
    });
    expect(blocked.status).toBe(403);
    expect(await runNoname(["state"], opts)).toEqual([]);
    const accepted = await fetch(`${view.liveUrl}api/review`, { method: "POST", headers, body });
    expect(accepted.status).toBe(200);
    expect(JSON.stringify(await runNoname(["state"], opts))).toContain("approved from the ledger browser");
    const replay = await fetch(`${view.liveUrl}api/review`, { method: "POST", headers, body });
    expect(replay.status).toBe(409);
    const all = await runNoname<{ reviews: { reviewer_id: string; reason: string }[] }[]>(["proposals", "--all"], opts);
    expect(all[0].reviews).toHaveLength(1);
    expect(all[0].reviews[0]).toMatchObject({ reviewer_id: "test-human", reason: "explicit browser approval" });
    const reused = await buildLedgerView(ctx, resolveConfig({ dbDir: dir }));
    expect(reused.liveUrl).toBe(view.liveUrl);
    expect((await runNoname<{ ok: boolean }>(["verify"], opts)).ok).toBe(true);
  }, 30_000);

  it("keeps the real live page accessible when the snapshot path is a directory", async () => {
    await mkdir(join(dir, "noname-ledger.html"));
    const view = await buildLedgerView({} as Parameters<typeof buildLedgerView>[0], resolveConfig({ dbDir: dir }));
    expect(view.htmlPath).toBeNull();
    expect(view.html).toBe("");
    expect(view.snapshotError).toBeTruthy();
    const response = await fetch(view.liveUrl);
    expect(response.status).toBe(200);
    expect(await response.text()).toContain("等你判断");
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
