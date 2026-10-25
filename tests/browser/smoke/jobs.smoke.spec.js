const { test, expect } = require("@playwright/test");
const { collectBrowserErrors } = require("../helpers/mocks");

test("jobs page loads with no browser errors @smoke", async ({ page }) => {
  const errors = collectBrowserErrors(page);
  await page.route("**/api/v1/jobs**", (route) => route.fulfill({ json: [] }));

  await page.goto("/jobs");

  await expect(page).toHaveURL(/\/jobs$/);
  await expect(page.locator("body")).toBeVisible();
  expect(errors).toEqual([]);
});

test("new job wizard renders the terminal HUD chrome and rail @smoke", async ({ page }) => {
  const errors = collectBrowserErrors(page);

  await page.goto("/jobs/new");

  // Window-chrome title bar + vertical tick-rail (restyled shared stepper).
  await expect(page.locator(".nj-tbar")).toBeVisible();
  await expect(page.locator(".nj-tstat")).toContainText("trusted-local");
  const rail = page.locator("#wiz-stepper");
  await expect(rail.locator(".wiz-stepper__step")).toHaveCount(4);

  // The inline wizard JS ran render(): the head title tracks the active step.
  await expect(page.locator("#new-job-title")).toHaveText("Target & Scenario");

  expect(errors).toEqual([]);
});

test("new job manual workload step renders terminal HUD detail tiles @smoke", async ({ page }) => {
  const errors = collectBrowserErrors(page);

  await page.goto("/jobs/new");
  await page.locator('[name="target_id"]').first().check({ force: true });
  await page.locator('[name="scenario_id"][data-mode="manual_trigger"]').first().check({
    force: true,
  });
  await page.locator("#wiz-next-btn").click();

  await expect(page.locator("#new-job-title")).toHaveText("Workload");
  await expect(page.locator("#workload-panel-content")).toBeVisible();
  await expect(page.locator(".workload-card__desc").first()).toBeVisible();
  await expect(page.locator(".workload-card__flag").first()).toBeVisible();
  await expect(page.locator(".workload-card__status").first()).toBeVisible();

  const metrics = await page.locator(".workload-card").evaluateAll((cards) => {
    const doc = cards[0]?.ownerDocument;
    const view = doc?.defaultView;
    const visibleCards = cards.filter((card) => view?.getComputedStyle(card).display !== "none");
    const heights = visibleCards.map((card) => Math.round(card.getBoundingClientRect().height));
    const serviceBlocks = visibleCards.map((card) =>
      card.querySelector(".workload-card__services"),
    );
    const grid = doc?.querySelector(".workload-grid");
    const gridColumns = grid && view ? view.getComputedStyle(grid).gridTemplateColumns : "";

    return {
      visibleCount: visibleCards.length,
      descCount: visibleCards.filter((card) => card.querySelector(".workload-card__desc")).length,
      flagCount: visibleCards.filter((card) => card.querySelector(".workload-card__flag")).length,
      statusCount: visibleCards.filter((card) => card.querySelector(".workload-card__status"))
        .length,
      servicesPinned: serviceBlocks.every((block) => {
        if (!block) return false;
        const cardBox = block.closest(".workload-card").getBoundingClientRect();
        const blockBox = block.getBoundingClientRect();
        return cardBox.bottom - blockBox.bottom <= 12;
      }),
      equalHeight: Math.max(...heights) - Math.min(...heights) <= 2,
      columnCount: gridColumns.split(" ").filter(Boolean).length,
      inlineStyleCount: doc?.querySelectorAll("#workload-panel-content [style]").length || 0,
    };
  });

  expect(metrics.visibleCount).toBeGreaterThanOrEqual(3);
  expect(metrics.descCount).toBe(metrics.visibleCount);
  expect(metrics.flagCount).toBeGreaterThan(0);
  expect(metrics.statusCount).toBe(metrics.visibleCount);
  expect(metrics.servicesPinned).toBe(true);
  expect(metrics.equalHeight).toBe(true);
  expect(metrics.columnCount).toBe(3);
  expect(metrics.inlineStyleCount).toBe(0);
  expect(errors).toEqual([]);
});

test("new job scenario cards render as tall HUD rows @smoke", async ({ page }) => {
  const errors = collectBrowserErrors(page);

  await page.goto("/jobs/new");

  const first = page.locator(".wiz-scenario").first();
  await expect(first).toBeVisible();

  // Regression guard: stale "collapse to a single row" scenario rules once
  // duplicated the new-job-scoped selectors and partially won the cascade,
  // forcing display:contents on the head and align-items:center on the column
  // card so the icon/name/badge/desc stacked and centered. Pin the healthy
  // computed layout: head is a row, card is a left-aligned column, desc clamps.
  const m = await first.evaluate((el) => {
    const view = el.ownerDocument.defaultView;
    const cs = (sel) => {
      const n = el.querySelector(sel);
      return n ? view.getComputedStyle(n) : null;
    };
    const card = cs(".wiz-sc-card");
    const head = cs(".wiz-sc-head");
    const desc = cs(".wiz-sc-desc");
    return {
      headDisplay: head && head.display,
      cardFlexDir: card && card.flexDirection,
      cardAlign: card && card.alignItems,
      descWhiteSpace: desc && desc.whiteSpace,
      descClamp: desc && desc.webkitLineClamp,
      scenarioHeight: Math.round(el.getBoundingClientRect().height),
    };
  });

  expect(m.headDisplay).not.toBe("contents");
  expect(m.cardFlexDir).toBe("column");
  expect(m.cardAlign).not.toBe("center");
  expect(m.descWhiteSpace).not.toBe("nowrap");
  expect(m.descClamp).toBe("2");
  // Multi-line card, not the old collapsed single row (~30-40px).
  expect(m.scenarioHeight).toBeGreaterThanOrEqual(64);

  expect(errors).toEqual([]);
});

test("new job box click toggles selection off on re-click @smoke", async ({ page }) => {
  const errors = collectBrowserErrors(page);

  await page.goto("/jobs/new");

  const target = page.locator(".wiz-target").first();
  await target.click();
  await expect(target.locator("input")).toBeChecked();

  // Re-clicking the selected box clears it (radios have no native deselect).
  await target.click();
  await expect(target.locator("input")).not.toBeChecked();

  // Switching between boxes still works (deselect old, select new).
  const scenarios = page.locator(".wiz-scenario");
  await scenarios.nth(0).click();
  await scenarios.nth(1).click();
  await expect(scenarios.nth(0).locator("input")).not.toBeChecked();
  await expect(scenarios.nth(1).locator("input")).toBeChecked();

  expect(errors).toEqual([]);
});

test("new job backdrop click exits to /jobs @smoke", async ({ page }) => {
  await page.route("**/api/v1/jobs**", (route) => route.fulfill({ json: [] }));

  await page.goto("/jobs/new");
  await expect(page.locator(".pg-v2-creator")).toBeVisible();

  // A click inside the panel must NOT close the wizard.
  await page.locator(".pg-v2-creator").click({ position: { x: 400, y: 60 } });
  await expect(page).toHaveURL(/\/jobs\/new$/);

  // A click on the backdrop (outside the centered panel) exits to /jobs.
  await page.mouse.click(8, 8);
  await expect(page).toHaveURL(/\/jobs$/);
});

test("the new job wizard can be closed on a phone @smoke", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.route("**/api/v1/jobs**", (route) => route.fulfill({ json: [] }));

  await page.goto("/jobs/new");

  // The title bar is a flex row whose crumbs never wrap. Without a narrow-width
  // rule the row overflows its own box -- which is not a scroll container -- and
  // pushes the close control past the right edge, where it cannot be reached.
  const close = page.locator(".nj-close");
  await expect(close).toBeVisible();

  const box = await close.boundingBox();
  expect(box).not.toBeNull();
  expect(box.x + box.width).toBeLessThanOrEqual(375);

  await close.click();
  await expect(page).toHaveURL(/\/jobs$/);
});
