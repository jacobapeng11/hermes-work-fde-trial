#!/usr/bin/env python3
"""使用本地 Noto CJK 字体从设计令牌生成离线 PDF 封面 HTML。"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokens", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    token = json.loads(Path(args.tokens).read_text(encoding="utf-8"))
    page = f"""<!doctype html><meta charset=\"utf-8\"><style>
@font-face {{ font-family: NotoSans; src: url(file:///usr/local/share/fonts/hermes-office/NotoSansSC-VF.ttf); }}
@font-face {{ font-family: NotoSerif; src: url(file:///usr/local/share/fonts/hermes-office/NotoSerifSC-VF.ttf); }}
body {{ margin:0; width:794px; height:1123px; color:{token['text_light']}; background:{token['cover_bg']}; font-family:NotoSans,sans-serif; }}
main {{ padding:150px 78px; }} h1 {{ font-family:NotoSerif,serif; font-size:56px; line-height:1.18; }} p {{ font-size:18px; }} footer {{ position:absolute; bottom:80px; left:78px; }}
</style><main><p>中文办公文档</p><h1>{html.escape(token['title'])}</h1></main><footer>{html.escape(token['author'])}　{html.escape(token['date'])}</footer>"""
    Path(args.out).write_text(page, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
