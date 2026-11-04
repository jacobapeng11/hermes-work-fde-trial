# Runtime Session 上下文派生分支测试

覆盖专家派生 Session 的控制面行为：

- MySQL ChatLog 用户可见正文复制、来源标记与消息计数。
- 源会话活动 Run 拒绝，避免复制不完整快照。
- 终态 Run 的 ChatLog 投影未完成时拒绝派生，避免并发投影窗口导出空上下文。
- 会话列表按 50 条逻辑任务分页，并精确附带当前上下文链头；隐藏旧节点不占用列表上限。
- Hermes 官方 Session Import 载荷只包含安全可见正文。
- `current_schema.sql` 的来源字段与索引登记。

运行：

```bash
backend/.venv/bin/pytest -q backend/tests/unit/runtime_session_context_branch/
```
