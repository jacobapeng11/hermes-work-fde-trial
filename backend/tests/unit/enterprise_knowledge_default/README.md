# 新用户企业知识默认开关测试

## 目标

验证新个人 Runtime Workspace 创建时默认启用企业知识检索，并且设置读取结果为
`enabled=true, mode=full`。历史用户未显式修改前不在此模块批量迁移。

## 运行

```bash
backend/.venv/bin/pytest -q backend/tests/unit/enterprise_knowledge_default
```
