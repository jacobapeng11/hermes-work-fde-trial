# 用户 Web Clarify 闭环

本模块覆盖用户 Web 持久化 Clarify 的控制面状态机、自动默认扫描、幂等续跑、专家看板
worker 澄清分流和数据库契约。
测试使用 SQLite 内存表与 fake Runtime 命令，不连接真实 MySQL、Runtime、PVC 或 Provider。

文件职责：

- `test_lifecycle.py`：web_run 停驻、回答 claim、续跑等待与失败恢复。
- `test_scanner.py`：lifespan scanner 的自动默认与重试领取。
- `test_expert_worker.py`：专家 worker 分流（fake session）、就地 finalize、
  schema/模型契约；不依赖 aiosqlite，可在受限沙箱直接运行。
- `test_database_contract.py`：模型列与 `current_schema.sql` 增量登记契约。

```bash
backend/.venv/bin/pytest -q backend/tests/unit/runtime_clarify/
```

真实 60 秒自动继续、冷启动失败恢复、多副本部署和 Hermes 会话结果仍需按维护流程另行验收。
