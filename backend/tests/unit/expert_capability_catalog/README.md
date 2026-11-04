# 专家能力目录测试

## 目标

验证专家配置弹窗的市场 Skill 可见性契约：市场下架的公共 Skill 不得出现在“示例业务市场”
候选列表；已经绑定默认 Profile 的下架 Skill 仍保留在“已安装”列表，不影响运行时执行。

## 运行

```bash
backend/.venv/bin/pytest -q backend/tests/unit/expert_capability_catalog
```
