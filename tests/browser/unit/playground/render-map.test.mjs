import test from "node:test";
import assert from "node:assert/strict";

const { failureNote, addressRows } =
  await import("../../../../src/astinuslab/web/static/playground/render-map.js");

test("a CalledProcessError shows its outcome, not its invocation", () => {
  const summary =
    "Command '['bash', '/srv/astinuslab/ops/host/run_guest_script.sh', '10.42.1.10', " +
    "'/srv/astinuslab/storage/ssh_keys/key', '/srv/astinuslab/guest/workloads/dvwa/install.sh', " +
    "'390']' returned non-zero exit status 1.";
  assert.equal(failureNote(summary), "returned non-zero exit status 1.");
});

test("a plain message is kept as written", () => {
  assert.equal(failureNote("image not available"), "image not available");
});

test("a long message without an outcome clause is truncated, not dropped", () => {
  const note = failureNote("x".repeat(400));
  assert.equal(note.length, 96);
  assert.ok(note.endsWith("…"));
});

test("only the first line reaches the node", () => {
  assert.equal(failureNote("first line\nsecond line"), "first line");
});

test("an absent summary does not throw", () => {
  assert.equal(failureNote(null), "");
  assert.equal(failureNote(undefined), "");
});

test("partial reachability remains visible alongside each NIC address", (t) => {
  const previous = globalThis.document;
  t.after(() => {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  });
  globalThis.document = {
    createElement() {
      return {
        dataset: {},
        attributes: {},
        children: [],
        setAttribute(name, value) {
          this.attributes[name] = value;
        },
        appendChild(child) {
          this.children.push(child);
        },
      };
    },
  };
  const vm = {
    reachability_status: "partial",
    nics: [{ network_id: "a", ip: "10.0.0.2" }, { network_id: "b" }],
  };
  const rows = addressRows(vm, { a: "outside", b: "inside" });
  assert.deepEqual(
    rows.map((row) => row.children.map((child) => child.textContent)),
    [
      ["reachability", "partial"],
      ["outside", "10.0.0.2"],
      ["inside", "—"],
    ],
  );
  assert.equal(addressRows({ ...vm, reachability_status: "ready" }, {}).length, 2);
});

test("a segment name too long for its column stays readable on hover", (t) => {
  const previous = globalThis.document;
  t.after(() => {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  });
  globalThis.document = {
    createElement() {
      return {
        dataset: {},
        attributes: {},
        children: [],
        setAttribute(name, value) {
          this.attributes[name] = value;
        },
        appendChild(child) {
          this.children.push(child);
        },
      };
    },
  };
  const vm = { nics: [{ network_id: "a", ip: "10.0.0.2" }] };
  const [row] = addressRows(vm, { a: "network-fuzzy-packet-42" });
  const [key] = row.children;
  assert.equal(key.textContent, "network-fuzzy-packet-42");
  assert.equal(key.attributes.title, "network-fuzzy-packet-42");
});
