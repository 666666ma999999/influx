# news_shock レーン実装 — 商品名つき供給ショックのニュース収集

- 承認: 2026-08-16 オーナー「OK」（計画= tasks/xprice_reform_review.md 第2R→ゲート②通過）
- 入場条件の正本: docs/price-watch-universe.md §16u（商品名必須・帰属はカード経由のみ・ペーパー前向き）

## 成功基準

1. Phase 0: 直結型37社のうち「監視系列があるのに受益カード無し」が **0件**（系列自体が無い銘柄は対象外として明記）
2. Phase 1: 商品名×供給ショック語の AND 判定で発火→受益カードの銘柄が**型ラベル付き**で Mac 通知に出る。
   台帳 `data/news_shock/news_log.jsonl`（追記専用）・selftest 固定テスト全通過・RSS 1クエリの実測疎通
3. Phase 2: `tasks/news_shock_preregister.md` を凍結（評価=通知後の翌営業日寄り→+5/+20営業日 対TOPIX超過）・
   凍結前に Codex GO 取得（P3規約）
4. 非ゴール: 判定層再設計・価格レーン日次化・Xレーン変更（別議題）

## 進捗

- [x] Phase 0 完了（2026-08-16）: カード10枚追加（8社・7系列・sources v4）
  - confirmed 1社（5021 コスモ=E2 43.7%）／provisional 7社（1515・8002・9824・9934・3036×2系列・4043・5480＝証拠序列どおり30%未達/未取得を明記）
  - **対象外7社（価格系列そのものが sources に未実装＝price-source-onboarding 候補）**:
    1663 K&O(ヨウ素)・3861 王子/3865 北越(パルプ)・5302 日本カーボン(黒鉛電極)・
    2282 日本ハム(枝肉)・2811 カゴメ(加工トマト)・9119 飯野海運(BCTI)
  - 検証: sources に実在する系列を持つ直結型のうちカード無し **0件**（機械照合）
- [ ] Phase 1: config・collector・runner・selftest・疎通
- [ ] Phase 2: プレレジ→Codex GO→凍結
