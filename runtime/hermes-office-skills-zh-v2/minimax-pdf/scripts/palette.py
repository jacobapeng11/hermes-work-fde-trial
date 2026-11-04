#!/usr/bin/env python3
"""生成只使用镜像内 Noto CJK 字体的离线 PDF 设计令牌。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


PALETTES = {
    "report": ("#1B2A38", "#3B6D8A"),
    "proposal": ("#22272E", "#4E6070"),
    "academic": ("#F5F4F0", "#2A436A"),
    "general": ("#1F2329", "#4A6070"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--type", default="general")
    parser.add_argument("--author", default="")
    parser.add_argument("--date", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    cover_bg, accent = PALETTES.get(args.type, PALETTES["general"])
    tokens = {
        "title": args.title, "author": args.author, "date": args.date,
        "doc_type": args.type, "cover_bg": cover_bg, "accent": accent,
        "accent_lt": "#E6EFF5", "text_light": "#F7F7F5", "page_bg": "#FAFAF8",
        "dark": "#1A1E24", "body_text": "#2C2C30", "muted": "#6B7280",
        "cover_pattern": "fullbleed", "mood": "chinese-office",
        "font_display": "Noto Serif CJK SC", "font_body": "Noto Sans CJK SC",
        "font_display_rl": "NotoSerifCJKSC-Bold", "font_body_rl": "NotoSansCJKSC",
        "font_body_b_rl": "NotoSansCJKSC-Bold", "font_heading": "NotoSerifCJKSC-Bold",
        "font_body_b": "NotoSansCJKSC-Bold",
        "font_paths": {
            "NotoSansCJKSC": "/usr/local/share/fonts/hermes-office/NotoSansSC-VF.ttf",
            "NotoSansCJKSC-Bold": "/usr/local/share/fonts/hermes-office/NotoSansSC-VF.ttf",
            "NotoSerifCJKSC-Bold": "/usr/local/share/fonts/hermes-office/NotoSerifSC-VF.ttf",
        },
        "size_display": 54, "size_h1": 22, "size_h2": 15, "size_h3": 11.5,
        "size_body": 10.5, "size_caption": 8.5, "size_meta": 8,
        "margin_left": 79, "margin_right": 79, "margin_top": 79, "margin_bottom": 79,
        "section_gap": 26, "para_gap": 8, "line_gap": 17,
    }
    Path(args.out).write_text(json.dumps(tokens, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
