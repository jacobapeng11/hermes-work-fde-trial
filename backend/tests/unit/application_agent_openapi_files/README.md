# Application Agent OpenAPI 文件模块测试

本目录覆盖 OpenAPI Conversation 生成文件的控制面索引、签名下载和 API 契约。
文件正文仍在 Runtime PVC；本目录只验证 Backend 元数据、授权和代理边界，不伪装真实 Runtime 文件验收。

运行命令：

```bash
backend/.venv/bin/pytest -q backend/tests/unit/application_agent_openapi_files/
```
