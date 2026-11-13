"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const qa = path.resolve(__dirname, "..", ".qa");
fs.mkdirSync(qa, { recursive: true });
const root = fs.mkdtempSync(path.join(qa, "release-"));
const home = path.join(root, "home");
fs.mkdirSync(path.join(home, ".agents"), { recursive: true });
const env = { ...process.env, HOME: home, APERIO_NEXT_HOME: path.join(root, "engine"), npm_config_cache: path.join(root, "npm-cache") };
const artifact = path.resolve(__dirname, "..", "dist", "aperio-next-1.2.0-rc.2.tgz");
function run(command, args) {
  const result = spawnSync(command, args, { env, encoding: "utf8", timeout: 120000 });
  process.stdout.write(JSON.stringify({ command, args, status: result.status, stdout: result.stdout, stderr: result.stderr }) + "\n");
  if (result.status !== 0) throw new Error(`fixture command failed: ${command}`);
  return result.stdout;
}
const launch = command => run("npx", ["--yes", "--package", artifact, "aperio-next", command]);
launch("install");
launch("doctor");
const engine = path.join(env.APERIO_NEXT_HOME, "bin", "aperio");
run(engine, ["core-identity", "--json"]);
const project = path.join(root, "project");
fs.mkdirSync(project);
run(engine, ["onboarding-confirm-init", project, "--approve", "--json"]);
run(engine, ["integrations", "install", "--host", "codex-cli", "--scope", "project", "--project-instructions", "--json", project]);
launch("update");
launch("doctor");
const state = fs.readFileSync(path.join(project, ".aperio", "state.json"));
launch("uninstall");
if (!state.equals(fs.readFileSync(path.join(project, ".aperio", "state.json")))) throw new Error("project state changed");
if (fs.existsSync(env.APERIO_NEXT_HOME)) throw new Error("uninstall did not remove owned root");
process.stdout.write("PASS: packed install, identity, doctor, project registration, update, uninstall; project state preserved\n");
