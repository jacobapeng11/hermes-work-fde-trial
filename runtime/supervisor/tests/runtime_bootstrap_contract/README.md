# Runtime Bootstrap Config Revision 测试

验证 Supervisor 能识别 `bootstrap-generation-N` 启动配置 revision：

- 下载 initial ConfigBundle 时透传精确 revision；
- bootstrap generation 高于 PVC 中旧 `generation-N` 时允许发布新启动包；
- 本地 current-config 状态记录新 revision。

运行：

```bash
PYTHONPATH=runtime/supervisor backend/.venv/bin/python -m pytest -q runtime/supervisor/tests/runtime_bootstrap_contract
```
