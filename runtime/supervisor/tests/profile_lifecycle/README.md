# Profile 生命周期投影测试

## 模块目标

验证 Supervisor 写入 Hermes 专家 Profile 时，平台基础插件、业务插件和同 revision 配置漂移遵守完整的 Profile 生命周期合同：

- `kanban-lifecycle`、`clarify-lifecycle`、`platform-main-conversation-boundary-v1` 始终保留；
- 业务插件仍由 `managed_plugin_ids` 表达，可添加和移除；
- Hermes 当前配置缺少基础插件时，同 revision `apply()` 必须判定漂移并重写；
- named 专家 Profile 必须继承默认根配置的平台 `compression` 段，缺失或漂移时同 revision 重写；
- 输出列表去重且顺序稳定。

## 目录职责

本目录只覆盖 Supervisor Profile 投影的最终配置合同，使用 Fake Dashboard、Fake Downloader 和临时目录，不连接真实 Runtime、PVC、MySQL 或 Provider。

Backend Desired 生成、权限、Rollout 和用户 Web Clarify 卡片链路由对应模块测试负责；本目录不重复覆盖。

## 关键场景

- 新建专家 Profile 初始插件为空时补齐平台基础插件；
- 业务插件在新 revision 添加和移除；
- 同 revision Hermes 配置漂移后恢复 `clarify-lifecycle`；
- 同 revision 专家 Profile `compression` 缺失或漂移后恢复平台配置；
- 重复与乱序插件输出稳定。

## 运行命令

```bash
PYTHONPATH=runtime/supervisor backend/.venv/bin/python -m pytest -q \
  runtime/supervisor/tests/profile_lifecycle/test_baseline_plugins.py
```
