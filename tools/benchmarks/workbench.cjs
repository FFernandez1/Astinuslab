// One synthetic result delivered to the real workbench template and JavaScript.
// Invoked only inside benchmark_workflows.py's private cgroup and isolated server.
const { chromium } = require("playwright");
const playwrightPackage = require("playwright/package.json");
const { performance } = require("node:perf_hooks");

async function main() {
  const port = Number(process.argv[2]);
  const count = Number(process.argv[3]);
  if (
    !Number.isInteger(port) ||
    port < 1 ||
    port > 65535 ||
    !Number.isInteger(count) ||
    count < 1 ||
    count > 10000
  )
    throw new Error("invalid bounded UI workload");
  const plugin = "linux.pslist.PsList";
  const started = performance.now();
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    const payload = {
      status: "completed",
      job_id: "benchmark-job",
      run_id: "benchmark-run",
      plugin,
      columns: ["PID", "COMM"],
      rows: Array.from({ length: count }, (_, index) => [index, "synthetic-" + index]),
      artifact_id: "synthetic-artifact",
    };
    let delivery;
    await page.route("**/volatility", async (route) => {
      const response = await route.fetch();
      const body = (await response.text())
        .replace('data-analysis-ready="0"', 'data-analysis-ready="1"')
        .replace('data-vol3-enabled="0"', 'data-vol3-enabled="1"');
      await route.fulfill({ response, body });
    });
    await page.route("**/api/v1/volatility/**", async (route) => {
      const url = new URL(route.request().url());
      let json;
      if (url.pathname.endsWith("/dumps"))
        json = {
          dumps: [
            {
              job_id: "benchmark-job",
              file: "synthetic-memory.lime",
              os_family: "linux",
              os_label: "Linux synthetic",
              symbols: "ready",
            },
          ],
        };
      else if (url.pathname.endsWith("/catalog"))
        json = {
          count: 1,
          categories: [{ id: "processes", label: "Processes", plugins: [{ name: plugin }] }],
        };
      else if (url.pathname.endsWith("/queue")) json = { runs: [] };
      else if (url.pathname.endsWith("/latest")) json = { status: "none" };
      else if (url.pathname.endsWith("/runs")) json = { run_id: "benchmark-run" };
      else {
        delivery = await page.evaluate(() => performance.now());
        json = payload;
      }
      await route.fulfill({ json });
    });
    await page.goto(`http://127.0.0.1:${port}/volatility`);
    await page.getByRole("button", { name: plugin, exact: true }).click();
    await page.locator("#results-notrun").waitFor({ state: "visible" });
    await page.locator("#run-btn").click();
    await page.waitForFunction(
      (rows) =>
        document.querySelectorAll("#res-body tr").length === rows &&
        document.querySelector("#task-state").textContent === "completed",
      count,
    );
    const painted = await page.evaluate(
      () =>
        new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now()))),
        ),
    );
    if (
      !Number.isFinite(delivery) ||
      errors.length ||
      (await page.locator("#export-csv").isDisabled())
    )
      throw new Error("workbench result validation failed: " + errors.join("; "));
    const actualRows = await page.locator("#res-body tr").count();
    if (
      actualRows !== count ||
      !(await page.locator("#res-body").textContent()).includes("synthetic-" + (count - 1))
    )
      throw new Error("visible row content/count differs");
    process.stdout.write(
      JSON.stringify({
        kind: "synthetic result delivery-to-workbench DOM and two animation frames; real template/JS",
        operation_seconds: (painted - delivery) / 1000,
        delivery_to_paint_ms: painted - delivery,
        browser_setup_and_flow_seconds: (performance.now() - started) / 1000,
        input_rows: count,
        visible_dom_rows: actualRows,
        browser_version: browser.version(),
        browser_executable: chromium.executablePath(),
        playwright_version: playwrightPackage.version,
        playwright_resolved_module: require.resolve("playwright"),
        node_version: process.version,
        page_errors: errors,
      }),
    );
  } finally {
    await browser.close();
  }
}
main().catch((error) => {
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
});
