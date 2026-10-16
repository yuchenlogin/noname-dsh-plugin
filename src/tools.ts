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

// Serialize curation checks within this host so two sessions opening the same
// ledger do not both propose an identical memory. Kernel review still owns
// durable revisions and concurrency across separate processes.
const curationQueues = new Map<string, Promise<unknown>>();
async function curate<T>(opts: NonameRunOptions, operation: () => Promise<T>): Promise<T> {
  const key = opts.dbPath ?? opts.dbDir;
  const previous = curationQueues.get(key) ?? Promise.resolve();
  const pending = previous.catch(() => {}).then(operation);
  curationQueues.set(key, pending);
  try {
    return await pending;
  } finally {
    if (curationQueues.get(key) === pending) curationQueues.delete(key);
  }
}

interface MemoryRecord {
  id: string;
  layer: string;
  logical_key: string;
  content: unknown;
}

function sameMemory(left: unknown, right: Record<string, string>): boolean {
  if (!left || typeof left !== "object" || Array.isArray(left)) return false;
  const value = left as Record<string, unknown>;
  return Object.keys(value).length === Object.keys(right).length &&
    Object.keys(right).every((key) => value[key] === right[key]);
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
        "Save one useful source event as evidence, not approved memory. " +
        (config.autoIngest
          ? "Ordinary tool results are captured automatically; do not duplicate them. "
          : "Automatic tool-result capture is off; use noname_record to preserve selected useful evidence. ") +
        "Do not record every conversation turn. Use for an explicit decision, correction, verified finding or recovery checkpoint. For durable memory, use noname_remember with this event's id.",
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
        return `recorded event ${r.id}\nSaved as evidence only; no approved memory changed.`;
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_remember",
      description:
        "Propose one concise, evidence-backed memory for human review. Use high for lasting constraints and mid for task progress/next steps. First inspect noname_state/inbox to avoid duplicates. Reuse the same stable key for a correction: approval replaces the prior current state while preserving its history. Never turn each conversation turn into memory; propose only information that will change future work. Taste/preferences require explicit human judgement.",
      parameters: {
        layer: { type: "string", required: true, description: "high (lasting project constraint) or mid (task state)" },
        key: { type: "string", required: true, description: "Stable subject key, e.g. project.storage; reuse it for corrections" },
        text: { type: "string", required: true, description: "One standalone sentence explaining the current decision or next step" },
        source_event: { type: "string", required: true, description: "evt_ id containing the decision or correction; use noname_record/search" },
        reason: { type: "string", required: true, description: "Why future work needs this memory or why it replaces earlier state" },
        next_step: { type: "string", description: "Concrete next action for task state, when useful" },
      },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        if (args.layer !== "high" && args.layer !== "mid") {
          throw new Error("noname_remember: layer must be high or mid");
        }
        for (const field of ["key", "text", "source_event", "reason"] as const) {
          if (!args[field].trim()) throw new Error(`noname_remember: ${field} must not be empty`);
        }
        const opts = optsFor(ctx, config, exec);
        return await curate(opts, async () => {
          await ensureNonameInit(opts);
          const key = args.key.trim();
          const content: Record<string, string> = { text: args.text.trim(), reason: args.reason.trim() };
          if (args.next_step?.trim()) content.next_step = args.next_step.trim();
          const states = await runNoname<MemoryRecord[]>(["state", "--layer", args.layer], opts);
          const proposals = await runNoname<MemoryRecord[]>(["proposals"], opts);
          const current = states.find((record) => record.logical_key === key);
          const pending = proposals.filter((record) => record.layer === args.layer && record.logical_key === key);
          const latest = pending.at(-1);
          if (latest && sameMemory(latest.content, content)) {
            return `already_pending: Memory candidate ${latest.id} already contains this decision.\n` +
              "No duplicate was created. Open noname_ledger for human review.";
          }
          if (!pending.length && current && sameMemory(current.content, content)) {
            return `unchanged: Approved memory for ${key} already contains this decision. No candidate was created.`;
          }
          const proposal = await runNoname<{ id: string }>([
            "propose", "--layer", args.layer, `--key=${key}`,
            "--content", JSON.stringify(content),
            `--source-event=${args.source_event}`, "--by", "dsh-agent", `--reason=${args.reason}`,
          ], opts);
          return `Memory candidate ${proposal.id} awaits human review.\n${args.text.trim()}\n` +
            `Subject: ${key} (${args.layer}). Approved memory has not changed.\n` +
            (current ? `On approval, this replaces current revision ${current.id}.\n` : "") +
            (pending.length ? `${pending.length} earlier candidate(s) for this subject remain available for human review.\n` : "") +
            "Open noname_ledger for review; reuse this subject key when the decision changes.";
        });
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "noname_package",
      description:
        "Recall a portable NoName context package at the start of work and before context switches. Approved canon/task state guides continuity; raw evidence is history, pending proposals are unapproved, and taste is a soft preference. None overrides current instructions. Surface only relevant decisions and next steps to the user, and honestly state when approved layers are empty.",
      parameters: {
        task: { type: "string", required: true, description: "The task to continue" },
        session: { type: "string", required: true, description: "Session id to assemble from" },
      },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        // `package` emits markdown, not JSON: raw mode.
        const opts = optsFor(ctx, config, exec);
        await ensureNonameInit(opts);
        return await runNoname<string>(
          ["package", "--task", args.task, "--session", args.session],
          { ...opts, raw: true },
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
      description: "Show approved project memory. Choose high for lasting constraints, mid for task progress, or omit layer for both. Check before proposing a new memory; corrections should reuse the existing logical_key.",
      parameters: { layer: { type: "string", description: "high or mid; omitted returns both" } },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        if (args.layer && args.layer !== "high" && args.layer !== "mid") {
          throw new Error("noname_state: layer must be high or mid");
        }
        const cmd = args.layer ? ["state", "--layer", args.layer] : ["state"];
        return JSON.stringify(await runNoname(cmd, optsFor(ctx, config, exec)));
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
        "Record an AUTHORED taste only when the user explicitly asks to save their stated preference. It is active immediately: do not infer consent from ordinary conversation. For inferred taste, use noname_taste_propose and wait for human review. Taste is a soft preference and never rewrites facts, overrides instructions or lowers verification standards.",
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
        "Apply an explicitly authorized human decision to a taste record: adopt, edit (requires content JSON), pause, resume or retire. Never invent human authorization or treat an inferred preference as consent. Writes a new immutable head; use its returned id for later actions.",
      parameters: {
        taste_id: { type: "string", required: true, description: "tst_ id of the taste record" },
        action: { type: "string", required: true, description: "adopt | edit | pause | resume | retire" },
        reviewer: { type: "string", required: true, description: "Reviewer id (the human)" },
        content: { type: "string", description: "JSON object for edit, e.g. {\"attitude\":\"Prefer a minimal reproduction\"}" },
        reason: { type: "string", description: "Reason for the action" },
      },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        if (args.action === "edit" && !args.content?.trim()) {
          throw new Error("noname_taste_review: edit requires content JSON");
        }
        if (args.content && args.action !== "edit") {
          throw new Error("noname_taste_review: content only applies to edit");
        }
        if (args.content) {
          try {
            const content = JSON.parse(args.content);
            if (!content || typeof content !== "object" || Array.isArray(content)) throw new Error();
          } catch {
            throw new Error("noname_taste_review: content must be a JSON object");
          }
        }
        const cmd = ["taste-review", "--taste-id", args.taste_id, "--action", args.action, "--reviewer", args.reviewer];
        if (args.content) cmd.push("--content", args.content);
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
        "Open the NoName ledger: current decisions and next steps, changes waiting for judgement, taste/preferences, decision history and the activity archive. Return its live URL for the human to inspect sources and review candidates with an identity and reason, plus a read-only offline snapshot. Approval is performed by the human in the browser.",
      parameters: {},
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(_args, exec) {
        const { buildLedgerView } = await import("./ui/ledger-view.js");
        const view = await buildLedgerView(ctx, config, exec);
        // byteLength, not String#length: the ledger is full of Chinese, so the
        // UTF-16 unit count understated the file by ~6% while the message said
        // "bytes".  A size the reader cannot trust is worse than no size.
        const bytes = Buffer.byteLength(view.html, "utf8");
        const snapshot = view.htmlPath
          ? `ledger written to ${view.htmlPath} (${bytes} bytes; offline snapshot)`
          : `Offline snapshot unavailable: ${view.snapshotError ?? "export failed"}. Live review remains available.`;
        return `live ledger: ${view.liveUrl}\nOpen this URL in a browser to review memory candidates. The local server stops after one hour without activity.\n${snapshot}`;
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
        "Inspect possible memory in a session. Defaults to a preview without creating candidates. Prefer noname_remember for one concise, evidence-backed decision. Set create_candidates=true only when the user asks to collect candidates for review; never extract every session automatically or claim candidates are approved.",
      parameters: {
        session: { type: "string", required: true, description: "Session id to extract from" },
        create_candidates: { type: "boolean", description: "Explicit opt-in to create review candidates (default false)" },
      },
      output: { schema: { type: "string" }, render: (_a, v) => text(v) },
      async execute(args, exec) {
        const opts = optsFor(ctx, config, exec);
        await ensureNonameInit(opts);
        const cmd = ["extract", "--session", args.session];
        if (!args.create_candidates) cmd.push("--no-proposals");
        return JSON.stringify(await runNoname(cmd, opts));
      },
    }),
  );
}

export const NONAME_TOOL_NAMES = [
  "noname_record",
  "noname_remember",
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
