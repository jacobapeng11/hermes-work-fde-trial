# 用户离职清理单元测试

覆盖离职清理 DB 删除合同：

- `runtime_agent_profile_revision` 仅按目标用户及其 Profile 归属删除；
- 仍存活跨用户 Profile 的 revision 不因 `created_by_user_id` 指向离职者而被误删。

运行：

```bash
backend/.venv/bin/pytest -q backend/tests/unit/user_offboarding
```
