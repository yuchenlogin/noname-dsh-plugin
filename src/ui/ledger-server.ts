/** Own the bounded, loopback ledger servers launched by this plugin. */
import type { Context } from "@deepseek-ai/cordis";
import { resolve } from "node:path";
import { runNoname, type NonameRunOptions } from "../kernel.js";

const IDLE_TIMEOUT_SECONDS = 3600;

interface LedgerServer {
  url: string;
  pid: number;
  idle_timeout: number;
}

interface ServerOwner {
  entries: Map<string, Promise<LedgerServer>>;
  closed: boolean;
}

const owners = new WeakMap<Context, ServerOwner>();
const activeOwners = new Set<ServerOwner>();

function ownerFor(ctx: Context): ServerOwner {
  let owner = owners.get(ctx);
  if (owner) return owner;
  owner = { entries: new Map(), closed: false };
  owners.set(ctx, owner);
  activeOwners.add(owner);
  const current = owner;
  // Real Cordis contexts own their servers; minimal/headless hosts still get
  // the kernel's one-hour idle bound. Register only once per plugin context.
  if (typeof ctx.effect === "function") {
    ctx.effect(() => () => stopOwner(current));
  }
  return owner;
}

function validateServer(value: LedgerServer): LedgerServer {
  const url = new URL(value.url);
  if (
    url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port ||
    url.username || url.password || url.search || url.hash ||
    !/^\/[A-Za-z0-9_-]+\/$/.test(url.pathname) ||
    !Number.isSafeInteger(value.pid) || value.pid <= 0 ||
    value.idle_timeout !== IDLE_TIMEOUT_SECONDS
  ) {
    throw new Error("ledger-serve returned an invalid loopback server handle");
  }
  return value;
}

async function reachable(server: LedgerServer): Promise<boolean> {
  try {
    const response = await fetch(`${server.url}api/health`, {
      signal: AbortSignal.timeout(1500),
      redirect: "error",
    });
    const health = await response.json() as { ok?: boolean };
    return response.ok && health.ok === true;
  } catch {
    return false;
  }
}

async function stopServer(server: LedgerServer): Promise<void> {
  // Use the authenticated shutdown API, never a recorded pid that may have
  // expired and been reused by an unrelated process.
  const url = new URL(server.url);
  try {
    const response = await fetch(`${server.url}api/stop`, {
      method: "POST",
      headers: {
        Origin: url.origin,
        "X-NoName-Token": url.pathname.split("/")[1],
      },
      signal: AbortSignal.timeout(1500),
      redirect: "error",
    });
    await response.arrayBuffer();
  } catch {
    // An expired server needs no cleanup; an unreachable one still has its
    // kernel-enforced idle timeout if the host is shutting down.
  }
}

async function stopOwner(owner: ServerOwner): Promise<void> {
  owner.closed = true;
  const entries = [...owner.entries.values()];
  owner.entries.clear();
  activeOwners.delete(owner);
  // A launch still in flight notices `closed` and terminates its result.
  await Promise.allSettled(entries.map(async (entry) => stopServer(await entry)));
}

/** Plugin disposal and test teardown; no server is left unbounded. */
export async function stopLedgerServers(ctx?: Context): Promise<void> {
  if (ctx) {
    const owner = owners.get(ctx);
    if (owner) await stopOwner(owner);
  } else {
    await Promise.all([...activeOwners].map(stopOwner));
  }
}

/** Reuse a live server for this context's ledger, coalescing parallel opens. */
export async function openLedgerServer(ctx: Context, opts: NonameRunOptions): Promise<string> {
  if (opts.signal?.aborted) throw new Error("ledger server call aborted");
  const owner = ownerFor(ctx);
  if (owner.closed) throw new Error("ledger server context has been disposed");
  const key = resolve(opts.dbPath ?? resolve(opts.dbDir, "harness.db"));
  const existing = owner.entries.get(key);
  if (existing) {
    const server = await existing;
    if (opts.signal?.aborted) throw new Error("ledger server call aborted");
    const healthy = await reachable(server);
    if (opts.signal?.aborted) throw new Error("ledger server call aborted");
    if (healthy) {
      if (owner.closed) throw new Error("ledger server context has been disposed");
      return server.url;
    }
    // Health checks do not render the ledger. A failed health response can
    // still leave shutdown reachable; retire that service before replacing it.
    await stopServer(server);
    // Delete only the stale entry: another call may already be replacing it.
    if (owner.entries.get(key) === existing) owner.entries.delete(key);
  }
  if (owner.closed) throw new Error("ledger server context has been disposed");
  if (opts.signal?.aborted) throw new Error("ledger server call aborted");
  let launching = owner.entries.get(key);
  if (!launching) {
    launching = (async () => {
      // Killing the CLI wrapper between detach and readiness loses the
      // daemon's handle. Protect this bounded handoff (kernel startup <=15s),
      // then honor cancellation only after we can stop the actual service.
      const server = validateServer(await runNoname<LedgerServer>(
        ["ledger-serve", "--background", "--idle-timeout", String(IDLE_TIMEOUT_SECONDS)],
        { ...opts, signal: undefined, timeoutMs: Math.max(opts.timeoutMs ?? 30_000, 20_000) },
      ));
      if (owner.closed || opts.signal?.aborted) {
        await stopServer(server);
        throw new Error(owner.closed
          ? "ledger server context was disposed during launch"
          : "ledger server call aborted during launch");
      }
      return server;
    })();
    owner.entries.set(key, launching);
  }
  try {
    return (await launching).url;
  } catch (error) {
    if (owner.entries.get(key) === launching) owner.entries.delete(key);
    throw error;
  }
}
