"""キャラ台帳（作品をまたいで持つ人物の一覧）の読み込み。

台帳は人が手で書くか、sheet.py が解析結果の新しい人物を追記する JSON:

    {"characters": [{"id": "shigeo", "name": "シゲオ", "role": "主人公・語り手", "appearance": "…"}]}

id だけ必須。name・role・appearance は任意（名前のない人物も載せられるように）。
ほかの欄（sheet.py が書く根拠の欄など）があってもよいが、VLM には PROFILE_FIELDS だけを渡す。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# 台帳にいない人物には、解析結果で c1, c2, ...（VLM の仮の id）を使うので、台帳の id には使えない
_LOCAL_ID = re.compile(r"c\d+")


class LedgerError(ValueError):
    """台帳の形式が正しくないときに送出する。"""


def validate_ledger(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("characters"), list):
        raise LedgerError('ledger must be an object with a "characters" list')
    seen = set()
    for entry in data["characters"]:
        entry_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(entry_id, str) or not entry_id:
            raise LedgerError(f"every character needs a non-empty string id: {entry!r}")
        if _LOCAL_ID.fullmatch(entry_id):
            raise LedgerError(f"id {entry_id!r} is reserved for persons not in the ledger (c1, c2, ...)")
        if entry_id in seen:
            raise LedgerError(f"duplicate id: {entry_id!r}")
        seen.add(entry_id)
    return data


def load_ledger(path: str | Path) -> dict[str, Any]:
    return validate_ledger(json.loads(Path(path).read_text(encoding="utf-8")))


# 解析時に VLM に渡す欄（人物の特徴）。キャラシートの根拠の欄（登場・座標・画像のパスなど）は渡さない
PROFILE_FIELDS = ("id", "name", "role", "appearance")


def profiles(ledger: dict[str, Any]) -> dict[str, Any]:
    """台帳から、VLM に渡す欄だけを残したもの（値が null の欄は省く）。"""
    return {
        "characters": [
            {k: c[k] for k in PROFILE_FIELDS if c.get(k) is not None} for c in ledger["characters"]
        ]
    }


def ledger_ids(ledger: dict[str, Any] | None) -> list[str]:
    return [c["id"] for c in ledger["characters"]] if ledger else []
