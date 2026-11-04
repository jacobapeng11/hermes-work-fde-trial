# 平台身份 ConfigBundle 投影测试

## 目标

验证个人 Runtime 的平台身份和文件交付规则由签名 ConfigBundle 渲染到 `SOUL.md`；
`USER.md` / `MEMORY.md` 归 Hermes 动态维护，存量平台托管块只在 Runtime 离线预检时迁移清理。

## 运行

```bash
backend/.venv/bin/pytest -q backend/tests/unit/platform_identity_config
```
