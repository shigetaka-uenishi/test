"""漫画ページ画像を Claude で解析し、座標付きの JSON に変換する。

解析は 2 段階に分かれている。

1. request_page: 画像・台帳・あらすじを VLM に送り、答えをそのまま記録（VLM の作業記録）にする。API を呼ぶ。
2. build_result_from_record: 作業記録から、番号を確定した最終 JSON を組み立てる。API は呼ばない。

VLM が付ける番号（人物の c1、コマの読む順番など）は仮のもので、欄の名前に tmp_ を付けている。
最終 JSON では、コードが確定した番号を id などに入れ、VLM の仮の番号も tmp_ の欄に残す。
"""

from __future__ import annotations

import base64
import io
import json
import math
import re
from pathlib import Path
from typing import Any

from PIL import Image

from .ledger import ledger_ids, profiles, validate_ledger
from .schema import model_output_schema

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
MAX_TOKENS = 64000

# API がこれを超える画像を縮小すると、モデルの答えるピクセル座標が元画像とずれる。
# 超える画像はこちらで縮小して送り、座標を元の大きさに戻す。
MAX_LONG_EDGE = 2576
MAX_PIXELS = 3_750_000

_MEDIA_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}

SYSTEM_PROMPT = """\
You analyze a single page of a comic (manga) and describe its layout and content.

For every panel on the page, report:
- its bounding box,
- its position in reading order (`tmp_reading_order`, 1 = read first),
- what happens in it,
- the characters that appear, with their bounding boxes and facial expressions,
- every piece of text (narration boxes, speech balloons, thought balloons, sound effects),
  transcribed verbatim in the original language, with its bounding box and speaker.

Coordinates: the image is {width}x{height} pixels. Give every box as integer pixel coordinates, \
where (0, 0) is the top-left corner of the page and ({width}, {height}) the bottom-right. \
Box edges should hug the visible element.
For text, the element is its container when it has one: if the text sits inside a speech balloon, \
thought balloon or narration box, box the whole balloon or box along its outline (excluding the tail), \
not just the characters. Text without a container (sound effects, text drawn directly on the art) \
is boxed tightly around the characters.

If a text element or character overlaps two panels, assign it to the panel where most of it lies.
Japanese manga is usually read right-to-left; decide the direction from the page itself.

People: list every distinct person who appears, speaks or narrates on this page once in `persons`, \
with a temporary id `tmp_id` for this page (c1, c2, ...). Each appearance in a panel points to its \
person with `tmp_person`; when the same person appears in several panels, use the same tmp_id. \
If the same person is drawn more than once in one panel (clones, afterimages and the like), list each \
drawing as a separate appearance with the same `tmp_person`, and say how it is drawn in `description`. \
`tmp_speaker` is the tmp_id of the person who speaks or narrates a text, or empty if unknown.
Match each person against the character ledger given with the page. Set `ledger_id` only when you are \
confident it is that ledger entry, and say why in `match_evidence` (a name written on the page, \
distinctive features). If unsure, leave `ledger_id` empty: an unknown person is better than a wrong match. \
Two persons never share a `ledger_id`. If this page reveals a name that is not in the ledger, \
the person stays unmatched; mention the name in `notes` instead. If two persons might be the same \
but you are not sure, keep them separate and mention it in `notes`.
`notes` are short handover notes for whoever organizes the character ledger: if you notice something \
important that characterizes an unknown person (a name, a role, that two persons may be the same), \
write it briefly, just in case, referring to persons by tmp_id (c1, ...). Leave it empty if there is \
nothing. Do not note what happens on the page; that belongs in `summary`.
The story so far, if given, is context only; describe what this page shows.

Write `summary`, `description`, `expression`, `appearance`, `match_evidence` and `notes` in {language}.
"""


class AnalysisError(RuntimeError):
    """モデルが結果を返せなかったとき（拒否・出力上限など）に送出する。"""


def _sent_size(width: int, height: int) -> tuple[int, int]:
    """API の上限に収まる送信サイズ。上限以内ならそのまま。"""
    scale = min(1.0, MAX_LONG_EDGE / max(width, height), math.sqrt(MAX_PIXELS / (width * height)))
    if scale == 1.0:
        return width, height
    return max(1, math.floor(width * scale)), max(1, math.floor(height * scale))


def _encode_image(path: Path) -> tuple[str, str, int, int, int, int]:
    """(media_type, base64, 元の幅, 元の高さ, 送る幅, 送る高さ) を返す。"""
    with Image.open(path) as img:
        media_type = _MEDIA_TYPES.get(img.format or "")
        if media_type is None:
            raise ValueError(f"unsupported image format: {img.format} ({path})")
        width, height = img.size
        sent_w, sent_h = _sent_size(width, height)
        if (sent_w, sent_h) == (width, height):
            raw = path.read_bytes()
        else:
            buf = io.BytesIO()
            img.convert("RGB").resize((sent_w, sent_h), Image.LANCZOS).save(buf, format="PNG")
            raw, media_type = buf.getvalue(), "image/png"
    data = base64.standard_b64encode(raw).decode("ascii")
    return media_type, data, width, height, sent_w, sent_h


def to_pixel_bbox(box: dict[str, int], width: int, height: int, scale: float = 1.0) -> dict[str, int]:
    """モデルが答えたピクセルボックスを、元画像のピクセル座標 {x, y, width, height} にする。

    scale は送った画像から元画像への倍率（縮小して送ったときだけ 1 より大きい）。
    範囲外の値は画像の端に収め、min と max が逆なら入れ替える。
    """

    def to_original(v: int, limit: int) -> int:
        return max(0, min(limit, round(v * scale)))

    x0, x1 = sorted((to_original(box["x_min"], width), to_original(box["x_max"], width)))
    y0, y1 = sorted((to_original(box["y_min"], height), to_original(box["y_max"], height)))
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


# 人物の仮の id（c1, c2, ...）。申し送り事項の文中では、日本語に続けて書かれても拾えるよう英数字だけで区切る
_TMP_ID = re.compile(r"c\d+")
_TMP_ID_IN_TEXT = re.compile(r"(?<![A-Za-z0-9])c\d+(?![0-9])")


def _resolve_persons(
    model_persons: list[dict[str, Any]], ledger: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """VLM の人物（仮の id 付き）を最終の人物に直し、仮の id → 確定した id の対応表と一緒に返す。

    台帳と一致した人物は台帳の id（既知）、それ以外は VLM の仮の id をそのまま使う（不明）。
    仮の id が c+数字の形でないか重複しているときだけ、空いている c+数字を振る。
    同じ台帳 id を指す人物が複数あれば、最初の 1 人にまとめる。
    """
    entries = {c["id"]: c for c in ledger["characters"]} if ledger else {}
    used = {p["tmp_id"] for p in model_persons if _TMP_ID.fullmatch(p["tmp_id"])}
    persons: list[dict[str, Any]] = []
    tmp_to_id: dict[str, str] = {}
    next_free = 1
    for p in model_persons:
        tmp_id, ledger_id = p["tmp_id"], p["ledger_id"]
        if ledger_id in entries:
            tmp_to_id.setdefault(tmp_id, ledger_id)
            if any(q["id"] == ledger_id for q in persons):
                continue
            person_id, status, name = ledger_id, "known", entries[ledger_id].get("name")
        else:
            person_id = tmp_id
            if not _TMP_ID.fullmatch(tmp_id) or any(q["id"] == tmp_id for q in persons):
                while f"c{next_free}" in used:
                    next_free += 1
                person_id = f"c{next_free}"
                used.add(person_id)
            tmp_to_id.setdefault(tmp_id, person_id)
            status, name = "unknown", None
        persons.append(
            {
                "id": person_id,
                "tmp_id": tmp_id,
                "status": status,
                "name": name,
                "appearance": p["appearance"],
                "match_evidence": p["match_evidence"] or None,
            }
        )
    return persons, tmp_to_id


def _appearance_ids(panel_id: str, person_ids: list[str | None]) -> list[str]:
    """登場の id: コマの id ＋ 人物の id（例: p1-c2, p3-shigeo）。

    同じコマに同じ人物が複数回描かれていたら、その人物の登場すべてに -1, -2, ... を付ける。
    """
    keys = [pid or "none" for pid in person_ids]
    counts = {k: keys.count(k) for k in keys}
    seen: dict[str, int] = {}
    ids = []
    for k in keys:
        if counts[k] == 1:
            ids.append(f"{panel_id}-{k}")
        else:
            seen[k] = seen.get(k, 0) + 1
            ids.append(f"{panel_id}-{k}-{seen[k]}")
    return ids


def build_result(
    model_output: dict[str, Any],
    *,
    source: str,
    width: int,
    height: int,
    model: str,
    scale: float = 1.0,
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """VLM の答え（送った画像のピクセル座標・仮の番号）から、番号を確定した最終 JSON を組み立てる。"""
    persons, tmp_to_id = _resolve_persons(model_output["persons"], ledger)

    def person_id(tmp: str) -> str | None:
        return tmp_to_id.get(tmp)

    panels = sorted(model_output["panels"], key=lambda p: p["tmp_reading_order"])
    out_panels = []
    for i, panel in enumerate(panels, start=1):
        panel_id = f"p{i}"
        appearances = panel["characters"]
        ids = _appearance_ids(panel_id, [person_id(c["tmp_person"]) for c in appearances])
        out_panels.append(
            {
                "id": panel_id,
                "reading_order": i,
                "tmp_reading_order": panel["tmp_reading_order"],
                "bbox": to_pixel_bbox(panel["box"], width, height, scale),
                "description": panel["description"],
                "characters": [
                    {
                        "id": appearance_id,
                        "person": person_id(c["tmp_person"]),
                        "tmp_person": c["tmp_person"],
                        "description": c["description"],
                        "expression": c["expression"],
                        "bbox": to_pixel_bbox(c["box"], width, height, scale),
                    }
                    for appearance_id, c in zip(ids, appearances)
                ],
                "texts": [
                    {
                        "id": f"{panel_id}-t{j}",
                        "type": t["type"],
                        "content": t["content"],
                        "speaker": person_id(t["tmp_speaker"]),
                        "tmp_speaker": t["tmp_speaker"],
                        "bbox": to_pixel_bbox(t["box"], width, height, scale),
                    }
                    for j, t in enumerate(panel["texts"], start=1)
                ],
            }
        )
    # 申し送り事項の中の仮の id（c1 など）を、確定した id に直す
    notes = [
        _TMP_ID_IN_TEXT.sub(lambda m: person_id(m.group(0)) or m.group(0), note) for note in model_output["notes"]
    ]
    return {
        "source": {"file": source, "width": width, "height": height},
        "model": model,
        "coordinate_system": "pixels, origin top-left, bbox = {x, y, width, height}",
        "page": {
            "summary": model_output["summary"],
            "reading_direction": model_output["reading_direction"],
        },
        "persons": persons,
        "panels": out_panels,
        "notes": notes,
    }


def build_result_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """VLM の作業記録（request_page の戻り値）から最終 JSON を組み立てる。API は呼ばない。"""
    src = record["source"]
    return build_result(
        record["output"],
        source=src["file"],
        width=src["width"],
        height=src["height"],
        model=record["model"],
        scale=src["width"] / src["sent_width"],
        ledger=record["ledger"],
    )


def _context_blocks(ledger: dict[str, Any] | None, synopsis: str | None) -> list[dict[str, Any]]:
    # 台帳（作品全体でほぼ固定）→ あらすじ（ページごとに変わる）の順に置く
    ledger_text = (
        json.dumps(profiles(ledger), ensure_ascii=False, indent=2) if ledger and ledger["characters"] else "(empty)"
    )
    blocks = [{"type": "text", "text": f"Character ledger:\n{ledger_text}"}]
    if synopsis and synopsis.strip():
        blocks.append({"type": "text", "text": f"Story so far:\n{synopsis.strip()}"})
    return blocks


def request_page(
    image_path: str | Path,
    *,
    client: Any = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    language: str = "Japanese",
    ledger: dict[str, Any] | None = None,
    synopsis: str | None = None,
) -> dict[str, Any]:
    """画像 1 枚を VLM に送り、答えをそのまま VLM の作業記録として返す（加工しない）。

    作業記録には、答え（output）のほか、使ったモデル・渡した台帳とあらすじ・画像の大きさを残す。
    build_result_from_record に渡せば、API を呼ばずに最終 JSON を何度でも作り直せる。
    """
    path = Path(image_path)
    if ledger is not None:
        validate_ledger(ledger)
    media_type, data, width, height, sent_w, sent_h = _encode_image(path)

    if client is None:
        import anthropic

        client = anthropic.Anthropic()

    with client.beta.messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": model_output_schema(ledger_ids(ledger))},
        },
        system=SYSTEM_PROMPT.format(width=sent_w, height=sent_h, language=language),
        messages=[
            {
                "role": "user",
                "content": [
                    *_context_blocks(ledger, synopsis),
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
                    {"type": "text", "text": "Analyze this page."},
                ],
            }
        ],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        raise AnalysisError(f"the model declined to analyze {path} (category: {category})")
    if response.stop_reason == "max_tokens":
        raise AnalysisError(f"output for {path} was cut off at max_tokens")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise AnalysisError(f"no text output for {path} (stop_reason: {response.stop_reason})")

    return {
        "source": {"file": path.as_posix(), "width": width, "height": height, "sent_width": sent_w, "sent_height": sent_h},
        "model": response.model,
        "effort": effort,
        "language": language,
        "ledger": ledger,
        "synopsis": synopsis,
        "output": json.loads(text),
    }


def analyze_page(image_path: str | Path, **kwargs: Any) -> dict[str, Any]:
    """画像 1 枚を、キャラ台帳とあらすじを踏まえて解析し、最終 JSON を返す（作業記録は返さない）。"""
    return build_result_from_record(request_page(image_path, **kwargs))
