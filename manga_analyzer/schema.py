"""Claude に返させる JSON の形（structured outputs 用の JSON Schema）。

座標はモデルに、送った画像のピクセル値で答えさせる。高解像度対応のモデルは、
API の上限（長辺 2576px・約 3.75MP）以内の画像なら縮小せずに見て、
ピクセルと 1:1 で座標を返す。上限を超える画像は analyzer 側で縮小して送り、
座標を元の大きさに戻す（定規実験 #8 で、正規化より誤差が小さいことを確認済み）。

VLM が付ける番号は仮のもので、欄の名前に tmp_ を付ける（人物の tmp_id は c1, c2, ...）。
台帳の人物と一致したときは ledger_id に台帳の id を書かせる（台帳にある id 以外は選べないよう enum で縛る）。
確定した番号（既知の人物は台帳の id、コマは p1, p2, ... など）への付け替えは analyzer が行う。
"""

from __future__ import annotations

from typing import Any

_BOX = {
    "type": "object",
    "description": "Bounding box in pixels of the given image (origin top-left).",
    "properties": {
        "x_min": {"type": "integer"},
        "y_min": {"type": "integer"},
        "x_max": {"type": "integer"},
        "y_max": {"type": "integer"},
    },
    "required": ["x_min", "y_min", "x_max", "y_max"],
    "additionalProperties": False,
}

_CHARACTER = {
    "type": "object",
    "description": "One appearance of a person in this panel.",
    "properties": {
        "tmp_person": {"type": "string", "description": "The tmp_id of the person in `persons`."},
        "description": {"type": "string", "description": "What the person is doing in this panel."},
        "expression": {"type": "string"},
        "box": _BOX,
    },
    "required": ["tmp_person", "description", "expression", "box"],
    "additionalProperties": False,
}

TEXT_TYPES = ["narration", "speech", "thought", "sound_effect", "other"]

_TEXT = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": TEXT_TYPES},
        "content": {"type": "string", "description": "Verbatim transcription in the original language."},
        "tmp_speaker": {
            "type": "string",
            "description": "The tmp_id of the person who speaks or narrates it, or empty string if none/unknown.",
        },
        "box": _BOX,
    },
    "required": ["type", "content", "tmp_speaker", "box"],
    "additionalProperties": False,
}

_PANEL = {
    "type": "object",
    "properties": {
        "tmp_reading_order": {"type": "integer", "description": "Position in reading order, 1 = read first."},
        "box": _BOX,
        "description": {"type": "string"},
        "characters": {"type": "array", "items": _CHARACTER},
        "texts": {"type": "array", "items": _TEXT},
    },
    "required": ["tmp_reading_order", "box", "description", "characters", "texts"],
    "additionalProperties": False,
}

def model_output_schema(ledger_ids: list[str]) -> dict[str, Any]:
    """台帳の id を ledger_id の選択肢にしたスキーマを返す（"" は台帳にいない＝不明）。"""
    person = {
        "type": "object",
        "properties": {
            "tmp_id": {"type": "string", "description": "Temporary id for this page: c1, c2, ..."},
            "ledger_id": {
                "type": "string",
                "enum": ["", *ledger_ids],
                "description": "The matching ledger entry, or empty string if not in the ledger or unsure.",
            },
            "appearance": {"type": "string"},
            "match_evidence": {
                "type": "string",
                "description": "Why this is the ledger entry. Empty string when ledger_id is empty.",
            },
        },
        "required": ["tmp_id", "ledger_id", "appearance", "match_evidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "reading_direction": {"type": "string", "enum": ["right_to_left", "left_to_right"]},
            "persons": {"type": "array", "items": person},
            "panels": {"type": "array", "items": _PANEL},
            "notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short handover notes about unknown persons, referring to them by tmp_id (c1, ...).",
            },
        },
        "required": ["summary", "reading_direction", "persons", "panels", "notes"],
        "additionalProperties": False,
    }
