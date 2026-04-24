# Gold Set — LLM 分類精度計測用の正解データ

plan.md M1 T1.6 の成果物。7 カテゴリ LLM 分類 (`collector/llm_classifier.py`) の F1 計測基準。

## ファイル

| ファイル | 役割 | 書き込み者 |
|---|---|---|
| `candidates.jsonl` | 中立サンプル（LLM 出力を除外したツイート）。ラベラーはこちらを読む | `sample_gold_set_candidates.py` |
| `gold_set.jsonl` | 人手で付与した**正解ラベル** | 維持者（手動） |
| `answer_key.jsonl` | 各 news_id に対する LLM 推測（`sampled_from_category` 含む）。F1 計測時のみ使用 | `sample_gold_set_candidates.py` |

## 中立性ルール（必守）

1. **LLM 出力非提示**: `candidates.jsonl` には `llm_categories` / `categories` が含まれない。ラベラーはテキストのみを見て判断する。
2. **時期層化**: 直近 6 ヶ月を月次バケットに分割しサンプリング。季節的偏向・特定期間のテーマ偏りを回避。
3. **カテゴリ層化**: 7 カテゴリそれぞれから規定数 (M1: 5 件, M3: 10 件, M6: 15+ 件) を均等にサンプル。
4. **LLM 推測との照合は禁止**: ラベリング中に `answer_key.jsonl` を見ない。F1 計測時のみ自動突合。

## `gold_set.jsonl` スキーマ

1 行 1 ツイート（JSON Lines）:

```json
{
  "news_id": "1234567890",
  "tweet_url": "https://x.com/user/status/1234567890",
  "username": "user",
  "posted_at": "2026-03-15T10:00:00+09:00",
  "text": "ツイート本文",
  "labels": ["market_trend"],
  "labeler": "masaaki",
  "labeled_at": "2026-04-19T12:00:00+09:00",
  "notes": ""
}
```

**`labels` ルール**:
- 必ず 7 カテゴリのいずれかから選ぶ:
  `recommended_assets`, `purchased_assets`, `ipo`, `market_trend`, `bullish_assets`, `bearish_assets`, `warning_signals`
- 複数カテゴリ該当時は配列で全て記録（順序は重要度順）
- どのカテゴリにも該当しない場合は空配列 `[]` と `notes` に理由記録
- `is_contrarian=True` アカウント (gihuboy) の強気発言 → `warning_signals`

## ラベリング手順

```bash
# 1. 候補生成（本プロジェクトで1回のみ、追加時は --per-category を増やす）
python scripts/sample_gold_set_candidates.py --per-category 5

# 2-a. 既存の人手教師データ（output/human_annotations.json, annotator="human"）から自動マッピング
#      URL キーで突合し、一致分だけ gold_set.jsonl に書き出す。LLM 出力は一切触らない。
#      annotator != "human" は fail-fast で拒否される（中立性保護）。
python3 scripts/apply_human_annotations.py                # 実行
python3 scripts/apply_human_annotations.py --dry-run       # 差分確認のみ
python3 scripts/apply_human_annotations.py --labeler foo   # labeler 上書き

# 2-b. 2-a で一致しなかった候補を HTML UI で手動ラベル付け
python3 scripts/build_gold_set_labeler.py   # HTML ビルド
open output/label_gold_set.html             # ブラウザで 35 件をラベル付け
# 完了後ダウンロードした gold_set.jsonl を data/gold_set/gold_set.jsonl にマージ

# 3. 検証（30 分以内で 35 件、できれば 2 名でダブルラベル）
#    - 2 名のラベルが一致しない場合は話し合って確定
#    - 一致率 (Cohen's κ) 0.6 未満の場合はガイドライン再検討
```

## 維持計画

| マイルストーン | サイズ | 担当 |
|---|---|---|
| M1 完了 (2026-05) | 35 件（7 × 5） | ユーザー |
| M3 完了 (2026-07) | 70 件（7 × 10） | ユーザー |
| M6 完了 (2026-10) | 100 件以上 | ユーザー + 月次 +5 件ペース |

## F1 計測

`gold_set.jsonl` が 35 件以上揃ったら:

```bash
# 将来実装予定: scripts/measure_f1.py (plan.md M2 T2.0)
# python scripts/measure_f1.py --gold data/gold_set/gold_set.jsonl
```

7 カテゴリ macro F1 ≥ 0.80 が M2 着手ゲート条件。
