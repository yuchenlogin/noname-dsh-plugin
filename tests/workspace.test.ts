/**
 * Ledger scope tests: one ledger per DSH workspace (project), shared by every
 * session in it -- the user's mental model, not one db per session and not one
 * global bucket for every project.
 *
 * These run against the REAL kernel: the scope rule decides where evidence
 * lands, so a stub that only checks paths would miss the part that matters.
 */

import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { resolveConfig } from "../src/config.js";
import { registerNonameTools } from "../src/tools.js";
import { drainIngests, registerIngestion } from "../src/ingest.js";
import { runNoname } from "../src/kernel.js";
import { buildLedgerView } from "../src/ui/ledger-view.js";
import { describeLedgerTarget, ledgerTarget, resolveWorkspace, sessionIdOf } from "../src/workspace.js";

let root: string;
beforeEach(async () => {
  root = await mkdtemp(join(tmpdir(), "noname-scope-"));
});
afterEach(async () => {
  await rm(root, { recursive: true, force: true });
});

/** A ctx whose only services are `tools` and (optionally) the workspace registry. */
function makeCtx(workspaces?: { path: string; title?: string; sessionIds: string[] }[]) {
  const registered: { name: string; execute: (a: unknown, e: unknown) => Promise<unknown> }[] = [];
  const ctx = {
    tools: { register: (t: (typeof registered)[number]) => registered.push(t) },
    get: (name: string) =>
      name === "workspaceRegistry" && workspaces
        ? { list: () => workspaces.map((w) => ({ ...w, id: w.path, sessionIds: w.sessionIds })) }
        : undefined,
  };
  return { ctx: ctx as unknown as Parameters<typeof registerNonameTools>[0], registered };
}

const execFor = (sessionId: string) => ({ agent: { sessionId }, name: "test-tool" });

describe("sessionIdOf", () => {
  it("reads every host shape, and reports absence honestly", () => {
    expect(sessionIdOf({ sessionId: "s1" })).toBe("s1");
    expect(sessionIdOf({ session: { id: "s2" } })).toBe("s2");
    // DSH 0.2.0-rc.2 carries the id on the agent, not on the execution.
    expect(sessionIdOf({ agent: { session: { id: "s3" } } })).toBe("s3");
    expect(sessionIdOf({ agent: { sessionId: "s4" } })).toBe("s4");
    expect(sessionIdOf({ name: "bash" })).toBeUndefined();
    expect(sessionIdOf(undefined)).toBeUndefined();
  });
});

describe("resolveWorkspace", () => {
  it("maps a session to its workspace, and refuses to guess between several", () => {
    const a = join(root, "alpha");
    const b = join(root, "beta");
    const workspaces = [
      { path: a, title: "alpha", sessionIds: ["sA"] },
      { path: b, title: "beta", sessionIds: ["sB"] },
    ];
    const ctx = makeCtx(workspaces).ctx;
    expect(resolveWorkspace(ctx, "sB").workspace?.path).toBe(b);
    // No session + several projects: no workspace rather than writing one
    // project's evidence into another's ledger.
    expect(resolveWorkspace(ctx).workspace).toBeUndefined();
    expect(resolveWorkspace(ctx)).toMatchObject({ registry: true, workspaces: 2 });
    // A single project is unambiguous even without a session.
    expect(resolveWorkspace(makeCtx([workspaces[0]]).ctx).workspace?.path).toBe(a);
  });

  it("reports an unreachable registry instead of pretending it looked", () => {
    expect(resolveWorkspace(makeCtx().ctx, "sA")).toMatchObject({ registry: false, workspaces: 0 });
    expect(resolveWorkspace(undefined, "sA").registry).toBe(false);
  });
});

describe("describeLedgerTarget", () => {
  it("names the scope, the session and the reason for a fallback", () => {
    const a = join(root, "alpha");
    const scoped = ledgerTarget(
      makeCtx([{ path: a, title: "alpha", sessionIds: ["sA"] }]).ctx,
      resolveConfig({}),
      "sA",
    );
    expect(describeLedgerTarget(scoped)).toContain("scope=workspace title=alpha");
    expect(describeLedgerTarget(scoped)).toContain("session=sA");

    // A reachable registry with two projects, but no session on the execution.
    const noSession = ledgerTarget(
      makeCtx([
        { path: join(root, "x"), sessionIds: [] },
        { path: join(root, "y"), sessionIds: [] },
      ]).ctx,
      resolveConfig({}),
    );
    expect(describeLedgerTarget(noSession)).toContain("no session id on the execution");
    expect(describeLedgerTarget(ledgerTarget(makeCtx().ctx, resolveConfig({}), "sA"))).toContain(
      "workspace registry unreachable",
    );
    const ungrouped = ledgerTarget(
      makeCtx([
        { path: join(root, "x"), sessionIds: ["other"] },
        { path: join(root, "y"), sessionIds: ["another"] },
      ]).ctx,
      resolveConfig({}),
      "sA",
    );
    expect(describeLedgerTarget(ungrouped)).toContain("session groups under no workspace");
  });
});

describe("ledgerTarget", () => {
  it("scopes to <workspace>/.noname and points the kernel boundary at the project", () => {
    const a = join(root, "alpha");
    const target = ledgerTarget(makeCtx([{ path: a, sessionIds: ["sA"] }]).ctx, resolveConfig({}), "sA");
    expect(target.scope).toBe("workspace");
    expect(target.dbDir).toBe(join(a, ".noname"));
    // The kernel's workspace boundary must be the real project, or snapshots
    // read the wrong git state and packages print a meaningless boundary.
    expect(target.root).toBe(a);
  });

  it("keeps an explicit dbDir override", () => {
    const target = ledgerTarget(undefined, resolveConfig({ dbDir: join(root, "custom") }), "sA");
    expect(target).toMatchObject({ scope: "config", dbDir: join(root, "custom") });
  });

  it("falls back to the legacy per-user directory with no workspace", () => {
    const target = ledgerTarget(makeCtx().ctx, resolveConfig({}), "sA");
    expect(target.scope).toBe("global");
    expect(target.dbDir).toContain("noname");
  });
});

describe("one ledger per workspace, shared by its sessions", () => {
  it("writes each session's evidence into its own project ledger", async () => {
    const a = join(root, "proj-a");
    const b = join(root, "proj-b");
    const { mkdir } = await import("node:fs/promises");
    await mkdir(a, { recursive: true });
    await mkdir(b, { recursive: true });

    const { ctx, registered } = makeCtx([
      { path: a, title: "proj-a", sessionIds: ["session-a1", "session-a2"] },
      { path: b, title: "proj-b", sessionIds: ["session-b1"] },
    ]);
    registerNonameTools(ctx, resolveConfig({}));
    const record = registered.find((t) => t.name === "noname_record")!;

    // Two sessions of project A, one of project B: A's sessions must SHARE a
    // ledger, and B must be untouched by A's evidence.
    await record.execute({ session: "session-a1", event_type: "finding", text: "a1 fact" }, execFor("session-a1"));
    await record.execute({ session: "session-a2", event_type: "finding", text: "a2 fact" }, execFor("session-a2"));
    await record.execute({ session: "session-b1", event_type: "finding", text: "b1 fact" }, execFor("session-b1"));

    const dbA = join(a, ".noname", "harness.db");
    const dbB = join(b, ".noname", "harness.db");
    expect(existsSync(dbA)).toBe(true);
    expect(existsSync(dbB)).toBe(true);

    const eventsA = await runNoname<{ session_id: string; payload: { text?: string } }[]>(["ledger"], {
      dbDir: join(a, ".noname"),
    });
    const eventsB = await runNoname<{ session_id: string }[]>(["ledger"], { dbDir: join(b, ".noname") });
    expect(new Set(eventsA.map((e) => e.session_id))).toEqual(new Set(["session-a1", "session-a2"]));
    expect(eventsB.map((e) => e.session_id)).toEqual(["session-b1"]);
    expect(JSON.stringify(eventsB)).not.toContain("a1 fact");

    // The ledger HTML lands inside the project boundary the kernel enforces.
    const view = await buildLedgerView(ctx, resolveConfig({}), execFor("session-a1"));
    expect(view.htmlPath).toBe(join(a, ".noname", "noname-ledger.html"));
    expect(view.html.length).toBeGreaterThan(1000);
  }, 60_000);

  it("ingests a tool result into the project ledger of the session that ran it", async () => {
    const a = join(root, "ingest-proj");
    await (await import("node:fs/promises")).mkdir(a, { recursive: true });
    const handlers: Record<string, (exec: unknown, result: unknown) => void> = {};
    const ctx = {
      on: (event: string, h: (exec: unknown, result: unknown) => void) => {
        handlers[event] = h;
      },
      get: () => ({ list: () => [{ path: a, id: a, title: "ingest-proj", sessionIds: ["sess-in"] }] }),
    } as unknown as Parameters<typeof registerIngestion>[0];
    registerIngestion(ctx, resolveConfig({}));
    handlers["tools/result"](
      { name: "bash", callId: "c1", agent: { sessionId: "sess-in" } },
      { content: [{ type: "text", text: "ingested into the project" }] },
    );
    await drainIngests();

    const events = await runNoname<{ session_id: string; payload: { text?: string } }[]>(["ledger"], {
      dbDir: join(a, ".noname"),
    });
    expect(events.map((e) => e.session_id)).toEqual(["sess-in"]);
    expect(JSON.stringify(events)).toContain("ingested into the project");
  }, 30_000);
});
