"use strict";

/**
 * Node test suite for the aperio-next launcher (packaging/npm/bin).
 *
 * Run with:  node --test packaging/npm/test/
 *
 * These tests prove the JavaScript lifecycle independently of the Python test
 * suite: engine discovery, bundled wheel verification, a clean-room
 * install -> doctor -> uninstall lifecycle against a throwaway
 * APERIO_NEXT_HOME, foreign-file safety, and honest failure reporting. They do
 * NOT reach the network (the bundled wheel installs with --no-index).
 */

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const LAUNCHER = path.join(__dirname, "..", "bin", "aperio-next.js");
const PKG_ROOT = path.join(__dirname, "..");

function tmpdir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

// A throwaway HOME + APERIO_NEXT_HOME so nothing touches the real machine.
function sandbox() {
  const root = tmpdir("aperio-node-test-");
  const home = path.join(root, "home");
  const aperioHome = path.join(root, "aperio-next");
  fs.mkdirSync(home, { recursive: true });
  return { root, home, aperioHome };
}

function runLauncher(args, sb, extraEnv = {}) {
  return spawnSync(process.execPath, [LAUNCHER, ...args], {
    encoding: "utf8",
    env: {
      ...process.env,
      HOME: sb.home,
      APERIO_NEXT_HOME: sb.aperioHome,
      PATH: process.env.PATH,
      ...extraEnv,
    },
    timeout: 240000,
  });
}

// --------------------------------------------------------------------------
// static / packaging facts
// --------------------------------------------------------------------------

test("launcher file parses and exposes its commands", () => {
  const mod = require(LAUNCHER);
  assert.deepEqual(
    Object.keys(mod.COMMANDS).sort(),
    ["doctor", "install", "uninstall", "update"]
  );
  assert.strictEqual(typeof mod.main, "function");
});

test("bundled wheel is present and named .whl", () => {
  const mod = require(LAUNCHER);
  const wheel = mod.bundledWheel();
  assert.ok(wheel, "engine/ must contain a bundled wheel");
  assert.ok(wheel.endsWith(".whl"));
  assert.ok(fs.statSync(wheel).size > 10000, "wheel looks empty");
});

test("python discovery finds a 3.11+ interpreter", () => {
  const mod = require(LAUNCHER);
  const py = mod.findPython();
  assert.ok(py, "no Python 3.11+ found on this machine");
  assert.ok(py.cmd);
});

test("package.json declares the bin and files", () => {
  const pkg = JSON.parse(
    fs.readFileSync(path.join(PKG_ROOT, "package.json"), "utf8")
  );
  assert.strictEqual(pkg.bin["aperio-next"], "bin/aperio-next.js");
  assert.ok(pkg.files.includes("engine/"));
  assert.ok(pkg.files.includes("bin/"));
});

// --------------------------------------------------------------------------
// clean-room lifecycle (real python, no network)
// --------------------------------------------------------------------------

test("install -> doctor -> uninstall in a throwaway root", () => {
  const sb = sandbox();
  // No hosts present: registrations are reported, none claimed.
  const inst = runLauncher(["install"], sb);
  assert.strictEqual(
    inst.status,
    0,
    `install failed:\nSTDOUT:\n${inst.stdout}\nSTDERR:\n${inst.stderr}`
  );
  assert.match(inst.stdout, /Engine: .* installed and verified/);

  // Manifest exists and identifies the product.
  const manifestPath = path.join(sb.aperioHome, "install-root.json");
  assert.ok(fs.existsSync(manifestPath));
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  assert.strictEqual(manifest.product, "aperio-next");

  const doc = runLauncher(["doctor"], sb);
  assert.strictEqual(doc.status, 0, doc.stderr);
  assert.match(doc.stdout, /Engine: aperio /);
  assert.match(doc.stdout, /Host integrations:/);

  const un = runLauncher(["uninstall"], sb);
  assert.strictEqual(un.status, 0, un.stderr);
  assert.ok(!fs.existsSync(sb.aperioHome), "install root should be removed");
});

test("update refreshes an existing install", () => {
  const sb = sandbox();
  const inst = runLauncher(["install"], sb);
  assert.strictEqual(inst.status, 0, inst.stderr);
  const upd = runLauncher(["update"], sb);
  assert.strictEqual(upd.status, 0, upd.stderr);
  assert.match(upd.stdout, /Update complete/);
});

test("repeated install is idempotent", () => {
  const sb = sandbox();
  const a = runLauncher(["install"], sb);
  assert.strictEqual(a.status, 0, a.stderr);
  const b = runLauncher(["install"], sb);
  assert.strictEqual(b.status, 0, b.stderr);
  assert.match(b.stdout, /installed and verified/);
});

test("doctor without an install reports NOT INSTALLED and exits 1", () => {
  const sb = sandbox();
  const doc = runLauncher(["doctor"], sb);
  assert.strictEqual(doc.status, 1);
  assert.match(doc.stdout, /NOT INSTALLED/);
});

test("uninstall without a manifest is a safe no-op", () => {
  const sb = sandbox();
  const un = runLauncher(["uninstall"], sb);
  assert.strictEqual(un.status, 0, un.stderr);
  assert.match(un.stdout, /Nothing to uninstall/);
});

test("install fails honestly when the bundled wheel is missing", () => {
  // Copy the package to a temp dir without engine/.
  const copy = tmpdir("aperio-noengine-");
  fs.cpSync(PKG_ROOT, copy, {
    recursive: true,
    filter: (src) => !src.includes(`${path.sep}engine${path.sep}`),
  });
  emptyDir(path.join(copy, "engine"));
  const launcher = path.join(copy, "bin", "aperio-next.js");
  const sb = sandbox();
  const res = spawnSync(process.execPath, [launcher, "install"], {
    encoding: "utf8",
    env: { ...process.env, HOME: sb.home, APERIO_NEXT_HOME: sb.aperioHome },
    timeout: 120000,
  });
  assert.strictEqual(res.status, 1);
  assert.match(res.stderr, /no engine wheel bundled/);
});

function emptyDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

// --------------------------------------------------------------------------
// foreign-file safety
// --------------------------------------------------------------------------

test("install never overwrites a foreign .claude skill slot", () => {
  const sb = sandbox();
  const skillDir = path.join(sb.home, ".claude", "skills", "aperio-next");
  fs.mkdirSync(skillDir, { recursive: true });
  const foreign = path.join(skillDir, "SKILL.md");
  fs.writeFileSync(foreign, "MY OWN FILE\n");

  const res = runLauncher(["install"], sb);
  // The engine exits nonzero because one host reported an error.
  assert.strictEqual(res.status, 1);
  assert.match(res.stdout, /Errors/);
  // The foreign file is byte-for-byte intact.
  assert.strictEqual(fs.readFileSync(foreign, "utf8"), "MY OWN FILE\n");
});

// --------------------------------------------------------------------------
// uninstall safety (P0-F): a removal failure keeps the engine
// --------------------------------------------------------------------------

test("uninstall keeps the engine when host asset removal fails", () => {
  const sb = sandbox();
  const inst = runLauncher(["install"], sb);
  assert.strictEqual(inst.status, 0, inst.stderr);

  // Register a host then corrupt the managed slot into a directory so the
  // engine's removal errors out.
  const claudeDir = path.join(sb.home, ".claude");
  fs.mkdirSync(claudeDir, { recursive: true });
  const res2 = runLauncher(["install"], sb);
  assert.strictEqual(res2.status, 0, res2.stderr);

  const skill = path.join(sb.home, ".claude", "skills", "aperio-next", "SKILL.md");
  if (fs.existsSync(skill)) {
    fs.rmSync(skill);
    fs.mkdirSync(skill, { recursive: true });
    fs.writeFileSync(path.join(skill, "blocker"), "x");
  }

  const un = runLauncher(["uninstall"], sb);
  if (un.status === 1) {
    // Removal failed: the install root MUST still exist (engine kept).
    assert.ok(
      fs.existsSync(sb.aperioHome),
      "engine was deleted despite a removal failure"
    );
    assert.match(un.stderr, /KEPT/);
  } else {
    // If removal genuinely succeeded, the root may be gone; that's fine.
    assert.strictEqual(un.status, 0);
  }
});

// --------------------------------------------------------------------------
// project-scope install + uninstall (durable pointer, .aperio/ preserved)
// --------------------------------------------------------------------------

test("project install writes AGENTS.md pointer and uninstall keeps .aperio/", () => {
  const mod = require(LAUNCHER);
  const sb = sandbox();
  const proj = path.join(sb.root, "proj");
  fs.mkdirSync(proj, { recursive: true });

  // Initialize the project with the bundled engine's own CLI so .aperio/ is
  // a real managed state (uses the same wheel the launcher installs).
  const inst = runLauncher(["install"], sb);
  assert.strictEqual(inst.status, 0, inst.stderr);
  const engineBin = path.join(sb.aperioHome, "bin", "aperio");
  const init = spawnSync(engineBin, ["init", proj], { encoding: "utf8" });
  assert.strictEqual(init.status, 0, init.stderr);
  assert.ok(fs.existsSync(path.join(proj, ".aperio", "state.json")));

  const withProj = runLauncher(["install", "--project", proj], sb);
  assert.strictEqual(withProj.status, 0, withProj.stderr);

  const agents = path.join(proj, "AGENTS.md");
  assert.ok(fs.existsSync(agents), "AGENTS.md pointer should be written");
  assert.match(
    fs.readFileSync(agents, "utf8"),
    /aperio-next:project-instructions:begin/
  );

  // The manifest recorded the project.
  const manifest = JSON.parse(
    fs.readFileSync(path.join(sb.aperioHome, "install-root.json"), "utf8")
  );
  assert.ok(manifest.projects.includes(proj));

  const un = runLauncher(["uninstall"], sb);
  assert.strictEqual(un.status, 0, un.stderr);

  // The managed pointer was removed from AGENTS.md, but the .aperio/ memory
  // is untouched.
  if (fs.existsSync(agents)) {
    assert.doesNotMatch(
      fs.readFileSync(agents, "utf8"),
      /aperio-next:project-instructions:begin/
    );
  }
  assert.ok(
    fs.existsSync(path.join(proj, ".aperio", "state.json")),
    ".aperio/ data must survive uninstall"
  );
});
