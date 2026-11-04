# 聊天图片输入单元测试

## 目标

覆盖模型 vision 能力序列化、附件 MIME 合同、个人企微 image-only / 混合附件 prompt 组装、
图片路径隐藏与公开历史剥离，以及图片附件的 fail-closed 门控。

## 运行

```bash
PYTHONPATH=.:backend backend/.venv/bin/python -m pytest -q backend/tests/unit/chat_image_input
```
