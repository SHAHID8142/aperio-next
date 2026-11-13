"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const attempt = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../.qa/codex-attempt.json"), "utf8"));
const label = process.argv[2];
if (!/^[a-z-]+$/.test(label || "")) throw new Error("provide a lowercase evidence label");
const prompt = fs.readFileSync(process.argv[3], "utf8");
const sandbox = process.argv[4] === "read-only" ? "read-only" : "workspace-write";
const result = spawnSync("codex", ["exec", "--ephemeral", "--json", "--sandbox", sandbox, "--skip-git-repo-check", "--add-dir", attempt.root, "-C", attempt.project, prompt], {
  env: { ...process.env, APERIO_NEXT_HOME: path.join(attempt.root, "engine") }, encoding: "utf8", timeout: 240000, maxBuffer: 8 * 1024 * 1024,
});
fs.writeFileSync(path.join(attempt.root, `${label}.jsonl`), result.stdout || "");
fs.writeFileSync(path.join(attempt.root, `${label}.stderr`), result.stderr || "");
process.stdout.write(JSON.stringify({ root: attempt.root, status: result.status, signal: result.signal, error: result.error?.message, stdout: result.stdout, stderr: result.stderr }) + "\n");
process.exitCode = result.status === 0 ? 0 : 1;
