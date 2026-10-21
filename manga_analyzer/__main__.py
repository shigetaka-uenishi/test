"""コマンドライン: python -m manga_analyzer IMAGE [IMAGE ...] [-o OUT_DIR]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyzer import DEFAULT_EFFORT, DEFAULT_MODEL, AnalysisError, analyze_page


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manga_analyzer",
        description="漫画ページ画像を解析し、座標と内容の解釈を JSON で出力します。",
    )
    parser.add_argument("images", nargs="+", type=Path, help="解析する画像ファイル")
    parser.add_argument(
        "-o", "--out-dir", type=Path,
        help="JSON の出力先ディレクトリ（<画像名>.json）。省略時は標準出力",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--language", default="Japanese", help="説明文の言語（既定: Japanese）")
    args = parser.parse_args(argv)

    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)

    status = 0
    for image in args.images:
        try:
            result = analyze_page(image, model=args.model, effort=args.effort, language=args.language)
        except AnalysisError as e:
            print(f"error: {e}", file=sys.stderr)
            status = 1
            continue
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out_dir:
            out = args.out_dir / f"{image.stem}.json"
            out.write_text(text + "\n", encoding="utf-8")
            print(f"wrote {out}", file=sys.stderr)
        else:
            print(text)
    return status


if __name__ == "__main__":
    sys.exit(main())
