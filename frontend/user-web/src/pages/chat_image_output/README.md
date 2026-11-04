# 聊天 Agent 图片输出测试

该模块验证 Runtime 产生的图片 artifact 如何按 `runtimeRunId` 安全投影回
Assistant 聊天气泡，并隐藏 Hermes 的 MEDIA 内部路径标记。历史回放、实时文件索引刷新和用户上传附件互不混淆。

运行：

```bash
npm --prefix frontend/user-web test -- src/pages/chat_image_output
```
