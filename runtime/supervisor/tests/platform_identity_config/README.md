# 平台身份配置边界测试

## 目标

验证个人 Runtime 的 `memories/USER.md` 与 `memories/MEMORY.md` 是 Hermes 动态数据：
Supervisor 配置刷新不读取、不备份、不创建它们；平台身份由签名 ConfigBundle 中的
`SOUL.md` 承载。Application Runtime 的应用级 `USER.md` 仍保持既有受管行为。

## 运行

```bash
PYTHONPATH=runtime/supervisor backend/.venv/bin/python -m pytest -q \
  runtime/supervisor/tests/platform_identity_config
```
