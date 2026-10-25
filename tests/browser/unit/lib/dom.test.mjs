import test from "node:test";
import assert from "node:assert/strict";

test("shared dom lib exports el and $", async () => {
  globalThis.document = {
    createElement: (tag) => ({
      tag,
      appendChild() {},
      setAttribute() {},
      addEventListener() {},
      style: { setProperty() {} },
      dataset: {},
    }),
    getElementById: () => null,
  };
  const dom = await import("../../../../src/astinuslab/web/static/lib/dom.js");
  assert.equal(typeof dom.el, "function");
  assert.equal(typeof dom.$, "function");
});
