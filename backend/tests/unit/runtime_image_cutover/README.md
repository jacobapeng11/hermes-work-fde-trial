# Runtime 镜像懒切换单元测试

## 目录职责

验证 Runtime 镜像全量切换中“缩零个人 Runtime 延后激活”的控制面契约：

- 延后切换后的 Binding 终态必须保持登录准入可用（`ready + pending_reload`）。
- 缺席 KService 只允许在显式 `--allow-absent` 验收下通过目标镜像校验。
- zero-scale 预创建必须显式启用 `initial-scale=0`，只等待 KService Ready，
  不得调用 Runtime health 激活 Pod。
- 登录 reconcile 只有在目标 KService 精确匹配且无 Pod 时才可直接激活；
  控制脚本必须用显式 `--precreated-zero-scale` 验收，不得伪装 live imageID。
- 预创建完成后的 MySQL Binding 必须保持登录准入可用（`ready + pending_reload`）。
- 预创建阶段不得重排队 Profile/MCP 投影；登录激活成功后才恢复投影重试。
- Fleet 编排必须按活 Pod 分类 eager/deferred，且旧 checkpoint 在 Pod 归零后不得猜测分类。

## 运行命令

```bash
backend/.venv/bin/pytest -q backend/tests/unit/runtime_image_cutover/
```
