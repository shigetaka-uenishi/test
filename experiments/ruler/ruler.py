"""定規実験（#8）: 正解の位置が分かっている画像で、モデルの座標の答え方を測る。

    python experiments/ruler/ruler.py make            # 定規画像と正解 JSON を作る（API なし）
    python experiments/ruler/ruler.py run --runs 3    # 各画像 × 各形式でモデルに答えさせる（API あり）
    python experiments/ruler/ruler.py score --draw    # 答えを採点し、表と重ね描き画像を出す（API なし）

run だけが API を呼ぶ。run は API 環境で実行し、答え（results/）をコミットして持ち帰る。
画像と重ね描きは out/ に置く（シード固定でいつでも作り直せるので、コミットしない）。
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import statistics
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "out"
IMAGES = OUT / "images"
OVERLAYS = OUT / "overlays"
ANSWERS = Path(__file__).parent / "results"

# 漫画ページ（1419×2000）と、同じ画素数の正方形
CANVASES = {
    "tall": (1419, 2000, 4, 6),  # 幅, 高さ, 列数, 行数
    "square": (1685, 1685, 5, 5),
}
FORMATS = ["normalized", "pixel"]
NORMALIZED_SCALE = 1000
OUTLINE = 4
SEED = 8

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"


# --- make: 定規画像を作る -------------------------------------------------------

def _ids(n: int) -> list[str]:
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # I と O は数字と紛らわしいので除く
    return [letters[i % len(letters)] + (str(i // len(letters) + 1) if i >= len(letters) else "") for i in range(n)]


def make_canvas(name: str, width: int, height: int, cols: int, rows: int, rng: random.Random) -> dict[str, Any]:
    """格子の各マスに四角を 1 つずつ、位置と大きさをばらつかせて置く（ページ全体に散らすため）。"""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    cell_w, cell_h = width / cols, height / rows
    margin = 12
    targets = []
    for i, target_id in enumerate(_ids(cols * rows)):
        cx, cy = i % cols, i // cols
        w = rng.randint(60, int(cell_w) - 2 * margin)
        h = rng.randint(60, int(cell_h) - 2 * margin)
        x0 = round(cx * cell_w + rng.uniform(margin, cell_w - margin - w))
        y0 = round(cy * cell_h + rng.uniform(margin, cell_h - margin - h))
        x1, y1 = x0 + w, y0 + h
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline="black", width=OUTLINE)
        font = ImageFont.load_default(size=max(16, min(w, h) // 3))
        draw.text(((x0 + x1) / 2, (y0 + y1) / 2), target_id, fill="black", font=font, anchor="mm")
        targets.append({"id": target_id, "x_min": x0, "y_min": y0, "x_max": x1, "y_max": y1})

    image_path = IMAGES / f"{name}.png"
    img.save(image_path)
    truth = {"image": image_path.name, "width": width, "height": height, "targets": targets}
    (IMAGES / f"{name}.truth.json").write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
    return truth


def cmd_make(args: argparse.Namespace) -> int:
    IMAGES.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    for name, (w, h, cols, rows) in CANVASES.items():
        truth = make_canvas(name, w, h, cols, rows, rng)
        print(f"wrote {IMAGES / truth['image']} ({len(truth['targets'])} targets)", file=sys.stderr)
    return 0


# --- run: モデルに答えさせる ----------------------------------------------------

def _schema(fmt: str) -> dict[str, Any]:
    unit = "normalized to 0-1000" if fmt == "normalized" else "in pixels"
    return {
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": f"Bounding box of one rectangle, {unit} (origin top-left).",
                    "properties": {
                        "id": {"type": "string", "description": "The label written inside the rectangle."},
                        "x_min": {"type": "integer"},
                        "y_min": {"type": "integer"},
                        "x_max": {"type": "integer"},
                        "y_max": {"type": "integer"},
                    },
                    "required": ["id", "x_min", "y_min", "x_max", "y_max"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["targets"],
        "additionalProperties": False,
    }


def _system_prompt(fmt: str, width: int, height: int) -> str:
    task = (
        "The image shows black-outlined rectangles on a white background, each labeled with an ID inside.\n"
        "Report the bounding box of every rectangle. Box edges should be the outer edge of its black outline.\n\n"
    )
    if fmt == "normalized":
        # manga_analyzer の SYSTEM_PROMPT と同じ言い方にそろえる
        coords = (
            f"Coordinates: give every box as integers normalized to 0-{NORMALIZED_SCALE} on both axes, "
            f"where (0, 0) is the top-left corner of the image and ({NORMALIZED_SCALE}, {NORMALIZED_SCALE}) the bottom-right."
        )
    else:
        coords = (
            f"The image is {width}x{height} pixels. Coordinates: give every box as integer pixel coordinates, "
            f"where (0, 0) is the top-left corner of the image and ({width}, {height}) the bottom-right."
        )
    return task + coords


def ask(client: Any, image_path: Path, fmt: str, width: int, height: int, model: str, effort: str) -> dict[str, Any]:
    data = base64.standard_b64encode(image_path.read_bytes()).decode("ascii")
    with client.beta.messages.stream(
        model=model,
        max_tokens=64000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": _schema(fmt)}},
        system=_system_prompt(fmt, width, height),
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
                    {"type": "text", "text": "List the bounding boxes."},
                ],
            }
        ],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"{image_path.name} / {fmt}: stop_reason={response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    return {
        "model": response.model,
        "effort": effort,
        "format": fmt,
        "usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
        "output": json.loads(text),
    }


def cmd_run(args: argparse.Namespace) -> int:
    import anthropic

    client = anthropic.Anthropic()
    ANSWERS.mkdir(parents=True, exist_ok=True)
    for truth_path in sorted(IMAGES.glob("*.truth.json")):
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        name = truth_path.name.removesuffix(".truth.json")
        for fmt in args.formats:
            for r in range(1, args.runs + 1):
                out = ANSWERS / f"{name}__{fmt}__r{r}.json"
                if out.exists():  # 途中で止まっても、やり直しで費用が二重にかからないように
                    continue
                answer = ask(client, IMAGES / truth["image"], fmt, truth["width"], truth["height"], args.model, args.effort)
                out.write_text(json.dumps(answer, indent=2) + "\n", encoding="utf-8")
                print(f"wrote {out}", file=sys.stderr)
    return 0


# --- score: 採点する ------------------------------------------------------------

def to_pixels(box: dict[str, Any], fmt: str, width: int, height: int) -> dict[str, float]:
    if fmt == "pixel":
        return {k: float(box[k]) for k in ("x_min", "y_min", "x_max", "y_max")}
    sx, sy = width / NORMALIZED_SCALE, height / NORMALIZED_SCALE
    return {"x_min": box["x_min"] * sx, "y_min": box["y_min"] * sy, "x_max": box["x_max"] * sx, "y_max": box["y_max"] * sy}


def fit_line(pairs: list[tuple[float, float]]) -> tuple[float, float, float]:
    """答え = a × 正解 + b を最小二乗で当てはめ、(a, b, 残差の RMS) を返す。"""
    xs, ys = zip(*pairs)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    a = sum((x - mx) * (y - my) for x, y in pairs) / sxx
    b = my - a * mx
    rms = (statistics.fmean([(y - (a * x + b)) ** 2 for x, y in pairs])) ** 0.5
    return a, b, rms


def iou(p: dict[str, float], t: dict[str, float]) -> float:
    ix = max(0.0, min(p["x_max"], t["x_max"]) - max(p["x_min"], t["x_min"]))
    iy = max(0.0, min(p["y_max"], t["y_max"]) - max(p["y_min"], t["y_min"]))
    inter = ix * iy
    area = lambda b: max(0.0, b["x_max"] - b["x_min"]) * max(0.0, b["y_max"] - b["y_min"])
    union = area(p) + area(t) - inter
    return inter / union if union else 0.0


def score_answer(truth: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    by_id = {t["id"]: t for t in truth["targets"]}
    preds = {
        b["id"]: to_pixels(b, answer["format"], truth["width"], truth["height"])
        for b in answer["output"]["targets"]
    }
    matched = [(preds[i], by_id[i]) for i in by_id if i in preds]
    x_pairs = [(t[k], p[k]) for p, t in matched for k in ("x_min", "x_max")]
    y_pairs = [(t[k], p[k]) for p, t in matched for k in ("y_min", "y_max")]
    ax, bx, rx = fit_line(x_pairs)
    ay, by, ry = fit_line(y_pairs)
    return {
        "a_x": ax, "b_x": bx, "rms_x": rx,
        "a_y": ay, "b_y": by, "rms_y": ry,
        "mean_abs_err": statistics.fmean([abs(p - t) for t, p in x_pairs + y_pairs]),
        "iou": statistics.fmean([iou(p, t) for p, t in matched]),
        "missing": len(set(by_id) - set(preds)),
        "extra": len(set(preds) - set(by_id)),
        "preds": preds,
    }


def draw_overlay(truth: dict[str, Any], preds: dict[str, dict[str, float]], out: Path) -> None:
    """正解（緑）と答え（赤）を重ねて描く。"""
    with Image.open(IMAGES / truth["image"]) as src:
        img = src.convert("RGB")
    draw = ImageDraw.Draw(img)
    for t in truth["targets"]:
        draw.rectangle((t["x_min"], t["y_min"], t["x_max"], t["y_max"]), outline=(20, 160, 60), width=3)
    for p in preds.values():
        draw.rectangle((p["x_min"], p["y_min"], p["x_max"], p["y_max"]), outline=(230, 40, 40), width=3)
    img.save(out)


COLUMNS = ["a_x", "b_x", "rms_x", "a_y", "b_y", "rms_y", "mean_abs_err", "iou", "missing", "extra"]


def _cell(values: list[float]) -> str:
    if len(values) == 1:
        return f"{values[0]:.3f}"
    return f"{statistics.fmean(values):.3f} ± {statistics.stdev(values):.3f}"


def cmd_score(args: argparse.Namespace) -> int:
    rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for answer_path in sorted(ANSWERS.glob("*.json")):
        name, fmt, _ = answer_path.stem.split("__")
        truth = json.loads((IMAGES / f"{name}.truth.json").read_text(encoding="utf-8"))
        answer = json.loads(answer_path.read_text(encoding="utf-8"))
        s = score_answer(truth, answer)
        rows.setdefault((name, fmt), []).append(s)
        if args.draw:
            OVERLAYS.mkdir(parents=True, exist_ok=True)
            draw_overlay(truth, s["preds"], OVERLAYS / f"{answer_path.stem}.png")

    if not rows:
        print(f"no answers in {ANSWERS}", file=sys.stderr)
        return 1
    print("| 画像 | 形式 | 回数 | " + " | ".join(COLUMNS) + " |")
    print("|" + "---|" * (len(COLUMNS) + 3))
    for (name, fmt), scores in sorted(rows.items()):
        cells = [_cell([s[c] for s in scores]) for c in COLUMNS]
        print(f"| {name} | {fmt} | {len(scores)} | " + " | ".join(cells) + " |")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="定規実験（#8）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("make", help="定規画像と正解 JSON を作る")
    run = sub.add_parser("run", help="モデルに答えさせる（API を呼ぶ）")
    run.add_argument("--runs", type=int, default=1, help="各条件の実行回数")
    run.add_argument("--formats", nargs="+", default=FORMATS, choices=FORMATS)
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    score = sub.add_parser("score", help="答えを採点する")
    score.add_argument("--draw", action="store_true", help="正解と答えの重ね描き画像も出す")
    args = parser.parse_args(argv)
    return {"make": cmd_make, "run": cmd_run, "score": cmd_score}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
