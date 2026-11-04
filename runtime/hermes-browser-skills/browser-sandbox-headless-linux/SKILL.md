---
name: browser-sandbox-headless-linux
description: browser_* 报 chrome-not-running 时自启 chromium 接 CDP；任务结束必须关 tab/浏览器防内存堆积；公司内网登录墙则停。
version: 1.1.0
author: hermes-curator
license: internal
metadata:
  hermes:
    tags: [browser, headless, linux, cdp, zero-trust, login-wall]
    related_skills: [hermes-browser-zh]
---

# 无头 Linux 沙箱的浏览器 bring-up（Hermes browser_* 工具）

## When to Use

触发条件：
- `browser_exec`/browser_* 工具报 `chrome-not-running: no supported Chromium-family browser is running`；
- 日志提示「检测不到浏览器 / no supported Chromium」；
- 需要访问 **公司内网 / 零信任网关保护页面**（如示例业务 CRM `*.gateway.example.com`）。

本平台沙箱是无图形界面的 headless Linux，没有默认在跑的用户浏览器。`browser-exec`/browser_* 类的 harness 默认要求**系统里有一个带调试端口、且 profile 被它能识别的 Chromium 在跑**，否则工具调用直接报上述错误。

## 根因（来自 browser_harness/daemon.py）

- `supported_browser_running()`（POSIX 分支）：遍历 `_LINUX_PROFILES`（`~/.config/chromium` 等），读每个 profile 的 `SingletonLock` 符号链接，解析末尾 PID，`os.kill(pid,0)` 探活。
- 连接顺序：`BU_CDP_WS`/`BU_CDP_URL` 环境变量 → 各 profile 的 `DevToolsActivePort` → 最后探测端口 **9222/9223** 的 `/json/version`。
- harness 只「认」能通过上面任一路径找到的浏览器；它自己不会在 headless 下帮你把浏览器拉起来。

## 正解步骤（已验证可用）

1. 先确认系统有 chromium 可执行文件（本镜像在 `/usr/bin/chromium`，Chrome 152）。
2. **用后台进程自启 chromium，必须指定标准 profile 路径**（`$HOME/.config/chromium`）：

```bash
/usr/bin/chromium --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --remote-debugging-port=9222 --user-data-dir=$HOME/.config/chromium about:blank
```

   ⚠️ **不要用自定义 `--user-data-dir=/tmp/xxx`**。否则 `SingletonLock` 不在标准 profile 目录下，`supported_browser_running()` 返回 False，harness 依然报 chrome-not-running——这是最隐蔽的坑。
3. 自检：
   - `ls -l $HOME/.config/chromium/SingletonLock` 存在、指向新起的 PID；
   - `curl -s http://127.0.0.1:9222/json/version` 返回 Browser/Chrome 版本。
4. 再调 `browser_exec`——harness 的 `ensure_daemon()` 有「stale daemon / closed Chrome」自愈逻辑，会通过 9222 probe 连上。
5. 若之前有残留僵尸 chromium，先 `pkill -f` 清掉再起新的，避免 SingletonLock 指向死 PID。
6. 若 9222 上已有活着的 Chromium：先 `list_tabs()` 检查历史残留 tab；确认无并行任务在用时，关闭全部非 `about:blank` 旧 tab 后再开始新任务（详见下节「任务收尾」）。

## 任务收尾（必须执行，防内存泄漏）

本技能启动的是**共享常驻 Chromium**：tab 不会随任务结束自动关闭，browser_harness daemon
也没有空闲回收；hermes 原生 browser_* 的 120s 空闲清理只管 agent-browser 会话，管不到这套
共享浏览器。每个残留 tab 常驻一个 120–200MB 的 renderer 进程，广告多的页面（如百度）还会挂
几十个 iframe——不清理会把 Runtime 内存从 300MB 级推到 1.5GB+（2026-10-03 已实际发生）。

每次浏览器任务**必须**按以下顺序收尾：

1. **任务结束前，关闭本次自建的 tab**（`browser_exec` 内直接用 helper 名）：

   ```python
   close_tab(current_tab())
   ```

   一次任务开了多个 tab 时，先 `list_tabs()` 核对，把本次打开的逐个 `switch_tab()` +
   `close_tab()`；只关自己开的 tab，不要关其他并行任务的 tab。

2. **整轮浏览器任务全部结束后，彻底关闭 Chromium 释放内存**：

   ```bash
   # 精确终止本技能启动的调试 Chromium（别 pkill 掉无关 chromium）
   pkill -f 'chromium .*remote-debugging-port=9222'
   ```

   下次需要时按上文重新 bring-up 即可；SingletonLock 指向死 PID 时 harness 会自愈。

3. 由 Bash 启动的其他后台服务（如 `python3 -m http.server`）同理：任务结束立即关闭，
   不留长驻进程。

## browser_exec 的 Python helper 导入名（常见踩坑）

`browser_exec` 的 `code` 里有 pre-imported helper（`new_tab` / `goto_url` / `page_info` / `js` / `fill_input` 等），但它们**不通过 `import google_utils` 或 `import tools.browser` 引入**——直接用名字即可。若误写 `from google_utils import ...` 或 `from tools.browser import ...` 会报 `ModuleNotFoundError`，此时**去掉 import 行、直接用 helper 名字**即可；若 helper 名仍解析不到，且浏览器 bring-up 非本任务重点，可退而用 MCP `parse_url` 抓取页面内容（对纯文本抓取同样有效，且不依赖浏览器状态），不必死磕 browser_exec。

## 失败时别走的弯路

- 不用手动导出 `BU_CDP_WS` / `BU_CDP_URL`（那是给专用自动化 Chrome/profile 用的；标准 profile + 探活已足够）。
- 不要把失败当成「浏览器工具坏了」——是环境没有在跑的浏览器，正解就是自启。
- harness daemon 是常驻进程且可能 stale：报错后直接再次 `browser_exec` 通常能触发自愈，先别急着翻日志。

## 企业内网 / 零信任登录墙（该停就停）
访问**公司内网系统**（如示例业务 CRM）时，沙箱是从内网外的网关 IP 访问的，很容易被示例公司 0 信任网关拦截：

- 现象：访问 `https://xxx.gateway.example.com/...` 后被 302 到 `gateway.example.com:6440/portal/shortcut.html` → 再到 `identity.example.com/ac_portal/...`，页面只显示登录框（如 "English" 语言切换按钮），标题 Loading...
- **处理铁律**：这是企业的登录墙，**不猜账号密码、不尝试绕过、不替用户输凭证**。如实报告 302 跳转链路，明确告知这是零信任单点登录认证页，请用户在自己的电脑上完成认证（或提供合规登录方式）。
- 产出：把重定向后的真实 URL、页面可见内容（body innerText、title）一并报给用户，停止继续自动化。

## 关联参考

- `references/sina-news-realtime.md` — 抓取新浪新闻当日实时头条的已验证方法（`parse_url` 直接解析 `https://news.sina.com.cn/`，不必启动浏览器；含大结果 JSON 解析模板）。当用户问"今天有什么新闻/热点"而 search_web 返回空或旧数据时，先看这篇。
