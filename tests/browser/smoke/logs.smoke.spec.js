/* global document, requestAnimationFrame */
const { test, expect } = require("@playwright/test");
const fs = require("node:fs/promises");
const path = require("node:path");

test.describe("logs viewer @smoke", () => {
  test.describe.configure({ mode: "default" });
  let logPath;
  test.beforeEach(async ({ page }, testInfo) => {
    const dir = path.join(testInfo.config.metadata.smokeStorageDir, "logs");
    await fs.mkdir(dir, { recursive: true });
    logPath = path.join(dir, "runner.log");
    const rows = Array.from(
      { length: 160 },
      (_, i) =>
        `2026-09-19 10:00:00 [INFO] request_id=abcdef0123456789 astinuslab.runner: Event ${i} completed`,
    );
    rows.push(
      "2026-09-19 10:00:01 [INFO] request_id=abc astinuslab.main: http_request method=GET path=/static/site.css status=200",
    );
    rows.push(
      "2026-09-19 10:00:02 [INFO] request_id=abc astinuslab.main: http_request method=GET path=/static/broken.css status=500",
    );
    rows.push(
      "2026-09-19 10:00:03 [ERROR] request_id=abc astinuslab.runner: Disk full <script>alert(1)</script>",
    );
    await fs.writeFile(logPath, rows.join("\n") + "\n");
    await page.goto("/logs?log=runner.log&lines=200");
  });

  test("Latest reaches the end while controls remain visible", async ({ page }) => {
    const viewport = page.locator("#log-viewport");
    await expect
      .poll(() => viewport.evaluate((el) => el.scrollHeight > el.clientHeight))
      .toBe(true);
    await page.getByRole("button", { name: /Latest/ }).click();
    await expect
      .poll(() => viewport.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight))
      .toBeLessThan(3);
    await expect(page.getByRole("button", { name: "Update view" })).toBeInViewport();
  });

  test("search survives update and empty matches have consistent feedback", async ({ page }) => {
    const search = page.locator("#log-search");
    await search.fill("Disk full");
    await expect(page.locator(".logs-showing")).toContainText("1 of 163");
    await page.getByRole("button", { name: "Update view" }).click();
    await expect(search).toHaveValue("Disk full");
    await expect(page.locator(".log-line:visible")).toHaveCount(1);
    await search.fill("does-not-exist");
    await expect(page.getByText("No matching lines.", { exact: true })).toBeVisible();
    await expect(page.locator(".logs-showing")).toContainText("0 of 163");
    await page.getByRole("button", { name: "Clear search", exact: true }).click();
    await expect(page.locator(".log-line:visible")).toHaveCount(163);
  });

  test("routine filter keeps failures and raw entries remain available", async ({ page }) => {
    await page.getByLabel("Hide routine requests").check();
    await expect(page.locator(".log-line:visible")).toHaveCount(162);
    await page.locator("#log-search").fill("broken.css");
    await expect(page.locator(".log-line:visible")).toHaveCount(1);
    const entry = page.locator(".log-line:visible");
    await entry.getByText("Raw entry", { exact: true }).click();
    await expect(entry.locator(".log-line__raw")).toContainText("request_id=abc");
    await page.locator("#log-search").fill("Disk full");
    await expect(page.locator(".log-line:visible")).toContainText("<script>alert(1)</script>");
  });

  test("mobile navigation collapses and message uses the row width", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByRole("heading", { name: "Logs", exact: true })).toBeInViewport();
    const menu = page.getByRole("button", { name: "Menu", exact: true });
    await expect(menu).toHaveAttribute("aria-expanded", "false");
    await menu.click();
    await expect(
      page.locator(".sidebar").getByRole("link", { name: "Jobs", exact: true }),
    ).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(menu).toHaveAttribute("aria-expanded", "false");
    await expect(menu).toBeFocused();
    const widths = await page
      .locator(".log-line")
      .first()
      .evaluate((el) => ({
        row: el.clientWidth,
        message: el.querySelector(".log-line__text").getBoundingClientRect().width,
      }));
    expect(widths.message / widths.row).toBeGreaterThan(0.85);
  });

  test("live refresh reports success, pauses for reading, and reports failures", async ({
    page,
  }) => {
    await page.getByRole("button", { name: /Auto-refresh/ }).click();
    await expect(page.locator("#log-tail-state")).toContainText("Live");
    await expect(page.locator("#log-refresh-status")).toContainText("Updated");
    await page.locator("#log-viewport").evaluate((el) => {
      el.scrollTop = 0;
    });
    await expect(page.locator("#log-tail-state")).toContainText("Paused");
    await fs.appendFile(logPath, "2026-09-19 10:00:04 [ERROR] New failure\n");
    await page.getByRole("button", { name: /Auto-refresh/ }).click();
    await expect(page.locator(".log-line").last()).toContainText("New failure");
    await page.getByRole("button", { name: /Auto-refresh/ }).click();
    await page.route("**/logs/fragment?**", (route) =>
      route.fulfill({ status: 503, body: "Unavailable" }),
    );
    await page.getByRole("button", { name: /Auto-refresh/ }).click();
    await expect(page.locator("#log-refresh-status")).toContainText("Refresh failed");
    await expect(page.locator(".log-line").last()).toContainText("New failure");
  });

  test("reading during an HTMX swap pauses before delayed settling", async ({ page }) => {
    await page.evaluate(() => {
      const content = document.querySelector("#log-viewport").parentElement;
      content.setAttribute("hx-swap", "innerHTML settle:300ms");
      document.addEventListener(
        "htmx:afterSwap",
        (event) => {
          if (!event.target.querySelector("#log-viewport")) return;
          requestAnimationFrame(() => {
            const viewport = document.querySelector("#log-viewport");
            viewport.scrollTop = viewport.scrollHeight;
            requestAnimationFrame(() => {
              viewport.scrollTop = 0;
            });
          });
        },
        { once: true },
      );
    });
    await page.getByRole("button", { name: /Auto-refresh/ }).click();
    await expect(page.locator("#log-tail-state")).toContainText("Paused");
    await expect(page.locator("#log-refresh-status")).toContainText("Paused for reading");
  });

  test("Celery wrappers are readable without dropping the original entry", async ({ page }) => {
    const raw =
      "[2026-09-19 19:14:05,724: WARNING/MainProcess] 2026-09-19 19:14:05 [INFO] request_id=- celery.worker.strategy: Task reconcile[123] received";
    await fs.appendFile(logPath, raw + "\n");
    await page.reload();
    const row = page.locator(".log-line").last();
    await expect(row.locator(".log-line__text")).toHaveText("Task reconcile[123] received");
    await row.getByText("Raw entry", { exact: true }).click();
    await expect(row.locator(".log-line__raw")).toHaveText(raw);
  });

  test("copy uses the full raw entry while download includes the whole file", async ({
    page,
    context,
    browserName,
  }) => {
    let copiedText;
    if (browserName === "chromium") {
      await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    } else {
      // Firefox/WebKit do not expose Chromium's clipboard permissions. Observe
      // the exact payload only after the native, user-activated write succeeds.
      await page.exposeFunction("recordClipboardWrite", (text) => {
        copiedText = text;
      });
      await page.evaluate(() => {
        const clipboard = globalThis.navigator.clipboard;
        const write = clipboard.writeText.bind(clipboard);
        clipboard.writeText = async (text) => {
          await write(text);
          await globalThis.recordClipboardWrite(text);
        };
      });
    }
    await page.locator("#log-search").fill("Disk full");
    await page.getByRole("button", { name: "Copy visible", exact: true }).click();
    await expect(page.getByRole("button", { name: "✓ Copied", exact: true })).toBeVisible();
    const expected =
      "2026-09-19 10:00:03 [ERROR] request_id=abc astinuslab.runner: Disk full <script>alert(1)</script>";
    if (browserName === "chromium") {
      expect(await page.evaluate(() => globalThis.navigator.clipboard.readText())).toBe(expected);
    } else {
      await expect.poll(() => copiedText).toBe(expected);
    }
    const download = await page.request.get(
      await page.getByRole("link", { name: "Download full log" }).getAttribute("href"),
    );
    expect(download.ok()).toBe(true);
    expect(await download.text()).toContain("Event 0 completed");
    await page.evaluate(() => {
      Object.defineProperty(globalThis.navigator, "clipboard", {
        configurable: true,
        value: { writeText: () => Promise.reject(new Error("Permission denied")) },
      });
    });
    await page.locator("[data-copy-log-view]").click();
    await expect(page.locator("[data-copy-log-view]")).toHaveText("Copy failed");
  });

  test("polling retains filters and wrapping", async ({ page }) => {
    await page.clock.install();
    await page.getByLabel("Wrap", { exact: true }).uncheck();
    await page.getByLabel("Hide routine requests").check();
    await page.locator("#log-search").fill("Disk full");
    await page.clock.runFor(200);
    await page.getByRole("button", { name: /Auto-refresh/ }).click();
    await expect(page.locator(".log-line:visible")).toHaveCount(1);
    await fs.appendFile(
      logPath,
      `2026-09-19 10:00:05 [ERROR] request_id=xyz astinuslab.runner: Disk full again ${"long payload ".repeat(60)}\n`,
    );
    await page.clock.runFor(5100);
    await expect(page.locator(".log-line:visible")).toHaveCount(2);
    await expect(page.locator(".logs-showing")).toContainText("2 of 164");
    await expect(page.locator("#log-content")).not.toHaveClass(/htmx-settling/);
    await expect(page.locator("#log-viewport")).toHaveClass(/log-viewport--nowrap/);
    const latest = page.locator(".log-line").last();
    await expect(latest.locator(".log-line__time")).toHaveCount(1);
    expect(await latest.locator(".log-line__text").evaluate((el) => el.clientHeight)).toBeLessThan(
      30,
    );
    await page.getByLabel("Wrap", { exact: true }).check();
    expect(
      await latest.locator(".log-line__text").evaluate((el) => el.clientHeight),
    ).toBeGreaterThan(30);
    await page.getByLabel("Wrap", { exact: true }).uncheck();
    await page.getByRole("button", { name: "Update view" }).click();
    await expect(page.locator("#log-search")).toHaveValue("Disk full");
    await expect(page.getByLabel("Hide routine requests")).toBeChecked();
    await expect(page.getByLabel("Wrap", { exact: true })).not.toBeChecked();
  });

  test("narrow and light layouts keep logs readable and navigation usable", async ({ page }) => {
    await page.getByRole("button", { name: "Toggle theme", exact: true }).click();
    for (const width of [320, 390, 768]) {
      await page.setViewportSize({ width, height: 844 });
      const geometry = await page.locator("#log-viewport").evaluate((el) => ({
        height: el.clientHeight,
        right: el.getBoundingClientRect().right,
      }));
      expect(geometry.height).toBeGreaterThan(160);
      expect(geometry.right).toBeLessThanOrEqual(width);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "Menu", exact: true }).click();
    await page.getByLabel("Language", { exact: true }).selectOption("es");
    await expect(page.getByLabel("Buscar en líneas cargadas", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Menú", exact: true }).click();
    await page.locator(".sidebar").getByRole("link", { name: "Jobs", exact: true }).click();
    await expect(page).toHaveURL(/\/jobs$/);
  });

  for (const width of [390, 768, 1280]) {
    test(`Wrap reflows messages and raw entries at ${width}px`, async ({ page }) => {
      const raw = `2026-09-20 10:00:00 [INFO] request_id=${"a".repeat(180)} astinuslab.runner: ${"long payload with spaces ".repeat(35)}${"Z".repeat(160)}`;
      await fs.writeFile(logPath, raw + "\n");
      await page.reload();
      await page.setViewportSize({ width, height: 844 });
      if (width <= 640) await page.getByText("View options", { exact: true }).click();
      const row = page.locator(".log-line").first();
      await row.getByText("Raw entry", { exact: true }).click();
      const sizes = () =>
        row.evaluate((el) => ({
          message: el.querySelector(".log-line__text").clientHeight,
          raw: el.querySelector(".log-line__raw").clientHeight,
        }));
      const wrapped = await sizes();
      expect(wrapped.message).toBeGreaterThan(40);
      expect(wrapped.raw).toBeGreaterThan(40);
      await page.getByLabel("Wrap", { exact: true }).uncheck();
      const unwrapped = await sizes();
      expect(unwrapped.message).toBeLessThan(30);
      expect(unwrapped.raw).toBeLessThan(30);
      const vp = page.locator("#log-viewport");
      expect(await vp.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
      await page.getByLabel("Wrap", { exact: true }).check();
      expect((await sizes()).raw).toBeGreaterThan(unwrapped.raw);
      expect(await vp.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThan(2);
    });
  }

  test("rows keep the timestamp beside its message", async ({ page }) => {
    const row = page.locator(".log-line").first();
    await expect(row).toHaveClass(/log-line--structured/);
    const boxes = await row.evaluate((el) => {
      const box = (selector) => {
        const rect = el.querySelector(selector).getBoundingClientRect();
        return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right };
      };
      return { time: box(".log-line__time"), text: box(".log-line__text") };
    });
    // The timestamp column precedes the message and shares its row.
    expect(boxes.time.right).toBeLessThanOrEqual(boxes.text.left);
    expect(boxes.time.top).toBeLessThan(boxes.text.bottom);
    expect(boxes.text.top).toBeLessThan(boxes.time.bottom);
  });

  test("unwrapped messages do not paint over the Raw entry control", async ({ page }) => {
    await fs.writeFile(logPath, `2026-09-20 10:00:00 [INFO] app: ${"payload ".repeat(90)}\n`);
    await page.reload();
    await page.getByLabel("Wrap", { exact: true }).uncheck();
    const overlap = await page
      .locator(".log-line")
      .first()
      .evaluate((el) => {
        const range = el.ownerDocument.createRange();
        range.selectNodeContents(el.querySelector(".log-line__text"));
        const text = range.getBoundingClientRect();
        const button = el.querySelector("summary").getBoundingClientRect();
        return (
          text.right > button.left &&
          text.left < button.right &&
          text.top < button.bottom &&
          text.bottom > button.top
        );
      });
    expect(overlap).toBe(false);
  });

  test("short windows and expanded mobile options leave a usable log pane", async ({ page }) => {
    for (const size of [
      { width: 1280, height: 500 },
      { width: 768, height: 600 },
      { width: 390, height: 667 },
    ]) {
      await page.setViewportSize(size);
      if (size.width <= 640) await page.getByText("View options", { exact: true }).click();
      const vp = page.locator("#log-viewport");
      expect(await vp.evaluate((el) => el.clientHeight)).toBeGreaterThanOrEqual(160);
      await page.getByRole("button", { name: /Latest/ }).click();
      await vp.scrollIntoViewIfNeeded();
      await expect(page.locator(".log-line").last()).toBeInViewport();
    }
  });

  test("manual pause discards an in-flight refresh", async ({ page }) => {
    let release;
    let started;
    let finished;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    const received = new Promise((resolve) => {
      started = resolve;
    });
    const settled = new Promise((resolve) => {
      finished = resolve;
    });
    await fs.appendFile(logPath, "2026-09-20 10:00:00 [ERROR] Late arriving entry\n");
    await page.route("**/logs/fragment?**", async (route) => {
      const response = await route.fetch();
      started();
      await gate;
      try {
        await route.fulfill({ response });
      } finally {
        finished();
      }
    });
    const live = page.getByRole("button", { name: /Auto-refresh/ });
    await live.click();
    await received;
    await live.click();
    release();
    await settled;
    await expect(page.locator("#log-content")).not.toHaveClass(/htmx-request/);
    await expect(page.locator(".log-line")).toHaveCount(163);
    await expect(page.locator(".log-line").last()).not.toContainText("Late arriving entry");
  });

  test("opening a raw entry pauses polling so inspection is not replaced", async ({ page }) => {
    const response = page.waitForResponse((url) => url.url().includes("/logs/fragment?"));
    await page.getByRole("button", { name: /Auto-refresh/ }).click();
    await response;
    await expect(page.locator("#log-content")).not.toHaveClass(/htmx-request/);
    await page.locator(".log-line").last().getByText("Raw entry", { exact: true }).click();
    await expect(page.getByRole("button", { name: /Auto-refresh/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    await expect(page.locator(".log-line").last().locator("details")).toHaveAttribute("open", "");
  });

  test("search is reflected in the URL and survives a browser reload", async ({ page }) => {
    await page.locator("#log-search").fill("Disk full");
    await expect(page.locator(".log-line:visible")).toHaveCount(1);
    await expect(page).toHaveURL(/search=Disk(?:\+|%20)full/);
    await page.reload();
    await expect(page.locator("#log-search")).toHaveValue("Disk full");
    await expect(page.locator(".log-line:visible")).toHaveCount(1);
  });

  test("changing severity updates the line limit without blocking form submission", async ({
    page,
  }) => {
    await page.getByLabel("Level", { exact: true }).selectOption("error");
    await page.getByLabel("Lines", { exact: true }).fill("2000");
    await page.getByRole("button", { name: "Update view" }).click();
    await expect(page).toHaveURL(/level=error/);
    await expect(page.locator(".log-line")).toHaveCount(1);
    await page.getByLabel("Level", { exact: true }).selectOption("all");
    await page.getByRole("button", { name: "Update view" }).click();
    await expect(page).toHaveURL(/level=all/);
    await expect(page.getByLabel("Lines", { exact: true })).toHaveValue("1000");
    await expect(page.locator(".log-line")).toHaveCount(163);
  });

  test("empty files and unknown formats remain usable", async ({ page }) => {
    await fs.writeFile(logPath, "");
    await page.reload();
    await expect(page.locator(".logs-showing")).toHaveText("0 of 0 loaded lines");
    await page.getByRole("button", { name: "Copy visible", exact: true }).click();
    await expect(page.locator("[data-copy-log-view]")).toHaveText("Nothing to copy");
    const raw = `Traceback: ${"unstructured payload ".repeat(50)}`;
    await fs.writeFile(logPath, raw + "\n");
    await page.getByRole("button", { name: /Auto-refresh/ }).click();
    await expect(page.locator(".log-line__text")).toHaveText(raw);
    await page.getByRole("button", { name: /Auto-refresh/ }).click();
    expect(await page.locator(".log-line__text").evaluate((el) => el.clientHeight)).toBeGreaterThan(
      30,
    );
    await page.getByLabel("Wrap", { exact: true }).uncheck();
    expect(await page.locator(".log-line__text").evaluate((el) => el.clientHeight)).toBeLessThan(
      30,
    );
  });

  test("pausing raw inspection remains paused across polling intervals and resumes explicitly", async ({
    page,
  }) => {
    await page.clock.install();
    const response = page.waitForResponse((url) => url.url().includes("/logs/fragment"));
    await page.getByRole("button", { name: /Auto-refresh/ }).click();
    await response;
    await expect(page.locator("#log-content")).not.toHaveClass(/htmx-request/);
    const summary = page.locator(".log-line").last().locator("summary");
    await summary.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("button", { name: /Auto-refresh/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    await fs.appendFile(logPath, "New entry while reading\n");
    await page.clock.runFor(11000);
    await expect(summary).toBeFocused();
    await expect(page.locator(".log-line")).toHaveCount(163);
    await expect(page.locator(".log-line").last().locator("details")).toHaveAttribute("open", "");
    await page.getByRole("button", { name: /Auto-refresh/ }).click();
    await expect(page.locator(".log-line")).toHaveCount(164);
  });

  test("file metadata follows successful refreshes", async ({ page }) => {
    const metadata = page.locator(".logs-panel .wiz-vm-col__sub");
    const before = await metadata.textContent();
    await fs.appendFile(logPath, `New entry ${"x".repeat(4000)}\n`);
    await page.getByRole("button", { name: /Auto-refresh/ }).click();
    await expect(page.locator(".log-line")).toHaveCount(164);
    await expect(metadata).not.toHaveText(before);
    const size = (await fs.stat(logPath)).size / 1024;
    await expect(metadata).toContainText(`${size.toFixed(1)} KB`);
  });

  test("bookmarked file searches are visible and can be cleared", async ({ page }) => {
    await page.goto("/logs?log=runner.log&lines=200&q=Disk+full");
    await expect(page.locator(".log-line")).toHaveCount(1);
    await expect(page.getByLabel("Search file", { exact: true })).toHaveValue("Disk full");
    await page.getByLabel("Search file", { exact: true }).fill("");
    await page.getByRole("button", { name: "Update view" }).click();
    await expect(page.locator(".log-line")).toHaveCount(163);
  });
});
