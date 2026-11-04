# Runtime 会话精确读取测试

## 模块目标

验证个人 Runtime Session 的单条只读读取契约，支撑看板区编辑入口找回不在最近 50 条列表中的来源会话。

## 覆盖范围

- 按 `runtime_workspace_id + runtime_session_id` 读取当前用户可读的会话摘要。
- 会话不存在、已删除或不属于当前用户时返回 HTTP 404。
- 不覆盖列表排序、Run 历史、Runtime Edge 或真实 Kubernetes 行为。

## 运行命令

```bash
backend/.venv/bin/pytest -q backend/tests/unit/runtime_session_lookup/
```
