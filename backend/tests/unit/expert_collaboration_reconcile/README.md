# Expert Collaboration Reconcile 测试模块

## 目标

验证专家看板协作 Reconciler 的等待期调度契约：无进展等待使用指数退避，状态变化重置退避，超过最大等待次数后终态化，避免固定两秒轮询把空闲 Knative Runtime 永久钉在一副本。

## 目录职责

- `conftest.py`：模块内共享的最小 SQLite 数据库、Fake Profile Adapter、Fake Runner 与看板 create 事件构造器。
- `test_wait_backoff.py`：等待退避、重置、耗尽终态化与 `current_schema.sql` 列登记的单元契约。

## 运行

```bash
backend/.venv/bin/pytest -q backend/tests/unit/expert_collaboration_reconcile/test_wait_backoff.py
```
