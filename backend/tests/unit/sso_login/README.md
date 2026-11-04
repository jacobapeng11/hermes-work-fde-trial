# 企业登录单元测试

覆盖配置选择、标准 OAuth2 请求、可信工号、一次性浏览器绑定、重放及配置失效。
包含 PKCE 默认 S256、显式关闭时无 challenge/verifier、Basic/Post 两种认证及保留参数不可覆盖。
`test_http_backchannel.py` 验证默认拒绝 HTTP、仅 dev 显式启用 Token/UserInfo HTTP、浏览器 URL 仍要求 HTTPS、
非法 URL/非 dev 环境拒绝、事务摘要失效及原 POST/Bearer 请求合同；不验证真实 IDT 是否监听 HTTP 端口。
HTTP 使用 MockTransport，Redis 使用受控 fake；不调用实际身份服务、MySQL 或 Runtime。
`test_diagnostics.py` 验证上游失败的请求编号/阶段关联、状态和 MIME 类别、Token 校验与传输异常分类，
并用敏感值哨兵确认凭据、响应、异常原文及无效请求编号不进入日志。

Windows 收集登录路由时会经过已有 Backend Runtime 模块的 fcntl 导入；本目录仅提供导入垫片，
实际调用文件锁会失败，不能用这些结果声明 Linux 文件锁、PVC 或真实 Runtime 已通过。

运行：`backend/.venv/Scripts/python.exe -m pytest -q backend/tests/unit/sso_login/`
Linux 使用同一虚拟环境的 `backend/.venv/bin/pytest`。
