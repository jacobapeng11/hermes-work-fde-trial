# 桌面认证回归

此模块以纯函数和模拟 Response 验证 Enterprise SSO 网关准入与平台 OAuth2 的边界，不调用真实服务器或接触登录凭据。

- `gateway-auth.test.mjs`：识别已确认的网关 HTML；先完成网关认证再重试配置接口；取消/错误配置不得继续；精确导航白名单和平台 Cookie 隔离。
- 主窗口的认证完成/错误文档路由继续由 `../desktop-routing.test.mjs` 覆盖。
- 单测不替代真实 Enterprise SSO、IDT、Cookie 交接、桌面登录和聊天 TTFT 验收。

运行：`npm --prefix cowork-hermes-desktop run test:desktop`。
