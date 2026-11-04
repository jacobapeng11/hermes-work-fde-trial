# Application Agent Studio 文件模块测试

本目录覆盖应用 Agent Studio 调试页面的会话文件索引、上传状态机、附件授权和 Runtime 投影。
它只验证 Backend 控制面契约；真实 Supervisor 直传、PVC 落盘和 Hermes 读取附件属于环境验收，不在单元测试中伪装通过。

运行命令：

```bash
backend/.venv/bin/pytest -q backend/tests/unit/application_agent_studio_files/
```
