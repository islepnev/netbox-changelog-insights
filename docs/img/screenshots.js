// Screenshots for the README, from a NetBox with the synthetic changelog (dev/generate_changelog.py).
// Usage: node docs/img/screenshots.js [base URL, default http://127.0.0.1:8001] (login admin / admin)
// Needs Playwright with Chromium. The NetBox side menu is left out: each shot shows the page content only.
const path = require('path');
const { chromium } = require('playwright');

const base = process.argv[2] || 'http://127.0.0.1:8001';
const out = __dirname;
const shots = [
  // name, path, viewport width, height of the clip in CSS pixels
  ['hero', '/plugins/changelog-insights/pulse/?period=90', 1280, 860],
  ['pulse', '/plugins/changelog-insights/pulse/?period=30', 1280, 860],
  ['contributors', '/plugins/changelog-insights/contributors/?period=90', 1280, 860],
  ['activity', '/plugins/changelog-insights/activity/?period=90&by=user', 1280, 860],
  ['chains', '/plugins/changelog-insights/chains/?period=30&onboarding=1&kind=automation', 1280, 860],
];

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, deviceScaleFactor: 2 });
  await context.addInitScript(() => localStorage.setItem('netbox-color-mode', 'light'));
  const page = await context.newPage();
  await page.goto(`${base}/login/`);
  await page.fill('#id_username', 'admin');
  await page.fill('#id_password', 'admin');
  await Promise.all([page.waitForNavigation(), page.click('button[type=submit]')]);
  for (const [name, url, width, height] of shots) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto(base + url);
    const box = await page.locator('.page-wrapper').boundingBox();
    await page.screenshot({
      path: path.join(out, `${name}.png`),
      clip: { x: box.x, y: box.y, width: box.width, height: Math.min(height, box.height) },
      fullPage: true,
    });
    console.log(name);
  }
  await browser.close();
})();
