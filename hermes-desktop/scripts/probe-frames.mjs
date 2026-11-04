// Inspect the CRM page frame tree: where do the menus (项目管理) live?
import { chromium } from "playwright-core";
import { join } from "node:path";
import { homedir } from "node:os";

const profileDir = join(homedir(), "AppData", "Roaming", "cowork-hermes-desktop", "browser-automation", "chrome");
const context = await chromium.launchPersistentContext(profileDir, {
  executablePath: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  headless: false,
  viewport: null,
});
const page = context.pages()[0] ?? await context.newPage();
await page.goto("https://hermes-crm.gateway.example.com/Default.aspx", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => undefined);
for (let i = 0; i < 10 && !/Default\.aspx|userlogin/i.test(page.url()); i += 1) await page.waitForTimeout(2000);
await page.waitForTimeout(3000);
console.log("page url:", page.url());
console.log("frames:", page.frames().length);
for (const frame of page.frames()) {
  const info = await frame.evaluate(() => ({
    iframes: document.querySelectorAll("iframe").length,
    interactable: document.querySelectorAll("a,button,input,textarea,select").length,
    projectHits: Array.from(document.querySelectorAll("a,span,li,td,div"))
      .filter((n) => /项目管理/.test((n.textContent || "").replace(/\s+/g, "")) && n.textContent.trim().length < 16).length,
    menuSample: Array.from(document.querySelectorAll("a")).map((a) => a.textContent.trim()).filter((t) => t && t.length < 14).slice(0, 30),
  })).catch(() => ({ iframes: 0, interactable: 0, projectHits: 0, menuSample: ["<frame detached>"] }));
  console.log(`--- frame: ${frame.url().slice(0, 90)}`);
  console.log(`    iframes=${info.iframes} interactable=${info.interactable} 项目管理命中=${info.projectHits}`);
  console.log(`    菜单样本: ${JSON.stringify(info.menuSample)}`);
}
await context.close();
