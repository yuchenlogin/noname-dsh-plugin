/**
 * DSH model-visible tools backed by the NoName kernel.
 *
 * Each tool is a thin adapter: defineTool declares the model-facing schema,
 * execute() bridges to `python -m noname_harness <cmd>`, and output.render
 * converts the kernel's JSON into model-readable text.  No kernel logic is
 * re-implemented here -- approval gates, sandbox boundaries and the ledger
 * all stay inside NoName.
 *
 * The tool set deliberately spans NoName's two taste tracks (authored AND
 * adopted) and its review lifecycle, because taste is a first-class citizen
 * in NoName, not a single record primitive.
 */

import type { Context } from "@deepseek-ai/cordis";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { ensureNonameInit, runNoname, type NonameRunOptions } from "./kernel.js";
import type { NonameConfig } from "./config.js";
import { describeLedgerTarget, ledgerTarget, sessionIdOf } from "./workspace.js";

/** Uniform model-facing rendering: strings pass through, values pretty-print. */
function text(value: unknown): { type: "text"; text: string }[] {
  return [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }];
}

/**
 * Bridge options for one call: the ledger is resolved per call from the
 * calling session's workspace, so two sessions in two projects never share a
 * db even though they run in the same host process.
 */
function optsFor(ctx: Context, config: NonameConfig, exec: unknown): NonameRunOptions {
  const target = ledgerTarget(ctx, config, sessionIdOf(exec));
  const signal = (exec as { signal?: AbortSignal } | undefined)?.signal;
  return {
    dbDir: target.dbDir,
    root: target.root,
    pythonPath: config.pythonPath,
    timeoutMs: config.timeoutMs,
    signal,
  };
}

export function registerNonameTools(ctx: Context, config: NonameConfig): void {
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
        // Fail in the adapter, before spawning a sidecar: an empty record is
        // not evidence, and the error should name the caller's mistake rather
        // than a kernel exit code.  (The kernel enforces the same rule -- this
        // is the cheap, clear half of the same guarantee.)
        if (!args.text.trim()) {
          throw new Error(
            "noname_record: `text` is empty; an event with no content cannot be recalled or cited",
          );
        }
        const opts = optsFor(ctx, config, exec);
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
        return await runNoname<string>(
          ["package", "--task", args.task, "--session", args.session],
          { ...optsFor(ctx, config, exec), raw: true },
        );
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_search",
      description:
        "Search the NoName evidence stream and memory (full-text). Use to recall past decisions, findings, or events. Chinese/Japanese/Korean queries match substrings (several terms are ANDed); other queries use token matching, so a Latin substring may need to be a whole word.",
      parameters: { query: { type: "string", required: true, description: "Search query" } },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        return JSON.stringify(await runNoname(["search", args.query], optsFor(ctx, config, exec)));
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_state",
      description: "Show the current approved canon (stable high-level memory) of the project: the constitution the model proposed and the human approved.",
      parameters: {},
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(_args, exec) {
        return JSON.stringify(await runNoname(["state", "--layer", "high"], optsFor(ctx, config, exec)));
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
        return JSON.stringify(await runNoname(["inbox"], optsFor(ctx, config, exec)));
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_taste_add",
      description:
        "Record an AUTHORED taste: the user's own stated attitude (highest authority, active immediately). Taste shapes ordering/expression but never rewrites facts or lowers verification standards.",
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
        return JSON.stringify(await runNoname(cmd, optsFor(ctx, config, exec)));
      },
    }),
  );

  // --- Adopted taste track: the MODEL proposes from a striking moment; the
  // human reviews.  This is the second rail of NoName's dual-track taste. ---

  ctx.tools.register(
    defineTool({
      name: "noname_taste_propose",
      description:
        "Propose an ADOPTED taste candidate from a model moment that impressed the user (an answer, a piece of work). It becomes a candidate only -- a human reviews before it is adopted. Never passes off as the user's own words.",
      parameters: {
        attitude: { type: "string", required: true, description: "The attitude shown in that moment" },
        example: { type: "string", description: "What the model actually did" },
        source_event: { type: "string", required: true, description: "evt_ id of the source event (from search/ledger)" },
        reason: { type: "string", required: true, description: "Why this moment was striking" },
      },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        const content: Record<string, string> = { attitude: args.attitude };
        if (args.example) content.example = args.example;
        const r = await runNoname(
          ["taste-propose", "--content", JSON.stringify(content), "--source-event", args.source_event, "--reason", args.reason],
          optsFor(ctx, config, exec),
        );
        return JSON.stringify(r);
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_taste_review",
      description:
        "Review a taste record's lifecycle: adopt (activate a candidate), edit (new version), pause, resume, or retire. Writes an immutable review record.",
      parameters: {
        taste_id: { type: "string", required: true, description: "tst_ id of the taste record" },
        action: { type: "string", required: true, description: "adopt | edit | pause | resume | retire" },
        reviewer: { type: "string", required: true, description: "Reviewer id (the human)" },
        reason: { type: "string", description: "Reason for the action" },
      },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        const cmd = ["taste-review", "--taste-id", args.taste_id, "--action", args.action, "--reviewer", args.reviewer];
        if (args.reason) cmd.push("--reason", args.reason);
        const r = await runNoname<{
          id: string;
          status: string;
          track: string;
          supersedes_id: string | null;
        }>(cmd, optsFor(ctx, config, exec));
        // Every review writes a NEW head record -- records are immutable and the
        // old one is superseded -- so the id that comes back is not the id that
        // went in.  Returning raw JSON buried that: adopting tst_A and then
        // pausing tst_A fails with "has been superseded".  Lead with the head.
        const superseded = r.supersedes_id ? ` supersedes=${r.supersedes_id}` : "";
        return (
          `${args.action}: ${r.track} taste is now ${r.status} — head=${r.id}${superseded}\n` +
          `Use head=${r.id} for any further review of this record.\n` +
          JSON.stringify(r)
        );
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_card_queue",
      description: "Show the taste-card review queue: cards due for the periodic 'is this still me?' review, with staleness markers.",
      parameters: {},
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(_args, exec) {
        return JSON.stringify(await runNoname(["card-queue"], optsFor(ctx, config, exec)));
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_ledger",
      description:
        "Render the NoName ledger as a five-view HTML report (review inbox / state / version evolution / causal map / timeline) and return its file path. Open the file to inspect why the project is the way it is. This is the ledger's form in DSH (no host-reachable sidebar seam exists).",
      parameters: {},
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(_args, exec) {
        const { buildLedgerView } = await import("./ui/ledger-view.js");
        const view = await buildLedgerView(ctx, config, exec);
        // byteLength, not String#length: the ledger is full of Chinese, so the
        // UTF-16 unit count understated the file by ~6% while the message said
        // "bytes".  A size the reader cannot trust is worse than no size.
        const bytes = Buffer.byteLength(view.html, "utf8");
        return `ledger written to ${view.htmlPath} (${bytes} bytes; open it to view the five views)`;
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_verify",
      description:
        "Verify the integrity of the NoName ledger: every event and evidence span is re-hashed under the version it was written with. Reports which ledger was checked, how many rows are only payload-signed (legacy v1), and which rows are corrupt.",
      parameters: {},
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(_args, exec) {
        const opts = optsFor(ctx, config, exec);
        // acceptExitCodes: the kernel prints the full report to stdout and then
        // exits 1 when integrity fails.  Rejecting on that code threw the report
        // away and replaced it with a bare "noname exited 1" bridge error --
        // losing exactly the "which ledger was this?" line the next statement
        // exists to produce.
        const r = await runNoname<{
          ok: boolean;
          bad_event_ids?: string[];
          bad_evidence_ids?: string[];
          unverified_event_ids?: string[];
          unverified_evidence_ids?: string[];
          hash_coverage?: {
            events?: { v1_payload_only?: number; v2_full_provenance?: number };
            evidence?: { v1_content_only?: number; v2_full_provenance?: number };
          };
        }>(["verify"], { ...opts, acceptExitCodes: [1] });
        // Report WHICH ledger this call used, and why: a silent fallback to
        // the global directory once looked exactly like a working
        // workspace-scoped ledger, and only the number of events gave it away.
        const target = ledgerTarget(ctx, config, sessionIdOf(exec));
        const targetLine = describeLedgerTarget(target);
        const coverage = r.hash_coverage;
        const coverageLine = coverage
          ? `coverage: events ${coverage.events?.v2_full_provenance ?? 0} fully signed / ` +
            `${coverage.events?.v1_payload_only ?? 0} legacy payload-only; ` +
            `evidence ${coverage.evidence?.v2_full_provenance ?? 0} fully signed / ` +
            `${coverage.evidence?.v1_content_only ?? 0} legacy content-only`
          : "";
        if (r.ok) {
          return `ledger integrity OK — ${targetLine}${coverageLine ? `\n${coverageLine}` : ""}`;
        }
        const corrupt = [
          ...(r.bad_event_ids ?? []).map((id) => `event ${id}`),
          ...(r.bad_evidence_ids ?? []).map((id) => `evidence ${id}`),
        ];
        const unverified = [
          ...(r.unverified_event_ids ?? []).map((id) => `event ${id}`),
          ...(r.unverified_evidence_ids ?? []).map((id) => `evidence ${id}`),
        ];
        const detail = [
          corrupt.length ? `modified (hash mismatch): ${corrupt.join(", ")}` : "",
          unverified.length ? `unverifiable (unknown hash version): ${unverified.join(", ")}` : "",
        ]
          .filter(Boolean)
          .join("; ");
        return (
          `ledger integrity FAILED — ${targetLine}\n` +
          (detail ? `${detail}\n` : "") +
          (coverageLine ? `${coverageLine}\n` : "") +
          "The ledger is append-only; do not edit it in place."
        );
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_extract",
      description:
        "Run memory extraction over a session's events: surface durable memory candidates (never auto-confirmed -- a human reviews before anything becomes canon).",
      parameters: { session: { type: "string", required: true, description: "Session id to extract from" } },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        return JSON.stringify(await runNoname(["extract", "--session", args.session], optsFor(ctx, config, exec)));
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
  "noname_taste_propose",
  "noname_taste_review",
  "noname_card_queue",
  "noname_ledger",
  "noname_verify",
  "noname_extract",
] as const;
