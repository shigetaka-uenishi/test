import json

from PIL import Image

from manga_analyzer.analyzer import build_result
from manga_analyzer.sheet import build_sheets, main, write_sheets
from test_analyzer import LEDGER, PAGE_1, copy_output


def page_result(output=None, ledger=LEDGER):
    return build_result(
        output or copy_output(), source=PAGE_1.as_posix(), width=1419, height=2000, model="m", ledger=ledger
    )


def test_sheet_is_made_only_for_unknown_persons():
    (sheet,) = build_sheets(page_result(), source="out/manga_page_01.json")

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
    (sheet,) = build_sheets(page_result(output))
    assert sheet["lines"] == [{"id": "p2-t1", "type": "narration", "content": "なんとかやっていけそうだ"}]


def test_without_ledger_everyone_gets_a_sheet():
    sheets = build_sheets(page_result(ledger=None))
    assert [(s["id"], s["source"]["person"]) for s in sheets] == [("unk-001", "c1"), ("unk-002", "c2")]
    # 同じページの別の不明の人物も、シートの id で書かれる
    assert sheets[1]["notes"] == ["unk-002の名前はカルロスらしい（unk-001が呼んでいる）"]


def test_ids_do_not_collide_with_taken_ones():
    sheets = build_sheets(page_result(ledger=None), taken={"unk-001", "unk-003", "shigeo"})
    assert [s["id"] for s in sheets] == ["unk-004", "unk-005"]


def test_write_sheets_crops_the_person(tmp_path):
    (path,) = write_sheets(page_result(), tmp_path)

    assert path == tmp_path / "unk-001.json"
    sheet = json.loads(path.read_text(encoding="utf-8"))
    assert sheet["image"] == "unk-001/p2-c2.png"
    assert sheet["appearances"][0]["image"] == "unk-001/p2-c2.png"
    with Image.open(tmp_path / sheet["image"]) as crop, Image.open(PAGE_1) as page:
        assert crop.size == (300, 420)
        assert crop.getpixel((0, 0)) == page.convert("RGB").getpixel((400, 980))


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
    (path,) = write_sheets(page_result(output), tmp_path)
    sheet = json.loads(path.read_text(encoding="utf-8"))
    assert [a["id"] for a in sheet["appearances"]] == ["p1-c2", "p2-c2"]
    assert sheet["image"] == "unk-001/p2-c2.png"


def test_empty_box_is_not_cropped(tmp_path):
    output = copy_output()
    output["panels"][0]["characters"][0]["box"] = {"x_min": 400, "y_min": 980, "x_max": 400, "y_max": 1400}
    (path,) = write_sheets(page_result(output), tmp_path)
    sheet = json.loads(path.read_text(encoding="utf-8"))
    assert sheet["image"] is None
    assert sheet["appearances"][0]["image"] is None


def test_cli_numbers_sheets_across_pages_and_avoids_ledger_ids(tmp_path):
    result_path = tmp_path / "manga_page_01.json"
    result_path.write_text(json.dumps(page_result(ledger=None), ensure_ascii=False), encoding="utf-8")
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps({"characters": [{"id": "unk-001"}]}), encoding="utf-8")
    out = tmp_path / "sheets"

    assert main([str(result_path), "-o", str(out), "--ledger", str(ledger_path)]) == 0
    assert sorted(p.name for p in out.glob("*.json")) == ["unk-002.json", "unk-003.json"]
    assert (out / "unk-003" / "p2-c2.png").exists()

    # 同じページを作り直しても、シートは増えずに同じ id で上書きされる
    assert main([str(result_path), "-o", str(out)]) == 0
    assert sorted(p.name for p in out.glob("*.json")) == ["unk-002.json", "unk-003.json"]

    # 別のページの人物は、すでにあるシートの続きの番号になる
    other = json.loads(result_path.read_text(encoding="utf-8"))
    other["source"]["file"] = PAGE_1.as_posix().replace("01", "02")
    other_path = tmp_path / "manga_page_02.json"
    other_path.write_text(json.dumps(other, ensure_ascii=False), encoding="utf-8")
    assert main([str(other_path), "-o", str(out)]) == 0
    assert sorted(p.name for p in out.glob("*.json"))[-2:] == ["unk-004.json", "unk-005.json"]
