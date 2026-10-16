import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, it } from "vitest";

it("ships the usable sidecar and built tools without Python caches or local ledgers", async () => {
  const { stdout } = await promisify(execFile)("npm", ["pack", "--dry-run", "--json", "--ignore-scripts"], {
    cwd: resolve(dirname(fileURLToPath(import.meta.url)), ".."),
  });
  const [packed] = JSON.parse(stdout) as { files: { path: string }[] }[];
  const paths = packed.files.map((entry) => entry.path);
  expect(paths).toContain("dist/tools.js");
  expect(paths).toContain("vendor/noname_harness_pkg/noname_harness/__init__.py");
  expect(paths.filter((path) => /(?:__pycache__|\.py[cod]$|harness\.db$|(?:^|\/)\.noname\/)/.test(path))).toEqual([]);
}, 15_000);
