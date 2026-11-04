# Runtime Warm Keepalive 测试模块

## 目标

验证 Backend 保活决策契约：WebUI/桌面 WebSocket 在线或 Run 执行中永不缩容，两者空闲后按 idle TTL 继续保温，过期或投影陈旧时停止保活且不唤醒已缩零 Runtime。

## 目录职责

- `test_keepalive_decision.py`：`decide_warm_keepalive` 纯函数分支单测。

## 运行

```bash
backend/.venv/bin/pytest -q backend/tests/unit/runtime_warm_keepalive/test_keepalive_decision.py
```
