---
name: pptx-generator
description: "使用镜像内 PptxGenJS 创建、编辑、预览中文 PPTX 演示文稿，默认使用 Noto Sans CJK SC。"
license: MIT
metadata:
  version: "2.0.0-zh"
  category: productivity
---

# 中文 PPTX

使用镜像内锁定的 `pptxgenjs`：`NODE_PATH=/opt/hermes/office-node/node_modules` 已配置。所有标题、正文、图表、页码和备注默认显式写入 `fontFace: "Noto Sans CJK SC"`；不要使用系统未内置字体或在线字体。

```javascript
const pptxgen = require('pptxgenjs');
const pptx = new pptxgen();
pptx.layout = 'LAYOUT_16x9';
const slide = pptx.addSlide();
slide.addText('项目进展汇报', { x: 0.7, y: 0.7, w: 8.8, h: 0.7,
  fontFace: 'Noto Sans CJK SC', fontSize: 30, bold: true, color: '17365D' });
slide.addText('本页说明本周交付、风险与下周计划。', { x: 0.9, y: 1.8, w: 8, h: 1,
  fontFace: 'Noto Sans CJK SC', fontSize: 20, color: '222222' });
pptx.writeFile({ fileName: '项目进展汇报.pptx' });
```

每页完成后检查文本是否溢出；交付前以独立 LibreOffice profile 转为 PDF 并核对中文文本。编辑已有演示文稿时保留未要求修改的幻灯片与版式。不得执行 `npm install`、全局安装、浏览器下载或在线字体请求。
