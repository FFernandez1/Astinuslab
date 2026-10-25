const { expect } = require("@playwright/test");

// Opening the target wizard is the same three lines in every spec that touches
// it, and one of them is easy to get wrong: the image grid arrives
// asynchronously and Next stays disabled until an image is selected, so a spec
// that starts driving the form too early times out on a control whose tooltip
// blames the missing image. Waiting for the grid here means no spec has to know.
async function openTargetWizard(page, { imageId } = {}) {
  await page.getByRole("button", { name: "+ Target" }).click();
  const dialog = page.getByRole("dialog", { name: "Create target" });
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".pg-hud-image").first()).toBeVisible();
  if (imageId) await dialog.locator(`.pg-hud-image[data-value="${imageId}"]`).click();
  return dialog;
}

// Walk to the step that can submit. The wizard skips steps that do not apply to
// the chosen scenario, so the step count is not fixed -- but arriving somewhere
// without a submit control has to fail loudly rather than quietly submit from
// wherever the walk stopped.
async function advanceToCreate(dialog) {
  const submit = dialog.getByRole("button", { name: "Create target" });
  const next = dialog.getByRole("button", { name: "Next" });
  for (let step = 0; step < 4 && !(await submit.isVisible()); step += 1) {
    await expect(next).toBeEnabled();
    await next.click();
  }
  await expect(submit).toBeVisible();
  return submit;
}

module.exports = { openTargetWizard, advanceToCreate };
