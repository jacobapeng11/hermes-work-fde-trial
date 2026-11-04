# Projection Outbox 测试

本目录覆盖 Supervisor 本地 SQLite `projection_outbox` 的查询与索引契约：

- 待发送投影按 `outbox_id` 顺序读取；
- `acknowledged_at IS NULL` 查询必须使用部分索引，避免历史已确认行增长后全表扫描；
- 重启后索引和待发送队列语义保持稳定。

运行命令：

```bash
python -m pytest -q runtime/supervisor/tests/projection_outbox/
```
