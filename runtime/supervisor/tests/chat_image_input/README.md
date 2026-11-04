# Runtime 聊天图片输入测试

## 目标

验证执行面附件目标的校验、SQLite source detail 绑定、Dashboard `image.attach`
先于 `prompt.submit`、图片/文件附件 Prompt 分流，以及公开历史投影不泄露 Runtime 相对路径。

## 运行

```bash
PYTHONPATH=runtime/supervisor backend/.venv/bin/python -m pytest -q runtime/supervisor/tests/chat_image_input
```
