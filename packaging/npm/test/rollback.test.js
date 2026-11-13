"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const LAUNCHER = path.resolve(__dirname, "../bin/aperio-next.js");

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "aperio-rollback-"));
  const home = path.join(root, "home");
  const install = path.join(root, "engine");
  fs.mkdirSync(path.join(home, ".claude"), { recursive: true });
  fs.mkdirSync(path.join(home, ".config", "opencode"), { recursive: true });
  const env = { ...process.env, HOME: home, APERIO_NEXT_HOME: install };
  delete env.NODE_OPTIONS;
  const run = (args, extra = {}) => spawnSync(process.execPath, [LAUNCHER, ...args], {
    env: { ...env, ...extra }, encoding: "utf8", timeout: 120000,
  });
  const first = run(["install"]);
  assert.equal(first.status, 0, first.stderr);
  const skill = path.join(home, ".claude", "skills", "aperio-next", "SKILL.md");
  const command = path.join(home, ".config", "opencode", "commands", "aperio-next.md");
  assert.ok(fs.existsSync(skill));
  assert.ok(fs.existsSync(command));
  const before = new Map([skill, command].map(file => [file, fs.readFileSync(file)]));
  const projectData = path.join(root, "project", ".aperio", "state.json");
  fs.mkdirSync(path.dirname(projectData), { recursive: true });
  fs.writeFileSync(projectData, "fixture project data must survive\n");
  const unrelated = path.join(home, ".claude", "user-settings.json");
  fs.writeFileSync(unrelated, "unrelated user configuration\n");
  const touched = path.join(root, "failure-triggered");
  const preload = path.join(root, "inject.cjs");
  fs.writeFileSync(preload, `
const cp = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const original = cp.spawnSync;
const originalRename = fs.renameSync;
const originalRemove = fs.rmSync;
fs.renameSync = function(source, target) {
  const result = originalRename.call(this, source, target);
  if (process.env.ROLLBACK_TEST_MODE === 'manifest-interrupted' && target === path.join(process.env.APERIO_NEXT_HOME, 'install-root.json')) {
    fs.writeFileSync(process.env.ROLLBACK_TEST_TRIGGER, 'manifest-interrupted');
    fs.writeFileSync(target, '{interrupted manifest fixture');
    process.kill(process.pid, 'SIGKILL');
  }
  return result;
};
fs.rmSync = function(target, options) {
  if (['cleanup-interrupted', 'rollback-cleanup-interrupted'].includes(process.env.ROLLBACK_TEST_MODE) && target === path.join(process.env.APERIO_NEXT_HOME, '.update-backup')) {
    fs.writeFileSync(process.env.ROLLBACK_TEST_TRIGGER, process.env.ROLLBACK_TEST_MODE);
    originalRemove.call(this, path.join(target, 'bin'), { recursive: true, force: true });
    process.kill(process.pid, 'SIGKILL');
  }
  return originalRemove.call(this, target, options);
};
cp.spawnSync = function(command, args, options) {
  const mode = process.env.ROLLBACK_TEST_MODE;
  const isPip = args.includes('pip') && args.includes('install');
  const isRegistration = args[0] === 'integrations' && args[1] === 'install';
  const isRemoval = args[0] === 'integrations' && args[1] === 'remove';
  if (isRemoval && (mode === 'remove-host-exit' || (mode === 'remove-project-exit' && args.includes('project')))) {
    fs.writeFileSync(process.env.ROLLBACK_TEST_TRIGGER, mode);
    return { status: 1, stdout: JSON.stringify({ results: [], ok: false }), stderr: 'injected removal failure with valid JSON' };
  }
  const result = original.call(this, command, args, options);
  if (mode === 'rollback-cleanup-interrupted' && isPip) {
    fs.writeFileSync(process.env.ROLLBACK_TEST_TRIGGER + '.pip', mode);
    return { status: 1, stdout: '', stderr: 'injected installation failure before rollback cleanup interruption' };
  }
  if (mode === 'foreign-created' && isPip) {
    fs.writeFileSync(process.env.ROLLBACK_TEST_TRIGGER, mode);
    fs.mkdirSync(path.dirname(process.env.ROLLBACK_TEST_ASSET), { recursive: true });
    fs.writeFileSync(process.env.ROLLBACK_TEST_ASSET, 'Concurrent foreign skill must survive.\\n');
    return { status: 1, stdout: '', stderr: 'injected failure after foreign file creation' };
  }
  if (mode === 'concurrent-edit' && isPip) {
    fs.writeFileSync(process.env.ROLLBACK_TEST_TRIGGER, mode);
    fs.appendFileSync(process.env.ROLLBACK_TEST_ASSET, '\\nConcurrent user instruction must survive.\\n');
    return { status: 1, stdout: '', stderr: 'injected failure after concurrent user edit' };
  }
  if ((mode === 'engine' || mode === 'interrupted') && isPip) {
    fs.writeFileSync(process.env.ROLLBACK_TEST_TRIGGER, mode);
    const executable = path.join(path.dirname(command), process.platform === 'win32' ? 'aperio.exe' : 'aperio');
    fs.rmSync(executable, { force: true });
    if (mode === 'interrupted') process.kill(process.pid, 'SIGKILL');
    return { status: 1, stdout: '', stderr: 'injected engine installation failure' };
  }
  if ((mode === 'regeneration' || mode === 'partial-host') && isRegistration) {
    fs.writeFileSync(process.env.ROLLBACK_TEST_TRIGGER, mode);
    fs.writeFileSync(process.env.ROLLBACK_TEST_ASSET, 'injected partial regeneration\\n');
    if (mode === 'regeneration') return { status: 1, stdout: '', stderr: 'injected skill regeneration failure' };
    return { status: 1, stderr: 'injected host-registration failure', stdout: JSON.stringify({
      results: [{ host_id: 'claude-code', display: 'Claude Code', installed: [process.env.ROLLBACK_TEST_ASSET], skipped: [], errors: [] },
        { host_id: 'opencode', display: 'OpenCode', installed: [], skipped: [], errors: ['injected partial host failure'] }]
    }) };
  }
  if (mode === 'new-host' && isRegistration) {
    fs.writeFileSync(process.env.ROLLBACK_TEST_TRIGGER, mode);
    if (!fs.existsSync(process.env.ROLLBACK_TEST_ASSET)) throw new Error('new host registration did not happen');
    return { status: 1, stdout: result.stdout, stderr: 'injected post-registration failure' };
  }
  if (mode === 'project-pointer' && isRegistration) {
    fs.writeFileSync(process.env.ROLLBACK_TEST_TRIGGER, mode);
    const report = JSON.parse(result.stdout);
    report.project_instructions = { errors: ['injected pointer write failure'], written: [] };
    return { status: 0, stdout: JSON.stringify(report), stderr: '' };
  }
  return result;
};
`);
  return { root, run, env, skill, before, projectData, unrelated, touched, preload, install };
}

function managedProject(f) {
  const project = path.join(f.root, "managed-project");
  fs.mkdirSync(project);
  const executable = path.join(f.install, "bin", process.platform === "win32" ? "aperio.cmd" : "aperio");
  const initialized = spawnSync(executable, ["init", project], {
    env: f.env, encoding: "utf8", timeout: 10000,
  });
  assert.equal(initialized.status, 0, initialized.stderr);
  const installed = f.run(["install", "--project", project]);
  assert.equal(installed.status, 0, installed.stderr);
  return { project, executable, agents: path.join(project, "AGENTS.md") };
}

for (const mode of ["remove-host-exit", "remove-project-exit"]) {
  test(`uninstall preserves engine when removal exits nonzero with valid JSON: ${mode}`, () => {
    const f = fixture();
    if (mode === "remove-project-exit") managedProject(f);
    const failed = f.run(["uninstall"], {
      NODE_OPTIONS: `--require=${f.preload}`,
      ROLLBACK_TEST_MODE: mode,
      ROLLBACK_TEST_TRIGGER: f.touched,
    });
    assert.equal(fs.readFileSync(f.touched, "utf8"), mode, "removal failure must be triggered");
    assert.notEqual(failed.status, 0, "nonzero child exit cannot be ignored because JSON parsed");
    assert.ok(fs.existsSync(path.join(f.install, "install-root.json")), "failed uninstall must preserve engine root");
    assert.equal(f.run(["doctor"]).status, 0);
    fs.rmSync(f.root, { recursive: true, force: true });
  });
}

test("rollback preserves concurrent user edits to project instructions", () => {
  const f = fixture();
  const project = managedProject(f);
  const prior = fs.readFileSync(project.agents, "utf8");
  const failed = f.run(["update"], {
    NODE_OPTIONS: `--require=${f.preload}`,
    ROLLBACK_TEST_MODE: "concurrent-edit",
    ROLLBACK_TEST_TRIGGER: f.touched,
    ROLLBACK_TEST_ASSET: project.agents,
  });
  assert.equal(fs.readFileSync(f.touched, "utf8"), "concurrent-edit");
  assert.notEqual(failed.status, 0);
  assert.equal(fs.readFileSync(project.agents, "utf8"), prior + "\nConcurrent user instruction must survive.\n");
  const identity = spawnSync(project.executable, ["core-identity", "--json"], {
    env: f.env, encoding: "utf8", timeout: 10000,
  });
  assert.equal(identity.status, 0);
  assert.equal(JSON.parse(identity.stdout).product, "aperio-next");
  fs.rmSync(f.root, { recursive: true, force: true });
});

test("rollback removes managed assets first created during a failed update", () => {
  const f = fixture();
  fs.mkdirSync(path.join(f.env.HOME, ".cursor"));
  const newAsset = path.join(f.env.HOME, ".cursor", "skills", "aperio-next", "SKILL.md");
  assert.equal(fs.existsSync(newAsset), false);
  const failed = f.run(["update"], {
    NODE_OPTIONS: `--require=${f.preload}`,
    ROLLBACK_TEST_MODE: "new-host",
    ROLLBACK_TEST_TRIGGER: f.touched,
    ROLLBACK_TEST_ASSET: newAsset,
  });
  assert.equal(fs.readFileSync(f.touched, "utf8"), "new-host");
  assert.notEqual(failed.status, 0);
  assert.equal(fs.existsSync(newAsset), false, "new registration must not survive failed update");
  for (const [file, bytes] of f.before) assert.ok(fs.readFileSync(file).equals(bytes));
  fs.rmSync(f.root, { recursive: true, force: true });
});

test("project pointer errors make install fail even when engine report exits zero", () => {
  const f = fixture();
  const failed = f.run(["install"], {
    NODE_OPTIONS: `--require=${f.preload}`,
    ROLLBACK_TEST_MODE: "project-pointer",
    ROLLBACK_TEST_TRIGGER: f.touched,
  });
  assert.equal(fs.readFileSync(f.touched, "utf8"), "project-pointer");
  assert.notEqual(failed.status, 0, "required project pointer failure must aggregate to nonzero");
  assert.match(failed.stderr + failed.stdout, /pointer write failure/);
  fs.rmSync(f.root, { recursive: true, force: true });
});

test("failed fresh engine installation can be repaired by retrying install", () => {
  const f = fixture();
  assert.equal(f.run(["uninstall"]).status, 0);
  assert.equal(fs.existsSync(f.install), false);
  const failed = f.run(["install"], {
    NODE_OPTIONS: `--require=${f.preload}`,
    ROLLBACK_TEST_MODE: "engine",
    ROLLBACK_TEST_TRIGGER: f.touched,
  });
  assert.equal(fs.readFileSync(f.touched, "utf8"), "engine");
  assert.notEqual(failed.status, 0);
  const retry = f.run(["install"]);
  assert.equal(retry.status, 0, `retry must repair owned partial installation: ${retry.stderr}`);
  assert.equal(f.run(["doctor"]).status, 0);
  assert.equal(fs.readFileSync(f.projectData, "utf8"), "fixture project data must survive\n");
  assert.equal(fs.readFileSync(f.unrelated, "utf8"), "unrelated user configuration\n");
  fs.rmSync(f.root, { recursive: true, force: true });
});

test("install refuses foreign and symlinked roots without changing their content", () => {
  const f = fixture();
  const foreign = path.join(f.root, "foreign");
  fs.mkdirSync(foreign);
  const sentinel = path.join(foreign, "user-data.txt");
  fs.writeFileSync(sentinel, "unrelated foreign root\n");
  const refused = f.run(["install"], { APERIO_NEXT_HOME: foreign });
  assert.notEqual(refused.status, 0);
  assert.equal(fs.readFileSync(sentinel, "utf8"), "unrelated foreign root\n");
  assert.deepEqual(fs.readdirSync(foreign), ["user-data.txt"]);
  const linked = path.join(f.root, "linked");
  fs.symlinkSync(f.install, linked, process.platform === "win32" ? "junction" : "dir");
  const manifest = fs.readFileSync(path.join(f.install, "install-root.json"));
  const symlinkRefused = f.run(["update"], { APERIO_NEXT_HOME: linked });
  assert.notEqual(symlinkRefused.status, 0);
  assert.ok(fs.readFileSync(path.join(f.install, "install-root.json")).equals(manifest));
  assert.equal(f.run(["doctor"]).status, 0);
  fs.rmSync(f.root, { recursive: true, force: true });
});

for (const mode of ["engine", "regeneration", "partial-host", "interrupted"]) {
  test(`failed update preserves or restores last known-good installation: ${mode}`, () => {
    const f = fixture();
    const failed = f.run(["update"], {
      NODE_OPTIONS: `--require=${f.preload}`,
      ROLLBACK_TEST_MODE: mode,
      ROLLBACK_TEST_TRIGGER: f.touched,
      ROLLBACK_TEST_ASSET: f.skill,
    });
    assert.equal(fs.readFileSync(f.touched, "utf8"), mode, "failure injection must actually run");
    assert.notEqual(failed.status, 0, "failed update must not report success");
    if (mode === "interrupted") {
      assert.equal(failed.signal, "SIGKILL", "launcher must really be interrupted");
      const recovered = f.run(["doctor"]);
      assert.equal(recovered.status, 0, `interrupted update must recover on next invocation: ${recovered.stderr}`);
    }
    const executable = path.join(f.install, "bin", process.platform === "win32" ? "aperio.cmd" : "aperio");
    const identity = spawnSync(executable, ["core-identity", "--json"], {
      env: f.env, encoding: "utf8", timeout: 10000,
    });
    assert.equal(identity.status, 0, `last known-good engine must still run: ${identity.stderr}`);
    assert.equal(JSON.parse(identity.stdout).product, "aperio-next");
    for (const [file, bytes] of f.before) {
      assert.ok(fs.readFileSync(file).equals(bytes), `compatible integration must be restored byte-for-byte: ${file}`);
    }
    assert.equal(fs.readFileSync(f.projectData, "utf8"), "fixture project data must survive\n");
    assert.equal(fs.readFileSync(f.unrelated, "utf8"), "unrelated user configuration\n");
    const doctor = f.run(["doctor"]);
    assert.equal(doctor.status, 0, doctor.stderr);
    fs.rmSync(f.root, { recursive: true, force: true });
  });
}

for (const broken of ["missing", "corrupt"]) {
  test(`update repairs a ${broken} executable using the owned verified engine module`, () => {
    const f = fixture();
    const executable = path.join(f.install, "venv", process.platform === "win32" ? "Scripts" : "bin", process.platform === "win32" ? "aperio.exe" : "aperio");
    assert.ok(fs.existsSync(executable));
    if (broken === "missing") fs.unlinkSync(executable);
    else fs.writeFileSync(executable, "corrupted executable fixture\n");
    const repaired = f.run(["update"]);
    assert.equal(repaired.status, 0, `owned engine module should permit repair: ${repaired.stderr}`);
    assert.equal(f.run(["doctor"]).status, 0);
    const shim = path.join(f.install, "bin", process.platform === "win32" ? "aperio.cmd" : "aperio");
    const identity = spawnSync(shim, ["core-identity", "--json"], {
      env: f.env, encoding: "utf8", timeout: 10000,
    });
    assert.equal(identity.status, 0);
    assert.equal(JSON.parse(identity.stdout).product, "aperio-next");
    for (const [file, bytes] of f.before) assert.ok(fs.readFileSync(file).equals(bytes));
    assert.equal(fs.readFileSync(f.projectData, "utf8"), "fixture project data must survive\n");
    assert.equal(fs.readFileSync(f.unrelated, "utf8"), "unrelated user configuration\n");
    fs.rmSync(f.root, { recursive: true, force: true });
  });
}

for (const mode of ["manifest-interrupted", "cleanup-interrupted", "rollback-cleanup-interrupted"]) {
  test(`next invocation safely recovers after ${mode}`, () => {
    const f = fixture();
    const interrupted = f.run(["update"], {
      NODE_OPTIONS: `--require=${f.preload}`,
      ROLLBACK_TEST_MODE: mode,
      ROLLBACK_TEST_TRIGGER: f.touched,
    });
    assert.equal(fs.readFileSync(f.touched, "utf8"), mode, "interruption boundary must be reached");
    assert.equal(interrupted.signal, "SIGKILL", "update must genuinely be killed");
    if (mode === "rollback-cleanup-interrupted") {
      assert.equal(fs.readFileSync(f.touched + ".pip", "utf8"), mode, "rollback must follow an actual injected install failure");
    }
    const recovered = f.run(["doctor"]);
    assert.equal(recovered.status, 0, `next invocation must recover safely: ${recovered.stderr}`);
    const executable = path.join(f.install, "bin", process.platform === "win32" ? "aperio.cmd" : "aperio");
    const identity = spawnSync(executable, ["core-identity", "--json"], {
      env: f.env, encoding: "utf8", timeout: 10000,
    });
    assert.equal(identity.status, 0, "working engine must survive recovery");
    assert.equal(JSON.parse(identity.stdout).product, "aperio-next");
    assert.equal(JSON.parse(fs.readFileSync(path.join(f.install, "install-root.json"), "utf8")).product, "aperio-next");
    for (const [file, bytes] of f.before) assert.ok(fs.readFileSync(file).equals(bytes));
    assert.equal(fs.readFileSync(f.projectData, "utf8"), "fixture project data must survive\n");
    assert.equal(fs.readFileSync(f.unrelated, "utf8"), "unrelated user configuration\n");
    assert.equal(fs.existsSync(path.join(f.install, ".update-backup")), false);
    assert.equal(fs.existsSync(path.join(f.install, ".update-committed")), false);
    fs.rmSync(f.root, { recursive: true, force: true });
  });
}

test("rollback preserves foreign content created at a previously absent host path", () => {
  const f = fixture();
  fs.mkdirSync(path.join(f.env.HOME, ".cursor"));
  const foreign = path.join(f.env.HOME, ".cursor", "skills", "aperio-next", "SKILL.md");
  assert.equal(fs.existsSync(foreign), false);
  const failed = f.run(["update"], {
    NODE_OPTIONS: `--require=${f.preload}`,
    ROLLBACK_TEST_MODE: "foreign-created",
    ROLLBACK_TEST_TRIGGER: f.touched,
    ROLLBACK_TEST_ASSET: foreign,
  });
  assert.equal(fs.readFileSync(f.touched, "utf8"), "foreign-created");
  assert.notEqual(failed.status, 0);
  assert.equal(fs.readFileSync(foreign, "utf8"), "Concurrent foreign skill must survive.\n");
  for (const [file, bytes] of f.before) assert.ok(fs.readFileSync(file).equals(bytes));
  assert.equal(f.run(["doctor"]).status, 0);
  fs.rmSync(f.root, { recursive: true, force: true });
});
