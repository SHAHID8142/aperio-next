"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const qa = path.resolve(__dirname, "../.qa");
const fixture = JSON.parse(fs.readFileSync(path.join(qa, "codex-attempt.json"), "utf8"));
const engine = path.join(fixture.root, "engine/bin/aperio");
const prompt = "/aperio-next\nContinue the previous task. This is a READ-ONLY cross-agent recovery acceptance check in a disposable project. Use the repository-local aperio-next skill, verify its isolated engine identity, and execute actual next, memory, work-status and recover. Report the current task, plan approval and pending review. Do not initialize, approve, edit, implement or mutate project state.";
const allow = ["Read", "Skill", ...["core-identity", "next", "memory", "work-status", "recover"].map(command => `Bash(${engine} ${command} *)`)];
const attempts = [
  ["claude", ["-p", prompt, "--output-format", "json", "--permission-mode", "plan", "--allowedTools", ...allow]],
  ["opencode", ["run", "--standalone", "--agent", "plan", "--format", "json", prompt]],
];
for (const [command, args] of attempts) {
  const result = spawnSync(command, args, { cwd: fixture.project, env: { ...process.env, APERIO_NEXT_HOME: path.join(fixture.root, "engine") }, encoding: "utf8", timeout: 120000, maxBuffer: 8 * 1024 * 1024 });
  const record = { command, status: result.status, signal: result.signal, error: result.error?.message, stdout: result.stdout, stderr: result.stderr };
  fs.writeFileSync(path.join(qa, `${command}-attempt.json`), JSON.stringify(record));
  process.stdout.write(JSON.stringify({ command, status: result.status, signal: result.signal, error: result.error?.message }) + "\n");
}
