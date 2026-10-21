"""Claude に返させる JSON の形（structured outputs 用の JSON Schema）。

座標はモデルに 0〜1000 の正規化値で答えさせ、analyzer 側で元画像の
ピクセル座標に変換する。モデルに渡る画像は API 側で縮小されることがあり、
ピクセル値を直接答えさせると縮小後の座標系とずれるため。
"""

NORMALIZED_SCALE = 1000

_BOX = {
    "type": "object",
    "description": "Bounding box, normalized to 0-1000 on both axes (origin top-left).",
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
