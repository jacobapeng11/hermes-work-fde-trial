# 企业登录 HTTP 集成

连接真实 FastAPI 路由、HTTP Cookie、OAuth2 MockTransport、字段映射和一次性存储适配器。
覆盖 PKCE 开启/关闭的 Web 与桌面完成流程、重放/浏览器绑定校验及切换开关后在换票前拒绝旧事务。
dev 显式开启 HTTP 后台接入时，Token/UserInfo 走 HTTP，浏览器授权/回调及完成流程保持 HTTPS。
真实路由传递同一 request_id 到上游失败日志和错误页；失败换票不查询/创建用户，响应和授权码不进入日志。
身份仓库、Redis 及权限使用受控替身；不声明已验证真实 MySQL/Redis、网关、enterprise_idp 或桌面 UI。
Windows fcntl 导入垫片实际调用锁时失败，仅允许测试不涉及 POSIX 文件锁的登录路径。

运行：`backend/.venv/Scripts/python.exe -m pytest -q backend/tests/integration/sso_login/`
Linux 使用 `backend/.venv/bin/pytest`。
