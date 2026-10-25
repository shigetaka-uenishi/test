"""Claude に返させる JSON の形（structured outputs 用の JSON Schema）。

座標はモデルに、送った画像のピクセル値で答えさせる。高解像度対応のモデルは、
API の上限（長辺 2576px・約 3.75MP）以内の画像なら縮小せずに見て、
ピクセルと 1:1 で座標を返す。上限を超える画像は analyzer 側で縮小して送り、
座標を元の大きさに戻す（定規実験 #8 で、正規化より誤差が小さいことを確認済み）。
"""

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
    "properties": {
        "name": {"type": "string", "description": "Name if known from the page, otherwise a short label."},
        "description": {"type": "string"},
        "expression": {"type": "string"},
        "box": _BOX,
    },
    "required": ["name", "description", "expression", "box"],
    "additionalProperties": False,
}

TEXT_TYPES = ["narration", "speech", "thought", "sound_effect", "other"]

_TEXT = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": TEXT_TYPES},
        "content": {"type": "string", "description": "Verbatim transcription in the original language."},
        "speaker": {"type": "string", "description": "Speaker name, or empty string if none/unknown."},
        "box": _BOX,
    },
    "required": ["type", "content", "speaker", "box"],
    "additionalProperties": False,
}

_PANEL = {
    "type": "object",
    "properties": {
        "reading_order": {"type": "integer"},
        "box": _BOX,
        "description": {"type": "string"},
        "characters": {"type": "array", "items": _CHARACTER},
        "texts": {"type": "array", "items": _TEXT},
    },
    "required": ["reading_order", "box", "description", "characters", "texts"],
    "additionalProperties": False,
}

MODEL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "reading_direction": {"type": "string", "enum": ["right_to_left", "left_to_right"]},
        "panels": {"type": "array", "items": _PANEL},
    },
    "required": ["summary", "reading_direction", "panels"],
    "additionalProperties": False,
}
