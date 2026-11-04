# Runtime Edge 测试模块

## 目录职责

验证 Runtime Edge WebSocket、内部命令与 Supervisor loopback API 之间的会话/Run 路由契约。测试使用
ASGI fake 和内存 fake dependency，不触碰真实 Runtime、PVC、Kubernetes 或 Provider。

## 关键场景

- 桌面端选中的会话与 Backend `internal_turn` 提交的定时任务 Run 一致时，本地能力桥能路由到该桌面连接。
- 桌面端停在同一用户的任意其他会话（工作文件夹是用户级而非会话级）时，desktop-status 报在线且本地能力桥仍路由到该桌面；定时任务产物因此优先生成本地。
- 完全没有桌面连接时，desktop-status 报离线、本地能力桥 409，Agent 回落云端生成（兜底路径）。
- 桌面中途断连再重连，Backend 提交的活动 Run 映射保留，重连后仍能路由本地。
- 桥按 run 归属用户的 user_id/org_id 过滤回退桌面，绝不把用户 7 的 run 交给用户 8 的桌面。
- Edge WebSocket `run.submit` / `run.resume` 的既有路由行为不被内部命令路径破坏。
- Backend 专家上下文派生分支只允许调用 Hermes 官方 `/api/sessions/import`，其它 Session Dashboard 路径仍然拒绝。

## 运行命令

```bash
PYTHONPATH=runtime/supervisor backend/.venv/bin/python -m pytest \
  -q runtime/supervisor/tests/runtime_edge/test_internal_turn_local_capability.py
```
