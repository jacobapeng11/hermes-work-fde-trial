# Model Relay 生命周期测试

## 模块目标

验证 `active_run` 模型线路授权与 Run 生命周期对齐：执行态续租、终态立即回收、
孤儿路由按原 TTL 过期，以及 Relay 内部错误到用户可见 `chat.error` 的受控翻译。

## 目录职责

- `test_active_run_route_renewal.py`：Registry 精确续租契约、Router 续租任务生命周期、
  post-turn 授权隔离和公共错误消息映射。

## 运行命令

```bash
PYTHONPATH=runtime/supervisor backend/.venv/bin/python -m pytest -q \
  runtime/supervisor/tests/model_relay_lifecycle/test_active_run_route_renewal.py
```

真实 Runtime 镜像切换、KService reset 和 Provider 调用不在本目录测试范围内。
