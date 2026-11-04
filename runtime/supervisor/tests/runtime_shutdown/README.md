# Runtime Shutdown 测试模块

## 目标

验证 Supervisor 关闭路径的子进程停止超时契约：空闲 Pod 快速释放 PVC
单写锁，活跃 Run 保持长排空语义。

## 目录职责

- `test_child_stop_timeout.py`：`shutdown_child_stop_timeout` 的分支单测。

## 运行

```bash
PYTHONPATH=runtime/supervisor backend/.venv/bin/python -m pytest \
  -q runtime/supervisor/tests/runtime_shutdown/test_child_stop_timeout.py
```
