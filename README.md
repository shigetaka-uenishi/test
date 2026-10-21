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
```

主なオプション: `--model`（既定 `claude-opus-5-5`）、`--effort`（既定 `high`）、`--language`（説明文の言語、既定 `Japanese`）。

### 出力 JSON

```json
{
  "source": {"file": "tests/fixtures/manga_page_01.jpg", "width": 1419, "height": 2000},
  "model": "claude-opus-5-5",
  "coordinate_system": "pixels, origin top-left, bbox = {x, y, width, height}",
  "page": {"summary": "…", "reading_direction": "right_to_left"},
  "panels": [
    {
      "id": "p1",
      "reading_order": 1,
      "bbox": {"x": 128, "y": 126, "width": 1163, "height": 812},
      "description": "…",
      "characters": [
        {"name": "シゲオ", "description": "…", "expression": "笑顔", "bbox": {"x": 0, "y": 0, "width": 0, "height": 0}}
      ],
      "texts": [
        {"id": "p1-t1", "type": "narration", "content": "俺は シゲオ\n日本人だ", "speaker": null, "bbox": {"x": 0, "y": 0, "width": 0, "height": 0}}
      ]
    }
  ]
}
```

- 座標は元画像のピクセル単位（左上が原点）です。
- `texts[].type` は `narration` / `speech` / `thought` / `sound_effect` / `other` のいずれかです。
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
├── manga_analyzer/   # 漫画ページ解析
└── tests/
    ├── test_analyzer.py
    └── fixtures/     # テスト用画像の置き場所
```

## ステータス

開発準備中
