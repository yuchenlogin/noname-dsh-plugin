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

  it("dedups the same callId but not the same content in different sessions", async () => {
    const { ctx, handlers } = stubCtx();
    registerIngestion(ctx, resolveConfig({ dbDir: dir }));
    const payload = { content: [{ type: "text", text: "identical-output" }] };
    handlers["tools/result"]({ name: "bash", callId: "dup", sessionId: "s1" }, payload);
    handlers["tools/result"]({ name: "bash", callId: "dup", sessionId: "s1" }, payload); // same callId -> dedup
    handlers["tools/result"]({ name: "bash", callId: "other", sessionId: "s2" }, payload); // diff session -> kept
    await drainIngests();
    const events = await runNoname<{ session_id: string }[]>(["ledger"], { dbDir: dir });
    const s1 = events.filter((e) => e.session_id === "s1").length;
    const s2 = events.filter((e) => e.session_id === "s2").length;
    expect(s1).toBe(1);
    expect(s2).toBe(1);
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

  it("respects autoIngest=false (no listener registered)", () => {
    const { ctx, handlers } = stubCtx();
    registerIngestion(ctx, resolveConfig({ dbDir: dir, autoIngest: false }));
    expect(handlers["tools/result"]).toBeUndefined();
  });
});
