"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const qa = path.resolve(__dirname, "..", ".qa");
const root = fs.mkdtempSync(path.join(qa, "native-"));
const home = path.join(root, "home");
const project = path.join(home, "project");
fs.mkdirSync(path.join(home, ".agents"), { recursive: true });
fs.mkdirSync(project);
const env = { ...process.env, HOME: home, APERIO_NEXT_HOME: path.join(root, "engine"), npm_config_cache: path.join(root, "npm-cache") };
function prepare(command, args, options = {}) {
  const result = spawnSync(command, args, { env, encoding: "utf8", timeout: 120000, ...options });
  if (result.status !== 0) throw new Error(result.stderr || `${command} failed`);
  return result;
}
prepare("git", ["init", home]);
const prefix = path.join(root, "npm");
prepare("npm", ["install", "--prefix", prefix, "--no-audit", "--no-fund", path.resolve(__dirname, "..", "dist", "aperio-next-1.2.0-rc.2.tgz")]);
prepare(process.execPath, [path.join(prefix, "node_modules", "aperio-next", "bin", "aperio-next.js"), "install"]);
const prompt = "$aperio-next\nUse the repository-local aperio-next skill at " + path.join(home, ".agents/skills/aperio-next/SKILL.md") + ". This is a disposable native activation acceptance test. Verify the engine identity and run the actual read-only aperio next command for the current project. Do not initialize or write project files. Report the observed classification and required consent step, then stop.";
const result = spawnSync("codex", ["exec", "--ephemeral", "--json", "--sandbox", "read-only", "--skip-git-repo-check", "-C", project, prompt], {
  env: { ...process.env, APERIO_NEXT_HOME: env.APERIO_NEXT_HOME }, encoding: "utf8", timeout: 180000, maxBuffer: 8 * 1024 * 1024,
});
fs.writeFileSync(path.join(root, "activation.jsonl"), result.stdout || "");
fs.writeFileSync(path.join(root, "activation.stderr"), result.stderr || "");
process.stdout.write(JSON.stringify({ root, project, status: result.status, signal: result.signal, error: result.error?.message, stdout: result.stdout, stderr: result.stderr }) + "\n");
const commands = (result.stdout || "").split("\n").filter(Boolean).map(line => JSON.parse(line))
  .filter(event => event.type === "item.completed" && event.item?.type === "command_execution" && event.item.exit_code === 0)
  .map(event => event.item);
const identity = commands.some(item => item.command.includes(path.join(root, "engine/bin/aperio")) &&
  item.command.includes("core-identity") && JSON.parse(item.aggregated_output).product === "aperio-next");
const next = commands.some(item => item.command.includes(path.join(root, "engine/bin/aperio")) &&
  /\snext\s/.test(item.command) && JSON.parse(item.aggregated_output).classification === "empty_new");
process.exitCode = result.status === 0 && identity && next && !fs.existsSync(path.join(project, ".aperio")) ? 0 : 1;
