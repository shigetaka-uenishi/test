"""解析結果の枠を元画像に描き込み、座標が合っているか目で確認するための画像を作る。

使い方: python -m manga_analyzer.draw out/manga_page_01.json [...]
（API は呼ばないので、解析済みの JSON から何度でも作り直せる）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# 枠の色: コマ=赤、人物=青、テキスト=緑
PANEL_COLOR = (230, 40, 40)
CHARACTER_COLOR = (40, 90, 230)
TEXT_COLOR = (20, 160, 60)


def _rect(bbox: dict[str, int]) -> tuple[int, int, int, int]:
    return (bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"])


def _label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, color: tuple[int, int, int], font) -> None:
    # 既定フォントは日本語を描けないので、ラベルは ASCII の ID だけにする
    left, top, right, bottom = draw.textbbox(xy, text, font=font)
    draw.rectangle((left - 3, top - 3, right + 3, bottom + 3), fill=color)
    draw.text(xy, text, fill=(255, 255, 255), font=font)


def draw_boxes(result: dict[str, Any], image_path: str | Path, output_path: str | Path) -> Path:
    """result（analyze_page の戻り値）の枠を image_path に描き、output_path に保存する。"""
    with Image.open(image_path) as src:
        img = src.convert("RGB")
    draw = ImageDraw.Draw(img)
    line = max(2, img.width // 300)
    font = ImageFont.load_default(size=max(12, img.width // 45))
    font_h = font.size

    for panel in result["panels"]:
        x0, y0, x1, y1 = _rect(panel["bbox"])
        draw.rectangle((x0, y0, x1, y1), outline=PANEL_COLOR, width=line * 2)
        _label(draw, (x0 + 6, y0 + 6), f"{panel['id']} (#{panel['reading_order']})", PANEL_COLOR, font)

        for character in panel["characters"]:
            cx0, cy0, cx1, cy1 = _rect(character["bbox"])
            draw.rectangle((cx0, cy0, cx1, cy1), outline=CHARACTER_COLOR, width=line)
            # 登場の id はコマ＋人物（p1-c2, p3-shigeo など）なので、どの人物かも分かる
            _label(draw, (cx0 + 6, cy1 - font_h - 8), character["id"], CHARACTER_COLOR, font)

        for text in panel["texts"]:
            tx0, ty0, tx1, ty1 = _rect(text["bbox"])
            draw.rectangle((tx0, ty0, tx1, ty1), outline=TEXT_COLOR, width=line)
            _label(draw, (tx0 + 6, ty0 + 6), text["id"], TEXT_COLOR, font)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def boxes_path_for(json_path: Path) -> Path:
    return json_path.with_name(f"{json_path.stem}_boxes.png")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manga_analyzer.draw",
        description="解析結果 JSON の枠を元画像に描き込み、<JSON名>_boxes.png として保存します。",
    )
    parser.add_argument("json_files", nargs="+", type=Path, help="manga_analyzer が出力した JSON")
    args = parser.parse_args(argv)

    for json_path in args.json_files:
        result = json.loads(json_path.read_text(encoding="utf-8"))
        out = draw_boxes(result, result["source"]["file"], boxes_path_for(json_path))
        print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
