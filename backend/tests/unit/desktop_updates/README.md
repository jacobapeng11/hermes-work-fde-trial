# Desktop Updates 单元测试

本目录覆盖桌面客户端自动更新源的 Backend 只读文件契约：

- Enterprise SSO 网关路径授权下的无应用层身份依赖访问；
- `latest.yml` 文本响应；
- URL 编码文件名、Range 分段下载；
- 安装包扩展名、路径穿越、目录外符号链接等文件安全边界。

运行命令：

```bash
cd backend
.venv/bin/pytest -q tests/unit/desktop_updates
```
