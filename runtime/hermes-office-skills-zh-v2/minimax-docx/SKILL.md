---
name: minimax-docx
description: "使用镜像内 OpenXML CLI 创建、编辑、校验和预览中文 DOCX 文档。"
license: MIT
metadata:
  version: "2.0.0-zh"
  category: productivity
---

# 中文 DOCX

只使用已发布的 CLI：`/opt/hermes/office-bin/minimax-docx/MiniMaxAIDocx.Cli`。新建文档默认以 `Noto Sans CJK SC` 作为正文、`Noto Serif CJK SC` 作为报告标题；CLI 会为 `ascii`、`highAnsi`、`eastAsia`、`complexScript` 写入一致字体映射。

## 创建与校验

```bash
CLI=/opt/hermes/office-bin/minimax-docx/MiniMaxAIDocx.Cli
$CLI create --output 项目报告.docx --type report --title "项目报告" --author "用户" --page-size a4 --header "项目组" --footer "内部资料" --content-json content.json
$CLI validate --input 项目报告.docx
soffice -env:UserInstallation=file:///tmp/lo-profile --headless --norestore --convert-to pdf --outdir preview 项目报告.docx
```

`content.json` 使用中文章节、正文、表格与日期示例。编辑已有文件前先执行 `$CLI analyze --input 原文件.docx`，只修改用户指定内容；完成后执行 `$CLI diff --before 原文件.docx --after 新文件.docx` 与 `$CLI validate --input 新文件.docx`。

不得执行 `dotnet restore`、在线 NuGet 引用、字体安装或全局工具安装。用户要求宋体、微软雅黑等非内置字体时，说明需由用户提供具备再分发授权的字体资产，默认不替换为该字体。
