# Runtime 上下文压缩公开提示测试

## 目标

验证 Hermes 0.20.1 上游上下文压缩耗尽错误在 Supervisor 公开事件中的稳定中文投影，
同时确保普通终端错误不被误改写。

## 运行

```bash
PYTHONPATH=runtime/supervisor backend/.venv/bin/python -m pytest -q runtime/supervisor/tests/context_compression
```
