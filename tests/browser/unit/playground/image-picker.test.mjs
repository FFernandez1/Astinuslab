import test from "node:test";
import assert from "node:assert/strict";
import { install, Node } from "../packages/page-harness.mjs";
import { buildImageGrid } from "../../../../src/astinuslab/web/static/playground/render-modal.js";

test("image arrows wrap, select exactly one radio and ignore unrelated keys", (t) => {
  install(t);
  const document = globalThis.document;
  document.createElement = (tag) =>
    Object.assign(new Node(tag), {
      focus() {
        document.activeElement = this;
      },
    });
  const values = [];
  const group = buildImageGrid({
    images: [{ id: "one" }, { id: "two" }],
    value: "one",
    labelledBy: "image-label",
    onChange: (id) => values.push(id),
  });
  const radios = group.querySelectorAll("[role=radio]");
  assert.equal(group.getAttribute("aria-labelledby"), "image-label");
  assert.deepEqual(
    radios.map((r) => r.getAttribute("tabindex")),
    ["0", "-1"],
  );
  const press = (tile, key) => {
    let prevented = false;
    for (const handler of tile.listeners.get("keydown"))
      handler({
        key,
        preventDefault() {
          prevented = true;
        },
      });
    return prevented;
  };
  for (const key of ["ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown"]) {
    const before = values.length;
    assert.equal(press(document.activeElement || radios[0], key), true);
    assert.equal(values.length, before + 1);
    assert.equal(radios.filter((r) => r.getAttribute("tabindex") === "0").length, 1);
    assert.equal(document.activeElement.getAttribute("aria-checked"), "true");
  }
  assert.equal(press(radios[0], "x"), false);
  assert.equal(press(radios[0], " "), true);
  assert.equal(values.at(-1), "one");
});

test("empty image group has no artificial selection", (t) => {
  install(t);
  const group = buildImageGrid({ images: [], value: "", labelledBy: "empty-label" });
  assert.equal(group.getAttribute("aria-labelledby"), "empty-label");
  assert.equal(group.children.length, 0);
});
