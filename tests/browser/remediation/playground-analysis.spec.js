const { test, expect } = require("@playwright/test");
const { URL } = require("node:url");
const plugin = "linux.pslist.PsList";

test("Playground capture identity survives verified context, queue and result; Jobs still select", async ({
  page,
}) => {
  const source = {
    selection_id: "artifact:art-source",
    artifact_id: "art-source",
    job_id: null,
    session_id: "pg-source",
    vm_label: "target-A",
    file: "same-name.lime",
    os_family: "linux",
    os_label: "Unknown capture context",
    kernel: "—",
    source: "pg-source · target-A · art-source",
    captured: "2026-09-07T10:23:45",
    symbols: "missing",
    context_required: true,
  };
  const second = {
    ...source,
    selection_id: "artifact:art-second",
    artifact_id: "art-second",
    source: "pg-source · target-B · art-second",
  };
  let contextRequests = 0;
  let releaseContext;
  const contextGate = new Promise((resolve) => {
    releaseContext = resolve;
  });
  await page.route("**/volatility", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      body: (await response.text()).replace('data-analysis-ready="0"', 'data-analysis-ready="1"'),
    });
  });
  await page.route("**/api/v1/volatility/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/dumps"))
      return route.fulfill({
        json: {
          dumps: [
            source,
            second,
            {
              job_id: "job-old",
              file: "old.lime",
              os_family: "linux",
              os_label: "Linux",
              symbols: "ready",
            },
          ],
        },
      });
    if (url.pathname.endsWith("/catalog"))
      return route.fulfill({
        json: {
          count: 1,
          categories: [{ id: "processes", label: "Processes", plugins: [{ name: plugin }] }],
        },
      });
    if (url.pathname.endsWith("/queue")) return route.fulfill({ json: { runs: [] } });
    if (url.pathname.endsWith("/latest")) return route.fulfill({ json: { status: "none" } });
    if (url.pathname.endsWith("/analysis-contexts")) {
      expect(route.request().postDataJSON()).toEqual({
        artifact_id: "art-source",
        kernel_release: "6.8.0-138-generic",
        architecture: "x86_64",
        verified: true,
        provenance: "Recorded capture inventory",
      });
      if (++contextRequests === 1) await contextGate;
      return route.fulfill({
        json: {
          ...source,
          job_id: "analysis-source",
          context_required: false,
          symbols: "ready",
          kernel: "6.8.0-138-generic",
        },
      });
    }
    if (url.pathname.endsWith("/runs")) {
      const body = route.request().postDataJSON();
      expect(body.job_id).toBe("analysis-source");
      return route.fulfill({ json: { run_id: "run-source", status: "queued" } });
    }
    expect(url.pathname).toContain("/runs/analysis-source/run-source");
    return route.fulfill({
      json: {
        job_id: "analysis-source",
        source_artifact_id: "art-source",
        plugin,
        run_id: "run-source",
        status: "completed",
        columns: ["PID", "COMM"],
        rows: [[42, "capture-fixture"]],
        artifact_id: "report-source",
      },
    });
  });
  await page.goto("/volatility");
  await expect(page.locator("#dump-select option").first()).toContainText("art-source");
  await expect(page.locator("#m-src")).toContainText("target-A");
  await expect(page.locator("#analysis-context-form")).toBeVisible();
  await page.getByRole("button", { name: plugin, exact: true }).click();
  await expect(page.locator("#run-btn")).toBeDisabled();
  await page.locator("#context-kernel").fill("6.8.0-138-generic");
  await page.locator("#context-architecture").fill("x86_64");
  await page.locator("#context-note").fill("Recorded capture inventory");
  await page.locator("#context-verified").check();
  await page.locator("#context-submit").click();
  await expect.poll(() => contextRequests).toBe(1);
  await page.locator("#dump-select").selectOption("artifact:art-second");
  await expect(page.locator("#context-kernel")).toHaveValue("");
  await expect(page.locator("#context-verified")).not.toBeChecked();
  releaseContext();
  await expect(page.locator("#context-submit")).toBeEnabled();
  await expect(page.locator("#m-src")).toContainText("art-second");
  await expect(page.locator("#analysis-context-form")).toBeVisible();
  await page.locator("#dump-select").selectOption("artifact:art-source");
  await page.getByRole("button", { name: plugin, exact: true }).click();
  await page.locator("#context-kernel").fill("6.8.0-138-generic");
  await page.locator("#context-architecture").fill("x86_64");
  await page.locator("#context-note").fill("Recorded capture inventory");
  await page.locator("#context-verified").check();
  await page.locator("#context-submit").click();
  await expect(page.locator("#analysis-context-form")).toBeHidden();
  await page.locator("#run-btn").click();
  await expect(page.locator("#res-body")).toContainText("capture-fixture");
  await expect(page.locator("#m-src")).toContainText("art-source");
  await expect(page.locator("#evidence-link")).toHaveAttribute(
    "href",
    "/api/v1/artifacts/report-source/download",
  );
  await page.locator("#dump-select").selectOption("job-old");
  await page.getByRole("button", { name: plugin, exact: true }).click();
  await expect(page.locator("#run-btn")).toBeEnabled();
  await expect(page.locator("#analysis-context-form")).toBeHidden();
});
