# 个人 Runtime Bootstrap ConfigBundle 单元测试

覆盖个人 Runtime 启动包的 bootstrap 合同：

- schema 34 bootstrap bundle 不读取模型目录，digest 不随默认模型或可见模型变化；
- 旧 schema 33 bundle 继续保留模型目录投影，保证存量 revision 语义不变；
- KService 准备器把存量 `generation-N` 迁移为下一代 `bootstrap-generation-N`；
- 内部下载接口按精确 revision 前缀选择新旧构建合同，不接受任意代数字符串。

运行：

```bash
backend/.venv/bin/pytest -q backend/tests/unit/personal_runtime_bootstrap
```
