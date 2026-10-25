/* global document, window, URL, getComputedStyle */
const { test, expect } = require("@playwright/test");
const fs = require("node:fs/promises");
const path = require("node:path");
const { randomUUID } = require("node:crypto");

async function box(locator) {
  await expect(locator).toBeVisible();
  return locator.evaluate((element) => {
    const { x, y, width, height } = element.getBoundingClientRect();
    return { x, y, width, height };
  });
}

async function uniformControls(locators) {
  const rectangles = await Promise.all(locators.map(box));
  const heights = rectangles.map((rectangle) => rectangle.height);
  expect(Math.min(...heights)).toBeGreaterThanOrEqual(26);
  expect(Math.max(...heights) - Math.min(...heights)).toBeLessThanOrEqual(2);
}

async function noPageOverflow(page) {
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth),
  ).toBeLessThanOrEqual(1);
}

// Portable packages ship disabled, so the page only exists when the run asked
// for it. Same contract as help.smoke.spec.js: skip when the feature was not
// requested, fail when it was requested and the backend did not pick it up.
const PACKAGES_EXPECTED = ["1", "true", "yes", "on"].includes(
  String(process.env.ASTINUSLAB_PACKAGES_ENABLED || "").toLowerCase(),
);

async function openPackagesPage(page) {
  // Routes registered here win over the describe's blanket GET-only route.
  await page.route("**/api/v1/lab-packages", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/v1/lab-packages/activity", (route) =>
    route.fulfill({ json: { prepares: [], instances: [], verifications: [] } }),
  );
  const response = await page.goto("/packages");
  if (response.status() === 404) {
    if (PACKAGES_EXPECTED)
      throw new Error(
        "ASTINUSLAB_PACKAGES_ENABLED is set for this run but /packages returned 404: " +
          "the backend did not pick the feature flag up, so the package page is untested.",
      );
    test.skip(true, "portable packages disabled; set ASTINUSLAB_PACKAGES_ENABLED=true to run this");
  }
  await page.evaluate(() => document.fonts.ready);
}

async function captureContext(page) {
  await page.route("**/api/v1/volatility/**", (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/dumps")) {
      return route.fulfill({
        json: {
          dumps: [
            {
              selection_id: "artifact:geometry-source",
              artifact_id: "geometry-source",
              file: "synthetic.lime",
              os_family: "linux",
              os_label: "Unknown capture context",
              source: "Synthetic browser fixture",
              symbols: "missing",
              context_required: true,
            },
          ],
        },
      });
    }
    if (pathname.endsWith("/catalog")) {
      return route.fulfill({ json: { count: 0, categories: [] } });
    }
    if (pathname.endsWith("/queue")) return route.fulfill({ json: { runs: [] } });
    if (pathname.endsWith("/latest")) return route.fulfill({ json: { status: "none" } });
    return route.abort();
  });
}

test.describe("panel geometry @smoke", () => {
  let logPath;
  let logUrl;

  test.beforeEach(async ({ page }, testInfo) => {
    // Each test owns its fixture; parallel logs-viewer tests use another file.
    const directory = path.join(testInfo.config.metadata.smokeStorageDir, "logs");
    await fs.mkdir(directory, { recursive: true });
    const filename = `geometry-${randomUUID()}.log`;
    logPath = path.join(directory, filename);
    logUrl = `/logs?log=${filename}`;
    await fs.writeFile(logPath, "2026-09-22 10:00:00 [INFO] Synthetic geometry fixture\n");
    // These tests inspect controls, never execute settings or analysis operations.
    await page.route("**/*", (route) =>
      ["GET", "HEAD"].includes(route.request().method()) ? route.continue() : route.abort(),
    );
  });

  test.afterEach(async () => {
    if (logPath) await fs.rm(logPath, { force: true });
  });

  for (const width of [390, 1280]) {
    test(`logs controls share a height at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 844 });
      await page.goto(logUrl);
      await page.evaluate(() => document.fonts.ready);
      if (await page.locator("[data-copy-log-view]").isHidden()) {
        await page.locator(".logs-view-options summary").click();
      }
      await uniformControls([
        page.locator("#level-select"),
        page.locator("#lines-input"),
        page.locator("#log-search"),
        page.getByRole("button", { name: "Update view", exact: true }),
        page.getByRole("button", { name: "Copy visible", exact: true }),
        page.getByRole("button", { name: /Auto-refresh/ }),
      ]);
      await noPageOverflow(page);
      await page.locator("#level-select").focus();
      await page.keyboard.press("Tab");
      await expect(page.locator("#lines-input")).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(page.locator("#log-search")).toBeFocused();
    });

    test(`settings fields and actions align without stretching checkboxes at ${width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 844 });
      await page.goto("/settings");
      await page.evaluate(() => document.fonts.ready);
      const retention = page.locator(".settings-retention-form").first();
      await uniformControls([
        retention.locator('[name="dumps_days"]'),
        retention.locator('[name="logs_days"]'),
        retention.getByRole("button", { name: "Preview", exact: true }),
        retention.getByRole("button", { name: "Save", exact: true }),
      ]);
      const checkbox = await box(retention.getByRole("checkbox"));
      expect(checkbox.width).toBeLessThanOrEqual(20);
      expect(checkbox.height).toBeLessThanOrEqual(20);
      await noPageOverflow(page);
    });

    test(`capture context reflows and remains keyboard usable at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 844 });
      await captureContext(page);
      await page.goto("/volatility");
      const form = page.locator("#analysis-context-form");
      await expect(form).toBeVisible();
      await page.evaluate(() => document.fonts.ready);
      const kernel = page.locator("#context-kernel");
      const architecture = page.locator("#context-architecture");
      const note = page.locator("#context-note");
      await uniformControls([kernel, architecture, note]);
      const panel = await box(form);
      const noteBox = await box(note);
      expect(noteBox.width / panel.width).toBeGreaterThan(0.85);
      for (const field of [kernel, architecture, note]) {
        const rectangle = await box(field);
        expect(rectangle.x).toBeGreaterThanOrEqual(panel.x);
        expect(rectangle.x + rectangle.width).toBeLessThanOrEqual(panel.x + panel.width);
      }
      if (width === 390) {
        const first = await box(kernel);
        const second = await box(architecture);
        expect(second.y).toBeGreaterThanOrEqual(first.y + first.height);
      }
      await kernel.focus();
      await page.keyboard.press("Tab");
      await expect(architecture).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(note).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(page.locator("#context-verified")).toBeFocused();
      await page.keyboard.press("Space");
      await expect(page.locator("#context-verified")).toBeChecked();
      await page.keyboard.press("Tab");
      await expect(page.locator("#context-submit")).toBeFocused();
      await noPageOverflow(page);
    });
  }

  for (const width of [1280, 1920]) {
    test(`page titles share the shell gutter at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      for (const url of [logUrl, "/settings", "/images", "/volatility", "/packages"]) {
        await page.goto(url);
        await page.evaluate(() => document.fonts.ready);
        const title = await box(page.getByRole("heading", { level: 1 }));
        const shellLabel = await box(page.locator(".eng-bar .section-label"));
        expect(
          Math.abs(title.x - shellLabel.x),
          `${url} title vs shell gutter`,
        ).toBeLessThanOrEqual(2);
      }
    });
  }

  test("packages tabs use the brand accent and keep their tablist wiring", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await openPackagesPage(page);

    // Resolve both tokens to the same rgb() form the computed underline uses.
    const { brand, info } = await page.evaluate(() => {
      const root = getComputedStyle(document.documentElement);
      const resolve = (token) => {
        const probe = document.createElement("span");
        probe.style.color = root.getPropertyValue(token).trim();
        document.body.append(probe);
        const value = getComputedStyle(probe).color;
        probe.remove();
        return value;
      };
      return { brand: resolve("--primary"), info: resolve("--c-info") };
    });
    const accent = await page
      .locator("#package-use-tab")
      .evaluate((element) => getComputedStyle(element).borderBottomColor);

    expect(accent, "active tab underline uses the brand accent").toBe(brand);
    expect(accent, "active tab must not use the info accent").not.toBe(info);

    // Review Focus 3: the tablist wiring must survive the control change.
    await expect(page.locator("#package-use-tab")).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#package-create-tab")).toHaveAttribute("aria-selected", "false");
    await expect(page.locator("#package-create-tab")).toHaveAttribute("tabindex", "-1");
    await page.locator("#package-create-tab").click();
    await expect(page.locator("#package-create-tab")).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#package-use-tab")).toHaveAttribute("tabindex", "-1");
    await uniformControls([page.locator("#package-use-tab"), page.locator("#package-create-tab")]);
  });

  test("packages panel heads are barred rows and the detail body keeps its id", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await openPackagesPage(page);

    const head = page.locator("#library-heading").locator("..");
    const style = await head.evaluate((element) => {
      const computed = getComputedStyle(element);
      return { display: computed.display, border: computed.borderBottomWidth };
    });
    expect(style.display, "panel head is the shared flex row").toBe("flex");
    expect(style.border, "panel head carries the shared bottom border").not.toBe("0px");

    // The Library heading and its checkbox share one row, which the inert
    // justify-content on the old page-local head never achieved.
    const heading = await box(page.locator("#library-heading"));
    const checkbox = await box(page.locator("#prepare-network"));
    expect(Math.abs(heading.y - checkbox.y), "heading and checkbox share a row").toBeLessThan(
      heading.height,
    );
    expect(checkbox.x).toBeGreaterThan(heading.x + heading.width);

    // Review Focus 5: #package-detail moved onto the panel body; the renderer
    // target and the focus hop in index.js must both survive.
    await expect(page.locator("#package-detail")).toHaveClass(/package-panel__body/);
    await noPageOverflow(page);
  });

  test("packages panel heads wrap rather than overflow on a phone", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await openPackagesPage(page);
    await noPageOverflow(page);
    const heading = await box(page.locator("#library-heading"));
    const checkbox = await box(page.locator("#prepare-network"));
    expect(checkbox.y, "the checkbox drops below the heading at phone width").toBeGreaterThan(
      heading.y,
    );
  });

  // Review Focus 2: --primary-soft is #0c1f14 dark and #d1fae5 light, and the
  // chosen filename sits on it. A rule that reads in one theme can fail the other.
  for (const theme of ["dark", "light"]) {
    test(`packages file picker stays readable in the ${theme} theme`, async ({ page }) => {
      await page.setViewportSize({ width: 1280, height: 900 });
      await openPackagesPage(page);
      await page.evaluate((value) => {
        document.documentElement.dataset.theme = value;
      }, theme);

      const drop = page.locator("#package-file-drop");
      await expect(drop).toBeVisible();

      // The selected fill is --primary-soft, which differs per theme and is the
      // whole reason this test exists, so put the picker in that state first.
      // The 140ms background transition must be suppressed before measuring, or
      // getComputedStyle returns a mid-transition colour rather than the target.
      await drop.evaluate((element) => {
        element.style.transition = "none";
        element.classList.add("is-selected");
        void element.offsetHeight;
      });
      const { fill, ink } = await page.locator("#package-file-name").evaluate((element) => ({
        fill: getComputedStyle(element.closest(".file-drop")).backgroundColor,
        ink: getComputedStyle(element).color,
      }));

      const luminance = (colour) => {
        const [r, g, b] = colour
          .match(/[\d.]+/g)
          .slice(0, 3)
          .map(Number);
        const channel = (value) => {
          const v = value / 255;
          return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
        };
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
      };
      const [light, dark] = [luminance(fill), luminance(ink)].sort((a, b) => b - a);
      const ratio = (light + 0.05) / (dark + 0.05);
      expect(ratio, `filename on the selected picker in the ${theme} theme`).toBeGreaterThanOrEqual(
        4.5,
      );
    });
  }

  test("the archive picker is reachable and reports an empty submit", async ({ page }) => {
    await openPackagesPage(page);

    // The input is visually hidden but must stay in the tab order: importing a
    // package is the page's primary action and cannot be pointer-only.
    await page.locator("#package-use-tab").focus();
    await page.keyboard.press("Tab");
    await expect(page.locator("#package-file")).toBeFocused();
    const ring = await page
      .locator("#package-file-drop")
      .evaluate((element) => getComputedStyle(element).outlineStyle);
    expect(ring, "the label shows the focus ring for the input behind it").not.toBe("none");

    // A display:none `required` input aborts submission before the submit event,
    // which made this localised message unreachable.
    await page.locator("#package-inspect-form button[type=submit]").click();
    await expect(page.locator("#package-error")).toBeVisible();
    await expect(page.locator("#package-error")).not.toBeEmpty();
  });
});
