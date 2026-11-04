# user_directory 单元测试

## 模块目标

验证组织目录树的公司层构建合同：com_id=49（示例公司）作为唯一一级公司节点，51（示例业务公司）、53（示例子公司）作为其下二级公司节点；未知 com_id 部门兜底挂入一级公司；无有效部门的用户按 com_id 直挂公司节点；双向无效的用户维持 `virtual:unassigned`；公司层与未归属节点不允许进入专家可见范围持久化合同。

## 目录职责

- `test_company_directory_tree.py`：公司分组、示例子公司直挂人员、断链根平铺、未知 com_id 兜底、搜索剪枝和 scope 防御过滤的纯函数单元测试。
- 仓库根的历史扁平测试 `tests/unit/test_user_directory.py` 继续覆盖路由注册、循环防护等既有回归，不在此重复。

## 运行命令

```bash
backend/.venv/bin/pytest -q backend/tests/unit/user_directory/test_company_directory_tree.py
```
