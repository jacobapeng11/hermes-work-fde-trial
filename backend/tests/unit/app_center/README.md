# App Center 单元测试

本目录覆盖看板区阶段 1 的 Backend 业务边界：

- 看板包格式与安全校验；
- 创建、幂等、权限、列表、复制、更新和下载的服务端权限契约；
- 自托管 `index.html` 与 ZIP 静态资源直出、返回平台注入、缩略图豁免、安全响应头和路径边界；
- 活数据报表冻结 manifest 校验、平台 session 壳页路由、安全数据注入、返回平台注入、CSP 边界以及 link/scope 打开授权；
- `current_schema.sql` 与 SQLAlchemy 模型的看板区表契约。

运行命令：

```bash
backend/.venv/bin/pytest -q backend/tests/unit/app_center/
```

本目录使用 fake service 和 SQLite 内存库，不访问真实 MySQL、Runtime/PVC/Hermes。
