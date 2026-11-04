#!/bin/sh
set -eu

fc-match 'Noto Sans CJK SC' | grep -qi 'Noto Sans CJK'
fc-match 'Noto Serif CJK SC' | grep -qi 'Noto Serif CJK'
command -v chromium
command -v soffice
command -v pandoc
command -v pdftotext
command -v dotnet
/opt/hermes/office-venv/bin/python -c 'import matplotlib, openpyxl, pandas, pypdf, reportlab; from markitdown import MarkItDown'
/opt/hermes/office-venv/bin/python -c 'import fitz, pathops, uharfbuzz, yaml'
node -e 'require("playwright"); require("pptxgenjs")'
test "$(find /opt/hermes/platform-default-skills -type f -name SKILL.md | wc -l | tr -d " ")" = 5
test "$(find /opt/hermes/platform-readonly-skills -type f -name SKILL.md | wc -l | tr -d " ")" = 1
test -f /opt/hermes/platform-readonly-skills/productivity/ppt-master/SKILL.md
test -f /opt/hermes/platform-readonly-skills/productivity/ppt-master/scripts/attribution_guard.py
test -f /usr/share/doc/hermes-office-fonts/LICENSE-OFL-1.1.txt
test -x /opt/hermes/office-bin/minimax-docx/MiniMaxAIDocx.Cli
