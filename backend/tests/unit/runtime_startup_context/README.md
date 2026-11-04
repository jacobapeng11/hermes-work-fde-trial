# Runtime 启动提示语境测试

## 目标

验证个人 Runtime 冷启动提示只依赖两类事实：用户真实登录次数，以及精确
Knative Service 下是否存在 `Running + Ready` Pod。接口不激活 Runtime、
不写数据库；Kubernetes 查询异常时统一降级为 `none`。

## 运行

```bash
backend/.venv/bin/pytest -q backend/tests/unit/runtime_startup_context
```
