# Runtime 维护脚本单元测试

覆盖 Runtime 维护入口的边界。当前重点验证：

- 个人/Application Runtime Endpoint 不可达或命令超时时，只有 Backend 活跃计数
  证明空闲才允许记录 deferred cancel；其它错误或仍有活跃 Run 时保持 fail closed；
- 单 Runtime 历史 failed checkpoint 若已被后续 fleet 目标恢复，重新执行时
  归档为 `failed-stale`，不误用旧镜像目标。

运行：

```bash
backend/.venv/bin/pytest -q backend/tests/unit/runtime_maintenance
```
