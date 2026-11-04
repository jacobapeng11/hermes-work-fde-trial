# 登录回归

覆盖 Backend 登录方式读取、OAuth2 路径、标签页业务票据恢复、完成时清除旧 JWT、强制刷新及错误页。
运行 `npm test -- src/authentication/`；原 `src/auth.test.ts` 继续覆盖 enterprise_idpAES 和平台会话。
此处使用 jsdom/fetch fake，不代表网关、真实提供方或 Electron 交互已经通过。
