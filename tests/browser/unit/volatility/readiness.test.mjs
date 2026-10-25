import test from "node:test";
import assert from "node:assert/strict";

const attributes = new Map();
const root = {
  getAttribute(name) {
    return attributes.get(name) ?? null;
  },
};
const elements = new Map([["vol-workbench", root]]);

globalThis.window = {};
globalThis.document = {
  readyState: "loading",
  addEventListener() {},
  getElementById(id) {
    return elements.get(id) ?? null;
  },
  querySelectorAll() {
    return [];
  },
  createElement() {
    return { classList: { add() {} }, appendChild() {}, setAttribute() {}, style: {} };
  },
  createTextNode(text) {
    return { textContent: text };
  },
};

const readiness = await import("../../../../src/astinuslab/web/static/volatility/readiness.js");
const catalog = await import("../../../../src/astinuslab/web/static/volatility/catalog.js");
const isf = await import("../../../../src/astinuslab/web/static/volatility/isf.js");
const { state } = await import("../../../../src/astinuslab/web/static/volatility/state.js");

test("readiness parses split capability data from the workbench root", () => {
  attributes.set("data-analysis-ready", "0");
  attributes.set("data-isf-build-ready", "1");

  assert.equal(readiness.analysisReady(), false);
  assert.equal(readiness.isfBuildReady(), true);

  attributes.set("data-analysis-ready", "1");
  attributes.set("data-isf-build-ready", "0");
  assert.equal(readiness.analysisReady(), true);
  assert.equal(readiness.isfBuildReady(), false);
});

test("later run-button rendering cannot re-enable unavailable analysis", () => {
  attributes.set("data-analysis-ready", "0");
  attributes.set("data-isf-build-ready", "1");
  const run = { disabled: false };
  const rerun = { disabled: false };
  elements.set("run-btn", run);
  elements.set("rerun-cmd", rerun);
  elements.set("yara-rules", { value: "" });
  state.running = false;
  state.plugin = "linux.pslist.PsList";
  state.dump = { symbols: "ready" };

  catalog.updateRunButton();

  assert.equal(run.disabled, true);
  assert.equal(rerun.disabled, true);
});

test("capability parsing fails closed for every degenerate attribute value", () => {
  // A future refactor to `!== "0"` or `Boolean(attr)` would fail open while the
  // happy-path tests above stayed green, so pin the closed behaviour explicitly.
  for (const value of ["0", "", " ", "1 ", "true", "yes", "01", "null"]) {
    attributes.set("data-analysis-ready", value);
    attributes.set("data-isf-build-ready", value);
    assert.equal(readiness.analysisReady(), false, `expected closed for ${JSON.stringify(value)}`);
    assert.equal(readiness.isfBuildReady(), false, `expected closed for ${JSON.stringify(value)}`);
  }

  attributes.delete("data-analysis-ready");
  attributes.delete("data-isf-build-ready");
  attributes.delete("data-vol3-enabled");
  assert.equal(readiness.analysisReady(), false);
  assert.equal(readiness.isfBuildReady(), false);
  assert.equal(readiness.vol3Enabled(), false);
});

test("capability parsing fails closed when the workbench root is absent", () => {
  elements.delete("vol-workbench");
  attributes.set("data-analysis-ready", "1");
  attributes.set("data-isf-build-ready", "1");
  attributes.set("data-vol3-enabled", "1");

  assert.equal(readiness.analysisReady(), false);
  assert.equal(readiness.isfBuildReady(), false);
  assert.equal(readiness.vol3Enabled(), false);

  elements.set("vol-workbench", root);
});

test("ISF submit stays disabled and never fetches when the integration is disabled", () => {
  // An unready dwarf2json must NOT block here: the provider chain can still deliver
  // a prebuilt or debuginfod ISF without ever invoking the tool.  Only a disabled
  // integration stops the submission.
  attributes.set("data-vol3-enabled", "0");
  attributes.set("data-isf-build-ready", "0");
  const button = { disabled: false, textContent: "Build ISF" };
  let submit;
  const form = {
    action: "/volatility/build",
    addEventListener(name, handler) {
      if (name === "submit") submit = handler;
    },
    querySelector() {
      return button;
    },
  };
  globalThis.document.querySelectorAll = (selector) => (selector === ".vol-isf-form" ? [form] : []);
  let fetchCalls = 0;
  globalThis.fetch = () => {
    fetchCalls += 1;
    return Promise.reject(new Error("must not fetch"));
  };

  isf.wireIsfBuildForms();
  assert.equal(typeof submit, "function");
  submit({ preventDefault() {} });

  assert.equal(button.disabled, true);
  assert.equal(button.textContent, "Build ISF");
  assert.equal(fetchCalls, 0);
});

test("ISF submit proceeds when only dwarf2json is unavailable", () => {
  attributes.set("data-vol3-enabled", "1");
  attributes.set("data-isf-build-ready", "0");
  const button = { disabled: false, textContent: "Build ISF" };
  let submit;
  const form = {
    action: "/volatility/build",
    addEventListener(name, handler) {
      if (name === "submit") submit = handler;
    },
    querySelector() {
      return button;
    },
  };
  globalThis.document.querySelectorAll = (selector) => (selector === ".vol-isf-form" ? [form] : []);
  // The handler serialises the form; the stub above is not a real HTMLFormElement.
  const RealFormData = globalThis.FormData;
  globalThis.FormData = function () {
    return { append() {} };
  };
  let fetchCalls = 0;
  globalThis.fetch = () => {
    fetchCalls += 1;
    return new Promise(() => {});
  };

  isf.wireIsfBuildForms();
  submit({ preventDefault() {} });

  assert.equal(fetchCalls, 1);
  assert.equal(button.textContent, "queued…");
  globalThis.FormData = RealFormData;
});
