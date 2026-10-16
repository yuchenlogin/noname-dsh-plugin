/** User journey through real DSH tool definitions, Python, SQLite and HTTP. */
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, expect, it } from "vitest";
import { resolveConfig } from "../src/config.js";
import { registerIngestion, drainIngests } from "../src/ingest.js";
import { runNoname } from "../src/kernel.js";
import { registerNonameTools } from "../src/tools.js";
import { stopLedgerServers } from "../src/ui/ledger-server.js";

let dir: string;
afterEach(async () => {
  await stopLedgerServers();
  if (dir) await rm(dir, { recursive: true, force: true });
});

it("continues work from automatic evidence, selectively remembers, and reviews a correction in the browser", async () => {
  dir = await mkdtemp(join(tmpdir(), "noname-journey-"));
  const tools = new Map<string, { execute: (args: Record<string, unknown>, exec: unknown) => Promise<unknown> }>();
  const handlers = new Map<string, (exec: unknown, result: unknown) => void>();
  const ctx = {
    tools: { register: (tool: { name: string } & { execute: (args: Record<string, unknown>, exec: unknown) => Promise<unknown> }) => tools.set(tool.name, tool) },
    on: (event: string, handler: (exec: unknown, result: unknown) => void) => handlers.set(event, handler),
  } as unknown as Parameters<typeof registerNonameTools>[0];
  const config = resolveConfig({ dbDir: dir });
  registerNonameTools(ctx, config);
  registerIngestion(ctx, config);
  const invoke = (name: string, args: Record<string, unknown>, session = "first") =>
    tools.get(name)!.execute(args, { agent: { sessionId: session } });

  // First use does not require a hidden initialization command.
  expect(String(await invoke("noname_package", { task: "Start the storage CLI", session: "first" }))).toContain("pkg_");
  handlers.get("tools/result")!(
    { name: "bash", callId: "tests", agent: { sessionId: "first" } },
    { content: [{ type: "text", text: "Verified SQLite persistence across restart." }] },
  );
  await drainIngests();
  expect(JSON.parse(String(await invoke("noname_state", {})))).toEqual([]);

  let previousHead = "";
  let liveUrl = "";
  for (const decision of ["Use SQLite for the local CLI.", "Use PostgreSQL for the shared service."]) {
    const record = String(await invoke("noname_record", { session: "first", event_type: "decision", text: decision }));
    const candidate = String(await invoke("noname_remember", {
      layer: "high", key: "project.storage", text: decision,
      source_event: record.match(/evt_[a-f0-9]+/)![0], reason: "Explicit project decision.",
    }));
    const id = candidate.match(/prp_[a-f0-9]+/)![0];
    const inbox = JSON.parse(String(await invoke("noname_inbox", {})));
    expect(inbox.counts.total).toBe(1);
    const ledger = String(await invoke("noname_ledger", {}));
    const opened = ledger.match(/live ledger: (\S+)/)![1];
    if (liveUrl) expect(opened).toBe(liveUrl);
    liveUrl = opened;
    const page = await (await fetch(liveUrl)).text();
    const version = page.match(new RegExp(`data-id="${id}" data-version="([a-f0-9]+)"`))![1];
    const url = new URL(liveUrl);
    const review = await fetch(`${liveUrl}api/review`, {
      method: "POST", headers: {
        "Content-Type": "application/json", Origin: url.origin,
        "X-NoName-Token": url.pathname.split("/")[1],
      },
      body: JSON.stringify({
        kind: "state_proposal", id, action: "accept", reviewer_id: "journey-test-human",
        reason: "Checked the explicit decision and its source.", expected_version: version,
        expected_head_id: previousHead,
      }),
    });
    expect(review.status).toBe(200);
    const current = JSON.parse(String(await invoke("noname_state", {})));
    expect(current).toHaveLength(1);
    expect(current[0].content.text).toBe(decision);
    if (previousHead) expect(current[0].supersedes_id).toBe(previousHead);
    previousHead = current[0].id;
  }
  const recalled = String(await invoke("noname_package", { task: "Continue the service", session: "second" }, "second"));
  expect(recalled).toContain("Use PostgreSQL for the shared service.");
  const eventsBefore = await runNoname<unknown[]>(["ledger", "--limit", "1000"], { dbDir: dir });
  handlers.get("tools/result")!(
    { name: "noname_package", callId: "recall", agent: { sessionId: "second" } },
    { content: [{ type: "text", text: recalled }] },
  );
  handlers.get("tools/result")!(
    { name: "noname_ledger", callId: "private-url", agent: { sessionId: "second" } },
    { content: [{ type: "text", text: liveUrl }] },
  );
  await drainIngests();
  expect(await runNoname(["ledger", "--limit", "1000"], { dbDir: dir })).toEqual(eventsBefore);
  expect(String(await invoke("noname_verify", {}))).toContain("integrity OK");
}, 30_000);
