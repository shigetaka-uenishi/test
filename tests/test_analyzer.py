import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from manga_analyzer import AnalysisError, analyze_page, to_pixel_bbox
from manga_analyzer.__main__ import main
from manga_analyzer.ledger import LedgerError, validate_ledger

FIXTURES = Path(__file__).parent / "fixtures"
PAGE_1 = FIXTURES / "manga_page_01.jpg"

# manga_page_01.jpg 1〜2 コマ目を想定したモデル出力（ピクセル座標・ページ内の人物参照）。
# reading_order をわざと逆順に並べ、並べ替えを確認する。
# P1 は台帳のシゲオと一致、P2 は台帳にいない人物。
MODEL_OUTPUT = {
    "summary": "日本人のシゲオが南米に到着する。",
    "reading_direction": "right_to_left",
    "persons": [
        {"ref": "P1", "ledger_id": "shigeo", "appearance": "眼鏡の日本人男性", "match_evidence": "眼鏡とひげ"},
        {"ref": "P2", "ledger_id": "", "appearance": "しま模様の服の巻き毛の男性", "match_evidence": ""},
    ],
    "panels": [
        {
            "reading_order": 2,
            "box": {"x_min": 128, "y_min": 960, "x_max": 1291, "y_max": 1420},
            "description": "現地の人々が笑顔で迎える。",
            "characters": [
                {
                    "person_ref": "P2",
                    "description": "両手を上げて喜ぶ",
                    "expression": "笑顔",
                    "box": {"x_min": 400, "y_min": 980, "x_max": 700, "y_max": 1400},
                }
            ],
            "texts": [
                {
                    "type": "narration",
                    "content": "なんとかやっていけそうだ",
                    "speaker_ref": "P1",
                    "box": {"x_min": 922, "y_min": 1090, "x_max": 1341, "y_max": 1280},
                }
            ],
        },
        {
            "reading_order": 1,
            "box": {"x_min": 128, "y_min": 126, "x_max": 1291, "y_max": 938},
            "description": "空港でリュックを背負ったシゲオ。",
            "characters": [
                {
                    "person_ref": "P1",
                    "description": "リュックを背負って立つ",
                    "expression": "笑顔",
                    "box": {"x_min": 639, "y_min": 160, "x_max": 1235, "y_max": 938},
                }
            ],
            "texts": [
                {
                    "type": "narration",
                    "content": "俺は シゲオ\n日本人だ",
                    "speaker_ref": "P1",
                    "box": {"x_min": 255, "y_min": 194, "x_max": 702, "y_max": 426},
                }
            ],
        },
    ],
    "discoveries": [
        {"subject_ref": "P1", "kind": "relationship", "content": "南米に来た", "evidence": "空港の場面"},
        {"subject_ref": "P2", "kind": "possibly_same_person", "content": "P1", "evidence": "確信なし"},
    ],
}

LEDGER = json.loads((FIXTURES / "ledger_shigeo.json").read_text(encoding="utf-8"))


class FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class FakeClient:
    """client.beta.messages.stream(...) だけを持つ、API を呼ばない代役。"""

    def __init__(self, message):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))
        self._message = message

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self._message)


def make_message(text=None, stop_reason="end_turn", stop_details=None):
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, stop_details=stop_details, model="claude-opus-5-5"
    )


def test_fixtures_exist():
    names = sorted(p.name for p in FIXTURES.glob("manga_page_*.jpg"))
    assert names == ["manga_page_01.jpg", "manga_page_02.jpg", "manga_page_03.jpg"]


def test_to_pixel_bbox_keeps_pixels():
    box = {"x_min": 0, "y_min": 1000, "x_max": 1419, "y_max": 2000}
    assert to_pixel_bbox(box, 1419, 2000) == {"x": 0, "y": 1000, "width": 1419, "height": 1000}


def test_to_pixel_bbox_clamps_and_swaps():
    box = {"x_min": 1200, "y_min": 600, "x_max": -50, "y_max": 400}
    assert to_pixel_bbox(box, 1000, 1000) == {"x": 0, "y": 400, "width": 1000, "height": 200}


def test_to_pixel_bbox_scales_back_when_resized():
    box = {"x_min": 100, "y_min": 200, "x_max": 300, "y_max": 400}
    assert to_pixel_bbox(box, 4000, 4000, scale=2.0) == {"x": 200, "y": 400, "width": 400, "height": 400}


def test_analyze_page_builds_pixel_json():
    client = FakeClient(make_message(json.dumps(MODEL_OUTPUT, ensure_ascii=False)))
    result = analyze_page(PAGE_1, client=client, ledger=LEDGER)

    assert result["source"] == {"file": PAGE_1.as_posix(), "width": 1419, "height": 2000}
    assert result["model"] == "claude-opus-5-5"
    assert result["page"]["reading_direction"] == "right_to_left"

    first, second = result["panels"]
    assert [first["id"], second["id"]] == ["p1", "p2"]
    assert first["description"] == "空港でリュックを背負ったシゲオ。"
    assert first["bbox"] == {"x": 128, "y": 126, "width": 1163, "height": 812}
    assert first["characters"][0]["id"] == "p1-c1"
    assert first["texts"][0]["id"] == "p1-t1"


def test_persons_are_resolved_against_ledger():
    client = FakeClient(make_message(json.dumps(MODEL_OUTPUT, ensure_ascii=False)))
    result = analyze_page(PAGE_1, client=client, ledger=LEDGER)

    # 台帳と一致した人物は台帳の id（既知）、それ以外は u1（不明）
    assert result["persons"] == [
        {"id": "shigeo", "status": "known", "name": "シゲオ", "appearance": "眼鏡の日本人男性", "match_evidence": "眼鏡とひげ"},
        {"id": "u1", "status": "unknown", "name": None, "appearance": "しま模様の服の巻き毛の男性", "match_evidence": None},
    ]
    first, second = result["panels"]
    assert first["characters"][0]["person"] == "shigeo"
    assert second["characters"][0]["person"] == "u1"
    # 話し手も人物の id で指す
    assert first["texts"][0]["speaker"] == "shigeo"
    assert second["texts"][0]["speaker"] == "shigeo"
    # 発見の対象と、同一人物の候補の相手も、出力の id に直る
    assert result["discoveries"] == [
        {"subject": "shigeo", "kind": "relationship", "content": "南米に来た", "evidence": "空港の場面"},
        {"subject": "u1", "kind": "possibly_same_person", "content": "shigeo", "evidence": "確信なし"},
    ]


def test_without_ledger_everyone_is_unknown():
    output = json.loads(json.dumps(MODEL_OUTPUT))
    for p in output["persons"]:
        p["ledger_id"] = ""
    result = analyze_page(PAGE_1, client=FakeClient(make_message(json.dumps(output))))

    assert [(p["id"], p["status"]) for p in result["persons"]] == [("u1", "unknown"), ("u2", "unknown")]
    assert result["panels"][0]["texts"][0]["speaker"] == "u1"


def test_persons_sharing_a_ledger_id_are_merged():
    output = json.loads(json.dumps(MODEL_OUTPUT))
    output["persons"][1]["ledger_id"] = "shigeo"
    result = analyze_page(PAGE_1, client=FakeClient(make_message(json.dumps(output))), ledger=LEDGER)

    assert [p["id"] for p in result["persons"]] == ["shigeo"]
    assert result["panels"][1]["characters"][0]["person"] == "shigeo"


def test_analyze_page_sends_image_and_schema():
    client = FakeClient(make_message(json.dumps(MODEL_OUTPUT)))
    analyze_page(PAGE_1, client=client, effort="medium", ledger=LEDGER, synopsis="シゲオが南米に着いた。\n")

    (call,) = client.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["effort"] == "medium"
    assert call["fallbacks"] == "default"
    # ledger_id は台帳にある id か、空文字（不明）しか選べない
    person = call["output_config"]["format"]["schema"]["properties"]["persons"]["items"]
    assert person["properties"]["ledger_id"]["enum"] == ["", "shigeo"]
    # 台帳 → あらすじ → 画像 の順に送る
    ledger_block, synopsis_block, image_block, _ = call["messages"][0]["content"]
    assert ledger_block["text"].startswith("Character ledger:") and "シゲオ" in ledger_block["text"]
    assert synopsis_block["text"] == "Story so far:\nシゲオが南米に着いた。"
    assert image_block["source"]["media_type"] == "image/jpeg"
    # 上限以内の画像はそのまま送り、そのピクセルサイズを伝える
    assert base64.standard_b64decode(image_block["source"]["data"]) == PAGE_1.read_bytes()
    assert "1419x2000 pixels" in call["system"]
    # 吹き出し・ナレーション枠の中の文字は、入れ物ごと囲ませる
    assert "box the whole balloon or box along its outline" in call["system"]


def test_large_image_is_downscaled_and_coords_scaled_back(tmp_path):
    from PIL import Image

    big = tmp_path / "big.png"
    Image.new("RGB", (4000, 3000), "white").save(big)
    output = {
        "summary": "s",
        "reading_direction": "right_to_left",
        "persons": [],
        "discoveries": [],
        "panels": [
            {
                "reading_order": 1,
                "box": {"x_min": 0, "y_min": 0, "x_max": 1000, "y_max": 500},
                "description": "d",
                "characters": [],
                "texts": [],
            }
        ],
    }
    client = FakeClient(make_message(json.dumps(output)))
    result = analyze_page(big, client=client)

    (call,) = client.calls
    (image_block,) = [b for b in call["messages"][0]["content"] if b["type"] == "image"]
    sent = Image.open(io.BytesIO(base64.standard_b64decode(image_block["source"]["data"])))
    w, h = sent.size
    assert max(w, h) <= 2576 and w * h <= 3_750_000
    assert f"{w}x{h} pixels" in call["system"]
    # 送った画像での座標が、元画像（4000x3000）の座標に戻っている
    scale = 4000 / w
    assert result["source"]["width"] == 4000
    assert result["panels"][0]["bbox"] == {"x": 0, "y": 0, "width": round(1000 * scale), "height": round(500 * scale)}


def test_refusal_raises():
    client = FakeClient(
        make_message(stop_reason="refusal", stop_details=SimpleNamespace(category="other"))
    )
    with pytest.raises(AnalysisError, match="declined"):
        analyze_page(PAGE_1, client=client)


def test_max_tokens_raises():
    client = FakeClient(make_message('{"summary": "', stop_reason="max_tokens"))
    with pytest.raises(AnalysisError, match="max_tokens"):
        analyze_page(PAGE_1, client=client)


def test_empty_ledger_is_sent_as_empty():
    client = FakeClient(make_message(json.dumps({**MODEL_OUTPUT, "persons": []})))
    analyze_page(PAGE_1, client=client)

    (call,) = client.calls
    content = call["messages"][0]["content"]
    assert content[0]["text"] == "Character ledger:\n(empty)"
    assert content[1]["type"] == "image"  # あらすじがなければ送らない
    person = call["output_config"]["format"]["schema"]["properties"]["persons"]["items"]
    assert person["properties"]["ledger_id"]["enum"] == [""]


@pytest.mark.parametrize(
    "ledger, message",
    [
        ({"people": []}, "characters"),
        ({"characters": [{"name": "シゲオ"}]}, "id"),
        ({"characters": [{"id": "u1"}]}, "reserved"),
        ({"characters": [{"id": "a"}, {"id": "a"}]}, "duplicate"),
    ],
)
def test_invalid_ledger_is_rejected(ledger, message):
    with pytest.raises(LedgerError, match=message):
        validate_ledger(ledger)


def test_ledger_without_name_is_allowed():
    validate_ledger({"characters": [{"id": "front-desk", "appearance": "フロントの女性"}]})


def test_cli_writes_json(tmp_path, monkeypatch):
    client = FakeClient(make_message(json.dumps(MODEL_OUTPUT, ensure_ascii=False)))
    import manga_analyzer.analyzer as analyzer

    monkeypatch.setattr(
        "manga_analyzer.__main__.analyze_page",
        lambda image, **kw: analyzer.analyze_page(image, client=client, **kw),
    )
    assert main([str(PAGE_1), "-o", str(tmp_path), "--ledger", str(FIXTURES / "ledger_shigeo.json")]) == 0

    data = json.loads((tmp_path / "manga_page_01.json").read_text(encoding="utf-8"))
    assert data["panels"][0]["texts"][0]["content"] == "俺は シゲオ\n日本人だ"
    assert data["persons"][0]["id"] == "shigeo"


def test_cli_rejects_invalid_ledger(tmp_path):
    bad = tmp_path / "ledger.json"
    bad.write_text('{"characters": [{"id": "u1"}]}', encoding="utf-8")
    with pytest.raises(SystemExit):
        main([str(PAGE_1), "--ledger", str(bad)])


def test_draw_boxes_writes_png(tmp_path):
    from PIL import Image

    from manga_analyzer.analyzer import build_result
    from manga_analyzer.draw import draw_boxes

    result = build_result(MODEL_OUTPUT, source=PAGE_1.as_posix(), width=1419, height=2000, model="m")
    out = draw_boxes(result, PAGE_1, tmp_path / "boxes.png")

    with Image.open(out) as img:
        assert img.size == (1419, 2000)
        # p1 の枠の左上（赤い線）に色が塗られている
        x, y = result["panels"][0]["bbox"]["x"], result["panels"][0]["bbox"]["y"]
        assert img.getpixel((x + 1, y + 1)) == (230, 40, 40)


def test_draw_cli_from_json(tmp_path):
    from manga_analyzer.analyzer import build_result
    from manga_analyzer.draw import main as draw_main

    result = build_result(MODEL_OUTPUT, source=PAGE_1.as_posix(), width=1419, height=2000, model="m")
    json_path = tmp_path / "manga_page_01.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    assert draw_main([str(json_path)]) == 0
    assert (tmp_path / "manga_page_01_boxes.png").exists()


def test_cli_draw_requires_out_dir():
    with pytest.raises(SystemExit):
        main([str(PAGE_1), "--draw"])
