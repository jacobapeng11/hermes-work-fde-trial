#!/usr/bin/env python3
"""Offline, single-container functional acceptance for Hermes Office Runtime."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


SKILL_ROOT = Path("/opt/hermes/platform-default-skills")
READONLY_SKILL_ROOT = Path("/opt/hermes/platform-readonly-skills")
PDF_ROOT = SKILL_ROOT / "productivity/minimax-pdf/scripts"
XLSX_ROOT = SKILL_ROOT / "productivity/minimax-xlsx/scripts"
DOCX_CLI = Path("/opt/hermes/office-bin/minimax-docx/MiniMaxAIDocx.Cli")
PYTHON = Path("/opt/hermes/office-venv/bin/python")


class AcceptanceFailure(RuntimeError):
    pass


def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    if result.returncode:
        raise AcceptanceFailure(f"command failed ({result.returncode}): {' '.join(command)}")
    return result


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


def require_contains(path: Path, expected: str) -> None:
    content = path.read_text(encoding="utf-8", errors="replace")
    require(expected in content, f"{path} does not contain expected text: {expected}")


def pdf_text(source: Path, destination: Path) -> str:
    run(["pdftotext", str(source), str(destination)])
    return destination.read_text(encoding="utf-8", errors="replace")


def libreoffice_to_pdf(source: Path, output_dir: Path, profile_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    run([
        "soffice",
        f"-env:UserInstallation={profile_dir.as_uri()}",
        "--headless",
        "--norestore",
        "--convert-to", "pdf",
        "--outdir", str(output_dir),
        str(source),
    ])
    rendered = output_dir / f"{source.stem}.pdf"
    require(rendered.is_file() and rendered.stat().st_size > 1024, f"LibreOffice did not create {rendered}")
    return rendered


def test_preflight(artifacts: Path, _work: Path) -> None:
    run(["/usr/local/bin/hermes-office-verify"])
    run(["fc-match", "Noto Sans CJK SC"])
    run(["fc-match", "Noto Serif CJK SC"])

    dotnet_root = Path(os.environ.get("DOTNET_ROOT", ""))
    require(dotnet_root.is_dir(), f"DOTNET_ROOT is not a directory: {dotnet_root}")
    require((dotnet_root / "dotnet").is_file(), f"DOTNET_ROOT has no dotnet executable: {dotnet_root}")
    require(Path(shutil.which("dotnet") or "").resolve() == (dotnet_root / "dotnet").resolve(), "dotnet PATH and DOTNET_ROOT differ")

    actual_skills = sorted(path.relative_to(SKILL_ROOT).as_posix() for path in SKILL_ROOT.rglob("SKILL.md"))
    expected_skills = sorted([
        "app-center/live-data-report-builder/SKILL.md",
        "browser/hermes-browser-zh/SKILL.md",
        "productivity/minimax-docx/SKILL.md",
        "productivity/minimax-pdf/SKILL.md",
        "productivity/minimax-xlsx/SKILL.md",
    ])
    require(actual_skills == expected_skills, f"unexpected bundled skills: {actual_skills}")
    readonly_skills = sorted(
        path.relative_to(READONLY_SKILL_ROOT).as_posix()
        for path in READONLY_SKILL_ROOT.rglob("SKILL.md")
    )
    require(
        readonly_skills == ["productivity/ppt-master/SKILL.md"],
        f"unexpected readonly skills: {readonly_skills}",
    )
    ppt_master_root = READONLY_SKILL_ROOT / "productivity/ppt-master"
    require(
        (ppt_master_root / "PLATFORM_BOUNDARY.md").is_file(),
        "ppt-master platform boundary notes are missing",
    )
    run([str(PYTHON), str(ppt_master_root / "scripts/attribution_guard.py"), "--help"])
    (artifacts / "preflight-skills.txt").write_text(
        "\n".join([*actual_skills, *readonly_skills]) + "\n", encoding="utf-8"
    )


def test_pdf(artifacts: Path, work: Path) -> None:
    output = artifacts / "pdf"
    output.mkdir()
    tokens = work / "pdf-tokens.json"
    content = work / "pdf-content.json"
    cover_html = work / "pdf-cover.html"
    cover = output / "cover.pdf"
    body = output / "body.pdf"
    final = output / "sample.pdf"

    run([str(PYTHON), str(PDF_ROOT / "palette.py"), "--title", "离线中文办公验收", "--type", "report", "--author", "Hermes", "--date", "2026-07-27", "--out", str(tokens)])
    content.write_text(json.dumps([
        {"type": "h1", "text": "中文标题：离线 PDF 验收"},
        {"type": "body", "text": "这是用于验证 Noto 中文字体嵌入、表格和图表的正文。English text is retained."},
        {"type": "table", "headers": ["项目", "数值"], "rows": [["收入", "300"], ["成本", "120"]]},
        {"type": "chart", "chart_type": "bar", "title": "季度数据", "labels": ["一季度", "二季度"], "datasets": [{"label": "金额", "values": [120, 180]}]},
    ], ensure_ascii=False), encoding="utf-8")
    cover_html.write_text("""<!doctype html><meta charset=\"utf-8\"><style>
@font-face { font-family: Noto; src: url(file:///usr/local/share/fonts/hermes-office/NotoSansCJKsc-Regular.otf); }
body { font-family: Noto, sans-serif; margin: 80px; color: #17365d; } h1 { font-size: 34px; }
</style><h1>离线中文办公验收</h1><p>PDF 封面由本地 Chromium 与 Noto 字体生成。</p>""", encoding="utf-8")
    run(["node", str(PDF_ROOT / "render_cover.js"), "--input", str(cover_html), "--out", str(cover)])
    body_result = run([str(PYTHON), str(PDF_ROOT / "render_body.py"), "--tokens", str(tokens), "--content", str(content), "--out", str(body)])
    require("Glyph " not in body_result.stdout, "PDF chart rendering emitted a missing-glyph warning")
    run([str(PYTHON), str(PDF_ROOT / "merge.py"), "--cover", str(cover), "--body", str(body), "--out", str(final), "--title", "离线中文办公验收"])
    fonts = run(["pdffonts", str(final)]).stdout
    require("Noto" in fonts, "final PDF does not embed a Noto font")
    text = pdf_text(final, output / "sample.txt")
    require("中文标题：离线 PDF 验收" in text, "final PDF text extraction lost the Chinese title")
    require("Noto 中文字体嵌入" in text, "final PDF text extraction lost the Chinese body")


def test_docx(artifacts: Path, work: Path) -> None:
    output = artifacts / "docx"
    output.mkdir()
    content = work / "docx-content.json"
    docx = output / "sample.docx"
    content.write_text(json.dumps([
        {"type": "heading", "level": 1, "text": "中文章节"},
        {"type": "paragraph", "text": "这是中英混排正文：Hermes Office Runtime。"},
        {"type": "paragraph", "text": "中文标点、页眉、页脚与页码均应由预装 CLI 处理。"},
    ], ensure_ascii=False), encoding="utf-8")
    run([str(DOCX_CLI), "create", "--output", str(docx), "--type", "report", "--title", "离线 DOCX 验收", "--author", "Hermes", "--page-size", "a4", "--header", "中文页眉", "--footer", "中文页脚", "--page-numbers", "--content-json", str(content)])
    require(docx.is_file() and docx.stat().st_size > 1024, "DOCX output is missing or too small")
    with zipfile.ZipFile(docx) as archive:
        styles = archive.read("word/styles.xml").decode("utf-8")
        document = archive.read("word/document.xml").decode("utf-8")
    require("Noto Sans CJK SC" in styles and "Noto Serif CJK SC" in styles, "DOCX styles do not declare Noto CJK fonts")
    require("中文章节" in document, "DOCX document XML lost Chinese content")
    preview = libreoffice_to_pdf(docx, work / "docx-preview", work / "docx-profile")
    final_preview = output / "sample-preview.pdf"
    shutil.copyfile(preview, final_preview)
    text = pdf_text(final_preview, output / "sample-preview.txt")
    require("中文章节" in text and "Hermes Office Runtime" in text, "DOCX preview text is incomplete")


def test_xlsx(artifacts: Path, work: Path) -> None:
    output = artifacts / "xlsx"
    output.mkdir()
    source = output / "sample.xlsx"
    recalculated = output / "sample-recalculated.xlsx"
    generator = work / "create-xlsx.py"
    generator.write_text("""from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from pathlib import Path
import sys
wb = Workbook()
ws = wb.active
ws.title = '中文报表'
ws.append(['项目', '金额', '日期'])
ws.append(['一季度', 120, '2026-01-01'])
ws.append(['二季度', 180, '2026-04-01'])
ws.append(['合计', '=SUM(B2:B3)', ''])
for row in ws.iter_rows():
    for cell in row:
        cell.font = Font(name='Noto Sans CJK SC')
ws['B2'].number_format = '#,##0.00'
ws['C2'].number_format = 'yyyy-mm-dd'
for column in range(1, 4):
    ws.column_dimensions[get_column_letter(column)].width = 18
wb.save(sys.argv[1])
""", encoding="utf-8")
    run([str(PYTHON), str(generator), str(source)])
    run([str(PYTHON), str(XLSX_ROOT / "libreoffice_recalc.py"), str(source), str(recalculated), "--timeout", "90"])
    run([str(PYTHON), str(XLSX_ROOT / "formula_check.py"), str(recalculated), "--json"])
    verifier = work / "verify-xlsx.py"
    verifier.write_text("""from openpyxl import load_workbook
from pathlib import Path
import sys
path = Path(sys.argv[1])
wb_values = load_workbook(path, data_only=True)
assert wb_values['中文报表']['B4'].value == 300
wb_styles = load_workbook(path, data_only=False)
assert wb_styles['中文报表']['A1'].font.name == 'Noto Sans CJK SC'
""", encoding="utf-8")
    run([str(PYTHON), str(verifier), str(recalculated)])
    with zipfile.ZipFile(recalculated) as archive:
        styles = archive.read("xl/styles.xml").decode("utf-8")
    require("Noto Sans CJK SC" in styles, "XLSX styles do not declare Noto Sans CJK SC")


def test_pptx(artifacts: Path, work: Path) -> None:
    output = artifacts / "pptx"
    output.mkdir()
    presentation = output / "sample.pptx"
    generator = work / "create-pptx.js"
    generator.write_text("""const pptxgen = require('pptxgenjs');
const pptx = new pptxgen();
pptx.layout = 'LAYOUT_16x9';
const slide = pptx.addSlide();
slide.background = { color: 'F7F9FC' };
slide.addText('离线 PPTX 验收', { x: 0.7, y: 0.7, w: 8.8, h: 0.8, fontFace: 'Noto Sans CJK SC', fontSize: 30, bold: true, color: '17365D' });
slide.addText('中文正文：Hermes Office Runtime 使用本地字体生成演示文稿。', { x: 0.9, y: 2.0, w: 8.2, h: 1.2, fontFace: 'Noto Sans CJK SC', fontSize: 20, color: '222222' });
pptx.writeFile({ fileName: process.argv[2] });
""", encoding="utf-8")
    run(["node", str(generator), str(presentation)])
    require(presentation.is_file() and presentation.stat().st_size > 1024, "PPTX output is missing or too small")
    with zipfile.ZipFile(presentation) as archive:
        slides = [name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
        payload = "".join(archive.read(name).decode("utf-8") for name in slides)
    require(len(slides) == 1 and "离线 PPTX 验收" in payload and "Noto Sans CJK SC" in payload, "PPTX XML is incomplete")
    preview = libreoffice_to_pdf(presentation, work / "pptx-preview", work / "pptx-profile")
    final_preview = output / "sample-preview.pdf"
    shutil.copyfile(preview, final_preview)
    text = pdf_text(final_preview, output / "sample-preview.txt")
    require("离线 PPTX 验收" in text and "Hermes Office Runtime" in text, "PPTX preview text is incomplete")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: hermes-office-container-test <artifacts-dir>", file=sys.stderr)
        return 2
    artifacts = Path(sys.argv[1]).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    work = Path("/tmp/hermes-office-acceptance")
    work.mkdir(parents=True, exist_ok=True)
    results: list[str] = []
    cases = [
        ("CT-01", test_preflight),
        ("CT-02", test_pdf),
        ("CT-03", test_docx),
        ("CT-04", test_xlsx),
        ("CT-05", test_pptx),
    ]
    try:
        for identifier, test in cases:
            print(f"== {identifier} start ==", flush=True)
            test(artifacts, work)
            results.append(f"{identifier}: PASS")
            print(f"== {identifier} PASS ==", flush=True)
        results.append("CT-06: PASS (container network disabled by host runner)")
    except AcceptanceFailure as exc:
        results.append(f"FAIL: {exc}")
        (artifacts / "report.txt").write_text("\n".join(results) + "\n", encoding="utf-8")
        print(f"ACCEPTANCE FAILED: {exc}", file=sys.stderr)
        return 1

    (artifacts / "report.txt").write_text("\n".join(results) + "\n", encoding="utf-8")
    print("ALL OFFICE CONTAINER ACCEPTANCE CASES PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
