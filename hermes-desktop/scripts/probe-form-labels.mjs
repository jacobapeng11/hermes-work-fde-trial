// Phase 3: click into a real CRM form page (项目查询) and measure the
// label-extraction hit rate on the actual form fields the agent struggled with.
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

// Click 项目查询 inside whichever frame hosts it.
let clicked = false;
for (const frame of page.frames()) {
  const link = frame.locator("a", { hasText: "项目查询" }).first();
  if (await link.count().catch(() => 0)) {
    try { await link.click({ timeout: 5000 }); clicked = true; console.log("clicked 项目查询 in", frame.url().slice(0, 60)); break; } catch { /* next frame */ }
  }
}
console.log("clicked:", clicked);
await page.waitForTimeout(5000);
// The form may open in a new tab; pick the newest non-blank page.
const active = context.pages().filter((p) => !p.isClosed() && p.url() !== "about:blank").pop() ?? page;
await active.waitForTimeout(2000);
console.log("form page url:", active.url());
console.log("form frames:", active.frames().length);

const labelOfSource = `(element) => {
  const labelledBy = element.getAttribute("aria-labelledby");
  if (labelledBy) {
    const text = labelledBy.split(/\\s+/).map((id) => document.getElementById(id)).filter(Boolean).map((t) => t.textContent || "").join(" ").replace(/\\s+/g, " ").trim();
    if (text) return { source: "aria-labelledby", text };
  }
  const wrappingLabel = element.closest("label");
  if (wrappingLabel && wrappingLabel.textContent.trim()) return { source: "closest-label", text: wrappingLabel.textContent.replace(/\\s+/g, " ").trim() };
  const id = element.getAttribute("id");
  if (id) {
    const forLabel = document.querySelector('label[for="' + CSS.escape(id) + '"]');
    if (forLabel && forLabel.textContent.trim()) return { source: "label-for", text: forLabel.textContent.replace(/\\s+/g, " ").trim() };
  }
  const title = element.getAttribute("title");
  if (title && title.trim()) return { source: "title", text: title.trim() };
  const cell = element.closest("td,th");
  if (cell) {
    const previous = cell.previousElementSibling;
    if (previous && previous.textContent.trim()) return { source: "prev-td", text: previous.textContent.replace(/\\s+/g, " ").trim() };
  }
  return null;
}`;

for (const frame of active.frames()) {
  const result = await frame.evaluate(`(() => {
    const labelOf = ${labelOfSource};
    const selector = "a,button,input,textarea,select,[role=button],[role=tab]";
    const nodes = Array.from(document.querySelectorAll(selector)).slice(0, 150);
    const rows = [];
    for (const node of nodes) {
      const tag = node.tagName.toLowerCase();
      const type = (node.getAttribute("type") || "").toLowerCase();
      const visible = node.getClientRects().length > 0 && node.offsetParent !== null;
      if (!visible) continue;
      const oldName = (node.getAttribute("aria-label") || node.getAttribute("placeholder") || node.textContent || (node.value !== undefined ? String(node.value) : "")).replace(/\\s+/g, " ").trim().slice(0, 50);
      const label = labelOf(node);
      rows.push({ tag: tag + (type ? ":" + type : ""), old: oldName || "(空)", label: label ? label.source + "→" + label.text.slice(0, 30) : "(提取不到)" });
    }
    return rows;
  })()`).catch(() => []);
  if (!result.length) continue;
  const oldBlind = result.filter((r) => r.old === "(空)").length;
  const fixed = result.filter((r) => r.old === "(空)" && r.label !== "(提取不到)").length;
  const anyLabel = result.filter((r) => r.label !== "(提取不到)").length;
  console.log(`\n=== frame ${frame.url().slice(0, 80)}`);
  console.log(`可见可交互元素 ${result.length}；旧盲区 ${oldBlind}；A 救回 ${fixed}；A 有标签 ${anyLabel}/${result.length}`);
  for (const row of result.slice(0, 40)) console.log(`  [${row.tag}] 旧="${row.old}"  A=${row.label}`);
}
await context.close();
