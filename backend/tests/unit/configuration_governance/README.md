# 配置治理隔离与报告契约

本模块验证原生启动与候选配置隔离、原生/候选 schema 对齐、只读状态判定和脱敏边界。
所有单测使用临时目录或 fake，不连接真实 MySQL/Kubernetes，不启动 Backend/Runtime。

```bash
backend/.venv/bin/pytest -q backend/tests/unit/configuration_governance/
```

真实服务读取另用 `scripts/config-governance-status.py --live`，不能将单测当作重启、
PVC 单写、聊天或 TTFT 实测通过。
