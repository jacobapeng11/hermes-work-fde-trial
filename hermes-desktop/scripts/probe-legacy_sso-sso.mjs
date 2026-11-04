// One-off diagnostic: launch a fresh-profile Chrome exactly like the desktop
// automation session does, navigate to the CRM URL, then observe the redirect
// chain, the final landing page and which cookies actually land in the profile.
import { chromium } from "playwright-core";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const profileDir = mkdtempSync(join(tmpdir(), "pw-legacy_sso-probe-"));
const executablePath = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

const context = await chromium.launchPersistentContext(profileDir, {
  executablePath,
  headless: false,
  viewport: null,
  acceptDownloads: true,
});
const page = context.pages()[0] ?? await context.newPage();
page.on("framenavigated", (frame) => {
  if (frame === page.mainFrame()) console.log(`[NAV] ${frame.url()}`);
});
page.on("console", (msg) => {
  if (msg.type() === "error") console.log(`[PAGE-ERR] ${msg.text().slice(0, 160)}`);
});

console.log("goto CRM URL with fresh profile ...");
try {
  const response = await page.goto("https://hermes-crm.gateway.example.com/Default.aspx", { waitUntil: "domcontentloaded", timeout: 60000 });
  console.log(`initial status=${response ? response.status() : "?"}`);
} catch (error) {
  console.log(`goto error: ${error.message.slice(0, 200)}`);
}

// Give the Enterprise SSO portal up to 60s to finish its client SSO bounce.
let landed = false;
for (let tick = 1; tick <= 12; tick += 1) {
  await page.waitForTimeout(5000);
  const url = page.url();
  const title = await page.title().catch(() => "");
  console.log(`t+${tick * 5}s url=${url} title=${title.slice(0, 60)}`);
  const onPortal = /sdpc\.demo\.com|enterprise_idp/i.test(url);
  if (!onPortal && /Default\.aspx|userlogin/i.test(url)) {
    landed = true;
    break;
  }
}

const all = await context.cookies();
console.log("--- cookies in fresh profile ---");
console.log(`total=${all.length}`);
for (const cookie of [...new Set(all.map((c) => c.domain))]) {
  const forHost = all.filter((c) => c.domain === cookie);
  const authish = forHost.filter((c) => /sess|ticket|token|auth|login|sid|jsession|asp\.net/i.test(c.name));
  console.log(`domain=${cookie} cookies=${forHost.length} auth-like=${authish.map((c) => c.name).join(",") || "-"} persistent=${forHost.some((c) => c.expires > 0)}`);
}
console.log(landed ? "RESULT: landed on CRM page" : "RESULT: still bouncing/portal after 60s");
await context.close();
try { rmSync(profileDir, { recursive: true, force: true }); } catch { /* profile locked, leave it */ }
