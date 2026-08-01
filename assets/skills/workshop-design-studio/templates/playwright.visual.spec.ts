// Visual assertions to APPEND to the AppKit template's tests/smoke.spec.ts.
//
// Do not add this as a separate spec file and do not create a second Playwright
// config: `databricks apps validate` already runs tests/smoke.spec.ts, and one
// gate is easier to keep green than two.
//
// Deliberately no toHaveScreenshot(): Playwright fails a snapshot assertion the
// first time it runs because no baseline exists yet, which would turn the gate
// red for every attendee on their first validate. These assertions all pass or
// fail on their own merits with no committed baseline.

import { expect, test } from "@playwright/test";

const widths = [375, 768, 1024, 1440] as const;

for (const width of widths) {
  test(`no horizontal overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width < 768 ? 844 : 900 });
    await page.goto("/");
    await page.waitForLoadState("networkidle");

    const overflows = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(overflows).toBe(false);
  });
}

test("keyboard focus is visible", async ({ page }) => {
  await page.goto("/");
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus-visible")).toBeVisible();
});

test("reduced motion remains usable", async ({ browser }) => {
  const context = await browser.newContext({ reducedMotion: "reduce" });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator("body")).toBeVisible();
  await context.close();
});
