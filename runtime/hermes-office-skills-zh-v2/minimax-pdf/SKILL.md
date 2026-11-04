---
name: minimax-pdf
description: "中文 PDF 的创建、填表、重排、预览与校验。所有默认输出使用镜像内 Noto CJK SC 字体和离线工具。"
license: MIT
metadata:
  version: "2.0.0-zh"
  category: productivity
---

# 中文 PDF

使用镜像内的 `/opt/hermes/office-venv/bin/python`、`node`、`/usr/bin/chromium`、`pdftotext` 与 `pdffonts`。默认字体为 `Noto Sans CJK SC`，标题可使用 `Noto Serif CJK SC`；不得联网加载字体或执行任何安装命令。

## 创建

1. 用 `scripts/palette.py` 生成本地字体令牌。
2. 将正文写成 `content.json`，其中中文标题、段落、表格、图表均使用中文样例和中文标点。
3. 用 `scripts/cover.py` 生成封面 HTML，再用 `scripts/render_cover.js` 以 `/usr/bin/chromium` 渲染封面。
4. 用 `scripts/render_body.py` 渲染正文，以 `scripts/merge.py` 合并；交付前执行 `pdffonts output.pdf` 和 `pdftotext output.pdf -`。

```bash
PY=/opt/hermes/office-venv/bin/python
$PY scripts/palette.py --title "项目月度报告" --type report --out tokens.json
$PY scripts/cover.py --tokens tokens.json --out cover.html
node scripts/render_cover.js --input cover.html --out cover.pdf
$PY scripts/render_body.py --tokens tokens.json --content content.json --out body.pdf
$PY scripts/merge.py --cover cover.pdf --body body.pdf --out 项目月度报告.pdf --title "项目月度报告"
pdffonts 项目月度报告.pdf
```

## 既有 PDF

先用 `scripts/fill_inspect.py` 读取表单字段，再用 `scripts/fill_write.py` 写入用户确认的数据。重排任务使用 `scripts/reformat_parse.py` 生成内容结构后再走创建流程；不得改变未要求的业务内容。

交付前必须确认中文文本可由 `pdftotext` 提取，并且 `pdffonts` 显示 Noto 字体已嵌入。
