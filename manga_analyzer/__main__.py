"""コマンドライン: python -m manga_analyzer IMAGE [IMAGE ...] [-o OUT_DIR] [--ledger L] [--synopsis S]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyzer import DEFAULT_EFFORT, DEFAULT_MODEL, AnalysisError, build_result_from_record, request_page
from .draw import boxes_path_for, draw_boxes
from .ledger import load_ledger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manga_analyzer",
        description="漫画ページ画像を解析し、座標と内容の解釈を JSON で出力します。",
    )
    parser.add_argument("images", nargs="+", type=Path, help="解析する画像ファイル")
    parser.add_argument(
        "-o", "--out-dir", type=Path,
        help="出力先ディレクトリ。<画像名>.json（最終 JSON）と <画像名>.vlm.json（VLM の作業記録）を保存する。"
        "省略時は最終 JSON だけを標準出力に出す",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--language", default="Japanese", help="説明文の言語（既定: Japanese）")
    parser.add_argument(
        "--draw", action="store_true",
        help="枠を元画像に描いた確認用画像 <画像名>_boxes.png も出力する（--out-dir が必要）",
    )
    parser.add_argument("--ledger", type=Path, help="キャラ台帳の JSON。省略時は空の台帳")
    parser.add_argument("--synopsis", type=Path, help="前のページまでのあらすじ（テキストファイル）")
    args = parser.parse_args(argv)

    if args.draw and not args.out_dir:
        parser.error("--draw には --out-dir が必要です")
    try:
        ledger = load_ledger(args.ledger) if args.ledger else None
    except (OSError, ValueError) as e:
        parser.error(f"台帳を読めません: {e}")
    synopsis = args.synopsis.read_text(encoding="utf-8") if args.synopsis else None
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)

    status = 0
    for image in args.images:
        try:
            record = request_page(
                image,
                model=args.model,
                effort=args.effort,
                language=args.language,
                ledger=ledger,
                synopsis=synopsis,
            )
        except AnalysisError as e:
            print(f"error: {e}", file=sys.stderr)
            status = 1
            continue
        result = build_result_from_record(record)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out_dir:
            vlm_out = args.out_dir / f"{image.stem}.vlm.json"
            vlm_out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"wrote {vlm_out}", file=sys.stderr)
            out = args.out_dir / f"{image.stem}.json"
            out.write_text(text + "\n", encoding="utf-8")
            print(f"wrote {out}", file=sys.stderr)
            if args.draw:
                boxes = draw_boxes(result, image, boxes_path_for(out))
                print(f"wrote {boxes}", file=sys.stderr)
        else:
            print(text)
    return status


if __name__ == "__main__":
    sys.exit(main())
