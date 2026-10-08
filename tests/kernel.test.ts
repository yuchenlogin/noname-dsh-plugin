/**
 * Python sidecar bridge tests.
 *
 * These run against the REAL kernel (vendor/noname-harness): the bridge is
 * only as trustworthy as the end-to-end contract, so the happy path and the
 * error mapping both exercise the true CLI.  A temp db dir keeps each test
 * hermetic.
 */

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  NonameBridgeError,
  ensureNonameInit,
  pingKernel,
  runNoname,
} from "../src/kernel.js";

let dir: string;
let opts: { dbDir: string };

beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "noname-bridge-"));
  opts = { dbDir: dir };
});

afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
});

describe("runNoname", () => {
  it("initializes a project and parses JSON stdout", async () => {
    const r = await runNoname<{ name: string; id: number }>(
      ["init", "--root", dir, "--name", "bridge test"],
      opts,
    );
    expect(r.name).toBe("bridge test");
    expect(r.id).toBe(1);
  });

  it("records and reads back an event round trip", async () => {
    await ensureNonameInit(opts);
    const evt = await runNoname<{ id: string; event_type: string }>(
      ["event", "--session", "s1", "--type", "user.message", "--payload", JSON.stringify({ text: "hello" })],
      opts,
    );
    expect(evt.id).toMatch(/^evt_/);
    expect(evt.event_type).toBe("user.message");
  });

  it("maps a nonzero exit to NonameBridgeError with stderr", async () => {
    await ensureNonameInit(opts);
    // `review` with a nonexistent proposal id is a domain error -> nonzero exit.
    const err = await runNoname(["review", "--proposal", "prp_missing", "--action", "accept", "--reviewer", "x"], opts).catch((e) => e);
    expect(err).toBeInstanceOf(NonameBridgeError);
    expect((err as NonameBridgeError).code).toBe("nonzero_exit");
    expect((err as NonameBridgeError).message).toContain("unknown proposal");
  });

  it("maps a missing python to spawn_failed", async () => {
    const err = await runNoname(["verify"], { ...opts, pythonPath: "python-that-does-not-exist-xyz" }).catch((e) => e);
    expect(err).toBeInstanceOf(NonameBridgeError);
    expect((err as NonameBridgeError).code).toBe("spawn_failed");
  });

  it("aborts a call on signal", async () => {
    // Abort BEFORE the event loop lets the fast `verify` finish: the promise
    // must reject as aborted regardless of whether the process had started.
    const controller = new AbortController();
    controller.abort();
    const err = await runNoname(["verify"], { ...opts, signal: controller.signal }).catch((e) => e);
    expect(err).toBeInstanceOf(NonameBridgeError);
    expect((err as NonameBridgeError).code).toBe("aborted");
  });
});

describe("ensureNonameInit", () => {
  it("is idempotent", async () => {
    const first = await ensureNonameInit(opts);
    const second = await ensureNonameInit(opts);
    expect(first.created).toBe(true);
    expect(second.created).toBe(false);
  });

  it("initializes a db file that another command created empty", async () => {
    // `verify` (and any other read) opens -- and therefore creates -- the db
    // file without initializing the project.  Treating "file exists" as
    // "initialized" made the next record/package/extract fail with
    // "project is not initialized"; init must run anyway.
    await runNoname(["verify"], opts);
    const res = await ensureNonameInit(opts);
    expect(res.created).toBe(false);
    const evt = await runNoname<{ id: string }>(
      ["event", "--session", "s-empty-db", "--type", "finding", "--payload", '{"text":"survives"}'],
      opts,
    );
    expect(evt.id).toMatch(/^evt_/);
  });
});

describe("pingKernel", () => {
  it("returns true when the kernel is reachable and consistent", async () => {
    expect(await pingKernel(opts)).toBe(true);
  });

  it("returns false for a broken python", async () => {
    expect(await pingKernel({ ...opts, pythonPath: "nope-python-xyz" })).toBe(false);
  });
});
