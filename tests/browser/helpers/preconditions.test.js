const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const { activeKeyPath } = require("./preconditions");

// Exercise the shipped module with synthetic host/key probes. No test in this
// group can execute virsh or read the operator's actual keystore.
function fullTierFixture({ enabled, reachable = true, key = true }) {
  const commands = [];
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(require.resolve("./preconditions"), "utf8"), {
    module,
    __dirname,
    process: {
      env: {
        ASTINUSLAB_E2E_FULL: enabled ? "1" : "0",
        ASTINUSLAB_STORAGE_DIR: "/synthetic-storage",
      },
    },
    require(name) {
      if (name === "node:path") return path;
      if (name === "node:child_process") {
        return {
          execFileSync(...args) {
            commands.push(args);
            if (!reachable) throw new Error("synthetic libvirt unavailable");
          },
        };
      }
      if (name === "node:fs") {
        return {
          existsSync: () => key,
          readFileSync: () => "synthetic-key",
        };
      }
      throw new Error(`unexpected dependency: ${name}`);
    },
  });
  return { ...module.exports, commands };
}

test("disabled full tier skips without probing host resources", () => {
  const fixture = fullTierFixture({ enabled: false, reachable: false, key: false });
  assert.equal(fixture.skipReason(), "ASTINUSLAB_E2E_FULL!=1");
  assert.deepEqual(fixture.commands, []);
});

test("explicit full tier fails when libvirt is unavailable", () => {
  const fixture = fullTierFixture({ enabled: true, reachable: false });
  assert.throws(() => fixture.skipReason(), /full tier requested.*libvirt not reachable/i);
});

test("explicit full tier fails when the active SSH key is unavailable", () => {
  const fixture = fullTierFixture({ enabled: true, key: false });
  assert.throws(() => fixture.skipReason(), /full tier requested.*no active SSH key/i);
});

test("explicit full tier proceeds when its synthetic prerequisites are ready", () => {
  const fixture = fullTierFixture({ enabled: true });
  assert.equal(fixture.skipReason(), null);
  assert.equal(fixture.commands.length, 1);
});

test("activeKeyPath resolves the configured storage directory at call time", (t) => {
  const previousStorageDir = process.env.ASTINUSLAB_STORAGE_DIR;
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "astinuslab-preconditions-"));
  const storageDir = path.join(temporaryRoot, "isolated-storage");
  const keysDir = path.join(storageDir, "ssh_keys");
  const activeFile = path.join(keysDir, ".active");
  const privateKey = path.join(keysDir, "isolated-key");
  const originalExistsSync = fs.existsSync;

  t.after(() => {
    fs.existsSync = originalExistsSync;
    if (previousStorageDir === undefined) {
      delete process.env.ASTINUSLAB_STORAGE_DIR;
    } else {
      process.env.ASTINUSLAB_STORAGE_DIR = previousStorageDir;
    }
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
  });

  fs.mkdirSync(keysDir, { recursive: true });
  fs.writeFileSync(activeFile, "isolated-key");
  fs.writeFileSync(privateKey, "PRIVATE_KEY");
  process.env.ASTINUSLAB_STORAGE_DIR = storageDir;

  fs.existsSync = (candidate) => {
    const resolved = path.resolve(candidate);
    assert.ok(
      resolved === activeFile || resolved === privateKey,
      `unexpected storage lookup outside isolated directory: ${resolved}`,
    );
    return originalExistsSync(candidate);
  };

  assert.equal(activeKeyPath(), privateKey);
});
