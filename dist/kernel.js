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
// Kernel resolution: prefer the vendored snapshot (self-contained, present in
// git-installed / npm-packed distributions), fall back to the git submodule
// (development checkout).  dist/ is one level deeper than src/, so resolve
// against both the dist and src layouts.
function kernelRootCandidates() {
    return [
        resolve(HERE, "..", "vendor", "noname_harness_pkg"), // dist/ -> vendored snapshot
        resolve(HERE, "..", "vendor", "noname-harness"), // dist/ -> submodule (dev)
        resolve(HERE, "vendor", "noname_harness_pkg"), // src/ layouts
        resolve(HERE, "vendor", "noname-harness"),
    ];
}
/**
 * Resolve the kernel root NOW, not at module load.
 *
 * A load-time constant is a trap: when this module is imported from a path
 * that later moves (a staging directory during install, a re-installed
 * profile), the frozen value points at a directory that no longer exists --
 * and `spawn(..., { cwd })` then fails with a bare `ENOENT` that names the
 * python binary and hides the real cause.  Resolving per call costs four
 * stat()s and turns that failure class into an explicit, named error.
 *
 * @returns the kernel root, or null when no candidate holds the package.
 */
export function resolveKernelRoot() {
    for (const c of kernelRootCandidates()) {
        // A valid root contains the importable noname_harness package.
        if (existsSync(join(c, "noname_harness", "__init__.py"))) {
            return c;
        }
    }
    return null;
}
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
    const kernelRoot = resolveKernelRoot();
    if (!kernelRoot) {
        throw new NonameBridgeError(`NoName kernel package not found next to ${HERE}: expected ` +
            `<plugin>/vendor/noname_harness_pkg/noname_harness/__init__.py ` +
            `(checked ${kernelRootCandidates().join(", ")})`, "kernel_missing");
    }
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
                cwd: kernelRoot,
                env: { ...process.env, PYTHONPATH: kernelRoot },
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
            rejectPromise(new NonameBridgeError(
            // Name the facts a spawn failure actually depends on: a bare
            // `spawn <python> ENOENT` cannot tell a missing interpreter from a
            // missing cwd, and the difference decided a whole debugging session.
            `spawn error: ${err.message} [python=${python} exists=${existsSync(python)} ` +
                `cwd=${kernelRoot} cwdExists=${existsSync(kernelRoot)} pid=${process.pid}]`, "spawn_failed", stderr));
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
/**
 * Initialize a NoName project at the db dir (idempotent, non-destructive).
 *
 * Always runs `init`: the kernel's init is idempotent and never overwrites an
 * existing project row (verified against the kernel).  Short-circuiting on
 * "the db file exists" was wrong, because every kernel command opens -- and
 * therefore creates -- the db file: a `verify`/`state`/`search` against a
 * fresh profile left an empty db behind, after which this function believed
 * the project was initialized and the next `record`/`package`/`extract`/
 * `ledger` call failed with "project is not initialized".
 */
export async function ensureNonameInit(opts) {
    const created = !existsSync(dbFile(opts));
    await runNoname(["init", "--root", opts.root ?? opts.dbDir, "--name", opts.name ?? "DeepSeek Harness"], opts);
    return { created };
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
export const paths = {
    /** Resolved fresh on every read -- see resolveKernelRoot(). */
    get KERNEL_ROOT() {
        return resolveKernelRoot() ?? kernelRootCandidates()[0];
    },
    dbFile,
};
