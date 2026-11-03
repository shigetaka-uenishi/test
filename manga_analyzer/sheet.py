"""解析結果の不明の人物をキャラシートにまとめ、キャラ台帳（キャラシートのデータベース）に追記する。

台帳は解析の --ledger に渡すのと同じ JSON（{"characters": [...]}）。1 人分がキャラシートで、
先頭の id・name・role・appearance が人物の特徴（解析時に VLM に渡すのはこの欄だけ）、
残りは根拠（どのページのどの人物から作ったか、登場・セリフ・申し送り事項・切り出し画像）。

使い方: python -m manga_analyzer.sheet out/manga_page_01.json [...] --ledger ledger.json
（API は呼ばない。台帳がなければ新しく作る。同じページの同じ人物は 2 度追記しない）

name・role はコードでは決められないので null にする（申し送り事項を見て人が埋める）。
別のページに出た同じ人物は別のシートになる（まとめ直しは #21 でスコープ外）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from PIL import Image

from .ledger import validate_ledger

# 新しい人物に振る id（台帳の id に使える形。c1, c2, ... はページの中だけの仮の id なので使わない）
_SHEET_ID = re.compile(r"unk-(\d+)")
# 申し送り事項の中の仮の id（analyzer と同じ区切り方）
_TMP_ID_IN_TEXT = re.compile(r"(?<![A-Za-z0-9])c\d+(?![0-9])")


def _area(bbox: dict[str, int]) -> int:
    return bbox["width"] * bbox["height"]


def _new_ids(count: int, taken: set[str]) -> list[str]:
    """taken と重ならない unk-001, unk-002, ... を count 個返す（既存の最大の番号の次から）。"""
    n = max((int(m.group(1)) for t in taken if (m := _SHEET_ID.fullmatch(t))), default=0) + 1
    ids = []
    while len(ids) < count:
        if f"unk-{n:03d}" not in taken:
            ids.append(f"unk-{n:03d}")
        n += 1
    return ids


def _sheet_key(sheet: dict[str, Any]) -> tuple[str, str] | None:
    """どのページ（元画像）のどの人物（仮の id）から作ったシートか。手で書いた人物は None。"""
    src = sheet.get("source")
    return (src["image"], src["person"]) if isinstance(src, dict) else None


def build_sheets(result: dict[str, Any], ledger: dict[str, Any], *, source: str | None = None) -> list[dict[str, Any]]:
    """最終 JSON の不明の人物のうち、まだ台帳にいない人物のキャラシートを作る（画像はまだ切り出さない）。

    id は台帳と重ならない unk-NNN。source は最終 JSON のパス（シートに記録する）。
    """
    image = result["source"]["file"]
    done = {key for c in ledger["characters"] if (key := _sheet_key(c))}
    unknown = [p for p in result["persons"] if p["status"] == "unknown" and (image, p["id"]) not in done]
    taken = {c["id"] for c in ledger["characters"]}
    to_sheet_id = dict(zip((p["id"] for p in unknown), _new_ids(len(unknown), taken)))

    def replace_ids(text: str) -> str:
        return _TMP_ID_IN_TEXT.sub(lambda m: to_sheet_id.get(m.group(0), m.group(0)), text)

    sheets = []
    for person in unknown:
        person_id = person["id"]
        appearances = [
            {
                "id": c["id"],
                "panel": panel["id"],
                "description": c["description"],
                "expression": c["expression"],
                "bbox": c["bbox"],
                "image": None,
            }
            for panel in result["panels"]
            for c in panel["characters"]
            if c["person"] == person_id
        ]
        lines = [
            {"id": t["id"], "type": t["type"], "content": t["content"]}
            for panel in result["panels"]
            for t in panel["texts"]
            if t["speaker"] == person_id
        ]
        notes = [
            replace_ids(note)
            for note in result["notes"]
            if any(m.group(0) == person_id for m in _TMP_ID_IN_TEXT.finditer(note))
        ]
        sheets.append(
            {
                "id": to_sheet_id[person_id],
                "name": None,
                "role": None,
                "appearance": person["appearance"],
                "image": None,
                "source": {"result": source, "image": image, "person": person_id},
                "appearances": appearances,
                "lines": lines,
                "notes": notes,
            }
        )
    return sheets


def crop_images(sheet: dict[str, Any], image_path: str | Path, images_dir: Path, base_dir: Path) -> None:
    """シートの登場ごとに元画像を切り出し、images_dir/<シートの id>/<登場の id>.png に保存する。

    appearances[].image と image（いちばん大きく写っている登場）に、base_dir（台帳のあるディレクトリ）
    からの相対パスを書く。面積が 0 の枠は切り出さない（image は null のまま）。
    """
    with Image.open(image_path) as src:
        img = src.convert("RGB")
    best = None
    for appearance in sheet["appearances"]:
        bbox = appearance["bbox"]
        if _area(bbox) == 0:
            continue
        out = images_dir / sheet["id"] / f"{appearance['id']}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        img.crop((bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"])).save(out)
        appearance["image"] = Path(_relpath(out, base_dir)).as_posix()
        if best is None or _area(bbox) > _area(best["bbox"]):
            best = appearance
    sheet["image"] = best["image"] if best else None


def _relpath(path: Path, base: Path) -> str:
    return os.path.relpath(path.resolve(), base.resolve())


def load_or_create_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"characters": []}
    return validate_ledger(json.loads(path.read_text(encoding="utf-8")))


def add_new_characters(
    result: dict[str, Any],
    ledger_path: str | Path,
    *,
    images_dir: str | Path | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """最終 JSON の新しい人物（不明の人物）をキャラシートにまとめ、台帳に追記して保存する。

    切り出し画像は images_dir（省略時は台帳と同じディレクトリの images/）に保存する。
    同じページの同じ人物がすでに台帳にあれば追記しない。追記したシートを返す。
    """
    ledger_path = Path(ledger_path)
    base_dir = ledger_path.parent
    images_dir = Path(images_dir) if images_dir else base_dir / "images"
    ledger = load_or_create_ledger(ledger_path)
    sheets = build_sheets(result, ledger, source=source)
    for sheet in sheets:
        crop_images(sheet, result["source"]["file"], images_dir, base_dir)
    ledger["characters"].extend(sheets)
    validate_ledger(ledger)
    base_dir.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sheets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manga_analyzer.sheet",
        description="解析結果 JSON の新しい人物をキャラシートにまとめ、キャラ台帳に追記します（API は呼びません）。",
    )
    parser.add_argument("json_files", nargs="+", type=Path, help="manga_analyzer が出力した最終 JSON（<画像名>.json）")
    parser.add_argument("--ledger", type=Path, required=True, help="追記するキャラ台帳の JSON（なければ作る）")
    parser.add_argument("--images-dir", type=Path, help="切り出し画像の保存先（省略時は台帳と同じディレクトリの images/）")
    args = parser.parse_args(argv)

    for json_path in args.json_files:
        result = json.loads(json_path.read_text(encoding="utf-8"))
        try:
            sheets = add_new_characters(result, args.ledger, images_dir=args.images_dir, source=json_path.as_posix())
        except (OSError, ValueError) as e:
            parser.error(f"台帳に追記できません: {e}")
        ids = ", ".join(s["id"] for s in sheets) or "なし"
        print(f"{json_path}: 追記 {ids} → {args.ledger}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
