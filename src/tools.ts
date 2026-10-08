/**
 * DSH model-visible tools backed by the NoName kernel.
 *
 * Each tool is a thin adapter: defineTool declares the model-facing schema,
 * execute() bridges to `python -m noname_harness <cmd>`, and output.render
 * converts the kernel's JSON into model-readable text.  No kernel logic is
 * re-implemented here -- approval gates, sandbox boundaries and the ledger
 * all stay inside NoName.
 */

import type { Context } from "@deepseek-ai/cordis";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { ensureNonameInit, runNoname, type NonameRunOptions } from "./kernel.js";
import type { NonameConfig } from "./config.js";

function text(value: unknown): { type: "text"; text: string }[] {
  return [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }];
}

function optsFor(config: NonameConfig, signal?: AbortSignal): NonameRunOptions {
  return {
    dbDir: config.dbDir ?? "",
    pythonPath: config.pythonPath,
    signal,
  };
}

export function registerNonameTools(ctx: Context, config: NonameConfig): void {
  const base = () => optsFor(config);

  ctx.tools.register(
    defineTool({
      name: "noname_record",
      description:
        "Record an evidence event into the NoName ledger (append-only). Use for decisions, findings, constraints, or any work context that should survive across sessions.",
      parameters: {
        session: { type: "string", required: true, description: "Session id this event belongs to" },
        event_type: { type: "string", required: true, description: "Event type, e.g. user.message / decision / finding" },
        text: { type: "string", required: true, description: "The content to record" },
      },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        const opts = optsFor(config, exec.signal);
        await ensureNonameInit(opts);
        const r = await runNoname<{ id: string }>(
          ["event", "--session", args.session, "--type", args.event_type, "--payload", JSON.stringify({ text: args.text })],
          opts,
        );
        return `recorded event ${r.id}`;
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_package",
      description:
        "Assemble a NoName context package: the portable, model-agnostic view (canon + task state + evidence pointers + taste) for handing work to a new session or model. Use before context switches.",
      parameters: {
        task: { type: "string", required: true, description: "The task to continue" },
        session: { type: "string", required: true, description: "Session id to assemble from" },
      },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        // `package` emits markdown, not JSON: raw mode.
        const r = await runNoname<string>(
          ["package", "--task", args.task, "--session", args.session],
          { ...optsFor(config, exec.signal), raw: true },
        );
        return r;
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_search",
      description: "Search the NoName evidence stream and memory (full-text). Use to recall past decisions, findings, or events.",
      parameters: {
        query: { type: "string", required: true, description: "Search query" },
      },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        const r = await runNoname(["search", args.query], optsFor(config, exec.signal));
        return JSON.stringify(r);
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_state",
      description: "Show the current approved canon (stable high-level memory) of the project. This is the constitution the model itself proposed and the human approved.",
      parameters: {},
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(_args, exec) {
        const r = await runNoname(["state", "--layer", "high"], optsFor(config, exec.signal));
        return JSON.stringify(r);
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_inbox",
      description: "Show the NoName review inbox: pending proposals and taste-card candidates waiting for human judgement. Review is a signature, not a button.",
      parameters: {},
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(_args, exec) {
        const r = await runNoname(["inbox"], optsFor(config, exec.signal));
        return JSON.stringify(r);
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_taste_add",
      description:
        "Record an authored taste: the user's own stated attitude (highest authority). Taste shapes ordering/expression but never rewrites facts or lowers verification standards.",
      parameters: {
        attitude: { type: "string", required: true, description: "The attitude, as a concrete statement" },
        example: { type: "string", description: "A concrete example of the attitude" },
        avoid: { type: "string", description: "What to avoid" },
        scope: { type: "string", description: "user or project (default project)" },
        reason: { type: "string", description: "Why this taste is worth recording" },
      },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        const content: Record<string, string> = { attitude: args.attitude };
        if (args.example) content.example = args.example;
        if (args.avoid) content.avoid = args.avoid;
        const cmd = ["taste-add", "--scope", args.scope ?? "project", "--content", JSON.stringify(content)];
        if (args.reason) cmd.push("--reason", args.reason);
        const r = await runNoname(cmd, optsFor(config, exec.signal));
        return JSON.stringify(r);
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_verify",
      description: "Verify the integrity of the NoName ledger (append-only evidence has not been corrupted).",
      parameters: {},
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(_args, exec) {
        const r = await runNoname<{ ok: boolean }>(["verify"], optsFor(config, exec.signal));
        return r.ok ? "ledger integrity OK" : "ledger integrity FAILED";
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_extract",
      description:
        "Run memory extraction over a session's events: surface durable memory candidates (never auto-confirmed -- a human reviews before anything becomes canon).",
      parameters: {
        session: { type: "string", required: true, description: "Session id to extract from" },
      },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        const r = await runNoname(["extract", "--session", args.session], optsFor(config, exec.signal));
        return JSON.stringify(r);
      },
    }),
  );
}

export const NONAME_TOOL_NAMES = [
  "noname_record",
  "noname_package",
  "noname_search",
  "noname_state",
  "noname_inbox",
  "noname_taste_add",
  "noname_verify",
  "noname_extract",
] as const;
