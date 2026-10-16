import type { Context } from "@deepseek-ai/cordis";
import { readFile } from "node:fs/promises";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { resolveConfig } from "../src/config.js";
import { runNoname } from "../src/kernel.js";
import { registerNonameTools } from "../src/tools.js";
import { openLedgerServer } from "../src/ui/ledger-server.js";
import { buildLedgerView } from "../src/ui/ledger-view.js";

vi.mock("../src/kernel.js", () => ({ ensureNonameInit: vi.fn(), runNoname: vi.fn() }));
vi.mock("../src/ui/ledger-server.js", () => ({ openLedgerServer: vi.fn() }));
vi.mock("node:fs/promises", () => ({ readFile: vi.fn() }));

const liveUrl = "http://127.0.0.1:43210/live-token/";
const config = resolveConfig({ dbDir: "/tmp/noname-optional-snapshot" });

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(runNoname).mockResolvedValue({});
  vi.mocked(openLedgerServer).mockResolvedValue(liveUrl);
  vi.mocked(readFile).mockResolvedValue("<html>snapshot</html>");
});

describe("optional ledger snapshot", () => {
  it("opens live review before exporting while preserving successful snapshot fields", async () => {
    const view = await buildLedgerView({} as Context, config);
    expect(view).toEqual({
      liveUrl, htmlPath: "/tmp/noname-optional-snapshot/noname-ledger.html",
      html: "<html>snapshot</html>",
    });
    expect(vi.mocked(openLedgerServer).mock.invocationCallOrder[0])
      .toBeLessThan(vi.mocked(runNoname).mock.invocationCallOrder[0]);
  });

  it("keeps live review available when the snapshot cannot be written", async () => {
    vi.mocked(runNoname).mockRejectedValueOnce(new Error("EACCES: snapshot is read-only"));
    const view = await buildLedgerView({} as Context, config);
    expect(view).toEqual({
      liveUrl, htmlPath: null, html: "", snapshotError: "EACCES: snapshot is read-only",
    });
    expect(readFile).not.toHaveBeenCalled();
  });

  it("reports a snapshot read failure in tool output while leading with the live URL", async () => {
    vi.mocked(readFile).mockRejectedValueOnce(new Error("snapshot cannot be read"));
    const registered: { name: string; execute: (args: unknown, exec: unknown) => Promise<unknown> }[] = [];
    const ctx = {
      tools: { register: (tool: (typeof registered)[number]) => registered.push(tool) },
    } as unknown as Context;
    registerNonameTools(ctx, config);
    const result = String(await registered.find((tool) => tool.name === "noname_ledger")!.execute({}, {}));
    expect(result).toMatch(new RegExp(`^live ledger: ${liveUrl}`));
    expect(result).toContain("Offline snapshot unavailable: snapshot cannot be read");
    expect(result).toContain("Live review remains available");
    expect(result).not.toContain("ledger written to null");
  });
});
