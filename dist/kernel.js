/**
 * Python sidecar bridge to the NoName Harness kernel.
 *
 * The kernel (vendor/noname-harness, pure Python stdlib + SQLite) is the
 * single source of truth for evidence, memory, taste and approvals.  This
 * bridge shells out to `python -m noname_harness <cmd>` and parses stdout
 * JSON -- the same contract the CLI uses, so the bridge is as stable as the
 * kernel itself.  Nothing in the DSH layer re-implements kernel logic.
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const HERE = dirname(fileURLToPath(import.meta.url));
// src/ -> plugin root -> vendor/noname-harness
const KERNEL_ROOT = resolve(HERE, "..", "vendor", "noname-harness");
export class NonameBridgeError extends Error {
    code;
    stderr;
    exitCode;
    constructor(message, code, stderr = "", exitCode = null) {
        super(message);
        this.code = code;
        this.stderr = stderr;
        this.exitCode = exitCode;
        this.name = "NonameBridgeError";
    }
}
function dbFile(opts) {
    return opts.dbPath ?? join(opts.dbDir, "harness.db");
}
/** Run `python -m noname_harness <args>` and return parsed stdout JSON. */
export async function runNoname(args, opts) {
    const python = opts.pythonPath ?? "python3";
    const timeoutMs = opts.timeoutMs ?? 30_000;
    const fullArgs = ["-m", "noname_harness", ...args, "--db", dbFile(opts)];
    await mkdir(opts.dbDir, { recursive: true });
    return new Promise((resolvePromise, rejectPromise) => {
        // A signal that is already aborted must not even spawn the process.
        if (opts.signal?.aborted) {
            rejectPromise(new NonameBridgeError("noname call aborted", "aborted"));
            return;
        }
        let child;
        try {
            child = spawn(python, fullArgs, {
                cwd: KERNEL_ROOT,
                env: { ...process.env, PYTHONPATH: KERNEL_ROOT },
                stdio: ["ignore", "pipe", "pipe"],
            });
        }
        catch (err) {
            rejectPromise(new NonameBridgeError(`failed to spawn ${python}: ${err.message}`, "spawn_failed"));
            return;
        }
        let stdout = "";
        let stderr = "";
        let settled = false;
        const timer = setTimeout(() => {
            if (!settled) {
                settled = true;
                child.kill("SIGKILL");
                rejectPromise(new NonameBridgeError(`noname call timed out after ${timeoutMs}ms`, "timeout", stderr));
            }
        }, timeoutMs);
        const onAbort = () => {
            if (!settled) {
                settled = true;
                clearTimeout(timer);
                child.kill("SIGTERM");
                rejectPromise(new NonameBridgeError("noname call aborted", "aborted", stderr));
            }
        };
        opts.signal?.addEventListener("abort", onAbort, { once: true });
        child.stdout?.on("data", (d) => (stdout += d.toString()));
        child.stderr?.on("data", (d) => (stderr += d.toString()));
        child.on("error", (err) => {
            if (settled)
                return;
            settled = true;
            clearTimeout(timer);
            opts.signal?.removeEventListener("abort", onAbort);
            rejectPromise(new NonameBridgeError(`spawn error: ${err.message}`, "spawn_failed", stderr));
        });
        child.on("close", (code) => {
            if (settled)
                return;
            settled = true;
            clearTimeout(timer);
            opts.signal?.removeEventListener("abort", onAbort);
            if (code !== 0) {
                rejectPromise(new NonameBridgeError(`noname exited ${code}: ${stderr.trim() || stdout.trim()}`.slice(0, 500), "nonzero_exit", stderr, code));
                return;
            }
            const text = stdout.trim();
            if (opts.raw) {
                resolvePromise(text);
                return;
            }
            try {
                resolvePromise(JSON.parse(text));
            }
            catch {
                rejectPromise(new NonameBridgeError(`non-JSON stdout: ${text.slice(0, 300)}`, "bad_json", stderr));
            }
        });
    });
}
/** Initialize a NoName project at the db dir if not already present (idempotent). */
export async function ensureNonameInit(opts) {
    const exists = existsSync(dbFile(opts));
    if (exists)
        return { created: false };
    await runNoname(["init", "--root", opts.root ?? opts.dbDir, "--name", opts.name ?? "DeepSeek Harness"], opts);
    return { created: true };
}
/** Verify the kernel is importable and the bridge works end to end. */
export async function pingKernel(opts) {
    try {
        await ensureNonameInit(opts);
        const result = await runNoname(["verify"], opts);
        return result.ok === true;
    }
    catch {
        return false;
    }
}
export const paths = { KERNEL_ROOT, dbFile };
