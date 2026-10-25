import test from "node:test";
import assert from "node:assert/strict";

const { wireCreatorHandoff } =
  await import("../../../../src/astinuslab/web/static/packages/creator.js");

function harness() {
  const fields = Object.fromEntries(
    ["package_id", "version", "title", "description", "creator", "license"].map((name) => [
      name,
      { value: name === "version" ? "1.0.0" : name },
    ]),
  );
  const output = { value: "" };
  const session = { value: "" };
  const button = { disabled: false };
  const status = { textContent: "", className: "" };
  let submit;
  const form = {
    elements: { namedItem: (name) => fields[name] },
    addEventListener: (_, handler) => {
      submit = handler;
    },
  };
  const ids = {
    "creator-load-form": form,
    "creator-load-button": button,
    "creator-load-status": status,
    "creator-definition": output,
    "creator-authored-session": session,
  };
  globalThis.document = { getElementById: (id) => ids[id], querySelector: () => null };
  wireCreatorHandoff();
  return { output, session, button, status, fields, submit: () => submit({ preventDefault() {} }) };
}

test("loading an authored lab fills canonical JSON and matching source identity", async () => {
  const h = harness();
  let request;
  const definition = { format_version: 1, package_id: "org.example.case", vms: [{ id: "web" }] };
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return new globalThis.Response(JSON.stringify({ definition, source_session_id: "pg-author" }));
  };
  await h.submit();
  assert.equal(request.url, "/api/v1/lab-packages/definitions/from-session");
  assert.equal(request.options.method, "POST");
  assert.deepEqual(JSON.parse(h.output.value), definition);
  assert.equal(h.session.value, "pg-author");
  assert.equal(h.button.disabled, false);
});

test("load failure keeps manual authoring intact and permits retry", async () => {
  const h = harness();
  h.output.value = "manual definition";
  h.session.value = "pg-manual";
  globalThis.fetch = async () =>
    new globalThis.Response(JSON.stringify({ detail: { message: "No active session" } }), {
      status: 404,
    });
  await h.submit();
  assert.equal(h.output.value, "manual definition");
  assert.equal(h.session.value, "pg-manual");
  assert.match(h.status.textContent, /No active session/);
  assert.equal(h.button.disabled, false);
});

test("a delayed load cannot overwrite edits made while awaiting the server", async () => {
  const h = harness();
  let finish;
  globalThis.fetch = () =>
    new Promise((resolve) => {
      finish = resolve;
    });
  const pending = h.submit();
  h.output.value = "new manual definition";
  finish(
    new globalThis.Response(
      JSON.stringify({ definition: { package_id: "old" }, source_session_id: "old" }),
    ),
  );
  await pending;
  assert.equal(h.output.value, "new manual definition");
  assert.equal(h.session.value, "");
  assert.equal(h.button.disabled, false);
});
