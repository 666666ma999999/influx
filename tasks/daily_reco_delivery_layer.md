# 配信ループ（表示層）実装 — 株レコメンドの出力段新設

紐付け: 2026-07-31 敵対的クロスレビュー（Fable別文脈+Codex 両者一致「証拠ループと配信ループの二層化」）
状態: 実装完了（2026-07-31）・翌朝ジョブでの実走確認待ち

## 成功基準

1. `output/recipe_shelf.md` が朝ジョブで再生成される（7/30 沈黙クラッシュの復旧）✅ 手動実行で確認済み
2. `output/daily_reco.md` に証拠段4区分（正式合格/有力/観察/使用禁止）のレコメンドが出る ✅ 有力4銘柄・掲載式付きで生成確認
3. 表示層生成の成否が paper_today「稼働状況」に常時掲示される → 翌朝ジョブで確認
4. macOS 通知が朝ジョブから届く ✅ 通知経路の単体テスト済み（osascript exit 0）→ 翌朝ジョブで実走確認
5. 凍結不変: trials.jsonl / ledger / state への書込みゼロ・新規統計推定ゼロ（α非消費）✅ 設計上読み取りのみ

## 変更ファイル

- `scripts/build_recipe_shelf.py` — スコアボード見出しの表記揺れ許容（`**確定n(nostop)**` 対応）
- `scripts/build_daily_reco.py` — 新設（配信層。ledger/meta/watchlist の凍結値引用のみ）
- `scripts/daily_screen.py` — 表示層ビルド2本化+成否掲示+通知

## 残課題（次周候補）

- 翌朝ジョブ（com.influx.paper-screen 07:30）で 3/4 の実走確認
- 敵対レビュー指摘6（配信品質KPI）・指摘8（pending 78件の判決到達フロー）は未着手・別議題
