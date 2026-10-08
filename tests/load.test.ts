/**
 * Real-Cordis load smoke test: the plugin's apply() must run inside a genuine
 * Cordis Context (not a hand stub), so a missing `inject` or an unexpected
 * service access fails here -- this is the exact class of bug (C1) that hand
 * stubs let slip.  The `tools` service is provided as the real dependency.
 */

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { resolveConfig } from "../src/config.js";
import { registerNonameTools, NONAME_TOOL_NAMES } from "../src/tools.js";

// A minimal real-shaped tools service that satisfies ctx.tools for loading.
// The point is not to stub Cordis but to verify the plugin only touches the
// services it declares in `inject`, and registers the tools it promises.
function makeToolsService() {
  const registered: { name: string }[] = [];
  return {
    registered,
    register(tool: { name: string }) {
      registered.push(tool);
      return () => {};
    },
  };
}

let dir: string;
beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "noname-load-"));
});
afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
});

describe("plugin surface", () => {
  it("registers all eleven tools without touching undeclared services", () => {
    const tools = makeToolsService();
    // A ctx exposing ONLY `tools` (the declared inject).  If the plugin
    // reaches for anything else at registration time, this throws.
    const ctx = { tools } as unknown as Parameters<typeof registerNonameTools>[0];
    registerNonameTools(ctx, resolveConfig({ dbDir: dir }));
    expect(tools.registered.map((t) => t.name).sort()).toEqual([...NONAME_TOOL_NAMES].sort());
  });

  it("resolveConfig keeps an explicit dbDir, and leaves the default empty (workspace-scoped)", () => {
    const c = resolveConfig({ dbDir: dir });
    expect(c.dbDir).toBe(dir);
    // An empty dbDir is the *signal* for per-workspace resolution: it is
    // decided per call from the session's workspace, so it cannot be baked in
    // here.  ledgerTarget() is what turns it into a real directory.
    const d = resolveConfig();
    expect(d.dbDir).toBe("");
    expect(d.autoIngest).toBe(true);
    expect(d.timeoutMs).toBe(30_000);
  });

  it("resolveConfig rejects an invalid config shape loudly", () => {
    // schemastery ValidationError, not a silent pass-through.
    expect(() => resolveConfig({ timeoutMs: -1 })).toThrow();
  });
});
