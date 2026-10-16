/**
 * Ingestion honesty tests: provenance (real session id), no-silent-loss
 * (failure is warned + re-queued), and dedup behaviour.
 */

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { registerIngestion, drainIngests } from "../src/ingest.js";
import { resolveConfig } from "../src/config.js";
import { runNoname } from "../src/kernel.js";

type Handler = (exec: unknown, result: unknown) => void;

function stubCtx() {
  const handlers: Record<string, Handler> = {};
  const ctx = {
    on: (event: string, h: Handler) => {
      handlers[event] = h;
    },
  };
  return { ctx: ctx as unknown as Parameters<typeof registerIngestion>[0], handlers };
}

let dir: string;
beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "noname-ingest-"));
});
afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
  vi.restoreAllMocks();
});

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("ingestion", () => {
  it("records a tool result under the REAL session id, not a shared bucket", async () => {
    const { ctx, handlers } = stubCtx();
    registerIngestion(ctx, resolveConfig({ dbDir: dir }));
    handlers["tools/result"](
      { name: "bash", callId: "c1", sessionId: "session-alpha" },
      { content: [{ type: "text", text: "did the thing alpha-unique" }] },
    );
    await drainIngests();
    const events = await runNoname<{ session_id: string }[]>(["ledger"], { dbDir: dir });
    const sessions = new Set(events.map((e) => e.session_id));
    expect(sessions.has("session-alpha")).toBe(true);
    expect(sessions.has("dsh")).toBe(false);
  });

  it("reads the session id from agent.sessionId (the real-host exec shape)", async () => {
    // DSH 0.2.0-rc.2 `ToolExecution` has no top-level sessionId: the id lives
    // on exec.agent.  Trusting only `sessionId` put every real-host event into
    // the shared `dsh-unknown` bucket while the tests kept passing.
    const { ctx, handlers } = stubCtx();
    registerIngestion(ctx, resolveConfig({ dbDir: dir }));
    handlers["tools/result"](
      { name: "bash", callId: "c-agent", agent: { sessionId: "session-real-host" } },
      { content: [{ type: "text", text: "ledger integrity OK" }] },
    );
    await drainIngests();
    const events = await runNoname<{ session_id: string }[]>(["ledger"], { dbDir: dir });
    expect(events.map((e) => e.session_id)).toContain("session-real-host");
    expect(events.some((e) => e.session_id === "dsh-unknown")).toBe(false);
  });

  it("dedups the same callId but not the same content in different sessions", async () => {
    const { ctx, handlers } = stubCtx();
    registerIngestion(ctx, resolveConfig({ dbDir: dir }));
    const payload = { content: [{ type: "text", text: "identical-output" }] };
    handlers["tools/result"]({ name: "bash", callId: "dup", sessionId: "s1" }, payload);
    handlers["tools/result"]({ name: "bash", callId: "dup", sessionId: "s1" }, payload); // same callId -> dedup
    handlers["tools/result"]({ name: "bash", callId: "dup", sessionId: "s2" }, payload); // same id in another session -> kept
    await drainIngests();
    const events = await runNoname<{ session_id: string }[]>(["ledger"], { dbDir: dir });
    const s1 = events.filter((e) => e.session_id === "s1").length;
    const s2 = events.filter((e) => e.session_id === "s2").length;
    expect(s1).toBe(1);
    expect(s2).toBe(1);
  });

  it("keeps NoName reads out of evidence, including the private ledger URL", async () => {
    const { ctx, handlers } = stubCtx();
    registerIngestion(ctx, resolveConfig({ dbDir: dir }));
    for (const name of ["noname_ledger", "noname_package", "noname_search", "noname_state", "noname_inbox", "noname_verify", "noname_card_queue", "noname_extract"]) {
      handlers["tools/result"]({ name, callId: name, sessionId: "s1" }, {
        content: [{ type: "text", text: "http://127.0.0.1:1234/private-review-token/" }],
      });
    }
    await drainIngests();
    expect(await runNoname(["ledger"], { dbDir: dir })).toEqual([]);
  });

  it("does not discard separate anonymous results with the same text prefix", async () => {
    const { ctx, handlers } = stubCtx();
    registerIngestion(ctx, resolveConfig({ dbDir: dir }));
    const prefix = "This command has a standard preamble. ".repeat(3);
    for (const ending of ["first finding", "second finding"]) {
      handlers["tools/result"]({ name: "bash", sessionId: "s1" }, {
        content: [{ type: "text", text: prefix + ending }],
      });
    }
    await drainIngests();
    const events = await runNoname<{ payload: { text: string } }[]>(["ledger"], { dbDir: dir });
    expect(events).toHaveLength(2);
    expect(events.map((event) => event.payload.text).join("\n")).toContain("second finding");
  });

  it("warns (not silently swallows) when the sidecar is unreachable", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { ctx, handlers } = stubCtx();
    registerIngestion(ctx, resolveConfig({ dbDir: dir, pythonPath: "missing-python-xyz" }));
    handlers["tools/result"]({ name: "bash", callId: "c9", sessionId: "s1" }, { content: [{ type: "text", text: "x" }] });
    await new Promise((r) => setTimeout(r, 300));
    expect(warn).toHaveBeenCalled();
    expect(String(warn.mock.calls[0][0])).toContain("failed to ingest evidence");
  });

  it("marks truncated evidence honestly (>500 chars gets a marker, shorter does not)", async () => {
    const { ctx, handlers } = stubCtx();
    registerIngestion(ctx, resolveConfig({ dbDir: dir }));
    handlers["tools/result"](
      { name: "bash", callId: "long", sessionId: "s1" },
      { content: [{ type: "text", text: "x".repeat(800) }] },
    );
    handlers["tools/result"](
      { name: "bash", callId: "short", sessionId: "s1" },
      { content: [{ type: "text", text: "short" }] },
    );
    await drainIngests();
    const events = await runNoname<{ payload: { text: string } }[]>(["ledger"], { dbDir: dir });
    const texts = events.map((e) => JSON.stringify(e.payload));
    const longOne = texts.find((t) => t.includes("truncated"));
    const shortOne = texts.find((t) => t.includes("short"));
    expect(longOne).toBeDefined();
    expect(longOne).toContain("full in DSH transcript");
    expect(shortOne).toBeDefined();
    expect(shortOne).not.toContain("truncated");
  });

  it("respects autoIngest=false (no listener registered)", () => {
    const { ctx, handlers } = stubCtx();
    registerIngestion(ctx, resolveConfig({ dbDir: dir, autoIngest: false }));
    expect(handlers["tools/result"]).toBeUndefined();
  });

  it("does not split a supplementary Unicode character at the excerpt boundary", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { ctx, handlers } = stubCtx();
    registerIngestion(ctx, resolveConfig({ dbDir: dir }));
    handlers["tools/result"]({ name: "bash", callId: "emoji", sessionId: "s1" }, {
      content: [{ type: "text", text: "x".repeat(499) + "🎉 end" }],
    });
    await drainIngests();
    const events = await runNoname<{ payload: { text: string } }[]>(["ledger"], { dbDir: dir });
    expect(events).toHaveLength(1);
    expect(events[0].payload.text).toContain("truncated");
    expect(warn).not.toHaveBeenCalled();
  });
});
