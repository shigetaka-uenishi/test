import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from manga_analyzer import AnalysisError, analyze_page, build_result_from_record, request_page, to_pixel_bbox
from manga_analyzer.__main__ import main
from manga_analyzer.ledger import LedgerError, validate_ledger

FIXTURES = Path(__file__).parent / "fixtures"
PAGE_1 = FIXTURES / "manga_page_01.jpg"

# manga_page_01.jpg 1〜2 コマ目を想定した VLM の答え（ピクセル座標・仮の番号）。
# tmp_reading_order をわざと逆順に並べ、並べ替えを確認する。
# c1 は台帳のシゲオと一致、c2 は台帳にいない人物。
MODEL_OUTPUT = {
    "summary": "日本人のシゲオが南米に到着する。",
    "reading_direction": "right_to_left",
    "persons": [
        {"tmp_id": "c1", "ledger_id": "shigeo", "appearance": "眼鏡の日本人男性", "match_evidence": "眼鏡とひげ"},
        {"tmp_id": "c2", "ledger_id": "", "appearance": "しま模様の服の巻き毛の男性", "match_evidence": ""},
    ],
    "panels": [
        {
            "tmp_reading_order": 2,
            "box": {"x_min": 128, "y_min": 960, "x_max": 1291, "y_max": 1420},
            "description": "現地の人々が笑顔で迎える。",
            "characters": [
                {
                    "tmp_person": "c2",
                    "description": "両手を上げて喜ぶ",
                    "expression": "笑顔",
                    "box": {"x_min": 400, "y_min": 980, "x_max": 700, "y_max": 1400},
                }
            ],
            "texts": [
                {
                    "type": "narration",
                    "content": "なんとかやっていけそうだ",
                    "tmp_speaker": "c1",
                    "box": {"x_min": 922, "y_min": 1090, "x_max": 1341, "y_max": 1280},
                }
            ],
        },
        {
            "tmp_reading_order": 1,
            "box": {"x_min": 128, "y_min": 126, "x_max": 1291, "y_max": 938},
            "description": "空港でリュックを背負ったシゲオ。",
            "characters": [
                {
                    "tmp_person": "c1",
                    "description": "リュックを背負って立つ",
                    "expression": "笑顔",
                    "box": {"x_min": 639, "y_min": 160, "x_max": 1235, "y_max": 938},
                }
            ],
            "texts": [
                {
                    "type": "narration",
                    "content": "俺は シゲオ\n日本人だ",
                    "tmp_speaker": "c1",
                    "box": {"x_min": 255, "y_min": 194, "x_max": 702, "y_max": 426},
                }
            ],
        },
    ],
    "notes": ["c2の名前はカルロスらしい（c1が呼んでいる）", "c12 という人物はいない"],
}

LEDGER = json.loads((FIXTURES / "ledger_shigeo.json").read_text(encoding="utf-8"))


def copy_output():
    return json.loads(json.dumps(MODEL_OUTPUT))


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
    # 読む順に並べ替えて p1, p2 を確定し、VLM の答えた番号は tmp_reading_order に残す
    assert [first["id"], second["id"]] == ["p1", "p2"]
    assert [first["reading_order"], second["reading_order"]] == [1, 2]
    assert [first["tmp_reading_order"], second["tmp_reading_order"]] == [1, 2]
    assert first["description"] == "空港でリュックを背負ったシゲオ。"
    assert first["bbox"] == {"x": 128, "y": 126, "width": 1163, "height": 812}
    assert first["texts"][0]["id"] == "p1-t1"


def test_persons_are_resolved_against_ledger():
    client = FakeClient(make_message(json.dumps(MODEL_OUTPUT, ensure_ascii=False)))
    result = analyze_page(PAGE_1, client=client, ledger=LEDGER)

    # 台帳と一致した人物は台帳の id（既知）、それ以外は VLM の仮の id のまま（不明）
    assert result["persons"] == [
        {
            "id": "shigeo",
            "tmp_id": "c1",
            "status": "known",
            "name": "シゲオ",
            "appearance": "眼鏡の日本人男性",
            "match_evidence": "眼鏡とひげ",
        },
        {
            "id": "c2",
            "tmp_id": "c2",
            "status": "unknown",
            "name": None,
            "appearance": "しま模様の服の巻き毛の男性",
            "match_evidence": None,
        },
    ]
    first, second = result["panels"]
    # 登場の id はコマ＋人物
    assert [(c["id"], c["person"], c["tmp_person"]) for c in first["characters"]] == [("p1-shigeo", "shigeo", "c1")]
    assert [(c["id"], c["person"], c["tmp_person"]) for c in second["characters"]] == [("p2-c2", "c2", "c2")]
    # 話し手も確定した id で指し、VLM の仮の id も残す
    assert (first["texts"][0]["speaker"], first["texts"][0]["tmp_speaker"]) == ("shigeo", "c1")
    # 申し送り事項の中の仮の id も確定した id に直る（日本語に続くものも拾い、存在しないものはそのまま）
    assert result["notes"] == ["c2の名前はカルロスらしい（shigeoが呼んでいる）", "c12 という人物はいない"]


def test_without_ledger_everyone_keeps_tmp_id():
    output = copy_output()
    for p in output["persons"]:
        p["ledger_id"] = ""
    result = analyze_page(PAGE_1, client=FakeClient(make_message(json.dumps(output))))

    assert [(p["id"], p["status"]) for p in result["persons"]] == [("c1", "unknown"), ("c2", "unknown")]
    assert result["panels"][0]["texts"][0]["speaker"] == "c1"
    assert result["panels"][0]["characters"][0]["id"] == "p1-c1"


def test_same_person_drawn_twice_in_a_panel():
    # 分身・残像: 同じコマに同じ人物の登場が 2 つ
    output = copy_output()
    clone = dict(output["panels"][1]["characters"][0], description="残像")
    output["panels"][1]["characters"].append(clone)
    result = analyze_page(PAGE_1, client=FakeClient(make_message(json.dumps(output))), ledger=LEDGER)

    ids = [c["id"] for c in result["panels"][0]["characters"]]
    assert ids == ["p1-shigeo-1", "p1-shigeo-2"]


def test_malformed_or_duplicate_tmp_ids_get_free_ids():
    output = copy_output()
    output["persons"] = [
        {"tmp_id": "c1", "ledger_id": "", "appearance": "a", "match_evidence": ""},
        {"tmp_id": "c1", "ledger_id": "", "appearance": "b", "match_evidence": ""},
        {"tmp_id": "P3", "ledger_id": "", "appearance": "c", "match_evidence": ""},
    ]
    result = analyze_page(PAGE_1, client=FakeClient(make_message(json.dumps(output))))

    assert [(p["id"], p["tmp_id"]) for p in result["persons"]] == [("c1", "c1"), ("c2", "c1"), ("c3", "P3")]


def test_persons_sharing_a_ledger_id_are_merged():
    output = copy_output()
    output["persons"][1]["ledger_id"] = "shigeo"
    result = analyze_page(PAGE_1, client=FakeClient(make_message(json.dumps(output))), ledger=LEDGER)

    assert [p["id"] for p in result["persons"]] == ["shigeo"]
    assert result["panels"][1]["characters"][0]["person"] == "shigeo"


def test_request_page_keeps_the_vlm_answer_untouched():
    client = FakeClient(make_message(json.dumps(MODEL_OUTPUT, ensure_ascii=False)))
    record = request_page(PAGE_1, client=client, ledger=LEDGER, synopsis="あらすじ")

    # VLM の作業記録: 答えはそのまま、使ったモデル・台帳・あらすじ・画像の大きさも残す
    assert record["output"] == MODEL_OUTPUT
    assert record["model"] == "claude-opus-5-5"
    assert record["ledger"] == LEDGER
    assert record["synopsis"] == "あらすじ"
    assert record["source"] == {
        "file": PAGE_1.as_posix(), "width": 1419, "height": 2000, "sent_width": 1419, "sent_height": 2000
    }
    # 作業記録から、API を呼ばずに最終 JSON を作り直せる
    assert build_result_from_record(record) == analyze_page(PAGE_1, client=client, ledger=LEDGER, synopsis="あらすじ")


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
        "notes": [],
        "panels": [
            {
                "tmp_reading_order": 1,
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
        ({"characters": [{"id": "c1"}]}, "reserved"),
        ({"characters": [{"id": "a"}, {"id": "a"}]}, "duplicate"),
    ],
)
def test_invalid_ledger_is_rejected(ledger, message):
    with pytest.raises(LedgerError, match=message):
        validate_ledger(ledger)


def test_ledger_without_name_is_allowed():
    validate_ledger({"characters": [{"id": "front-desk", "appearance": "フロントの女性"}]})


def test_cli_writes_json_and_vlm_record(tmp_path, monkeypatch):
    client = FakeClient(make_message(json.dumps(MODEL_OUTPUT, ensure_ascii=False)))
    import manga_analyzer.analyzer as analyzer

    monkeypatch.setattr(
        "manga_analyzer.__main__.request_page",
        lambda image, **kw: analyzer.request_page(image, client=client, **kw),
    )
    assert main([str(PAGE_1), "-o", str(tmp_path), "--ledger", str(FIXTURES / "ledger_shigeo.json")]) == 0

    data = json.loads((tmp_path / "manga_page_01.json").read_text(encoding="utf-8"))
    assert data["panels"][0]["texts"][0]["content"] == "俺は シゲオ\n日本人だ"
    assert data["persons"][0]["id"] == "shigeo"
    # VLM の答えそのものも、作業記録として隣に保存される
    record = json.loads((tmp_path / "manga_page_01.vlm.json").read_text(encoding="utf-8"))
    assert record["output"] == MODEL_OUTPUT


def test_rebuild_cli_recreates_json_without_api(tmp_path):
    from manga_analyzer.rebuild import main as rebuild_main

    record = request_page(PAGE_1, client=FakeClient(make_message(json.dumps(MODEL_OUTPUT))), ledger=LEDGER)
    record_path = tmp_path / "manga_page_01.vlm.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    assert rebuild_main([str(record_path)]) == 0
    rebuilt = json.loads((tmp_path / "manga_page_01.json").read_text(encoding="utf-8"))
    assert rebuilt == build_result_from_record(record)


def test_cli_rejects_invalid_ledger(tmp_path):
    bad = tmp_path / "ledger.json"
    bad.write_text('{"characters": [{"id": "c1"}]}', encoding="utf-8")
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
