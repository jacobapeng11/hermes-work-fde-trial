# Runtime Supervisor 测试目录

## 维护原则

测试根目录中已有的散乱文件保持不动，作为历史回归区。新测试按 Supervisor 负责的业务行为进入模块目录，避免继续扩大根目录的文件堆积。

推荐结构：

```text
runtime/supervisor/tests/
├── <历史测试文件>.py              # 保留，不再新增同层散乱文件
├── skill_lifecycle/
│   ├── test_projection_files.py
│   └── test_hermes_skill_reload.py
├── profile_projection/
└── runtime_edge/
```

Skill 生命周期模块只承接 Runtime 侧的代表性投影验证：当前版本替换、旧文件清理、失败恢复、幂等重试，以及 Hermes 实际技能发现状态。市场权限、MySQL 版本审计、Rollout 目标筛选和用户页面语义不在本目录重复测试。

真实 Runtime/PVC/Hermes 验收需要单独的环境测试入口和授权，不把环境副作用隐藏在普通单元测试中。

## 运行方式

```bash
python -m pytest -q runtime/supervisor/tests/skill_lifecycle/
```

模块有实际测试内容后再创建目录。新增测试应能单独运行，并在测试说明或提交记录中标明是否使用 fake Hermes、临时目录或真实 Runtime 环境。
