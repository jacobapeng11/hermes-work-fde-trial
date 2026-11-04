# Backend 测试目录

## 维护原则

`backend/tests/unit/` 和 `backend/tests/integration/` 中已有的扁平测试文件是历史回归区，不在本次或后续普通改造中搬迁。新的测试从业务模块目录开始组织，逐步形成清晰的测试边界。

推荐结构：

```text
backend/tests/
├── conftest.py                    # 仅跨模块基础 fixture
├── fixtures/                      # 共享测试素材，按业务模块分目录
│   └── <业务模块>/
├── unit/                          # 单元测试
│   ├── <业务模块>/
│   └── ...                         # 历史扁平测试保留，不再新增
└── integration/                   # 集成测试
    ├── <业务模块>/
    └── ...                         # 历史扁平测试保留，不再新增
```

## Skill 生命周期测试模块

本次 Skill 改造新增测试应归入 `skill_lifecycle`，覆盖以下职责：

- 单测：市场文件只保留最新版本、用户 PVC 版本清理、发布/下架状态转换、重新上架任务、个人删除清理、跨来源重名和失败重试。
- 集成测试：只保留市场发布更新、市场下架/重新上架、个人技能与市场技能重名、个人技能手动删除四条关键链路。
- 真实 Hermes/PVC 行为需要单独作为 Runtime 冒烟或环境验收记录，不与普通 Backend 集成测试混在一起。

推荐新增结构：

```text
backend/tests/
├── fixtures/
│   └── skill_lifecycle/
├── unit/
│   └── skill_lifecycle/
│       ├── test_market_storage.py
│       ├── test_profile_skill_cleanup.py
│       ├── test_skill_name_conflicts.py
│       └── test_personal_skill_deletion.py
└── integration/
    └── skill_lifecycle/
        ├── test_market_skill_flow.py
        ├── test_skill_name_conflict_flow.py
        └── test_personal_skill_deletion_flow.py
```

以上目录在有对应测试时创建，不预先创建空目录。文件名可以根据实际职责调整，但不得把一条完整业务链拆成按生产类命名的散乱测试。

## 运行方式

```bash
backend/.venv/bin/pytest -q backend/tests/unit/skill_lifecycle/
backend/.venv/bin/pytest -q backend/tests/integration/skill_lifecycle/
```

先运行新增模块，再根据跨模块影响扩大范围。测试记录应说明实际运行的目录、环境依赖和未覆盖的真实 Runtime/PVC 行为。
