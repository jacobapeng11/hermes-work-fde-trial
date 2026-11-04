# 应用企微用户查询回归

本模块使用内存 SQLite 验证应用企微的用户读模型，不连接实际数据库或 Runtime。

- `test_recognized_users.py`：账号绑定用户可见、旧映射兼容、跨来源去重、分页总数、搜索与组织/应用隔离。
- 权限入口使用局部 mock；真实鉴权、企微入站与 Runtime 执行链不属于本模块的集成验收范围。
- 总数表示访问过当前应用且身份已识别的独立平台用户数，包括历史撤销记录；仅绑定但未访问的账号不计入。

运行：`backend/.venv/bin/pytest -q backend/tests/unit/application_wecom/`
