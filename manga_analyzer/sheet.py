"""解析結果の不明の人物から、キャラシート（JSON）と人物の切り出し画像を作る。

使い方: python -m manga_analyzer.sheet out/manga_page_01.json [...] -o sheets/ [--ledger LEDGER]
（API は呼ばないので、解析済みの最終 JSON から何度でも作り直せる）

キャラシートは台帳の 1 人分（id・name・role・appearance）と同じ欄を先頭に持ち、そのまま台帳に貼れる。
台帳への記入そのものは別の工程（#21）。name・role はコードでは決められないので null にする。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from PIL import Image

from .ledger import ledger_ids, load_ledger

# 新しい人物に振る id（台帳の id に使える形。c1, c2, ... はページの中だけの仮の id なので使わない）
_SHEET_ID = re.compile(r"unk-(\d+)")
# 申し送り事項の中の仮の id（analyzer と同じ区切り方）
_TMP_ID_IN_TEXT = re.compile(r"(?<![A-Za-z0-9])c\d+(?![0-9])")


def _area(bbox: dict[str, int]) -> int:
    return bbox["width"] * bbox["height"]


def _new_ids(count: int, taken: set[str]) -> list[str]:
    """taken と重ならない unk-001, unk-002, ... を count 個返す（既存の最大の番号の次から）。"""
    start = max((int(m.group(1)) for t in taken if (m := _SHEET_ID.fullmatch(t))), default=0) + 1
    ids = []
    n = start
    while len(ids) < count:
        candidate = f"unk-{n:03d}"
        if candidate not in taken:
            ids.append(candidate)
        n += 1
    return ids


def build_sheets(
    result: dict[str, Any],
    *,
    taken: set[str] = frozenset(),
    reuse: dict[tuple[str, str], str] | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """最終 JSON の不明の人物ごとに、キャラシートを作る（画像はまだ切り出さない）。

    taken は使えない id（台帳・既存のシートの id）。reuse は (元画像, 仮の id) → 既存のシートの id で、
    同じページの同じ人物は同じ id で作り直す。source は最終 JSON のパス（シートに記録する）。
    """
    reuse = reuse or {}
    image = result["source"]["file"]
    unknown = [p for p in result["persons"] if p["status"] == "unknown"]
    to_sheet_id = {p["id"]: reuse[(image, p["id"])] for p in unknown if (image, p["id"]) in reuse}
    fresh = [p["id"] for p in unknown if p["id"] not in to_sheet_id]
    to_sheet_id.update(zip(fresh, _new_ids(len(fresh), set(taken))))

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
                "source": {"result": source, "image": image, "person": person_id},
                "appearances": appearances,
                "lines": lines,
                "notes": notes,
                "image": None,
            }
        )
    return sheets


def crop_images(sheet: dict[str, Any], image_path: str | Path, out_dir: Path) -> None:
    """シートの登場ごとに元画像を切り出し、out_dir/<シートの id>/<登場の id>.png に保存する。

    シートの appearances[].image と image（いちばん大きく写っている登場）に、out_dir からの相対パスを書く。
    面積が 0 の枠は切り出さない（image は null のまま）。
    """
    with Image.open(image_path) as src:
        img = src.convert("RGB")
    # 作り直すときは、前回の切り出し画像を消してから作る
    for old in (out_dir / sheet["id"]).glob("*.png"):
        old.unlink()
    best = None
    for appearance in sheet["appearances"]:
        bbox = appearance["bbox"]
        appearance["image"] = None
        if _area(bbox) == 0:
            continue
        rel = f"{sheet['id']}/{appearance['id']}.png"
        (out_dir / sheet["id"]).mkdir(parents=True, exist_ok=True)
        img.crop((bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"])).save(out_dir / rel)
        appearance["image"] = rel
        if best is None or _area(bbox) > _area(best["bbox"]):
            best = appearance
    sheet["image"] = best["image"] if best else None


def existing_sheets(out_dir: Path) -> dict[tuple[str, str], str]:
    """out_dir にあるシートの (元画像, 仮の id) → シートの id。"""
    sheets = {}
    for path in out_dir.glob("*.json"):
        if _SHEET_ID.fullmatch(path.stem):
            src = json.loads(path.read_text(encoding="utf-8"))["source"]
            sheets[(src["image"], src["person"])] = path.stem
    return sheets


def write_sheets(
    result: dict[str, Any], out_dir: Path, *, taken: set[str] = frozenset(), source: str | None = None
) -> list[Path]:
    """不明の人物ごとに、out_dir/<id>.json（キャラシート）と out_dir/<id>/*.png（切り出し画像）を保存する。

    同じページの同じ人物のシートがすでにあれば、同じ id で上書きする。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = existing_sheets(out_dir)
    sheets = build_sheets(result, taken=set(taken) | set(existing.values()), reuse=existing, source=source)
    paths = []
    for sheet in sheets:
        crop_images(sheet, result["source"]["file"], out_dir)
        path = out_dir / f"{sheet['id']}.json"
        path.write_text(json.dumps(sheet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manga_analyzer.sheet",
        description="解析結果 JSON の不明の人物ごとに、キャラシート <id>.json と切り出し画像 <id>/*.png を保存します。",
    )
    parser.add_argument("json_files", nargs="+", type=Path, help="manga_analyzer が出力した最終 JSON（<画像名>.json）")
    parser.add_argument("-o", "--out-dir", type=Path, required=True, help="キャラシートの出力先ディレクトリ")
    parser.add_argument("--ledger", type=Path, help="キャラ台帳の JSON（その id とは重ならない id を振る）")
    args = parser.parse_args(argv)

    try:
        taken = set(ledger_ids(load_ledger(args.ledger))) if args.ledger else set()
    except (OSError, ValueError) as e:
        parser.error(f"台帳を読めません: {e}")

    for json_path in args.json_files:
        result = json.loads(json_path.read_text(encoding="utf-8"))
        for path in write_sheets(result, args.out_dir, taken=taken, source=json_path.as_posix()):
            print(f"wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
