import { Context } from "@deepseek-ai/cordis";
import SystemPrompt, { renderPrompt } from "@deepseek-ai/dsh-system-prompt";
import ToolRuntime from "@deepseek-ai/dsh-tools";
import { describe, expect, it, vi } from "vitest";
import pluginEntry from "../src/index.js";
import * as kernel from "../src/kernel.js";
import { resolveConfig } from "../src/config.js";
import { readWritingPolicy, registerOutputPreferences, OUTPUT_PREFERENCE_SECTION } from "../src/output-preferences.js";

const policyPlugin = {
  name: "test-noname-writing",
  inject: ["systemPrompt"],
  apply(ctx: Context, config: { writingStyle?: "direct" | "off" }) {
    registerOutputPreferences(ctx, resolveConfig(config));
  },
};

describe("pre-generation writing preference on real Cordis/SystemPrompt", () => {
  it("loads the public plugin with real host services and preserves tools", async () => {
    const ping = vi.spyOn(kernel, "pingKernel").mockResolvedValue(true);
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const ctx = new Context();
    try {
      await ctx.plugin(SystemPrompt, {});
      await ctx.plugin(ToolRuntime, {});
      const plugin = await ctx.plugin(pluginEntry, { dbDir: "/tmp/noname-prompt-load", autoIngest: false });
      const assembly = await ctx.systemPrompt.assemble();
      expect(renderPrompt(assembly)).toContain(readWritingPolicy());
      expect(assembly.tools.some(t => t.name === "noname_package")).toBe(true);
      await plugin.dispose();
      const disposed = await ctx.systemPrompt.assemble();
      expect(renderPrompt(disposed)).not.toContain("noname-output-preferences");
      expect(disposed.tools.some(t => t.name === "noname_package")).toBe(false);
    } finally {
      await ctx.fiber.dispose();
      ping.mockRestore();
      log.mockRestore();
    }
  });

  it("delivers the shared policy once on every assembly and removes it on disposal/reload", async () => {
    const ctx = new Context();
    await ctx.plugin(SystemPrompt, { personaPrefix: "Host persona" });
    const plugin = await ctx.plugin(policyPlugin, {});
    try {
      for (let request = 0; request < 3; request++) {
        const assembly = await ctx.systemPrompt.assemble();
        const text = renderPrompt(assembly);
        expect(text).toContain("Host persona");
        expect(text).toContain(readWritingPolicy());
        expect(assembly.sections.filter(s => s.name === OUTPUT_PREFERENCE_SECTION)).toHaveLength(1);
      }
      await plugin.dispose();
      expect(renderPrompt(await ctx.systemPrompt.assemble())).not.toContain("noname-output-preferences");
      const reloaded = ctx.plugin(policyPlugin, { writingStyle: "off" });
      await reloaded;
      expect(renderPrompt(await ctx.systemPrompt.assemble())).not.toContain("noname-output-preferences");
      await reloaded.dispose();
      await ctx.plugin(policyPlugin, { writingStyle: "direct" });
      expect(renderPrompt(await ctx.systemPrompt.assemble())).toContain(readWritingPolicy());
    } finally {
      await ctx.fiber.dispose();
    }
  });

  it("honors the host's explicit complete-prompt override", async () => {
    const ctx = new Context();
    await ctx.plugin(SystemPrompt, {});
    await ctx.plugin(policyPlugin, {});
    try {
      ctx.systemPrompt.section({ name: "exact-host-prompt", order: 1, complete: true, text: "Exact prompt" });
      expect(renderPrompt(await ctx.systemPrompt.assemble())).toBe("Exact prompt");
    } finally {
      await ctx.fiber.dispose();
    }
  });

  it("validates the user switch", () => {
    expect(resolveConfig().writingStyle).toBe("direct");
    expect(resolveConfig({ writingStyle: "off" }).writingStyle).toBe("off");
    expect(() => resolveConfig({ writingStyle: "typo" as "direct" })).toThrow();
  });
});
