import test from "node:test";
import assert from "node:assert/strict";
import {
  getCleanupStatus,
  createNetwork,
  exportLabPackage,
} from "../../../../src/astinuslab/web/static/playground/api.js";
import { setState } from "../../../../src/astinuslab/web/static/playground/state.js";

const { normalizeSession } =
  await import("../../../../src/astinuslab/web/static/playground/api.js");

test("cleanup selection permits its named read but refuses mutations and export", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url) => {
    calls.push(url);
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ session_id: "pg/1", status: "destroying" }),
    };
  });
  setState({ cleanup: { session_id: "pg/1", phase: "accepted", status: "destroying" } });
  t.after(() => setState({ cleanup: null }));
  assert.equal((await getCleanupStatus("pg/1")).status, "destroying");
  assert.match(calls[0], /sessions\/pg%2F1\/cleanup-status$/);
  await assert.rejects(createNetwork({ name: "blocked" }), /Cleanup is selected/);
  await assert.rejects(exportLabPackage({}), /Cleanup is selected/);
  assert.equal(calls.length, 1);
});

test("normalizeSession blanks resources for destroying sessions", () => {
  const out = normalizeSession({ status: "destroying", session_id: "pg-1", vms: [], networks: [] });
  assert.equal(out.id, "pg-1");
  assert.deepEqual(out.vms, []);
  assert.deepEqual(out.networks, []);
});

test("normalizeSession derives id and hides destroyed VMs", () => {
  const out = normalizeSession({
    status: "running",
    session_id: "pg-2",
    vms: [
      { id: "vm-a", state: "running", nics: [] },
      { id: "vm-b", state: "destroyed", nics: [] },
      { id: "vm-c", state: "destroying", nics: [] },
    ],
    networks: [],
  });
  assert.equal(out.id, "pg-2");
  assert.equal(out.vms.length, 1);
  assert.equal(out.vms[0].id, "vm-a");
});

test("normalizeSession keeps failed VMs visible so a spawn failure leaves a trace", () => {
  const out = normalizeSession({
    status: "running",
    session_id: "pg-4",
    vms: [
      { id: "vm-a", state: "running", nics: [] },
      { id: "vm-b", state: "failed", error_summary: "image copy failed", nics: [] },
      { id: "vm-c", state: "teardown_failed", nics: [] },
    ],
    networks: [],
  });
  const ids = out.vms.map((vm) => vm.id);
  assert.deepEqual(ids, ["vm-a", "vm-b", "vm-c"]);
  assert.equal(out.vms[1].error_summary, "image copy failed");
});

test("normalizeSession preserves NIC diagnostics and warns when one NIC lacks an IP", () => {
  const out = normalizeSession({
    status: "running",
    session_id: "pg-3",
    networks: [
      { id: "net-a", name: "dmz", state: "active" },
      { id: "net-b", name: "inside", state: "active" },
    ],
    vms: [
      {
        id: "vm-a",
        state: "running",
        nics: [
          { id: "nic-a", job_network_id: "net-a", ip: "10.42.1.10", state: "active" },
          {
            id: "nic-b",
            job_network_id: "net-b",
            ip: null,
            state: "active",
            diagnostic: "IP lookup failed for nic-b: timed out",
          },
        ],
      },
    ],
  });

  const vm = out.vms[0];
  assert.equal(vm.ip, "partial");
  assert.equal(vm.reachability_status, "partial");
  assert.equal(vm.nics[1].network_name, "inside");
  assert.equal(vm.nics[1].diagnostic, "IP lookup failed for nic-b: timed out");
});
