# Desktop packaging tests

This module guards release assets and packaging entry points. The tests verify
the PNG runtime taskbar asset, Windows ICO and macOS ICNS sizes, icon entry
point references, and the generated Windows installer download URL.
They also guard the backend-environment server-origin pipeline used by Windows
and macOS packaging, and the macOS signing boundary where the desktop build
performs recursive Developer ID code signing while Apple Notary submission is
handled externally.
The update-manifest test additionally verifies that artifacts regenerated after
external Windows signing keep electron-updater's blockmap v2 and sha512 contract.

`desktop-build-config.test.mjs` 加载真实 Electron Vite 配置，验证显式服务器地址会固化到主进程、非法 Origin 被拒绝，以及未配置时保留原运行时回退。它只检查构建配置，不调用服务器或真实 SSO。

Run with:

```bash
npm --prefix cowork-hermes-desktop run test:desktop
```
