/* global window, document, getComputedStyle */
const { test, expect } = require("@playwright/test");
const { URL } = require("node:url");
const { Buffer } = require("node:buffer");
const { mockPlaygroundApi } = require("../helpers/mocks");

const metadata = {
  distro: "ubuntu",
  version: "22.04",
  family: "debian",
  default_username: "ubuntu",
  os_variant: "ubuntu22.04",
};
async function images(page) {
  await page.route("**/admin/images/*/metadata", (r) => r.fulfill({ json: metadata }));
  await page.goto("/images");
  return page.locator("[data-image-meta-filename]").first();
}

test("image picker accepts keyboard activation and native empty validation", async ({ page }) => {
  await images(page);
  const file = page.locator("#img-file");
  await page.locator("#drop-zone").scrollIntoViewIfNeeded();
  await file.focus();
  await expect(file).toBeFocused();
  const chooser = page.waitForEvent("filechooser");
  await page.keyboard.press("Space");
  await (
    await chooser
  ).setFiles({
    name: "fixture.qcow2",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("fixture"),
  });
  await expect(page.locator("#img-fname")).toHaveText("fixture.qcow2");
  await file.setInputFiles([]);
  await page.locator("form:has(#img-file) button[type=submit]").click();
  await expect(file).toBeFocused();
  expect(await file.evaluate((e) => e.validity.valueMissing)).toBe(true);
});

for (const width of [320, 390]) {
  test(`image metadata dialog manages focus and fits ${width}px short viewport`, async ({
    page,
  }) => {
    const opener = await images(page);
    await page.setViewportSize({ width, height: 420 });
    await opener.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "Image metadata" });
    await expect(dialog).toBeVisible();
    await expect(dialog.locator("[name=distro]")).toBeFocused();
    const bounds = await dialog.locator(".image-meta-dialog").boundingBox();
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    expect(bounds.height).toBeLessThanOrEqual(420);
    await dialog.getByRole("button", { name: "Save", exact: true }).focus();
    await page.keyboard.press("Tab");
    await expect(dialog.locator("[name=distro]")).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(opener).toBeFocused();
  });
}

for (const phase of ["load", "save", "inspect", "poll"]) {
  for (const failure of ["transport", "json", "refusal"]) {
    test(`images ${phase} ${failure} failure is visible and retryable`, async ({ page }) => {
      const opener = await images(page);
      const failed = (r) =>
        failure === "transport"
          ? r.abort("failed")
          : r.fulfill(
              failure === "json"
                ? { contentType: "application/json", body: "{" }
                : { status: 503, json: { detail: "fixture refusal" } },
            );
      const endpoint =
        phase === "load" || phase === "save"
          ? "**/admin/images/*/metadata"
          : phase === "inspect"
            ? "**/admin/images/*/inspect"
            : "**/admin/images/*/inspection";
      if (phase === "save") {
        await opener.click();
        await page.locator("#img-meta-form [name=version]").fill("retained-version");
      }
      await page.route(endpoint, failed);
      if (phase === "load") {
        await opener.click();
        await expect(page.locator("[data-image-vault-status]")).toContainText(
          "Could not load metadata",
        );
        await expect(opener).toBeEnabled();
        await page.unroute(endpoint, failed);
        await opener.click();
        await expect(page.locator("#img-meta-form")).toBeVisible();
      } else if (phase === "save") {
        await page.locator("#img-meta-form button[type=submit]").click();
        await expect(page.locator("#img-meta-form [role=alert]")).toContainText(
          "Metadata save failed",
        );
        await expect(page.locator("#img-meta-form button[type=submit]")).toBeEnabled();
        await expect(page.locator("#img-meta-form [name=version]")).toHaveValue("retained-version");
      } else {
        await page.route("**/admin/images/*/inspect", (r) =>
          phase === "inspect" ? failed(r) : r.fulfill({ json: { status: "queued" } }),
        );
        const button = page.locator("[data-reinspect-image-filename]").first();
        await button.click();
        await expect(button).toBeEnabled();
        await expect(page.locator("[data-image-vault-status]")).toContainText(
          /Could not|Inspection failed/,
        );
      }
    });
  }
}

async function playground(page) {
  const fixture = {
    captures: [],
    patches: [],
    session: {
      id: "fixture",
      job_id: "fixture",
      session_id: "fixture",
      status: "running",
      networks: [
        {
          id: "net-a",
          name: "fixture-network",
          state: "active",
          can_delete: true,
          attached_vm_count: 0,
        },
      ],
      vms: ["a", "b"].map((id) => ({
        id: `vm-${id}`,
        label: `VM ${id}`,
        role: "target",
        state: "running",
        image_id: "ubuntu",
        nics: [],
        services: [],
        can_capture_memory: true,
        can_capture_disk: true,
      })),
    },
  };
  await page.addInitScript(() => {
    window.fixtureStreams = [];
    window.EventSource = class {
      constructor(url) {
        this.url = url;
        this.handlers = {};
        window.fixtureStreams.push(this);
      }
      addEventListener(key, fn) {
        this.handlers[key] = fn;
      }
      close() {}
      emit(key, message) {
        this.handlers[key]?.({ data: JSON.stringify({ status: key, message }) });
      }
    };
  });
  await page.route("**/api/v1/**", (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname,
      method = route.request().method();
    if (p.endsWith("/current") || p.endsWith("/current-or-null"))
      return route.fulfill({ json: fixture.session });
    if (p.includes("/topology-layout/")) {
      const body = route.request().postDataJSON();
      fixture.patches.push(body);
      return route.fulfill({ json: body });
    }
    if (p.endsWith("/topology-layout")) return route.fulfill({ json: { nodes: [] } });
    if (p.endsWith("/captures") && method === "POST") {
      fixture.captures.push(p);
      return route.fulfill({ json: { acquisition_id: `acq-${fixture.captures.length}` } });
    }
    if (p.endsWith("/networks/net-a") && method === "DELETE")
      return route.fulfill({ status: 409, json: { detail: "fixture network refusal" } });
    if (p.endsWith("/recent-failure")) return route.fulfill({ json: null });
    if (p.endsWith("/metrics")) return route.fulfill({ json: {} });
    return route.fulfill({ json: [] });
  });
  await page.goto("/playground");
  await expect(page.locator("[data-node-id=vm-a]")).toBeVisible();
  return fixture;
}
const node = (page, id) => page.locator(`#map-world [data-node-id=${id}]`);

test("capture console owns one operation across VMs and ignores previous terminal callbacks", async ({
  page,
}) => {
  const fixture = await playground(page);
  await node(page, "vm-a").click();
  await page.locator("[data-detail-key=capture-memory-vm-a]").click();
  await expect.poll(() => page.evaluate(() => window.fixtureStreams.length)).toBe(1);
  await node(page, "vm-b").click();
  await expect(page.locator("[data-detail-key=capture-memory-vm-b]")).toBeDisabled();
  await page.evaluate(() => window.fixtureStreams[0].emit("completed", "A complete"));
  await expect(page.locator("[data-detail-key=capture-memory-vm-b]")).toBeEnabled();
  await page.locator("[data-detail-key=capture-memory-vm-b]").click();
  await expect.poll(() => page.evaluate(() => window.fixtureStreams.length)).toBe(2);
  await page.evaluate(() => {
    window.fixtureStreams[0].emit("running", "late A");
    window.fixtureStreams[0].emit("failed", "late A failure");
    window.fixtureStreams[1].emit("running", "B running");
  });
  await expect(page.locator("[data-detail-key=capture-memory-vm-b]")).toBeDisabled();
  const stream = await page.evaluate(
    async () => (await import("/static/playground/state.js")).getState().ui.stream,
  );
  expect(stream.vmId).toBe("vm-b");
  expect(stream.lines.join("\n")).toContain("B running");
  expect(stream.lines.join("\n")).not.toContain("late A");
  expect(fixture.captures).toHaveLength(2);
  await page.evaluate(() => window.fixtureStreams[1].emit("stream_timeout", "B timeout"));
  await expect(page.locator("[data-detail-key=capture-memory-vm-b]")).toBeEnabled();
});

test("map selection, saved arrows and meaningful polls retain node focus", async ({ page }) => {
  await page.clock.install();
  const fixture = await playground(page);
  const target = node(page, "vm-b");
  await target.focus();
  await page.keyboard.press("Enter");
  await expect(target).toBeFocused();
  for (let i = 1; i <= 2; i++) {
    await page.keyboard.press("ArrowRight");
    await page.clock.runFor(1000);
    await expect.poll(() => fixture.patches.length).toBe(i);
    await expect(target).toBeFocused();
  }
  fixture.session.vms[1].state = "stopped";
  await page.clock.runFor(5100);
  await expect(target).toHaveAttribute("data-state", "stopped");
  await expect(target).toBeFocused();
});

test("network refusal renders despite identical polls and no-IP VM waits for DHCP", async ({
  page,
}) => {
  await playground(page);
  await expect(page.locator("#pg-detail-panel")).toContainText(/DHCP|waiting/i);
  await node(page, "net-a").click();
  await page.getByRole("button", { name: "Delete Network", exact: true }).click();
  await expect(page.locator("#pg-detail-panel")).toContainText("fixture network refusal");
});

async function volatility(page, missing = false) {
  const fixture = { symbols: missing ? "missing" : "ready", failWindows: false, dumpRequests: 0 };
  await page.route("**/volatility", async (r) => {
    const response = await r.fetch();
    const body = (await response.text())
      .replace('data-analysis-ready="0"', 'data-analysis-ready="1"')
      .replace('data-vol3-enabled="0"', 'data-vol3-enabled="1"');
    await r.fulfill({ response, body });
  });
  await page.route("**/api/v1/volatility/**", (r) => {
    const url = new URL(r.request().url());
    if (url.pathname.endsWith("/dumps")) {
      fixture.dumpRequests++;
      return r.fulfill({
        json: {
          dumps: ["linux", "windows"].map((os) => ({
            job_id: `job-${os}`,
            selection_id: `capture-${os}`,
            artifact_id: `artifact-${os}`,
            file: `${os}.raw`,
            os_family: os,
            os_label: os,
            symbols: os === "linux" ? fixture.symbols : "ready",
          })),
        },
      });
    }
    if (url.pathname.endsWith("/catalog")) {
      const os = url.searchParams.get("os");
      if (os === "windows" && fixture.failWindows) return r.abort("failed");
      return r.fulfill({
        json: {
          count: 1,
          categories: [
            { id: "processes", label: "Processes", plugins: [{ name: `${os}.pslist.PsList` }] },
          ],
        },
      });
    }
    if (url.pathname.endsWith("/queue")) return r.fulfill({ json: { runs: [] } });
    if (url.pathname.endsWith("/latest")) return r.fulfill({ json: { status: "none" } });
    if (url.pathname.endsWith("/upload")) return r.fulfill({ json: { job_id: "isf-upload" } });
    return r.fulfill({ json: {} });
  });
  await page.goto("/volatility");
  await expect(page.locator('[data-plugin="linux.pslist.PsList"]')).toBeVisible();
  return fixture;
}

test("catalog failure clears stale OS choices and retry recovers", async ({ page }) => {
  const fixture = await volatility(page);
  fixture.failWindows = true;
  await page.locator("#dump-select").selectOption("capture-windows");
  await expect(page.locator('[data-plugin="linux.pslist.PsList"]')).toHaveCount(0);
  await expect(page.locator("#catalog")).toContainText(/Could not load|failed/i);
  fixture.failWindows = false;
  await page.locator("#catalog").getByRole("button", { name: /retry/i }).click();
  await expect(page.locator('[data-plugin="windows.pslist.PsList"]')).toBeVisible();
});

test("plugin selection keeps focus and semantics; symbol jump focuses Activity", async ({
  page,
}) => {
  await volatility(page, true);
  const plugin = page.locator('[data-plugin="linux.pslist.PsList"]');
  await plugin.focus();
  await page.keyboard.press("Enter");
  await expect(plugin).toBeFocused();
  await expect(plugin).toHaveAttribute("aria-pressed", "true");
  await page.locator("#m-sym").focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#ptab-activity-btn")).toBeFocused();
});

for (const ready of [true, false]) {
  test(`ISF upload reconciles authoritative Acquisition readiness (${ready}) without reload`, async ({
    page,
  }) => {
    const fixture = await volatility(page, true);
    await page.locator('[data-plugin="linux.pslist.PsList"]').click();
    await expect(page.locator("#run-btn")).toBeDisabled();
    await page.locator("#ptab-activity-btn").click();
    const row = page
      .locator(".vol-isfrow")
      .filter({ has: page.locator("[data-upload-isf-input]") })
      .first();
    fixture.symbols = ready ? "ready" : "missing";
    await row.locator("[data-upload-isf-input]").setInputFiles({
      name: "symbols.json",
      mimeType: "application/json",
      buffer: Buffer.from("{}"),
    });
    await expect.poll(() => fixture.dumpRequests).toBeGreaterThan(1);
    await page.locator("#ptab-workbench-btn").click();
    await expect(page.locator("#dump-select")).toHaveValue("capture-linux");
    if (ready) {
      await expect(page.locator("#run-btn")).toBeEnabled();
      await expect(page.locator("#m-sym")).not.toHaveClass(/is-missing/);
    } else await expect(page.locator("#run-btn")).toBeDisabled();
  });
}

test("small light primary and semantic status text meets 4.5 contrast", async ({ page }) => {
  await volatility(page);
  await page.locator('[data-plugin="linux.pslist.PsList"]').click();
  const ratios = await page.evaluate(() => {
    document.documentElement.dataset.theme = "light";
    const rgb = (value) => value.match(/[\d.]+/g).map(Number);
    const blend = (fg, bg) =>
      fg.slice(0, 3).map((c, i) => c * (fg[3] ?? 1) + bg[i] * (1 - (fg[3] ?? 1)));
    const background = (element) => {
      if (!element) return [255, 255, 255];
      return blend(
        rgb(getComputedStyle(element).backgroundColor),
        background(element.parentElement),
      );
    };
    const lum = (cs) =>
      cs
        .map((c) => {
          c /= 255;
          return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
        })
        .reduce((a, c, i) => a + c * [0.2126, 0.7152, 0.0722][i], 0);
    const elements = [document.querySelector("#run-btn")];
    for (const cls of ["vol-pill vol-pill--completed", "vol-pill vol-pill--queued"]) {
      const e = document.createElement("span");
      e.className = cls;
      e.textContent = "Fixture status";
      document.querySelector("#vol-workbench").appendChild(e);
      elements.push(e);
    }
    return elements.map((e) => {
      const bg = background(e),
        fg = blend(rgb(getComputedStyle(e).color), bg);
      const x = lum(fg),
        y = lum(bg);
      return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
    });
  });
  for (const ratio of ratios) expect(ratio).toBeGreaterThanOrEqual(4.5);
});

const contrastPages = [
  ["/images", [".wiz-btn--danger", ".wiz-badge--os-debian"]],
  ["/workloads", [".wiz-btn--danger"]],
  ["/jobs/new", [".wiz-badge--manual", ".wiz-sc--manual .wiz-sc-pill"]],
  [
    "/playground",
    [
      '.pg-v2-map-flag[data-kind="egress"]',
      "#pg-foot-sid",
      "#btn-import-recipe",
      "#btn-end-session",
    ],
  ],
  [
    "/settings",
    [".settings-pressure-banner", ".settings-service-state--bad", ".settings-danger-button"],
  ],
  ["/volatility", ["#vol-readiness-banner", "#vol-readiness-banner a"]],
];

for (const theme of ["dark", "light"]) {
  for (const [route, selectors] of contrastPages) {
    test(`${route} semantic text contrast meets 4.5 in ${theme}`, async ({ page }, testInfo) => {
      if (route === "/playground") {
        await mockPlaygroundApi(page);
        const session = { id: "pg-contrast", state: "running", networks: [], vms: [] };
        for (const endpoint of ["current-or-null", "current"]) {
          await page.route(`**/api/v1/playground/sessions/${endpoint}`, (request) =>
            request.fulfill({ json: session }),
          );
        }
      }
      await page.goto(route);
      if (route === "/settings") {
        // Exercise failure/pressure styles independently of this host's status.
        await page
          .locator(".settings-card")
          .first()
          .evaluate((card) => {
            for (const name of [
              "settings-pressure-banner",
              "settings-service-state--bad",
              "settings-danger-button",
            ]) {
              if (document.querySelector(`.${name}`)) continue;
              const element = document.createElement(name.endsWith("button") ? "button" : "p");
              element.className = name;
              element.textContent = "Fixture operator warning";
              card.appendChild(element);
            }
          });
      }
      await page.evaluate(async (value) => {
        document.documentElement.dataset.theme = value;
        getComputedStyle(document.body).color;
        await Promise.all(
          document
            .getAnimations()
            .filter((animation) => Number.isFinite(animation.effect.getTiming().iterations))
            .map((animation) => animation.finished.catch(() => {})),
        );
      }, theme);
      for (const selector of selectors) {
        const target = page.locator(selector).first();
        await expect(target).toBeVisible();
        const ratio = await target.evaluate((element) => {
          const rgb = (value) => value.match(/[\d.]+/g).map(Number);
          const blend = (fg, bg) =>
            fg.slice(0, 3).map((c, i) => c * (fg[3] ?? 1) + bg[i] * (1 - (fg[3] ?? 1)));
          const background = (node) =>
            node
              ? blend(rgb(getComputedStyle(node).backgroundColor), background(node.parentElement))
              : [255, 255, 255];
          const luminance = (channels) =>
            channels
              .map((c) => {
                c /= 255;
                return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
              })
              .reduce((sum, channel, i) => sum + channel * [0.2126, 0.7152, 0.0722][i], 0);
          const bg = background(element);
          const light = luminance(blend(rgb(getComputedStyle(element).color), bg));
          const dark = luminance(bg);
          return (Math.max(light, dark) + 0.05) / (Math.min(light, dark) + 0.05);
        });
        expect(ratio, `${route} ${theme} ${selector}`).toBeGreaterThanOrEqual(4.5);
      }
      await page.screenshot({ path: testInfo.outputPath("contrast.png"), fullPage: true });
    });
  }
}
