# 任务提示配置单元测试

## 模块目标

验证组织级任务提示配置的模型契约、Pydantic 校验、MySQL/SQLite 持久化、乐观锁和管理端/用户端 API 边界。

## 目录职责

- `test_task_prompt_config.py`：配置文档校验、保存与冲突行为、Admin/User 路由组织隔离。

## 运行命令

```bash
backend/.venv/bin/pytest -q backend/tests/unit/task_prompt_config
```
