"""VLM の作業記録（<画像名>.vlm.json）から、最終 JSON（<画像名>.json）を作り直す。

使い方: python -m manga_analyzer.rebuild out/manga_page_01.vlm.json [...]
（API は呼ばないので、番号の付け方などを変えたあとに何度でも作り直せる）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyzer import build_result_from_record


def result_path_for(record_path: Path) -> Path:
    name = record_path.name.removesuffix(".vlm.json").removesuffix(".json")
    return record_path.with_name(f"{name}.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manga_analyzer.rebuild",
        description="VLM の作業記録 <画像名>.vlm.json から、最終 JSON <画像名>.json を作り直します。",
    )
    parser.add_argument("records", nargs="+", type=Path, help="manga_analyzer が保存した <画像名>.vlm.json")
    args = parser.parse_args(argv)

    for record_path in args.records:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        out = result_path_for(record_path)
        out.write_text(json.dumps(build_result_from_record(record), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
