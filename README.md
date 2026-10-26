# 画像処理アプリ（開発準備中）

画像ファイルを読み込み、加工・変換などの処理を行うアプリケーションです。
現在は開発準備中で、機能の実装はまだ行われていません。

## 概要

- 画像ファイル（JPEG / PNG など）の読み込み
- リサイズ・トリミング・フォーマット変換などの画像処理
- 処理結果の画像ファイルとしての保存

※ 上記は予定している機能であり、仕様は今後変更される可能性があります。

## 漫画ページ解析（manga_analyzer）

漫画ページの画像を Claude で解析し、コマ・登場人物・セリフなどの
**座標**と**内容の解釈**を JSON で出力します。

### セットアップ

```bash
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=...   # Claude API キー
```

### 使い方

```bash
# 標準出力に JSON を出す
python -m manga_analyzer tests/fixtures/manga_page_01.jpg

# 複数枚をまとめて解析し、out/<画像名>.json に保存
python -m manga_analyzer tests/fixtures/*.jpg -o out/

# 枠を元画像に描いた確認用画像（out/<画像名>_boxes.png）も出す
python -m manga_analyzer tests/fixtures/*.jpg -o out/ --draw

# 解析済みの JSON から確認用画像だけ作り直す（API は呼ばない）
python -m manga_analyzer.draw out/*.json

# キャラ台帳と、前のページまでのあらすじを踏まえて解析する
python -m manga_analyzer tests/fixtures/manga_page_02.jpg -o out/ --ledger tests/fixtures/ledger_shigeo.json --synopsis synopsis.txt
```

確認用画像の枠の色: コマ＝赤、登場人物＝青、テキスト＝緑。ラベルの ID（`p1`, `p1-c1`, `p1-t1` など）は JSON の `id` と対応しています。登場人物のラベルには、どの人物か（台帳の `id`、または不明の `u1` など）も付きます。

主なオプション: `--model`（既定 `claude-opus-5-5`）、`--effort`（既定 `high`）、`--language`（説明文の言語、既定 `Japanese`）、`--ledger`（キャラ台帳の JSON。省略時は空の台帳）、`--synopsis`（前のページまでのあらすじのテキストファイル）。

### キャラ台帳

作品をまたいで持つ人物の一覧です。今は人が手で書きます（解析結果から台帳への記入は、まだ作っていません。#21）。

```json
{
  "characters": [
    {"id": "shigeo", "name": "シゲオ", "role": "主人公・語り手", "appearance": "黒髪の短髪、眼鏡、口ひげとあごひげ…"}
  ]
}
```

- `id` だけ必須です。`name`・`role`・`appearance` は省略できます（名前のない人物も載せられます）。
- `id` は、確認用画像のラベルに描けるよう英数字にしてください。`u1`, `u2`, … は台帳にいない人物用なので使えません。

### 出力 JSON

```json
{
  "source": {"file": "tests/fixtures/manga_page_01.jpg", "width": 1419, "height": 2000},
  "model": "claude-opus-5-5",
  "coordinate_system": "pixels, origin top-left, bbox = {x, y, width, height}",
  "page": {"summary": "…", "reading_direction": "right_to_left"},
  "persons": [
    {"id": "shigeo", "status": "known", "name": "シゲオ", "appearance": "…", "match_evidence": "…"},
    {"id": "u1", "status": "unknown", "name": null, "appearance": "…", "match_evidence": null}
  ],
  "panels": [
    {
      "id": "p1",
      "reading_order": 1,
      "bbox": {"x": 128, "y": 126, "width": 1163, "height": 812},
      "description": "…",
      "characters": [
        {"id": "p1-c1", "person": "shigeo", "description": "…", "expression": "笑顔", "bbox": {"x": 0, "y": 0, "width": 0, "height": 0}}
      ],
      "texts": [
        {"id": "p1-t1", "type": "narration", "content": "俺は シゲオ\n日本人だ", "speaker": "shigeo", "bbox": {"x": 0, "y": 0, "width": 0, "height": 0}}
      ]
    }
  ],
  "notes": ["u1 の名前はシゲオ（「俺は シゲオ 日本人だ」）"]
}
```

- 座標は元画像のピクセル単位（左上が原点）です。
- `texts[].type` は `narration` / `speech` / `thought` / `sound_effect` / `other` のいずれかです。
- `persons` はページに出てくる人物そのもの、`panels[].characters` はコマへの登場（枠 1 つ）です。登場は `person` で人物を指します。
  - 台帳の人物と一致したら `status: "known"` で、`id` は台帳の `id` です。
  - 台帳にいない人物は `status: "unknown"` で、`id` はそのページの中だけで有効な `u1`, `u2`, … です。同じページで何度登場しても同じ `id` です。
- `texts[].speaker` は、話し手（ナレーションなら語り手）の人物の `id` です。分からなければ `null` です。
- `notes` は、キャラ台帳を整理する人（またはエージェント）への申し送り事項です。不明の人物について、名前・役割・同じ人物かもしれない、など重要なことに気づいたときだけ、自由記述で短く書かれます。人物は `u1` などの `id` で書かれます。台帳への記入はしません。
- 座標はモデルの推定値のため、数％程度のずれが出ることがあります。

### テスト

```bash
pytest
```

テストは API を呼ばない代役クライアントで動くため、API キーは不要です。

## ディレクトリ構成

```
.
├── README.md         # このファイル
├── CLAUDE.md         # 開発ルール
├── pyproject.toml
├── manga_analyzer/   # 漫画ページ解析（draw.py: 確認用画像）
└── tests/
    ├── test_analyzer.py
    └── fixtures/     # テスト用画像の置き場所
```

## ステータス

開発準備中
