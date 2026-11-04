# Hermes Work 桌面客户端

Electron 客户端与用户 Web 共享主要业务界面，并提供本地工作目录能力。平台 HTTPS origin 必须在构建前显式指定；OAuth2 允许跳转域只填写客户身份源。

```bash
npm ci
export COWORK_SERVER_ORIGIN=https://app.example.com
export OAUTH2_DESKTOP_ALLOWED_ORIGINS='["https://idp.example.com"]'
npm run typecheck
npm run build
```

将两个示例域名替换为客户真实地址，再执行 `npm run package:win` 或 `npm run package:mac`。签名、更新源与安装包分发由客户自行配置。完整环境步骤见[部署指南](../docs/deployment.md)。
