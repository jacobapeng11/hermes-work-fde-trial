// Phase 2: navigate into a CRM form page (project management), then measure
// the label-extraction hit rate where the agent actually got stuck.
import { chromium } from "playwright-core";
import { join } from "node:path";
import { homedir } from "node:os";

const profileDir = join(homedir(), "AppData", "Roaming", "cowork-hermes-desktop", "browser-automation", "chrome");
const executablePath = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

const context = await chromium.launchPersistentContext(profileDir, { executablePath, headless: false, viewport: null });
const page = context.pages()[0] ?? await context.newPage();
await page.goto("https://hermes-crm.gateway.example.com/Default.aspx", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => undefined);
for (let i = 0; i < 10 && !/Default\.aspx|userlogin/i.test(page.url()); i += 1) await page.waitForTimeout(2000);
await page.waitForTimeout(3000);

// Find a link or menu item mentioning 项目 (project) anywhere in the DOM,
// including inside collapsed menus; click the first hit and settle.
const candidates = await page.evaluate(() => {
  const hits = [];
  for (const node of document.querySelectorAll("a,span,li,div")) {
    const text = (node.textContent || "").replace(/\s+/g, "").slice(0, 20);
    if (/项目管理/.test(text) && node.textContent.trim().length < 12) {
      const anchor = node.closest("a") ?? (node.tagName === "A" ? node : null);
      if (anchor) hits.push({ text: text.slice(0, 12), href: anchor.getAttribute("href") || "" });
    }
  }
  return hits.slice(0, 8);
});
console.log("项目管理候选入口:", JSON.stringify(candidates));

let entered = false;
for (const candidate of candidates) {
  try {
    const target = page.locator("a", { hasText: candidate.text.slice(0, 6) }).first();
    await target.click({ timeout: 5000 });
    await page.waitForTimeout(4000);
    if (!/Default\.aspx$/i.test(page.url())) { entered = true; break; }
  } catch { /* try the next candidate */ }
}
console.log(`entered form page: ${entered} url=${page.url()}`);
await page.waitForTimeout(3000);

const report = await page.evaluate(() => {
  const labelOf = (element) => {
    const labelledBy = element.getAttribute("aria-labelledby");
    if (labelledBy) {
      const text = labelledBy.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean).map((t) => t.textContent || "").join(" ").replace(/\s+/g, " ").trim();
      if (text) return { source: "aria-labelledby", text };
    }
    const wrappingLabel = element.closest("label");
    if (wrappingLabel && wrappingLabel.textContent.trim()) return { source: "closest-label", text: wrappingLabel.textContent.replace(/\s+/g, " ").trim() };
    const id = element.getAttribute("id");
    if (id) {
      const forLabel = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (forLabel && forLabel.textContent.trim()) return { source: "label-for", text: forLabel.textContent.replace(/\s+/g, " ").trim() };
    }
    const title = element.getAttribute("title");
    if (title && title.trim()) return { source: "title", text: title.trim() };
    const cell = element.closest("td,th");
    if (cell) {
      const previous = cell.previousElementSibling;
      if (previous && previous.textContent.trim()) return { source: "prev-td", text: previous.textContent.replace(/\s+/g, " ").trim() };
    }
    return null;
  };
  const selector = "a,button,input,textarea,select,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[contenteditable=true]";
  const nodes = Array.from(document.querySelectorAll(selector)).slice(0, 250);
  const rows = [];
  for (const node of nodes) {
    const tag = node.tagName.toLowerCase();
    const type = (node.getAttribute("type") || "").toLowerCase();
    if (tag === "input" && (type === "hidden" || node.offsetParent === null)) {
      rows.push({ tag: `${tag}:${type || "?"}`, kind: "hidden-or-invisible", old: "(被过滤前会混入)", label: "-" });
      continue;
    }
    const oldName = (node.getAttribute("aria-label") || node.getAttribute("placeholder") || node.textContent || (node.value !== undefined ? String(node.value) : "")).replace(/\s+/g, " ").trim().slice(0, 50);
    const label = labelOf(node);
    rows.push({ tag: tag + (type ? `:${type}` : ""), kind: "interactive", old: oldName || "(空)", label: label ? `${label.source}→${label.text.slice(0, 30)}` : "(提取不到)" });
  }
  return rows;
});

const interactive = report.filter((r) => r.kind === "interactive");
const hiddenCount = report.length - interactive.length;
const oldBlind = interactive.filter((r) => r.old === "(空)").length;
const oldBlindFixed = interactive.filter((r) => r.old === "(空)" && r.label !== "(提取不到)").length;
const anyLabel = interactive.filter((r) => r.label !== "(提取不到)").length;
console.log(`\n表单页可交互元素: ${interactive.length}，隐藏/不可见元素(噪音): ${hiddenCount}`);
console.log(`旧方案盲区(名称为空): ${oldBlind}，其中 A 方案可救回: ${oldBlindFixed}`);
console.log(`A 方案能给出标签的元素总数: ${anyLabel}/${interactive.length}\n`);
for (const row of interactive.slice(0, 50)) {
  console.log(`[${row.tag}] 旧="${row.old}"  A=${row.label}`);
}
await context.close();
