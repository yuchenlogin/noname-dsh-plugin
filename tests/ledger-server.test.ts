import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Context } from "@deepseek-ai/cordis";
import { runNoname } from "../src/kernel.js";
import { openLedgerServer, stopLedgerServers } from "../src/ui/ledger-server.js";

vi.mock("../src/kernel.js", () => ({ runNoname: vi.fn() }));

const handle = { url: "http://127.0.0.1:43210/secret-token/", pid: 1234, idle_timeout: 3600 };
const opts = { dbDir: "/tmp/noname-ledger-unit" };
const run = vi.mocked(runNoname);
let request: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  run.mockResolvedValue(handle);
  request = vi.fn(async () => new Response(JSON.stringify({ ok: true })));
  vi.stubGlobal("fetch", request);
});
afterEach(async () => {
  await stopLedgerServers();
  vi.unstubAllGlobals();
});

describe("ledger server lifecycle", () => {
  it("coalesces parallel opens and reuses one context's same ledger", async () => {
    const ctx = {} as Context;
    const urls = await Promise.all([openLedgerServer(ctx, opts), openLedgerServer(ctx, opts)]);
    expect(urls).toEqual([handle.url, handle.url]);
    expect(run).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledWith(
      ["ledger-serve", "--background", "--idle-timeout", "3600"],
      { ...opts, signal: undefined, timeoutMs: 30_000 },
    );
    expect(request.mock.calls.map((call) => call[0])).toEqual([`${handle.url}api/health`]);
  });

  it("checks lightweight health without rendering or stopping a healthy ledger", async () => {
    request.mockImplementation(async (url: string) => {
      if (url === handle.url) throw new Error("heavy ledger render unavailable");
      if (url.endsWith("api/health")) return new Response(JSON.stringify({ ok: true }));
      throw new Error("healthy ledger should not be stopped");
    });
    const ctx = {} as Context;
    await openLedgerServer(ctx, opts);
    await expect(openLedgerServer(ctx, opts)).resolves.toBe(handle.url);
    expect(run).toHaveBeenCalledTimes(1);
    expect(request.mock.calls.map((call) => call[0])).toEqual([`${handle.url}api/health`]);
  });

  it("keeps different ledgers separate and replaces an expired server", async () => {
    const ctx = {} as Context;
    await openLedgerServer(ctx, opts);
    await openLedgerServer(ctx, { dbDir: "/tmp/noname-ledger-other" });
    expect(run).toHaveBeenCalledTimes(2);
    request.mockResolvedValueOnce(new Response("expired", { status: 404 }));
    await openLedgerServer(ctx, opts);
    expect(run).toHaveBeenCalledTimes(3);
  });

  it("allows retry after a failed launch", async () => {
    const ctx = {} as Context;
    run.mockRejectedValueOnce(new Error("startup failed"));
    await expect(openLedgerServer(ctx, opts)).rejects.toThrow("startup failed");
    await expect(openLedgerServer(ctx, opts)).resolves.toBe(handle.url);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("rejects non-loopback URLs and unbounded handles", async () => {
    run.mockResolvedValueOnce({ ...handle, url: "http://example.org/token/" });
    await expect(openLedgerServer({} as Context, opts)).rejects.toThrow("invalid loopback");
    run.mockResolvedValueOnce({ ...handle, idle_timeout: 0 });
    await expect(openLedgerServer({} as Context, opts)).rejects.toThrow("invalid loopback");
    expect(request).not.toHaveBeenCalled();
  });

  it("owns one Cordis disposal hook and uses authenticated shutdown", async () => {
    const disposals: (() => Promise<void>)[] = [];
    const effect = vi.fn((start: () => () => Promise<void>) => disposals.push(start()));
    const ctx = { effect } as unknown as Context;
    await openLedgerServer(ctx, opts);
    await openLedgerServer(ctx, opts);
    expect(effect).toHaveBeenCalledTimes(1);
    await disposals[0]();
    expect(request).toHaveBeenCalledWith(`${handle.url}api/stop`, expect.objectContaining({
      method: "POST",
      headers: { Origin: "http://127.0.0.1:43210", "X-NoName-Token": "secret-token" },
    }));
    await expect(openLedgerServer(ctx, opts)).rejects.toThrow("disposed");
  });

  it("cleans up a server whose launch completes after plugin disposal", async () => {
    let ready!: (value: typeof handle) => void;
    run.mockImplementationOnce(() => new Promise((resolve) => { ready = resolve; }));
    const ctx = {} as Context;
    const opened = openLedgerServer(ctx, opts);
    // Attach the rejection handler before resolving the in-flight launch.
    const rejected = expect(opened).rejects.toThrow("disposed during launch");
    const stopped = stopLedgerServers(ctx);
    ready(handle);
    await Promise.all([rejected, stopped]);
    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0][0]).toBe(`${handle.url}api/stop`);
  });

  it("retains the readiness handoff after cancellation and stops the acquired server once", async () => {
    let ready!: (value: typeof handle) => void;
    run.mockImplementationOnce(() => new Promise((resolve) => { ready = resolve; }));
    const controller = new AbortController();
    const ctx = {} as Context;
    const opened = openLedgerServer(ctx, { ...opts, signal: controller.signal, timeoutMs: 100 });
    const rejected = expect(opened).rejects.toThrow("aborted during launch");
    controller.abort();
    expect(run).toHaveBeenCalledWith(
      ["ledger-serve", "--background", "--idle-timeout", "3600"],
      { ...opts, signal: undefined, timeoutMs: 20_000 },
    );
    ready(handle);
    await rejected;
    // Later disposal does not issue a second stop for this cancelled launch.
    await stopLedgerServers(ctx);
    expect(request.mock.calls.map((call) => call[0])).toEqual([`${handle.url}api/stop`]);
  });

  it("rejects an already-aborted open without starting or stopping a shared server", async () => {
    const ctx = {} as Context;
    await openLedgerServer(ctx, opts);
    const controller = new AbortController();
    controller.abort();
    await expect(openLedgerServer(ctx, { ...opts, signal: controller.signal })).rejects.toThrow("aborted");
    expect(run).toHaveBeenCalledTimes(1);
    expect(request).not.toHaveBeenCalled();
    await expect(openLedgerServer(ctx, opts)).resolves.toBe(handle.url);
  });

  it("cancels a cached caller while preserving the shared healthy server", async () => {
    const ctx = {} as Context;
    await openLedgerServer(ctx, opts);
    let respond!: (response: Response) => void;
    request.mockImplementationOnce(() => new Promise((resolve) => { respond = resolve; }));
    const controller = new AbortController();
    const opened = openLedgerServer(ctx, { ...opts, signal: controller.signal });
    const rejected = expect(opened).rejects.toThrow("aborted");
    // Allow the cached handle await to reach its health request.
    await Promise.resolve();
    controller.abort();
    respond(new Response(JSON.stringify({ ok: true })));
    await rejected;
    expect(request.mock.calls.map((call) => call[0])).toEqual([`${handle.url}api/health`]);
    await expect(openLedgerServer(ctx, opts)).resolves.toBe(handle.url);
    expect(run).toHaveBeenCalledTimes(1);
  });
});
