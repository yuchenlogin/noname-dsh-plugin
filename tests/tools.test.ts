/**
 * Tool contract tests: each registered tool declares a valid model-facing
 * schema and its execute() bridges to the correct kernel command.  The DSH
 * `defineTool` wrapper is exercised for real (schema inference + validation),
 * with a stub ctx capturing registrations.
 */

import { mkdtemp, readFile, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NONAME_TOOL_NAMES, registerNonameTools } from "../src/tools.js";
import { runNoname } from "../src/kernel.js";
import { resolveConfig } from "../src/config.js";
import { stopLedgerServers } from "../src/ui/ledger-server.js";

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
  await stopLedgerServers();
  await rm(dir, { recursive: true, force: true });
});

describe("tool registration", () => {
  it("registers the declared NoName tools", () => {
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

  it("describes evidence capture according to the active autoIngest configuration", () => {
    for (const autoIngest of [true, false]) {
      const { ctx, tools } = stubCtx();
      registerNonameTools(ctx, resolveConfig({ dbDir: dir, autoIngest }));
      const description = tools.find((tool) => tool.name === "noname_record")!.description;
      if (autoIngest) {
        expect(description).toContain("tool results are captured automatically");
        expect(description).not.toContain("capture is off");
      } else {
        expect(description).toContain("Automatic tool-result capture is off");
        expect(description).toContain("preserve selected useful evidence");
        expect(description).not.toContain("captured automatically");
      }
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

  it("can recall an untouched project and preview extraction without candidates", async () => {
    const { ctx, tools } = stubCtx();
    registerNonameTools(ctx, resolveConfig({ dbDir: dir }));
    const get = (name: string) => tools.find((tool) => tool.name === name)!;
    expect(String(await get("noname_package").execute({ task: "first visit", session: "fresh" }, {}))).toContain("pkg_");
    const source = String(await get("noname_record").execute({
      session: "fresh", event_type: "user.message", text: "We must use SQLite for durable persistence.",
    }, {}));
    expect(source).toContain("evidence only");
    expect(String(await get("noname_extract").execute({ session: "fresh" }, {}))).toContain("scanned_events");
    expect(await runNoname(["proposals"], { dbDir: dir })).toEqual([]);
  });

  it("remembers a concise candidate and replaces only the same key after human review", async () => {
    const { ctx, tools } = stubCtx();
    registerNonameTools(ctx, resolveConfig({ dbDir: dir }));
    const get = (name: string) => tools.find((tool) => tool.name === name)!;
    let previous: string | undefined;
    for (const decision of ["Use SQLite.", "Use PostgreSQL."]) {
      const recorded = String(await get("noname_record").execute({
        session: "correction", event_type: "decision", text: decision,
      }, {}));
      const output = String(await get("noname_remember").execute({
        layer: "high", key: "project.storage", text: decision,
        source_event: recorded.match(/evt_[a-f0-9]+/)![0], reason: "The user corrected the storage choice.",
      }, {}));
      expect(output).toContain("awaits human review");
      const before = JSON.parse(String(await get("noname_state").execute({}, {})));
      expect(before.map((record: { content: { text: string } }) => record.content.text)).toEqual(previous ? [previous] : []);
      await runNoname(["review", "--proposal-id", output.match(/prp_[a-f0-9]+/)![0], "--action", "accept", "--reviewer", "test-human"], { dbDir: dir });
      previous = decision;
    }
    const current = JSON.parse(String(await get("noname_state").execute({}, {})));
    expect(current).toHaveLength(1);
    expect(current[0].content.text).toBe("Use PostgreSQL.");
    expect(current[0].supersedes_id).toBeTruthy();
    expect((await runNoname<{ ok: boolean }>(["verify"], { dbDir: dir })).ok).toBe(true);
    await expect(get("noname_remember").execute({
      layer: "high", key: "project.invalid", text: "Not grounded", source_event: "evt_missing", reason: "test",
    }, {})).rejects.toThrow();
    expect(await runNoname(["proposals"], { dbDir: dir })).toEqual([]);
  });

  it("can update an explicitly saved taste using its new head and rejects invalid edit content", async () => {
    const { ctx, tools } = stubCtx();
    registerNonameTools(ctx, resolveConfig({ dbDir: dir }));
    const get = (name: string) => tools.find((tool) => tool.name === name)!;
    const original = JSON.parse(String(await get("noname_taste_add").execute({ attitude: "Be concise" }, {})));
    const output = String(await get("noname_taste_review").execute({
      taste_id: original.id, action: "edit", reviewer: "test-human",
      content: JSON.stringify({ attitude: "Be concise and preserve source links" }),
    }, {}));
    expect(output).toContain("Be concise and preserve source links");
    const head = output.match(/head=(tst_[a-f0-9]+)/)![1];
    expect(head).not.toBe(original.id);
    for (const content of ["not-json", "[]", "null"]) {
      await expect(get("noname_taste_review").execute({
        taste_id: head, action: "edit", reviewer: "test-human", content,
      }, {})).rejects.toThrow("JSON object");
    }
    await expect(get("noname_taste_review").execute({
      taste_id: head, action: "edit", reviewer: "test-human",
    }, {})).rejects.toThrow("requires content");
  });
});

describe("selective curation", () => {
  it("coalesces repeated proposals, retains changed pending content, and skips approved no-ops", async () => {
    const { ctx, tools } = stubCtx();
    registerNonameTools(ctx, resolveConfig({ dbDir: dir }));
    const get = (name: string) => tools.find((tool) => tool.name === name)!;
    const source = String(await get("noname_record").execute({
      session: "steady", event_type: "decision", text: "Build the CLI first, then verify persistence.",
    }, {})).match(/evt_[a-f0-9]+/)![0];
    const args = {
      layer: "mid", key: "task.cli", text: "Build the CLI first.",
      source_event: source, reason: "The user chose the first milestone.", next_step: "Verify persistence.",
    };
    const [first, repeated] = await Promise.all([
      get("noname_remember").execute(args, {}), get("noname_remember").execute(args, {}),
    ]);
    expect(String(first)).toContain("awaits human review");
    expect(String(repeated)).toContain("already_pending");
    const pending = await runNoname<{ id: string; content: unknown }[]>(["proposals"], { dbDir: dir });
    expect(pending).toHaveLength(1);
    expect(pending[0].content).toMatchObject({ text: args.text, reason: args.reason, next_step: args.next_step });
    await runNoname(["review", "--proposal-id", pending[0].id, "--action", "accept", "--reviewer", "test-human"], { dbDir: dir });
    expect(String(await get("noname_remember").execute(args, {}))).toContain("unchanged");
    expect(await runNoname(["proposals"], { dbDir: dir })).toEqual([]);
    const all = JSON.parse(String(await get("noname_state").execute({}, {})));
    expect(all[0].layer).toBe("mid");
    expect(JSON.parse(String(await get("noname_state").execute({ layer: "high" }, {})))).toEqual([]);
    for (const next_step of ["Verify concurrency.", "Verify Windows paths."]) {
      expect(String(await get("noname_remember").execute({ ...args, next_step }, {}))).toContain("awaits human review");
    }
    expect(await runNoname(["proposals"], { dbDir: dir })).toHaveLength(2);
  });
});

/**
 * Regressions from the live DeepSeek Harness acceptance run.
 *
 * Each of these failed before the fixes: `noname_verify` threw a bare bridge
 * error exactly when it had something important to say, an empty `text` was
 * accepted as evidence, the ledger size was reported in UTF-16 units while
 * labelled "bytes", and `taste_review` buried the fact that the id it returned
 * was not the id that was passed in.
 */
describe("regressions from the live acceptance run", () => {
  /** Tamper with a ledger the way somebody holding the file would. */
  async function corruptLedger(dbDir: string): Promise<void> {
    const { execFileSync } = await import("node:child_process");
    const script = `
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.executescript("DROP TRIGGER IF EXISTS session_events_append_only_update;")
conn.execute("UPDATE session_events SET payload_json = ?", ('{"text":"forged"}',))
conn.commit()
`;
    execFileSync("python3", ["-c", script, join(dbDir, "harness.db")]);
  }

  function toolsWith(dbDir: string) {
    const { ctx, tools } = stubCtx();
    registerNonameTools(ctx, resolveConfig({ dbDir }));
    return (name: string) => {
      const tool = tools.find((t) => t.name === name);
      if (!tool) throw new Error(`no such tool: ${name}`);
      return tool;
    };
  }

  it("noname_verify reports FAILED and names the ledger instead of throwing", async () => {
    const tool = toolsWith(dir);
    await tool("noname_record").execute(
      { session: "s1", event_type: "decision", text: "this row will be edited" },
      {},
    );
    await corruptLedger(dir);

    const out = String(await tool("noname_verify").execute({}, {}));
    expect(out).toContain("ledger integrity FAILED");
    // The whole point of the failure branch: say WHICH ledger was checked.
    expect(out).toContain("scope=config");
    expect(out).toContain(dir);
    expect(out).toContain("modified (hash mismatch)");
  });

  it("noname_verify reports OK with hash coverage on a healthy ledger", async () => {
    const tool = toolsWith(dir);
    await tool("noname_record").execute(
      { session: "s1", event_type: "decision", text: "healthy row" },
      {},
    );
    const out = String(await tool("noname_verify").execute({}, {}));
    expect(out).toContain("ledger integrity OK");
    expect(out).toContain("1 fully signed / 0 legacy payload-only");
  });

  it("noname_record refuses empty text and writes nothing", async () => {
    const tool = toolsWith(dir);
    await expect(
      tool("noname_record").execute({ session: "s1", event_type: "finding", text: "   " }, {}),
    ).rejects.toThrow(/text.*empty/);
    await expect(
      tool("noname_record").execute({ session: "s1", event_type: "finding", text: "" }, {}),
    ).rejects.toThrow();
    // Nothing was recorded, so a search finds nothing.
    const found = await tool("noname_search").execute({ query: "s1" }, {});
    expect(String(found)).toBe("[]");
  });

  it("noname_ledger reports the real byte count, not a UTF-16 unit count", async () => {
    const tool = toolsWith(dir);
    await tool("noname_record").execute(
      { session: "s1", event_type: "decision", text: "中文内容让字节数与字符数不同" },
      {},
    );
    const out = String(await tool("noname_ledger").execute({}, {}));
    expect(out).toMatch(/^live ledger: http:\/\/127\.0\.0\.1:\d+\/[^/]+\//);
    const path = out.match(/ledger written to (\S+) \(/)?.[1];
    const reported = Number(out.match(/\((\d+) bytes;/)?.[1]);
    expect(path).toBeTruthy();
    expect(reported).toBe((await stat(path!)).size);

    // Pin the regression: the ledger contains Chinese, so a String#length
    // reading is strictly smaller than the file.  If these ever coincide the
    // assertion above stopped proving anything.
    const html = await readFile(path!, "utf8");
    expect(reported).not.toBe(html.length);
  });

  it("noname_taste_review leads with the new head id and its superseded id", async () => {
    const tool = toolsWith(dir);
    const recorded = String(
      await tool("noname_record").execute(
        { session: "s1", event_type: "finding", text: "the moment that impressed" },
        {},
      ),
    );
    const sourceEvent = recorded.match(/evt_[0-9a-f]+/)?.[0];
    expect(sourceEvent).toBeTruthy();

    const proposed = String(
      await tool("noname_taste_propose").execute(
        {
          attitude: "prefer a minimal reproduction before a conclusion",
          source_event: sourceEvent!,
          reason: "it separated 'no memory' from 'wrong query semantics'",
        },
        {},
      ),
    );
    const candidateId = proposed.match(/tst_[0-9a-f]+/)?.[0];
    expect(candidateId).toBeTruthy();

    const adopted = String(
      await tool("noname_taste_review").execute(
        { taste_id: candidateId!, action: "adopt", reviewer: "tester", reason: "accepted" },
        {},
      ),
    );
    const headId = adopted.match(/head=(tst_[0-9a-f]+)/)?.[1];
    expect(headId).toBeTruthy();
    // Adoption is a new immutable version: the head is NOT the candidate id,
    // and the output has to say so or the next call fails with "superseded".
    expect(headId).not.toBe(candidateId);
    expect(adopted).toContain(`supersedes=${candidateId}`);
    expect(adopted).toContain(`Use head=${headId} for any further review`);

    // Every review writes a new head, not just adopt -- so a caller that keeps
    // reusing the id it passed in fails on the second call.  That is the trap
    // the headline exists to close.
    const paused = String(
      await tool("noname_taste_review").execute(
        { taste_id: headId!, action: "pause", reviewer: "tester", reason: "round trip" },
        {},
      ),
    );
    const pauseHead = paused.match(/head=(tst_[0-9a-f]+)/)?.[1];
    expect(paused).toContain("is now paused");
    expect(pauseHead).toBeTruthy();
    expect(pauseHead).not.toBe(headId);
    expect(paused).toContain(`Use head=${pauseHead} for any further review`);

    // Proof that the stale id really is dead, i.e. the headline is load-bearing.
    await expect(
      tool("noname_taste_review").execute(
        { taste_id: headId!, action: "resume", reviewer: "tester" },
        {},
      ),
    ).rejects.toThrow(/superseded/);
  });
});
