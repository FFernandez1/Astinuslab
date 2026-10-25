import test from "node:test";
import assert from "node:assert/strict";

const api = await import("../../../../src/astinuslab/web/static/packages/api.js");

test("package API exposes the lifecycle endpoints", () => {
  for (const name of [
    "inspectPackage",
    "commitInspection",
    "listPackages",
    "deletePackage",
    "preparePackage",
    "getPrepareStatus",
    "preflightPackage",
    "launchPackage",
    "getOperationStatus",
    "instanceAction",
  ]) {
    assert.equal(typeof api[name], "function", `${name} should be exported`);
  }
});

test("listActivity requests the durable activity endpoint and returns its envelope", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  const expected = {
    prepares: [{ id: "lprep-1", state: "running" }],
    verifications: [{ id: "lverify-1", state: "reserved" }],
    instances: [{ id: "linstance-1", state: "ready" }],
  };
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify(expected),
    };
  };

  try {
    assert.deepEqual(await api.listActivity(), expected);
    assert.deepEqual(calls, [
      ["/api/v1/lab-packages/activity", { method: "GET", headers: {}, body: undefined }],
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

const statusGets = [
  ["getPrepareStatus", "prepare"],
  ["getOperationStatus", "operations"],
  ["getVerificationStatus", "verifications"],
];

for (const [name, segment] of [...statusGets, ["listActivity", "activity"]]) {
  test(`${name} forwards AbortSignal to the encoded GET and preserves the response`, async () => {
    const controller = new globalThis.AbortController();
    const expected = { id: "id /?#", state: "running" };
    let received;
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (url, options) => {
      received = { url, options };
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify(expected),
      };
    };
    try {
      const result =
        name === "listActivity"
          ? await api[name]({ signal: controller.signal })
          : await api[name]("id /?#", { signal: controller.signal });
      assert.deepEqual(result, expected);
      assert.equal(
        received.url,
        name === "listActivity"
          ? "/api/v1/lab-packages/activity"
          : `/api/v1/lab-packages/${segment}/id%20%2F%3F%23`,
      );
      assert.equal(received.options.method, "GET");
      assert.equal(received.options.signal, controller.signal);
      assert.equal(received.options.body, undefined);
      assert.deepEqual(received.options.headers, {});
      controller.abort();
      assert.equal(received.options.signal.aborted, true);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
}

for (const [name, segment] of statusGets) {
  test(`${name} remains compatible with legacy identifier-only calls`, async () => {
    const originalFetch = globalThis.fetch;
    let received;
    globalThis.fetch = async (...args) => {
      received = args;
      return { ok: true, status: 200, text: async () => '{"state":"running"}' };
    };
    try {
      assert.deepEqual(await api[name]("id /?#"), { state: "running" });
      assert.deepEqual(received, [
        `/api/v1/lab-packages/${segment}/id%20%2F%3F%23`,
        { method: "GET", headers: {}, body: undefined },
      ]);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
}

test("abortable status requests preserve structured HTTP errors for retry classification", async () => {
  const originalFetch = globalThis.fetch;
  const payload = { detail: { code: "unavailable", message: "Status temporarily unavailable" } };
  globalThis.fetch = async () => ({
    ok: false,
    status: 503,
    statusText: "Service Unavailable",
    text: async () => JSON.stringify(payload),
  });
  try {
    await assert.rejects(
      api.getOperationStatus("op-1", { signal: new globalThis.AbortController().signal }),
      (error) => {
        assert.equal(error.status, 503);
        assert.equal(error.message, "Status temporarily unavailable");
        assert.deepEqual(error.payload, payload);
        return true;
      },
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetch abort rejection is propagated unchanged", async () => {
  const originalFetch = globalThis.fetch;
  const failure = Object.assign(new Error("aborted"), { name: "AbortError" });
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.signal.aborted, true);
    throw failure;
  };
  const controller = new globalThis.AbortController();
  controller.abort();
  try {
    await assert.rejects(
      api.listActivity({ signal: controller.signal }),
      (error) => error === failure,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("shared request changes preserve mutation method, encoded paths, JSON body, and CSRF", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const calls = [];
  globalThis.document = { querySelector: () => ({ content: "csrf-test-token" }) };
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return { ok: true, status: 204 };
  };
  try {
    assert.equal(await api.preparePackage("pkg /1", { disk: "minimal" }), null);
    assert.equal(await api.cancelPrepare("prep /1"), null);
    assert.equal(await api.deletePackage("pkg /1"), null);
    assert.deepEqual(calls, [
      [
        "/api/v1/lab-packages/pkg%20%2F1/prepare",
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": "csrf-test-token" },
          body: '{"disk":"minimal"}',
        },
      ],
      [
        "/api/v1/lab-packages/prepare/prep%20%2F1/cancel",
        { method: "POST", headers: { "X-CSRF-Token": "csrf-test-token" }, body: undefined },
      ],
      [
        "/api/v1/lab-packages/pkg%20%2F1",
        { method: "DELETE", headers: { "X-CSRF-Token": "csrf-test-token" }, body: undefined },
      ],
    ]);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
  }
});
