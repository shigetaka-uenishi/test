import json

from PIL import Image

from manga_analyzer import analyze_page
from manga_analyzer.analyzer import build_result
from manga_analyzer.ledger import validate_ledger
from manga_analyzer.sheet import add_new_characters, build_sheets, main
from test_analyzer import LEDGER, MODEL_OUTPUT, PAGE_1, FakeClient, copy_output, make_message


def page_result(output=None, ledger=LEDGER):
    return build_result(
        output or copy_output(), source=PAGE_1.as_posix(), width=1419, height=2000, model="m", ledger=ledger
    )


def copy_ledger():
    return json.loads(json.dumps(LEDGER))


def test_sheet_is_made_only_for_unknown_persons():
    (sheet,) = build_sheets(page_result(), copy_ledger(), source="out/manga_page_01.json")

    assert sheet["id"] == "unk-001"
    # 台帳の 1 人分と同じ欄を先頭に持つ。name・role はコードでは決めない
    assert list(sheet)[:4] == ["id", "name", "role", "appearance"]
    assert sheet["name"] is None and sheet["role"] is None
    assert sheet["appearance"] == "しま模様の服の巻き毛の男性"
    assert sheet["source"] == {"result": "out/manga_page_01.json", "image": PAGE_1.as_posix(), "person": "c2"}
    (appearance,) = sheet["appearances"]
    assert appearance["id"] == "p2-c2"
    assert appearance["panel"] == "p2"
    assert appearance["bbox"] == {"x": 400, "y": 980, "width": 300, "height": 420}
    # c2 に触れた申し送り事項だけを、シートの id に置き換えて残す（c12 は c2 ではない）
    assert sheet["notes"] == ["unk-001の名前はカルロスらしい（shigeoが呼んでいる）"]


def test_lines_are_texts_spoken_by_the_person():
    output = copy_output()
    output["panels"][0]["texts"][0]["tmp_speaker"] = "c2"
    (sheet,) = build_sheets(page_result(output), copy_ledger())
    assert sheet["lines"] == [{"id": "p2-t1", "type": "narration", "content": "なんとかやっていけそうだ"}]


def test_without_ledger_everyone_gets_a_sheet():
    sheets = build_sheets(page_result(ledger=None), {"characters": []})
    assert [(s["id"], s["source"]["person"]) for s in sheets] == [("unk-001", "c1"), ("unk-002", "c2")]
    # 同じページの別の不明の人物も、シートの id で書かれる
    assert sheets[1]["notes"] == ["unk-002の名前はカルロスらしい（unk-001が呼んでいる）"]


def test_ids_do_not_collide_with_ledger_ids():
    ledger = {"characters": [{"id": "unk-001"}, {"id": "unk-003"}, {"id": "shigeo"}]}
    sheets = build_sheets(page_result(ledger=None), ledger)
    assert [s["id"] for s in sheets] == ["unk-004", "unk-005"]


def test_add_new_characters_creates_ledger_and_crops(tmp_path):
    ledger_path = tmp_path / "db" / "ledger.json"
    (sheet,) = add_new_characters(page_result(), ledger_path)

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    validate_ledger(ledger)
    assert ledger["characters"] == [sheet]
    # 画像のパスは台帳のあるディレクトリからの相対パス
    assert sheet["image"] == "images/unk-001/p2-c2.png"
    assert sheet["appearances"][0]["image"] == "images/unk-001/p2-c2.png"
    with Image.open(ledger_path.parent / sheet["image"]) as crop, Image.open(PAGE_1) as page:
        assert crop.size == (300, 420)
        assert crop.getpixel((0, 0)) == page.convert("RGB").getpixel((400, 980))


def test_add_new_characters_keeps_existing_entries_and_skips_known(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(LEDGER, ensure_ascii=False), encoding="utf-8")

    sheets = add_new_characters(page_result(), ledger_path)

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert [c["id"] for c in ledger["characters"]] == ["shigeo", "unk-001"]
    assert ledger["characters"][0] == LEDGER["characters"][0]
    assert [s["source"]["person"] for s in sheets] == ["c2"]


def test_same_page_is_not_added_twice(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    add_new_characters(page_result(ledger=None), ledger_path)
    assert add_new_characters(page_result(ledger=None), ledger_path) == []
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert [c["id"] for c in ledger["characters"]] == ["unk-001", "unk-002"]


def test_representative_image_is_the_largest_appearance(tmp_path):
    output = copy_output()
    output["panels"][1]["characters"].append(
        {
            "tmp_person": "c2",
            "description": "奥に小さく写る",
            "expression": "無表情",
            "box": {"x_min": 200, "y_min": 200, "x_max": 250, "y_max": 260},
        }
    )
    (sheet,) = add_new_characters(page_result(output), tmp_path / "ledger.json")
    assert [a["id"] for a in sheet["appearances"]] == ["p1-c2", "p2-c2"]
    assert sheet["image"] == "images/unk-001/p2-c2.png"


def test_empty_box_is_not_cropped(tmp_path):
    output = copy_output()
    output["panels"][0]["characters"][0]["box"] = {"x_min": 400, "y_min": 980, "x_max": 400, "y_max": 1400}
    (sheet,) = add_new_characters(page_result(output), tmp_path / "ledger.json")
    assert sheet["image"] is None
    assert sheet["appearances"][0]["image"] is None


def test_next_page_sees_only_the_profile_fields(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    add_new_characters(page_result(ledger=None), ledger_path)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))

    client = FakeClient(make_message(json.dumps({**MODEL_OUTPUT, "persons": []})))
    analyze_page(PAGE_1, client=client, ledger=ledger)

    (call,) = client.calls
    sent = json.loads(call["messages"][0]["content"][0]["text"].removeprefix("Character ledger:\n"))
    # 根拠の欄（座標・画像のパスなど）と null の欄は送らない
    assert sent["characters"][1] == {"id": "unk-002", "appearance": "しま模様の服の巻き毛の男性"}
    person = call["output_config"]["format"]["schema"]["properties"]["persons"]["items"]
    assert person["properties"]["ledger_id"]["enum"] == ["", "unk-001", "unk-002"]


def test_cli_appends_pages_in_order(tmp_path):
    first = tmp_path / "manga_page_01.json"
    first.write_text(json.dumps(page_result(), ensure_ascii=False), encoding="utf-8")
    second_result = page_result(ledger=None)
    second_result["source"]["file"] = PAGE_1.as_posix().replace("01", "02")
    second = tmp_path / "manga_page_02.json"
    second.write_text(json.dumps(second_result, ensure_ascii=False), encoding="utf-8")
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(LEDGER, ensure_ascii=False), encoding="utf-8")

    assert main([str(first), str(second), "--ledger", str(ledger_path)]) == 0

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert [(c["id"], c.get("source", {}).get("person")) for c in ledger["characters"]] == [
        ("shigeo", None), ("unk-001", "c2"), ("unk-002", "c1"), ("unk-003", "c2")
    ]
    assert ledger["characters"][1]["source"]["result"] == first.as_posix()
    assert (tmp_path / "images" / "unk-003" / "p2-c2.png").exists()
