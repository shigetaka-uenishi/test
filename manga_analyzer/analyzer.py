"""漫画ページ画像を Claude で解析し、座標付きの JSON に変換する。"""

from __future__ import annotations

import base64
import io
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image

from .ledger import ledger_ids, validate_ledger
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
- its position in reading order (1 = read first),
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
with a page-local `ref` (P1, P2, ...). Each appearance in a panel points to its person with `person_ref`; \
when the same person appears in several panels, use the same ref. `speaker_ref` points to the person \
who speaks or narrates a text, or is empty if unknown.
Match each person against the character ledger given with the page. Set `ledger_id` only when you are \
confident it is that ledger entry, and say why in `match_evidence` (a name written on the page, \
distinctive features). If unsure, leave `ledger_id` empty: an unknown person is better than a wrong match. \
Two persons never share a `ledger_id`. If this page reveals a name that is not in the ledger, \
the person stays unmatched; report the name as a discovery instead.
In `discoveries`, report what this page newly reveals about a person that the ledger does not already \
say: a name, role, way of speaking, relationship or appearance, each with its evidence (quote the text, \
or describe what is drawn). If two persons might be the same but you are not sure, keep them separate \
and report kind `possibly_same_person` with the other person's ref as `content`.
The story so far, if given, is context only; describe what this page shows.

Write `summary`, `description`, `expression`, `appearance`, `match_evidence` and discovery `content` \
and `evidence` in {language}.
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


def _resolve_persons(
    model_persons: list[dict[str, Any]], ledger: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """モデルの人物（ref 付き）を出力の人物に直し、ref → 出力 id の対応表と一緒に返す。

    台帳と一致した人物は台帳の id（既知）、それ以外は u1, u2, ...（不明）。
    同じ台帳 id を指す人物が複数あれば、最初の 1 人にまとめる。
    """
    entries = {c["id"]: c for c in ledger["characters"]} if ledger else {}
    persons: list[dict[str, Any]] = []
    ref_to_id: dict[str, str] = {}
    unknown = 0
    for p in model_persons:
        ledger_id = p["ledger_id"]
        if ledger_id in entries:
            ref_to_id[p["ref"]] = ledger_id
            if any(q["id"] == ledger_id for q in persons):
                continue
            persons.append(
                {
                    "id": ledger_id,
                    "status": "known",
                    "name": entries[ledger_id].get("name"),
                    "appearance": p["appearance"],
                    "match_evidence": p["match_evidence"] or None,
                }
            )
        else:
            unknown += 1
            ref_to_id[p["ref"]] = f"u{unknown}"
            persons.append(
                {"id": f"u{unknown}", "status": "unknown", "name": None, "appearance": p["appearance"], "match_evidence": None}
            )
    return persons, ref_to_id


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
    """モデル出力（送った画像のピクセル座標・ページ内の人物参照）を、最終 JSON に組み立てる。"""
    persons, ref_to_id = _resolve_persons(model_output["persons"], ledger)

    def person_id(ref: str) -> str | None:
        return ref_to_id.get(ref)

    panels = sorted(model_output["panels"], key=lambda p: p["reading_order"])
    out_panels = []
    for i, panel in enumerate(panels, start=1):
        panel_id = f"p{i}"
        out_panels.append(
            {
                "id": panel_id,
                "reading_order": i,
                "bbox": to_pixel_bbox(panel["box"], width, height, scale),
                "description": panel["description"],
                "characters": [
                    {
                        "id": f"{panel_id}-c{k}",
                        "person": person_id(c["person_ref"]),
                        "description": c["description"],
                        "expression": c["expression"],
                        "bbox": to_pixel_bbox(c["box"], width, height, scale),
                    }
                    for k, c in enumerate(panel["characters"], start=1)
                ],
                "texts": [
                    {
                        "id": f"{panel_id}-t{j}",
                        "type": t["type"],
                        "content": t["content"],
                        "speaker": person_id(t["speaker_ref"]),
                        "bbox": to_pixel_bbox(t["box"], width, height, scale),
                    }
                    for j, t in enumerate(panel["texts"], start=1)
                ],
            }
        )
    discoveries = [
        {
            "subject": person_id(d["subject_ref"]),
            "kind": d["kind"],
            # 同一人物の候補は相手の ref が入っているので、出力の id に直す
            "content": (
                person_id(d["content"]) or d["content"] if d["kind"] == "possibly_same_person" else d["content"]
            ),
            "evidence": d["evidence"],
        }
        for d in model_output["discoveries"]
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
        "discoveries": discoveries,
    }


def _context_blocks(ledger: dict[str, Any] | None, synopsis: str | None) -> list[dict[str, Any]]:
    # 台帳（作品全体でほぼ固定）→ あらすじ（ページごとに変わる）の順に置く
    ledger_text = json.dumps(ledger, ensure_ascii=False, indent=2) if ledger and ledger["characters"] else "(empty)"
    blocks = [{"type": "text", "text": f"Character ledger:\n{ledger_text}"}]
    if synopsis and synopsis.strip():
        blocks.append({"type": "text", "text": f"Story so far:\n{synopsis.strip()}"})
    return blocks


def analyze_page(
    image_path: str | Path,
    *,
    client: Any = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    language: str = "Japanese",
    ledger: dict[str, Any] | None = None,
    synopsis: str | None = None,
) -> dict[str, Any]:
    """画像 1 枚を、キャラ台帳とあらすじを踏まえて解析し、座標と内容の解釈をまとめた dict を返す。"""
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

    return build_result(
        json.loads(text),
        source=path.as_posix(),
        width=width,
        height=height,
        model=response.model,
        scale=width / sent_w,
        ledger=ledger,
    )
