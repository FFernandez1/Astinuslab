/* global document, innerWidth, innerHeight, getComputedStyle, sessionStorage */
const { test, expect } = require("@playwright/test");
const { writeFileSync } = require("node:fs");
const { mockPlaygroundApi } = require("../helpers/mocks");

for (const lang of ["en", "es"])
  for (const [width, zoom, height = 1000] of [
    [768, 1],
    [1024, 1],
    [1440, 1],
    [1440, 2],
    [720, 1, 500],
  ]) {
    test(`${lang} wizard at ${width}×${height}px, review CSS zoom ${zoom} @smoke`, async ({
      page,
      context,
      baseURL,
    }, testInfo) => {
      await context.addCookies([{ name: "lang", value: lang, url: baseURL }]);
      await page.setViewportSize({ width, height });
      await mockPlaygroundApi(page);
      await page.route("**/catalog/workloads?image_id=**", (route) =>
        route.fulfill({
          json: [
            { id: "demo", display_name: "Fixture Workload", compatibility_status: "compatible" },
          ],
        }),
      );
      await page.goto("/playground");
      await page.getByRole("button", { name: "+ Target", exact: true }).click();
      const modal = page.locator("#create-vm-modal");
      const label = modal.locator('input[name="label"]');
      expect(await label.getAttribute("aria-label")).toBeTruthy();
      await label.fill("invalid label");
      await expect(modal).toContainText(
        lang === "es"
          ? "Usa letras, números, guiones bajos o guiones"
          : "Use letters, numbers, underscores, or hyphens",
      );
      await expect(page.locator("#create-vm-next")).toBeDisabled();
      await label.fill("locale-lab");
      await expect(modal).toContainText(
        lang === "es"
          ? "Formulario completo — preparación del host no comprobada"
          : "Form complete — host readiness not checked",
      );
      await expect(modal).not.toContainText("ready — launch on review");
      await page.locator("#create-vm-next").click();
      await expect(modal).toContainText(lang === "es" ? "Sin Workload" : "No workload");
      await expect(modal).toContainText(
        lang === "es" ? "Workload vulnerable." : "Vulnerable workload.",
      );
      await modal.locator('[data-value="demo"]').click();
      await page.locator("#create-vm-next").click();
      await page.locator("#create-vm-next").click();
      await expect(modal).toContainText(lang === "es" ? "Revisión del formulario" : "Form review");
      await expect(modal).toContainText(
        lang === "es"
          ? "Aquí no se comprueba la disponibilidad de la caché"
          : "Cache availability is not checked here",
      );
      await expect(modal).toContainText(
        lang === "es"
          ? "Las redes con salida permiten tráfico saliente"
          : "Egress-enabled networks allow outbound traffic",
      );
      await expect(page.locator("#create-vm-submit")).toBeInViewport();
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth - innerWidth),
      ).toBeLessThanOrEqual(2);
      if (zoom === 2)
        await page.evaluate(() => {
          document.body.style.zoom = "2";
        });
      await page.screenshot({
        path: testInfo.outputPath("locale-review.png"),
        animations: "disabled",
      });
      const metrics = await page.evaluate(() => {
        const rgba = (value) => {
          const parts = value.match(/[\d.]+/g).map(Number);
          return [...parts.slice(0, 3), parts[3] ?? 1];
        };
        const blend = (front, back) =>
          front.slice(0, 3).map((v, i) => v * front[3] + back[i] * (1 - front[3]));
        const luminance = (color) =>
          color
            .map((v) => v / 255)
            .map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
            .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
        const samples = [
          ".pg-hud-help",
          ".pg-hud-seqrow__v",
          ".pg-hud-mani__ready",
          ".pg-v2-infobox p",
          ".pg-v2-infobox a",
          "#create-vm-submit",
        ].map((selector) => {
          const node = document.querySelector(`#create-vm-modal ${selector}`);
          const ancestors = [];
          for (let current = node; current; current = current.parentElement)
            ancestors.unshift(current);
          const background = ancestors.reduce(
            (back, element) => blend(rgba(getComputedStyle(element).backgroundColor), back),
            [255, 255, 255],
          );
          const foreground = blend(rgba(getComputedStyle(node).color), background);
          const [low, high] = [luminance(foreground), luminance(background)].sort((a, b) => a - b);
          return {
            selector,
            foreground,
            background,
            contrast: Number(((high + 0.05) / (low + 0.05)).toFixed(2)),
            opacity: ancestors.reduce(
              (value, element) => value * Number(getComputedStyle(element).opacity),
              1,
            ),
          };
        });
        const inViewport = (id) => {
          const rect = document.getElementById(id).getBoundingClientRect();
          return (
            rect.left >= 0 &&
            rect.right <= innerWidth &&
            rect.top >= 0 &&
            rect.bottom <= innerHeight
          );
        };
        return {
          horizontalOverflow: document.documentElement.scrollWidth - innerWidth,
          primaryVisible: inViewport("create-vm-submit"),
          closeVisible: inViewport("create-vm-close"),
          samples,
        };
      });
      const metricsPath = testInfo.outputPath("layout-and-contrast.json");
      writeFileSync(
        metricsPath,
        JSON.stringify(
          {
            lang,
            width,
            height,
            zoomProbe: zoom === 2 ? "CSS zoom, not browser zoom" : "normal",
            ...metrics,
          },
          null,
          2,
        ),
      );
      await testInfo.attach("layout-and-contrast", {
        path: metricsPath,
        contentType: "application/json",
      });
      expect(metrics.primaryVisible).toBe(true);
      expect(metrics.closeVisible).toBe(true);
      expect(metrics.horizontalOverflow).toBeLessThanOrEqual(2);
      const content = modal.locator(".pg-hud-content");
      const scroll = await content.evaluate((node) => {
        const maximum = node.scrollHeight - node.clientHeight;
        node.scrollTop = maximum;
        return { maximum, actual: node.scrollTop };
      });
      if (zoom === 2 || height === 500) expect(scroll.maximum).toBeGreaterThan(0);
      expect(Math.abs(scroll.actual - scroll.maximum)).toBeLessThanOrEqual(1);
      const submit = modal.locator("#create-vm-submit");
      await modal.locator("#create-vm-back").focus();
      await page.keyboard.press("Tab");
      await expect(submit).toBeFocused();
      await expect(submit).toBeInViewport();
      // Trial clicks verify pointer reachability without submitting a VM request.
      await submit.click({ trial: true });
      await modal.locator("#create-vm-close").click({ trial: true });
      await page.keyboard.press("Escape");
      await expect(page.getByRole("button", { name: "+ Target", exact: true })).toBeFocused();
    });
  }

test("Spanish empty catalog and cleanup use selected language @smoke", async ({
  page,
  context,
  baseURL,
}) => {
  await context.addCookies([{ name: "lang", value: "es", url: baseURL }]);
  await mockPlaygroundApi(page);
  await page.route("**/catalog/images?role=target", (route) => route.fulfill({ json: [] }));
  await page.goto("/playground");
  await page.getByRole("button", { name: "+ Target", exact: true }).click();
  await expect(page.locator("#create-vm-modal")).toContainText(
    "No hay imágenes de Target disponibles",
  );
  await expect(page.getByRole("link", { name: "Gestionar imágenes", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.evaluate(() =>
    sessionStorage.setItem("astinuslab.playground.cleanup-session.v1", "locale-session"),
  );
  await page.route("**/sessions/locale-session/cleanup-status", (route) =>
    route.fulfill({ json: { session_id: "locale-session", status: "destroy_failed" } }),
  );
  await page.reload();
  await expect(page.locator("#pg-top-metrics")).toContainText("Limpieza incompleta");
  await expect(page.locator("#pg-top-metrics")).toContainText("locale-session");
});
