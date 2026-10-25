const { test, expect } = require("@playwright/test");

test("docs right toc highlights the selected homepage section", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");

  const toc = page.getByRole("navigation", { name: "Table of contents" });
  await expect(toc).toBeVisible();

  const reference = toc.locator('.md-nav__link[href="#reference"]');
  await reference.click();

  await expect(reference).toHaveClass(/md-nav__link--active/);
});

test.describe("docs right toc hash highlighting", () => {
  const anchors = [
    ["/", "#architecture"],
    ["/", "#development"],
    ["/", "#community"],
    ["/user/PLAYGROUND/", "#cannot-delete-a-network"],
    ["/user/PLAYGROUND/", "#restart-and-cleanup-behavior"],
    ["/usage/getting-started/", "#first-capture-in-playground"],
    ["/usage/examples/", "#capture-a-playground-vm"],
    ["/usage/troubleshooting/", "#disk-space-problems"],
    ["/user/PREBAKED_IMAGES/", "#requirements"],
    ["/user/VOLATILITY3/", "#plugin-failures"],
    ["/architecture/overview/", "#operating-modes"],
    ["/architecture/components/", "#scaling"],
    ["/architecture/data-flow/", "#artifact-flow"],
    ["/architecture/decisions/", "#use-installed-privileged-helpers"],
    ["/architecture/decisions/", "#treat-catalogs-as-user-maintained-source-data"],
    ["/architecture/decisions/", "#make-playground-ephemeral-keep-captures-durable"],
    ["/architecture/adr-vm-based-isf-generation/", "#decision"],
    ["/architecture/backend-boundaries/", "#refactor-rules"],
    ["/architecture/hotspots/", "#what-to-do-when-this-list-grows"],
    ["/architecture/adr-error-field-naming/", "#non-goals"],
    ["/api/overview/", "#error-format"],
    ["/api/authentication/", "#before-exposing-the-app"],
    ["/api/errors/", "#artifact-errors"],
    ["/api/playground/", "#playgroundacceptedout"],
    ["/api/playground/", "#playgroundcreateifneededspawnvmrequest"],
    ["/api/playground/", "#playgroundcaptureout"],
    ["/reference/DISTRO_SUPPORT/", "#scope"],
    ["/reference/FORENSIC_INTEGRITY/", "#5-network-isolation"],
    ["/development/testing/", "#shell-scripts"],
    ["/development/testing/", "#documentation"],
    ["/development/BACKEND/", "#5-security-mandates"],
    ["/development/FRONTEND/", "#store-module-pattern"],
    ["/development/ROADMAP/", "#roadmap"],
    ["/development/WORKLOADS/", "#6-troubleshooting-debugging"],
    ["/development/WORKLOADS/", "#view-logs"],
    ["/development/WORKLOADS/", "#failed-provisioning"],
    ["/development/REPOSITORY_LAYOUT/", "#frontend-templates-and-assets"],
    ["/community/GOVERNANCE/", "#decision-making-process"],
    ["/community/GOVERNANCE/", "#feedback"],
    ["/LICENSE/", "#no-liability"],
  ];

  for (const [path, hash] of anchors) {
    test(`highlights ${path}${hash}`, async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.goto(path);

      const toc = page.locator('.md-sidebar--secondary [data-md-component="toc"]');
      await expect(toc).toBeVisible();

      const link = toc.locator(`.md-nav__link[href="${hash}"]`);
      await expect(link).toBeVisible();
      await link.click();

      await expect(page).toHaveURL(new RegExp(`${hash}$`));
      await expect(link).toHaveClass(/md-nav__link--active/);
    });
  }
});
