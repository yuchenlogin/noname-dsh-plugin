/**
 * Tool contract tests: each registered tool declares a valid model-facing
 * schema and its execute() bridges to the correct kernel command.  The DSH
 * `defineTool` wrapper is exercised for real (schema inference + validation),
 * with a stub ctx capturing registrations.
 */

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NONAME_TOOL_NAMES, registerNonameTools } from "../src/tools.js";
import { resolveConfig } from "../src/config.js";

interface RegisteredTool {
  name: string;
  description: string;
  execute: (args: Record<string, unknown>, exec: { signal?: AbortSignal }) => Promise<unknown>;
}

function stubCtx() {
  const tools: RegisteredTool[] = [];
  const ctx = {
    tools: {
      register: (t: RegisteredTool) => tools.push(t),
    },
  };
  return { ctx: ctx as unknown as Parameters<typeof registerNonameTools>[0], tools };
}

let dir: string;
beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "noname-tools-"));
});
afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
});

describe("tool registration", () => {
  it("registers exactly the eleven NoName tools", () => {
    const { ctx, tools } = stubCtx();
    registerNonameTools(ctx, resolveConfig({ dbDir: dir }));
    expect(tools.map((t) => t.name).sort()).toEqual([...NONAME_TOOL_NAMES].sort());
  });

  it("every tool has a non-empty description for the model", () => {
    const { ctx, tools } = stubCtx();
    registerNonameTools(ctx, resolveConfig({ dbDir: dir }));
    for (const t of tools) {
      expect(t.description.length).toBeGreaterThan(10);
    }
  });
});

describe("tool execute (real kernel)", () => {
  it("noname_record persists an event retrievable via noname_search", async () => {
    const { ctx, tools } = stubCtx();
    registerNonameTools(ctx, resolveConfig({ dbDir: dir }));
    const record = tools.find((t) => t.name === "noname_record")!;
    const search = tools.find((t) => t.name === "noname_search")!;

    const out = await record.execute(
      { session: "s1", event_type: "decision", text: "use-append-only-ledger-xyz" },
      {},
    );
    expect(String(out)).toMatch(/recorded event evt_/);

    const found = await search.execute({ query: "use-append-only-ledger-xyz" }, {});
    expect(String(found)).toContain("use-append-only-ledger-xyz");
  });

  it("noname_verify reports integrity OK on a healthy db", async () => {
    const { ctx, tools } = stubCtx();
    registerNonameTools(ctx, resolveConfig({ dbDir: dir }));
    const verify = tools.find((t) => t.name === "noname_verify")!;
    const out = await verify.execute({}, {});
    expect(String(out)).toContain("integrity OK");
  });

  it("noname_state returns the (initially empty) canon projection", async () => {
    const { ctx, tools } = stubCtx();
    registerNonameTools(ctx, resolveConfig({ dbDir: dir }));
    const state = tools.find((t) => t.name === "noname_state")!;
    const out = await state.execute({}, {});
    expect(String(out)).toBe("[]");
  });

  it("a failing kernel call surfaces as a rejected promise, not a crash", async () => {
    const { ctx, tools } = stubCtx();
    registerNonameTools(ctx, resolveConfig({ dbDir: dir, pythonPath: "missing-python-xyz" }));
    const verify = tools.find((t) => t.name === "noname_verify")!;
    await expect(verify.execute({}, {})).rejects.toThrow();
  });
});
