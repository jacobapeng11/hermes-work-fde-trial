# WeKnora Rerank 治理单元测试

本目录覆盖管理端 WeKnora Rerank 默认模型配置的服务适配和 Admin API 契约：

- WeKnora 模型目录解析、显式配置与自动选择；
- 保存前激活模型校验；
- 完整 retrieval-config 回写，避免覆盖其它检索参数；
- 上游失败、响应契约错误和错误码映射；
- Admin API 响应脱敏、模型/关闭模式契约与审计记录；
- Hermes 侧“使用模型/关闭重排”模式持久化。

运行命令：

```bash
backend/.venv/bin/pytest -q backend/tests/unit/weknora_rerank_governance/
```
