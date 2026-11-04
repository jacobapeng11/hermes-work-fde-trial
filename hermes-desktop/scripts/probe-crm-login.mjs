// Interactive probe: open the REAL desktop automation profile, land on the
// CRM URL, and wait for the user to finish BOTH the Enterprise SSO portal login and
// the CRM application login. Then dump cookie persistence so we know whether
// future automation sessions stay signed in.
import { chromium } from "playwright-core";
import { join } from "node:path";
import { homedir } from "node:os";

const profileDir = join(homedir(), "AppData", "Roaming", "cowork-hermes-desktop", "browser-automation", "chrome");
const executablePath = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

console.log(`profile: ${profileDir}`);
const context = await chromium.launchPersistentContext(profileDir, {
  executablePath,
  headless: false,
  viewport: null,
  acceptDownloads: true,
});
const page = context.pages()[0] ?? await context.newPage();
page.on("framenavigated", (frame) => {
  if (frame === page.mainFrame()) console.log(`[NAV] ${frame.url().slice(0, 120)}`);
});
await page.goto("https://hermes-crm.gateway.example.com/Default.aspx", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => undefined);

console.log("");
console.log(">>> 请在弹出的 Chrome 窗口中操作：");
console.log(">>> 1) 如果出现 Enterprise SSO 认证页，完成账号密码登录；");
console.log(">>> 2) 到达 CRM 登录页（示例业务销售管理系统）后，再登录一次 CRM；");
console.log(">>> 全程不要关闭窗口，脚本自动检测。");
console.log("");

const done = async () => {
  await page.waitForTimeout(3000);
  const all = await context.cookies();
  console.log("--- 最终 profile cookie 清单 ---");
  for (const domain of [...new Set(all.map((c) => c.domain))]) {
    console.log(`domain=${domain}`);
    for (const c of all.filter((c) => c.domain === domain)) {
      const expiry = c.expires > 0 ? new Date(c.expires * 1000).toISOString().slice(0, 16).replace("T", " ") : "";
      console.log(`  ${c.name}  persistent=${c.expires > 0 ? `YES (至 ${expiry})` : "NO(会话级，关浏览器失效)"}`);
    }
  }
  console.log("浏览器窗口保持打开 15 秒供核对后自动关闭 ...");
  await page.waitForTimeout(15000).catch(() => undefined);
};

let lastUrl = "";
try {
  for (let tick = 1; tick <= 60; tick += 1) {
    await page.waitForTimeout(3000);
    const url = page.url();
    const title = await page.title().catch(() => "");
    if (url !== lastUrl) {
      lastUrl = url;
      console.log(`t+${tick * 3}s url=${url.slice(0, 100)} title=${title.slice(0, 40)}`);
    }
    const onPortalOrLogin = /sdpc\.demo\.com|enterprise_idp|userlogin|login/i.test(url) || /登录|登陆|统一身份/.test(title);
    if (!onPortalOrLogin && /Default\.aspx/i.test(url)) {
      console.log(`检测到已进入 CRM 主页面：${url}`);
      await done();
      process.exit(0);
    }
  }
  console.log("3 分钟内未检测到完成登录（仍停在认证/登录页）");
  await done();
} catch (error) {
  console.log(`窗口被关闭或会话中断：${error instanceof Error ? error.message.slice(0, 160) : String(error)}`);
  console.log("已中断，未输出最终 cookie（持久 cookie 若已种下仍然保留在 profile 中）。");
} finally {
  await context.close().catch(() => undefined);
}
