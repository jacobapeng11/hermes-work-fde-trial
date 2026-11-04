# Clarify 生命周期测试

## 模块目标

验证 Runtime 内部取消命令与用户 Web Clarify 停驻语义的边界：

- Clarify 取消命令携带 `clarify_request_id`；
- Supervisor 在 Clarify 取消终局补 `chat.clarify.requested` 权威事件；
- Dashboard marker 先到时不重复写事件；
- 普通取消不产生 Clarify 事件。

## 目录职责

本目录只覆盖 Supervisor 合同和 RunManager 权威事件写入，使用临时 SQLite 与 Fake Adapter，不连接真实 Runtime、PVC、MySQL 或 Provider。

Backend Clarify 状态机、用户点击和 resume Run 由 `backend/tests/unit/runtime_clarify/` 覆盖。

## 运行命令

```bash
PYTHONPATH=runtime/supervisor backend/.venv/bin/python -m pytest -q \
  runtime/supervisor/tests/clarify_lifecycle/
```
