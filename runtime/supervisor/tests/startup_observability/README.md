# Supervisor 启动相位可观测性测试

## 目录职责

验证冷启动相位计时收集器与 `main.py` 启动链路打点契约：

- 相位名稳定、去重、输出不含敏感数据。
- 汇总包含每段耗时与相对进程基线的累计耗时。
- SQLite 完整性回执只有在上次检查通过、干净退出且数据库指纹未变时才可复用。
- steady_state 关键相位（存储、SQLite、ConfigBundle、Run 恢复、Hermes 启动、ready）
  的打点顺序完整，且日志在 startup complete 后输出。

## 运行命令

```bash
PYTHONPATH=runtime/supervisor backend/.venv/bin/python -m pytest -q runtime/supervisor/tests/startup_observability/
```
