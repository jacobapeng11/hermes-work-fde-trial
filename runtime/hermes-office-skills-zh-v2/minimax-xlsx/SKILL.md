---
name: minimax-xlsx
description: "创建、读取、编辑、重算和校验中文 XLSX 工作簿，默认使用 Noto Sans CJK SC。"
license: MIT
metadata:
  version: "2.0.0-zh"
  category: productivity
---

# 中文 XLSX

使用 `/opt/hermes/office-venv/bin/python` 和本 Skill 的 XML 工具处理工作簿。新建工作表、标题、日期、人民币金额和提示文字默认使用中文；样式字体固定为 `Noto Sans CJK SC`。

## 规则

- 新建工作簿可用 `openpyxl`，必须为所有可见单元格写入 `Font(name="Noto Sans CJK SC")`；
- 修改既有 XLSX 一律走 `scripts/xlsx_unpack.py` → 定点 XML 修改 → `scripts/xlsx_pack.py`，不得用 `openpyxl` 全量回写；
- 所有计算值使用 Excel 公式，并在交付前用独立 LibreOffice profile 重算；
- 先执行 `scripts/xlsx_reader.py` 理解工作簿，后执行 `scripts/formula_check.py 文件.xlsx --json` 校验公式。

```bash
PY=/opt/hermes/office-venv/bin/python
$PY scripts/xlsx_reader.py 原始报表.xlsx
$PY scripts/xlsx_unpack.py 原始报表.xlsx /tmp/xlsx-work
# 只修改用户指定的 XML 单元格
$PY scripts/xlsx_pack.py /tmp/xlsx-work 更新后报表.xlsx
$PY scripts/libreoffice_recalc.py 更新后报表.xlsx 重算后报表.xlsx --timeout 90
$PY scripts/formula_check.py 重算后报表.xlsx --json
```

不得在任务中安装 Python/Node 包、下载字体或将临时 LibreOffice profile 写入用户持久目录。
