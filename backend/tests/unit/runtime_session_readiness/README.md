# Runtime Session readiness 单元测试

覆盖个人 Runtime 首次可用前的默认 Profile 执行门禁：

- 默认 Profile 存在但首个 Desired Revision 尚未 Applied 时，返回可重试的 `runtime_agent_profile_preparing`。
- Profile 真不存在时继续保留原有 `runtime agent profile not found` 语义。
- API 层将准备中映射为 503，并通过 `Retry-After` 提示客户端重试。

运行：

```bash
backend/.venv/bin/pytest -q backend/tests/unit/runtime_session_readiness/test_default_profile_readiness.py
```
