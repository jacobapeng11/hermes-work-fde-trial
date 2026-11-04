# Model Visibility 单元测试

## 模块目标

验证模型目录的人员/部门授权契约、发布事务、运行时可见性和应用 Agent 配置防线。测试只使用 SQLite 内存库或纯函数/Fake，不接触真实 MySQL、Redis、Runtime、PVC 或 Provider。

## 目录职责

- `test_model_catalog_scope.py`：管理端模型目录 scope 契约、授权目标校验、规范化、Permission 事务替换和回读。
- `test_company_root_scope.py`：公司虚拟根作为模型可见目标的发布校验、冗余归并和运行时授权衔接。
- `test_runtime_visibility.py`：运行时模型可见性、部门祖先继承、默认模型回退、缓存 key 和查询边界。
- `test_application_agent_model.py`：应用 Agent 创建者模型权限、保存防线和失效配置处理。
- `test_admin_directory_tree.py`：Admin 模型权限组织/部门/人员混合目录端点与鉴权边界。
- 后续运行时与应用 Agent 行为测试按行为边界拆分文件，不合并成全局大测试。

## 运行

```bash
backend/.venv/bin/pytest -q backend/tests/unit/model_visibility/
```
