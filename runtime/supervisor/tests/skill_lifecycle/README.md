# Skill 生命周期测试

验证 Supervisor 侧 Skill 配置投影、运行期缓存边界、失败恢复和 Hermes 技能发现状态。

当前模块包含：

- `test_current_release_cleanup.py`：当前版本投影后的历史 Release 清理。
- `test_existing_projection_cleanup.py`：复用既有投影时的旧目录清理。
- `test_config_cache_apply.py`：ConfigBundle apply 不把 Hermes 运行期缓存当作配置权威。
- `test_removal_bundle.py`：应用 Skill removal bundle 先隔离运行期变更，不读取
  symlink / 超限残留；成功后清理，reload 失败可回滚，并收敛为运行期只读权限。

运行：

```bash
PYTHONPATH=runtime/supervisor backend/.venv/bin/python -m pytest -q \
  runtime/supervisor/tests/skill_lifecycle
```

本目录测试使用临时目录、fake adapter/fake Hermes 和 mock transport，不访问真实 PVC、
Kubernetes 或外部服务。
