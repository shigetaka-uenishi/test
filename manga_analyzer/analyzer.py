"""漫画ページ画像を Claude で解析し、座標付きの JSON に変換する。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from PIL import Image

from .schema import MODEL_OUTPUT_SCHEMA, NORMALIZED_SCALE

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
MAX_TOKENS = 64000

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

Coordinates: give every box as integers normalized to 0-{scale} on both axes, \
where (0, 0) is the top-left corner of the page and ({scale}, {scale}) the bottom-right. \
Box edges should hug the visible element.

If a text element or character overlaps two panels, assign it to the panel where most of it lies.
Japanese manga is usually read right-to-left; decide the direction from the page itself.
Write `summary`, `description` and `expression` in {language}.
"""


class AnalysisError(RuntimeError):
    """モデルが結果を返せなかったとき（拒否・出力上限など）に送出する。"""


def _encode_image(path: Path) -> tuple[str, str, int, int]:
    with Image.open(path) as img:
        media_type = _MEDIA_TYPES.get(img.format or "")
        if media_type is None:
            raise ValueError(f"unsupported image format: {img.format} ({path})")
        width, height = img.size
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return media_type, data, width, height


def to_pixel_bbox(box: dict[str, int], width: int, height: int) -> dict[str, int]:
    """0〜1000 の正規化ボックスを、元画像のピクセル座標 {x, y, width, height} に変換する。"""

    def clamp(v: int) -> int:
        return max(0, min(NORMALIZED_SCALE, int(v)))

    x0, x1 = sorted((clamp(box["x_min"]), clamp(box["x_max"])))
    y0, y1 = sorted((clamp(box["y_min"]), clamp(box["y_max"])))
    px0 = round(x0 * width / NORMALIZED_SCALE)
    py0 = round(y0 * height / NORMALIZED_SCALE)
    px1 = round(x1 * width / NORMALIZED_SCALE)
    py1 = round(y1 * height / NORMALIZED_SCALE)
    return {"x": px0, "y": py0, "width": px1 - px0, "height": py1 - py0}


def build_result(
    model_output: dict[str, Any], *, source: str, width: int, height: int, model: str
) -> dict[str, Any]:
    """モデル出力（正規化座標）を、ピクセル座標の最終 JSON に組み立てる。"""
    panels = sorted(model_output["panels"], key=lambda p: p["reading_order"])
    out_panels = []
    for i, panel in enumerate(panels, start=1):
        panel_id = f"p{i}"
        out_panels.append(
            {
                "id": panel_id,
                "reading_order": i,
                "bbox": to_pixel_bbox(panel["box"], width, height),
                "description": panel["description"],
                "characters": [
                    {
                        "name": c["name"],
                        "description": c["description"],
                        "expression": c["expression"],
                        "bbox": to_pixel_bbox(c["box"], width, height),
                    }
                    for c in panel["characters"]
                ],
                "texts": [
                    {
                        "id": f"{panel_id}-t{j}",
                        "type": t["type"],
                        "content": t["content"],
                        "speaker": t["speaker"] or None,
                        "bbox": to_pixel_bbox(t["box"], width, height),
                    }
                    for j, t in enumerate(panel["texts"], start=1)
                ],
            }
        )
    return {
        "source": {"file": source, "width": width, "height": height},
        "model": model,
        "coordinate_system": "pixels, origin top-left, bbox = {x, y, width, height}",
        "page": {
            "summary": model_output["summary"],
            "reading_direction": model_output["reading_direction"],
        },
        "panels": out_panels,
    }


def analyze_page(
    image_path: str | Path,
    *,
    client: Any = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    language: str = "Japanese",
) -> dict[str, Any]:
    """画像 1 枚を解析し、座標と内容の解釈をまとめた dict を返す。"""
    path = Path(image_path)
    media_type, data, width, height = _encode_image(path)

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
            "format": {"type": "json_schema", "schema": MODEL_OUTPUT_SCHEMA},
        },
        system=SYSTEM_PROMPT.format(scale=NORMALIZED_SCALE, language=language),
        messages=[
            {
                "role": "user",
                "content": [
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
        json.loads(text), source=path.as_posix(), width=width, height=height, model=response.model
    )
