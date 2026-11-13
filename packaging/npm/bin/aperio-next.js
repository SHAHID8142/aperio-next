#!/usr/bin/env node
/**
 * aperio-next — one-line installer for the Aperio Next engine.
 *
 * Zero npm dependencies by design (supply-chain surface: this file only).
 * Commands:
 *   aperio-next install    bootstrap the Python engine + register host skills
 *   aperio-next doctor     report engine, runtime, and integration health
 *   aperio-next update     re-bootstrap the engine from the bundled wheel
 *   aperio-next uninstall  remove host skill assets and the engine install
 *
 * Design rules mirrored from the engine's docs:
 * - Only print success for facts that were actually verified.
 * - Never touch anything outside aperio's own install root and the hosts'
 *   documented skills/commands locations (the engine enforces that).
 * - Project data (.aperio/ inside your projects) is NEVER deleted by
 *   uninstall — it lives in your projects, not here.
 */

"use strict";

const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const IS_WIN = process.platform === "win32";
// Test/ops override: everything aperio installs lives inside this one root.
const APERIO_HOME =
  process.env.APERIO_NEXT_HOME || path.join(os.homedir(), ".aperio-next");
const VENV_DIR = path.join(APERIO_HOME, "venv");
const BIN_DIR = path.join(APERIO_HOME, "bin");
const MANIFEST = path.join(APERIO_HOME, "install-root.json");
const ENGINE_DIR = path.join(__dirname, "..", "engine");
const BACKUP = path.join(APERIO_HOME, ".update-backup");
const COMMITTED = path.join(APERIO_HOME, ".update-committed");
let transactionActive = false;
let moduleEngine = false;

function fail(msg) {
  if (transactionActive) {
    try {
      restoreUpdate();
    } catch (error) {
      msg += `\nRollback incomplete; backup retained at ${BACKUP}: ${error.message}`;
    }
  }
  process.stderr.write(`aperio-next: error: ${msg}\n`);
  process.exit(1);
}

function run(cmd, args, opts) {
  // Bound every child: a hung venv/pip/engine call must fail rather than
  // block the launcher forever. Callers may override with a larger timeout.
  const res = spawnSync(
    cmd,
    args,
    Object.assign({ encoding: "utf8", timeout: 600000 }, opts)
  );
  if (res.error) {
    return { ok: false, code: -1, stdout: "", stderr: String(res.error) };
  }
  return {
    ok: res.status === 0,
    code: res.status,
    stdout: res.stdout || "",
    stderr: res.stderr || "",
  };
}

// --------------------------------------------------------------------------
// python discovery (3.11+), no network, no third-party tools required
// --------------------------------------------------------------------------

function* pythonCandidates() {
  if (IS_WIN) {
    yield { cmd: "py", args: ["-3"] };
    yield { cmd: "python3" };
    yield { cmd: "python" };
    return;
  }
  for (const minor of [14, 13, 12, 11]) {
    yield { cmd: `python3.${minor}` };
  }
  yield { cmd: "python3" };
  yield { cmd: "python" };
}

function findPython() {
  for (const cand of pythonCandidates()) {
    const baseArgs = cand.args || [];
    const probe = run(cand.cmd, baseArgs.concat([
      "-c",
      "import sys; print(sys.version_info >= (3, 11))",
    ]), { stdio: ["ignore", "pipe", "pipe"] });
    if (probe.ok && probe.stdout.trim() === "True") {
      return { cmd: cand.cmd, args: baseArgs };
    }
  }
  return null;
}

function venvPython() {
  return IS_WIN
    ? path.join(VENV_DIR, "Scripts", "python.exe")
    : path.join(VENV_DIR, "bin", "python");
}

function vengineBin() {
  return IS_WIN
    ? path.join(VENV_DIR, "Scripts", "aperio.exe")
    : path.join(VENV_DIR, "bin", "aperio");
}

function bundledWheel() {
  if (!fs.existsSync(ENGINE_DIR)) {
    return null;
  }
  const wheels = fs
    .readdirSync(ENGINE_DIR)
    .filter((f) => f.endsWith(".whl"))
    .sort();
  return wheels.length ? path.join(ENGINE_DIR, wheels[0]) : null;
}

// --------------------------------------------------------------------------
// install root management
// --------------------------------------------------------------------------

function readManifest() {
  try {
    return JSON.parse(fs.readFileSync(MANIFEST, "utf8"));
  } catch {
    return null;
  }
}

function rejectSymlinkParents(target) {
  for (let current = path.resolve(target); ; current = path.dirname(current)) {
    try {
      if (fs.lstatSync(current).isSymbolicLink()) {
        const systemAlias = process.platform === "darwin" &&
          ["/var", "/tmp", "/etc"].includes(current) &&
          fs.realpathSync(current) === `/private${current}`;
        if (!systemAlias) throw new Error(`symlink refused: ${current}`);
      }
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    if (path.dirname(current) === current) break;
  }
}

function validateInstallRoot() {
  const root = path.resolve(APERIO_HOME);
  if ([path.parse(root).root, os.homedir(), process.cwd()].includes(root)) {
    throw new Error(`unsafe installation root: ${root}`);
  }
  rejectSymlinkParents(root);
  const recoveryOwned = fs.existsSync(path.join(BACKUP, "install-root.json")) &&
    JSON.parse(fs.readFileSync(path.join(BACKUP, "install-root.json"), "utf8")).product === "aperio-next";
  if (fs.existsSync(root) && fs.readdirSync(root).length && readManifest()?.product !== "aperio-next" && !recoveryOwned) {
    throw new Error(`foreign installation root: ${root}`);
  }
}

function checkedReport(args, field) {
  const result = engine(args);
  let data;
  try { data = JSON.parse(result.stdout); } catch { throw new Error("invalid engine JSON report"); }
  if (!result.ok || !data || !Array.isArray(data[field])) throw new Error("engine operation failed: " + result.stderr);
  return data;
}

function beginUpdate() {
  const manifest = readManifest();
  if (!manifest) return;
  if (manifest.version === "installing" && !manifest.hosts?.length && !manifest.projects?.length) return;
  const identity = checkedIdentity();
  if (!identity) throw new Error("cannot snapshot an unverified engine; restore a known-good installation before updating");
  const files = new Map();
  const remember = (file, instruction = false) => {
    rejectSymlinkParents(file);
    if (fs.existsSync(file) && !fs.statSync(file).isFile()) throw new Error(`not a regular file: ${file}`);
    files.set(file, fs.existsSync(file) ? { bytes: fs.readFileSync(file).toString("base64"), mode: fs.statSync(file).mode, instruction } : { bytes: null, instruction });
  };
  const collect = (args) => {
    const status = checkedReport(args, "hosts");
    for (const host of status.hosts) {
      for (const [file, state] of Object.entries(host.files)) {
        if (state === "managed" || state === "absent") remember(file);
      }
    }
  };
  collect(["integrations", "status", "--json"]);
  for (const project of manifest.projects || []) {
    if (!fs.existsSync(project)) continue;
    collect(["integrations", "status", "--scope", "project", "--json", project]);
    for (const name of ["AGENTS.md", "CLAUDE.md"]) remember(path.join(project, name), true);
  }
  fs.mkdirSync(BACKUP, { mode: 0o700 });
  for (const name of ["venv", "bin", "install-root.json"]) {
    fs.cpSync(path.join(APERIO_HOME, name), path.join(BACKUP, name), { recursive: true, verbatimSymlinks: true });
  }
  fs.writeFileSync(path.join(BACKUP, "journal.json"), JSON.stringify({ product: "aperio-next", pid: process.pid, files: [...files] }), { flag: "wx", mode: 0o600 });
  transactionActive = true;
}

function restoreUpdate() {
  const journal = JSON.parse(fs.readFileSync(path.join(BACKUP, "journal.json"), "utf8"));
  if (journal.product !== "aperio-next") throw new Error("foreign rollback journal");
  for (const name of ["venv", "bin", "install-root.json"]) {
    const target = path.join(APERIO_HOME, name);
    rejectSymlinkParents(name === "bin" ? APERIO_HOME : target);
    fs.rmSync(target, { recursive: true, force: true });
    fs.cpSync(path.join(BACKUP, name), target, { recursive: true, verbatimSymlinks: true });
  }
  for (const [file, prior] of journal.files) {
    rejectSymlinkParents(file);
    if (prior.instruction) {
      const old = prior.bytes === null ? "" : Buffer.from(prior.bytes, "base64").toString("utf8");
      const current = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
      if (old === current) continue;
      const section = /<!-- aperio-next:project-instructions:begin -->[\s\S]*?<!-- aperio-next:project-instructions:end -->/g;
      const oldSection = old.match(section)?.[0] || "";
      const restored = section.test(current) ? current.replace(section, () => oldSection) : current + (oldSection ? `\n\n${oldSection}\n` : "");
      if (restored) fs.writeFileSync(file, restored);
      else if (fs.existsSync(file)) fs.unlinkSync(file);
    } else if (prior.bytes !== null) {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, Buffer.from(prior.bytes, "base64"), { mode: prior.mode });
    } else if (fs.existsSync(file)) {
      const current = fs.readFileSync(file, "utf8");
      if (current.includes("<!-- aperio-next:managed v1 -->") && current.includes("<!-- /apario-next:managed v1 -->")) fs.unlinkSync(file);
      else process.stderr.write(`Foreign file preserved during rollback: ${file}\n`);
    }
  }
  commitUpdate();
  process.stderr.write("Previous engine and integration assets restored from rollback backup.\n");
}

function recoverUpdate() {
  if (fs.existsSync(COMMITTED)) {
    rejectSymlinkParents(COMMITTED);
    if (JSON.parse(fs.readFileSync(COMMITTED, "utf8")).product !== "aperio-next") throw new Error("foreign commit marker");
    rejectSymlinkParents(BACKUP);
    fs.rmSync(BACKUP, { recursive: true, force: true });
    fs.unlinkSync(COMMITTED);
    return;
  }
  if (!fs.existsSync(BACKUP)) return;
  rejectSymlinkParents(BACKUP);
  const journalFile = path.join(BACKUP, "journal.json");
  if (!fs.existsSync(journalFile)) throw new Error(`incomplete backup at ${BACKUP}; no installation mutation was started; inspect before retrying`);
  const journal = JSON.parse(fs.readFileSync(journalFile, "utf8"));
  if (journal.pid !== process.pid) {
    try { process.kill(journal.pid, 0); } catch (error) {
      if (error.code === "ESRCH") return restoreUpdate();
      throw error;
    }
    throw new Error("another installer operation is active");
  }
}

function commitUpdate() {
  if (fs.existsSync(BACKUP)) atomicJson(COMMITTED, { product: "aperio-next" });
  transactionActive = false;
  if (fs.existsSync(BACKUP)) fs.rmSync(BACKUP, { recursive: true });
  if (fs.existsSync(COMMITTED)) fs.unlinkSync(COMMITTED);
}

function atomicJson(file, value) {
  rejectSymlinkParents(file);
  const temporary = `${file}.${process.pid}.tmp`;
  const fd = fs.openSync(temporary, "wx", 0o600);
  try {
    fs.writeFileSync(fd, JSON.stringify(value, null, 2));
    fs.fsyncSync(fd);
  } finally { fs.closeSync(fd); }
  fs.renameSync(temporary, file);
}

function checkedIdentity(allowFallback = true) {
  const result = engine(["core-identity", "--json"]);
  try {
    if (result.ok && JSON.parse(result.stdout).product === "aperio-next") return true;
  } catch {}
  if (!allowFallback) return false;
  const fallback = run(venvPython(), ["-I", "-m", "aperio", "core-identity", "--json"]);
  try {
    const identity = JSON.parse(fallback.stdout);
    if (fallback.ok && identity.product === "aperio-next" && path.resolve(identity.python).startsWith(fs.realpathSync(VENV_DIR) + path.sep)) {
      moduleEngine = true;
      return true;
    }
  } catch {}
  return false;
}

function writeManifest(version, extra = {}) {
  fs.mkdirSync(APERIO_HOME, { recursive: true });
  const prior = readManifest() || {};
  // Preserve previously recorded project dirs across install/update; add any
  // new one so uninstall can later clean project-scope assets for every
  // project this install touched (never the project's .aperio/ data).
  const projects = new Set(prior.projects || []);
  if (extra.project) projects.add(path.resolve(extra.project));
  // Record which hosts actually received managed assets so uninstall can tell
  // a clean "nothing registered" state apart from "registrations exist but
  // the engine is gone" (which must NOT silently orphan those files).
  const hosts = new Set(prior.hosts || []);
  if (Array.isArray(extra.hosts)) {
    for (const h of extra.hosts) hosts.add(h);
  }
  atomicJson(
    MANIFEST,
      {
        product: "aperio-next",
        version,
        created: prior.created || new Date().toISOString(),
        updated: new Date().toISOString(),
        projects: [...projects],
        hosts: [...hosts],
      }
  );
}

function ensureEngineShim() {
  fs.mkdirSync(BIN_DIR, { recursive: true });
  const engine = vengineBin();
  if (!fs.existsSync(engine)) return null;
  const shim = path.join(BIN_DIR, IS_WIN ? "aperio.cmd" : "aperio");
  if (IS_WIN) {
    fs.writeFileSync(shim, `@echo off\r\n"${engine}" %*\r\n`);
  } else {
    try {
      fs.unlinkSync(shim);
    } catch {}
    fs.symlinkSync(engine, shim);
    fs.chmodSync(shim, 0o755);
  }
  return shim;
}

// --------------------------------------------------------------------------
// engine bootstrap
// --------------------------------------------------------------------------

function bootstrapEngine({ quiet }) {
  const wheel = bundledWheel();
  if (!wheel) {
    fail(
      "no engine wheel bundled in this package (packaging defect: " +
        "engine/ is empty); report this at " +
        "https://github.com/SHAHID8142/aperio-next/issues"
    );
  }
  const py = findPython();
  if (!py) {
    fail(
      "Python 3.11 or newer is required but was not found. Install it " +
        "from https://www.python.org/downloads/ (or your package manager), " +
        "then re-run: aperio-next install"
    );
  }
  if (!quiet) process.stdout.write(`* Python runtime found (${py.cmd})\n`);

  if (!fs.existsSync(venvPython())) {
    const mk = run(py.cmd, py.args.concat(["-m", "venv", VENV_DIR]));
    if (!mk.ok) {
      fail(
        `could not create the engine environment at ${VENV_DIR}\n` +
          mk.stderr.trim() +
          "\n(On Debian/Ubuntu: sudo apt install python3-venv, then re-run)"
      );
    }
    if (!quiet)
      process.stdout.write(`* Engine environment created (${VENV_DIR})\n`);
  }

  const pip = run(venvPython(), [
    "-m",
    "pip",
    "install",
    "--no-index",
    "--no-deps",
    "--force-reinstall",
    wheel,
  ]);
  if (!pip.ok) {
    fail(`could not install the engine: ${pip.stderr.trim()}`);
  }
  if (!fs.existsSync(vengineBin())) {
    fail("engine installed but the aperio executable is missing (defect)");
  }
  const ver = run(vengineBin(), ["--version"]);
  if (!ver.ok) {
    fail(`engine executable did not run: ${ver.stderr.trim()}`);
  }
  if (!checkedIdentity()) fail("engine identity is not aperio-next");
  if (!quiet) process.stdout.write(`* Engine verified: ${ver.stdout.trim()}\n`);

  const shim = ensureEngineShim();
  if (shim && !quiet)
    process.stdout.write(`* Engine shim: ${shim}\n`);
  return ver.stdout.trim();
}

function engine(args, opts) {
  if (moduleEngine) return run(venvPython(), ["-I", "-m", "aperio", ...args], opts);
  return run(vengineBin(), args, opts);
}

// --------------------------------------------------------------------------
// commands
// --------------------------------------------------------------------------

function cmdInstall(argv) {
  const projectScope = argv.includes("--project");
  const dirIdx = argv.indexOf("--project");
  const projectDir =
    dirIdx >= 0 && argv[dirIdx + 1] && !argv[dirIdx + 1].startsWith("-")
      ? argv[dirIdx + 1]
      : null;
  if (projectScope && !projectDir) {
    fail("--project needs a project directory argument");
  }
  if (!readManifest()) writeManifest("installing");
  beginUpdate();
  const version = bootstrapEngine({ quiet: false });
  writeManifest(version.replace(/^aperio\s+/, ""), { project: projectDir });

  process.stdout.write("* Registering agent skills (documented host " +
    "locations only; nothing else is touched)...\n");
  const engineArgs = ["integrations", "install", "--json"];
  if (projectScope) {
    // Project scope also writes the durable Aperio Next pointer into the
    // managed project's AGENTS.md, so a fresh agent session in that project
    // recognizes it as Aperio-managed without the user re-explaining.
    engineArgs.push("--scope", "both", "--project-instructions", projectDir);
  }
  const res = engine(engineArgs);
  // The engine exits nonzero when any host reports an error, but still emits
  // the full JSON report. Parse it in both cases so partial success is shown
  // accurately; only treat truly unreadable output as a hard failure.
  let payload = null;
  try {
    payload = JSON.parse(res.stdout);
  } catch {
    payload = null;
  }
  if (!payload || !Array.isArray(payload.results)) {
    if (res.stderr) process.stderr.write(res.stderr);
    fail("skill registration failed; run 'aperio-next doctor' for details");
  }
  const installed = [];
  const skipped = [];
  const errors = [];
  if (!res.ok) errors.push("engine reported registration failure");
  if (payload.project_instructions?.errors?.length) errors.push(...payload.project_instructions.errors);
  const registeredHosts = [];
  for (const r of payload.results) {
    for (const p of r.installed) {
      installed.push(`${r.display}: ${p}`);
      if (!registeredHosts.includes(r.host_id)) registeredHosts.push(r.host_id);
    }
    for (const p of r.skipped) skipped.push(`${r.display}: ${p}`);
    for (const e of r.errors) errors.push(`${r.display}: ${e}`);
  }
  if (registeredHosts.length) {
    writeManifest(version.replace(/^aperio\s+/, ""), { hosts: registeredHosts });
  }

  process.stdout.write("\nAperio Next installation\n\n");
  process.stdout.write(`Engine: ${version.replace(/^aperio\s+/, "")} — installed and verified\n`);
  if (installed.length) {
    process.stdout.write("\nSkills registered:\n");
    for (const line of installed) process.stdout.write(`  + ${line}\n`);
  } else {
    process.stdout.write(
      "\nNo supported coding agents detected on this machine — " +
      "skills will be registered when one is installed (re-run " +
      "'aperio-next install' or 'aperio integrations install').\n"
    );
  }
  if (skipped.length) {
    process.stdout.write("\nDetected-but-pending (host dir/executable missing):\n");
    for (const line of skipped) process.stdout.write(`  - ${line}\n`);
  }
  if (errors.length) {
    process.stdout.write("\nErrors (existing non-apario files were NOT touched):\n");
    for (const line of errors) process.stdout.write(`  ! ${line}\n`);
  }
  process.stdout.write(
    "\nNext: open a supported coding agent and activate Aperio Next:\n" +
    "  ZCode/Claude Code/OpenCode/Gemini/Cursor: /aperio-next   " +
    "Codex: $aperio-next\n" +
    (IS_WIN
      ? `Engine command: ${path.join(BIN_DIR, "aperio.cmd")}\n`
      : `Engine command: aperio  (add to PATH if missing: export PATH="${BIN_DIR}:$PATH")\n`)
  );
  if (errors.length) fail(errors.join("; "));
  commitUpdate();
}

function cmdDoctor() {
  process.stdout.write("Aperio Next doctor\n\n");
  const py = findPython();
  process.stdout.write(
    `Python 3.11+: ${py ? "found (" + py.cmd + ")" : "NOT FOUND"}\n`
  );
  const manifest = readManifest();
  process.stdout.write(
    `Install root: ${APERIO_HOME} ` +
      (manifest ? `(v${manifest.version})` : "(not installed)") +
      "\n"
  );
  if (!fs.existsSync(vengineBin())) {
    process.stdout.write("Engine: NOT INSTALLED — run 'aperio-next install'\n");
    process.exit(1);
  }
  const ver = engine(["--version"]);
  process.stdout.write(
    `Engine: ${ver.ok ? ver.stdout.trim() : "BROKEN"}\n`
  );
  const integ = engine(["integrations", "status", "--json"]);
  if (!integ.ok) {
    process.stdout.write(`Integrations: could not read status\n${integ.stderr}`);
    process.exit(1);
  }
  const payload = JSON.parse(integ.stdout);
  process.stdout.write("\nHost integrations:\n");
  for (const row of payload.hosts) {
    const states = Object.values(row.files);
    const managed = states.filter((s) => s === "managed").length;
    const foreign = states.filter((s) => s === "foreign").length;
    const state = managed
      ? `registered (${managed} file${managed > 1 ? "s" : ""})`
      : foreign
        ? "conflict: foreign file occupies the aperio-next slot"
        : "not registered";
    process.stdout.write(
      `  ${row.display.padEnd(18)} ${row.host_detected ? "detected    " : "not present"}  ${state}\n`
    );
  }
  process.stdout.write(
    "\nActivation must be tested inside a live agent session " +
      "(open the agent and run /aperio-next or $aperio-next).\n"
  );
}

function cmdUpdate() {
  if (!readManifest()) {
    process.stdout.write("Not installed yet — running a fresh install.\n\n");
    return cmdInstall([]);
  }
  beginUpdate();
  const version = bootstrapEngine({ quiet: false });
  writeManifest(version.replace(/^aperio\s+/, ""));
  process.stdout.write("* Refreshing host skill registrations...\n");
  const res = engine(["integrations", "install", "--json"]);
  let payload = null;
  try {
    payload = JSON.parse(res.stdout);
  } catch {
    payload = null;
  }
  if (!res.ok || !payload || !Array.isArray(payload.results)) {
    if (res.stderr) process.stderr.write(res.stderr);
    fail("update incomplete: skill refresh failed; run 'aperio-next doctor'");
  }
  const errors = [];
  for (const r of payload.results) {
    for (const e of r.errors) errors.push(`${r.display}: ${e}`);
  }
  if (errors.length) {
    for (const line of errors) process.stderr.write(`  ! ${line}\n`);
    fail("update incomplete: one or more hosts reported errors");
  }
  for (const project of readManifest().projects || []) {
    if (!fs.existsSync(project)) continue;
    const status = checkedReport(["integrations", "status", "--scope", "project", "--json", project], "hosts");
    const registered = status.hosts.filter(host => Object.values(host.files).includes("managed"));
    if (!registered.length) continue;
    const args = ["integrations", "install", "--scope", "project", "--project-instructions", "--json", project];
    for (const host of registered) args.push("--host", host.host);
    const report = checkedReport(args, "results");
    if (report.results.some(r => r.errors.length) || report.project_instructions?.errors?.length) fail("project integration refresh failed");
  }
  commitUpdate();
  process.stdout.write(
    `Update complete: ${version.replace(/^aperio\s+/, "")}\n`
  );
}

function cmdUninstall() {
  const manifest = readManifest();
  if (!manifest || manifest.product !== "aperio-next") {
    process.stdout.write(
      `Nothing to uninstall at ${APERIO_HOME} (no aperio-next manifest).\n` +
        "Project data in your projects' .aperio/ folders is never stored " +
        "here and was not touched.\n"
    );
    return;
  }
  process.stdout.write("* Removing host skill assets (aperio-managed files only)...\n");
  let removeErrors = [];
  let engineRan = false;
  if (fs.existsSync(vengineBin())) {
    if (!checkedIdentity(false)) fail("engine identity could not be verified; installation KEPT for repair");
    const res = engine(["integrations", "remove", "--json"]);
    if (!res.ok) removeErrors.push(`engine removal failed (exit ${res.code})`);
    engineRan = true;
    let payload = null;
    try {
      payload = JSON.parse(res.stdout);
    } catch {
      payload = null;
    }
    if (payload && Array.isArray(payload.results)) {
      if (payload.ok === false) removeErrors.push("engine reported incomplete removal");
      for (const r of payload.results) {
        for (const p of r.removed) process.stdout.write(`  - ${r.display}: ${p}\n`);
        for (const p of r.kept)
          process.stdout.write(`  = ${r.display}: kept (foreign content): ${p}\n`);
        for (const e of r.errors) {
          process.stdout.write(`  ! ${r.display}: ${e}\n`);
          removeErrors.push(`${r.display}: ${e}`);
        }
      }
    } else {
      // Broken engine OR unparseable output. Never treat this as success:
      // keep the install root so the registered paths still resolve and the
      // user can repair with 'aperio-next update'.
      const detail = res.stderr.trim() || res.stdout.trim() || "no output";
      removeErrors.push(
        `engine did not return a removal report (exit ${res.code}): ${detail.slice(0, 300)}`
      );
    }
    // Clean project-scope assets for every project this install registered.
    // This removes project skills and the AGENTS.md pointer only — never the
    // project's .aperio/ data, which is the durable state.
    for (const proj of manifest.projects || []) {
      if (!fs.existsSync(proj) || !fs.existsSync(path.join(proj, ".aperio"))) {
        continue;
      }
      const pr = engine([
        "integrations", "remove", "--scope", "project",
        "--project-instructions", "--json", proj,
      ]);
      if (!pr.ok) removeErrors.push(`project cleanup failed for ${proj} (exit ${pr.code})`);
      let ppayload = null;
      try {
        ppayload = JSON.parse(pr.stdout);
      } catch {
        ppayload = null;
      }
      if (ppayload && Array.isArray(ppayload.results)) {
        if (ppayload.ok === false) removeErrors.push(`project cleanup incomplete for ${proj}`);
        for (const r of ppayload.results) {
          for (const p of r.removed)
            process.stdout.write(`  - ${r.display} (project): ${p}\n`);
          for (const e of r.errors) {
            process.stdout.write(`  ! ${r.display} (project): ${e}\n`);
            removeErrors.push(`${r.display} (project): ${e}`);
          }
        }
        const pi = ppayload.project_instructions;
        if (pi && pi.errors && pi.errors.length) {
          for (const e of pi.errors) {
            process.stdout.write(`  ! project instructions: ${e}\n`);
            removeErrors.push(`project instructions: ${e}`);
          }
        }
      } else if (!pr.ok || !ppayload) {
        const detail = pr.stderr.trim() || pr.stdout.trim() || "no output";
        removeErrors.push(
          `project cleanup failed for ${proj} (exit ${pr.code}): ${detail.slice(0, 300)}`
        );
      }
    }
  }
  if (removeErrors.length) {
    // Keep the engine so registered skills still resolve. Tell the user what
    // to do; never claim the removal succeeded.
    process.stderr.write(
      "\naperio-next: could not remove all host skill assets:\n" +
        removeErrors.map((e) => `  ! ${e}\n`).join("") +
        `\nThe engine at ${APERIO_HOME} was KEPT so existing registrations ` +
        "still resolve. Re-run 'aperio-next uninstall' to retry.\n"
    );
    process.exit(1);
  }
  if (!engineRan) {
    // The engine executable is gone, but the manifest shows hosts that DID
    // receive managed assets. Deleting the install root would leave those
    // skill/command files registered and pointing at a vanished engine.
    // Refuse the destructive step and give repair instructions instead.
    if (Array.isArray(manifest.hosts) && manifest.hosts.length) {
      process.stderr.write(
        "\naperio-next: the engine executable is missing, so registered " +
          "host assets cannot be removed safely.\n" +
          `Registered hosts still on disk: ${manifest.hosts.join(", ")}\n` +
          `The engine at ${APERIO_HOME} was KEPT so nothing is half-removed.\n` +
          "Repair and retry either way:\n" +
          "  aperio-next update && aperio-next uninstall   (restore engine, then remove)\n" +
          "  or delete the aperio-next skill/command files in those hosts, " +
          "then re-run 'aperio-next uninstall'.\n"
      );
      process.exit(1);
    }
    process.stdout.write(
      "  engine executable missing and no host assets were registered; " +
        "safe to remove the install root\n"
    );
  }
  // Guard the destructive step: only delete APERIO_HOME when it carries our
  // manifest and looks like our own root.
  const realHome = fs.realpathSync(APERIO_HOME);
  if (realHome === path.parse(realHome).root) {
    fail(`refusing to delete filesystem root ${realHome}`);
  }
  fs.rmSync(APERIO_HOME, { recursive: true, force: true });
  process.stdout.write(
    `Removed ${APERIO_HOME}.\n` +
      "Your projects' .aperio/ data was never stored here and is intact.\n" +
      "To also remove project management from one project, run 'aperio " +
      "integrations remove --scope project <dir>' BEFORE deleting that " +
      "engine (optional; harmless if skipped).\n"
  );
}

// --------------------------------------------------------------------------

const COMMANDS = {
  install: cmdInstall,
  doctor: cmdDoctor,
  update: cmdUpdate,
  uninstall: cmdUninstall,
};

// Exposed for the Node test suite (test/launcher.test.js). Harmless at
// runtime: the CLI branch below only runs when this file is the entry point.
module.exports = {
  APERIO_HOME,
  VENV_DIR,
  BIN_DIR,
  MANIFEST,
  ENGINE_DIR,
  findPython,
  pythonCandidates,
  bundledWheel,
  vengineBin,
  venvPython,
  readManifest,
  writeManifest,
  run,
  COMMANDS,
  main: (argv) => {
    const [command, ...rest] = argv;
    if (!command || command === "help" || command === "--help" || command === "-h") {
      process.stdout.write(
        "aperio-next — one-line installer for the Aperio Next engine\n\n" +
          "Usage:\n" +
          "  aperio-next install [--project <dir>]   install engine + register agent skills\n" +
          "  aperio-next doctor                      health report (engine, python, hosts)\n" +
          "  aperio-next update                      refresh engine + skill assets\n" +
          "  aperio-next uninstall                   remove skills + engine (project .aperio/ data is kept)\n"
      );
      process.exit(command ? 0 : 2);
    }
    const fn = COMMANDS[command];
    if (!fn) fail(`unknown command: ${command} (try: install, doctor, update, uninstall)`);
    try {
      validateInstallRoot();
      recoverUpdate();
      fn(rest);
    } catch (error) {
      fail(error.message);
    }
  },
};

if (require.main === module) {
  module.exports.main(process.argv.slice(2));
}
