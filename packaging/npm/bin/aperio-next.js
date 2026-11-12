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

function fail(msg) {
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
  fs.writeFileSync(
    MANIFEST,
    JSON.stringify(
      {
        product: "aperio-next",
        version,
        created: prior.created || new Date().toISOString(),
        updated: new Date().toISOString(),
        projects: [...projects],
        hosts: [...hosts],
      },
      null,
      2
    )
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
  if (!quiet) process.stdout.write(`* Engine verified: ${ver.stdout.trim()}\n`);

  const shim = ensureEngineShim();
  if (shim && !quiet)
    process.stdout.write(`* Engine shim: ${shim}\n`);
  return ver.stdout.trim();
}

function engine(args, opts) {
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
  process.exit(errors.length ? 1 : 0);
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
    const res = engine(["integrations", "remove", "--json"]);
    engineRan = true;
    let payload = null;
    try {
      payload = JSON.parse(res.stdout);
    } catch {
      payload = null;
    }
    if (payload && Array.isArray(payload.results)) {
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
      let ppayload = null;
      try {
        ppayload = JSON.parse(pr.stdout);
      } catch {
        ppayload = null;
      }
      if (ppayload && Array.isArray(ppayload.results)) {
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
    fn(rest);
  },
};

if (require.main === module) {
  module.exports.main(process.argv.slice(2));
}
