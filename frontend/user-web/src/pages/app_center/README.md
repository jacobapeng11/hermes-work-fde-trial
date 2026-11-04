# 看板区前端测试

## 模块目标

覆盖个人工作区看板区与聊天之间的 HTML 预览、分享、编辑和更新确认链路。测试使用组件级 API Mock，不连接真实 Backend、Runtime 或 Hermes，也不替代真实环境验收。

## 目录职责

- `AgentWorkspaceHtmlPreview.test.tsx`：HTML/ZIP 产物预览、当前会话隔离、已分享状态、后续版本刷新以及更新看板区确认。
- `AgentWorkspaceEditApp.test.tsx`：从看板区点击「编辑」后的来源会话复用、最近列表外精确读取、不可用来源直接新建、只读当前会话边界和预填提示词，「新建看板」方式一典型问题的独立会话创建，以及方式二广场模板「编辑」的模板包下载、独立会话创建与附件/提示词注入。

## 关键场景

- 当前打开其他任务时，编辑看板仍回到 `sourceSessionId` 对应的可编辑会话，不新建任务。
- 当前会话只读时，仍可使用可编辑的来源会话并注入看板 ZIP。
- 来源会话不在最近 50 条列表时，通过单会话 GET 找回并进入旧会话。
- 看板没有来源会话、来源会话不存在或不可编辑时，直接新建会话，不回退当前会话。
- 预填提示词以本次附件为最新基线，要求最小修改并把根目录含 `index.html` 的 ZIP 交付到本次任务文件目录。
- 「新建看板」为三段式（看板介绍 / 方式一提示词 / 方式二模板），不展示创建方式 Tab 或 ZIP 上传；点击典型问题后新建独立会话并预填完整问题，不自动发送。
- 方式二展示广场全部看板且不受 `showBoardGallery` 控制；点击卡片「编辑」后下载模板 ZIP、新建独立会话并同时注入附件与模板提示词，不回来源会话、不产生指向他人看板的更新上下文。
- 新建任务后，旧任务迟到的 `chat-files` 响应不得自动打开 HTML 预览，也不得下载旧任务 HTML。

## 运行命令

```bash
npm --prefix frontend/user-web test -- src/pages/app_center/AgentWorkspaceEditApp.test.tsx
npm --prefix frontend/user-web test -- src/pages/app_center/AgentWorkspaceHtmlPreview.test.tsx
```
