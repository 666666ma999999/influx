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
- [x] Phase 1 完了（2026-08-16）: configs/news_shock.json（18クエリ・凍結語彙v2）・
  scripts/news_shock_collect.py（selftest 13件）・news_shock_eval.py（selftest 7件）・
  news_shock_run.sh・com.influx.news-shock.plist（07:20/19:00）。実RSS疎通3回
  （語彙v1→v2→慣用句ガードで誤発火 13→1→0・真陽性のみ残存）
- [x] Phase 2 完了（2026-08-16）: Codex 4審（CONDITIONAL→NO-GO→NO-GO→**APPROVE/GO**）。
  指摘対応= 版焼き込み・評価器の事前実装・単語境界・攻撃語ガード・部分障害ゲート・
  多重起動ロック（読込前）・ログ肥大・osascriptクオート・繰延の保留規則・書式統一

## 凍結記録（2026-08-16）

- configs/news_shock.json sha256 = `57ba12012285d04b5877d9edd065156e7c57bbc7bcb977548646bb53bdced282`
- prereg_sha256 (v1凍結時): `8a93f25e…`（履歴）
- prereg_sha256 (v2追補後 2026-08-16・Codex GO): `13550da87ce54bd85ac5ed3d7a495e539a5407052e02bdc392b98a43931bf576`
- 台帳初期化: 凍結時に data/news_shock/news_log.jsonl を削除→観測開始（凍結前の試走行は残さない）
- launchd 登録: ユーザーの `!` 手打ち待ち（本ファイル末尾のコマンド）

- prereg_sha256 (§7 R2時計追補後 2026-08-17・Codex GO): `88543aa5ccb172055f09c1e056ee119a6f22e017083623a237dfa35e4296cd03`
- R2実装: probe レーン（--first-seen-probe・2h毎・first_seen_probe.jsonl）＋ clock_report
  （結合/欠測/境界跨ぎ率・R3凍結条件= n>=30∧率>=10%∧件数>=3・Codex 3審で APPROVE/GO）

## launchd 登録コマンド（ユーザー実行）

probe（R2時計・2時間毎）の登録:
```
! cp ~/Desktop/biz/influx/config/launchd/com.influx.news-shock-probe.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.influx.news-shock-probe.plist && launchctl print gui/$(id -u)/com.influx.news-shock-probe | head -4
```
本線（登録済み）:
```
! cp ~/Desktop/biz/influx/config/launchd/com.influx.news-shock.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.influx.news-shock.plist && launchctl print gui/$(id -u)/com.influx.news-shock | head -5
```

## 2026-10-11 古い記事の通知止め（P-INF-46 付帯・オーナー「続けて」＝推奨どおりと解釈）

- **実害の実測**（母集団= 本線台帳 `data/news_shock/news_log.jsonl` の status=hit 852／852 行・2026-08-16〜10-11 07:20 便・pubdate 解釈不能 0）: 掲載から便までの日数= 7 日以内 269・8〜30 日 110・31〜180 日 266・**180 日超 207**（最古 2012-09）。検索句の `when:2d` を Google News RSS が守っていない。通知した事象（dup_event=false）360／360 件では 2 日以内 68・7 日超 283・**180 日超 159**。
- **入れた物**: `scripts/news_shock_collect.py` に鮮度関門（`pub_age_days`・`is_stale`・既定 `NOTIFY_MAX_AGE_DAYS = 180`・config `collect.notify_max_age_days` があれば優先）。掲載から 180 日超の記事は **Mac 通知だけ**止める。台帳は従来どおり status=hit で残し `pub_age_days`／`stale` を足す・run_summary に `stale` 件数。**発火条件・語彙・config は変えていない**（config の sha は不変＝事前登録の母集団はそのまま）。日付が読めない記事は通す。
- **検証**: `--selftest` 全通過（鮮度関門 1 件追加・実害 2 本= 2022-08 Boliden 亜鉛 1507.3 日・2021-05 Johnson Matthey プラチナ 1971.8 日が stale）／`tests/test_news_shock_stale_gate.py` 5／5（本線 main() を取得と通知だけ差し替えて実走・関門を外すと古い記事も通知される故障注入つき）／一括ゲート 874 本 OK（Docker）／台帳の読み手 3 本（news_shock_eval・news_shock_clock_report・coverage_census）の selftest 通過。本番便の初回実走= 次の定時便（2026-10-11 19:00）＝未実走。
- **採点側の所見（事後の層別・判定には使っていない・P-INF-47 で裁定待ち）**: 評価済み事象（+5 営業日・338／338 事象・訂正行反映）は全体 勝ち 206・中央値 +2.23pt と見えるが、**掲載 2 日以内の本来の対象 57 事象は勝ち 15（26%）・中央値 −2.93pt**。8〜180 日 120 事象（勝ち 92）と 180 日超 153 事象（勝ち 95）は入口日が初便 2026-08-16 に 144 事象集中＝1 日の相場を数え直している。+20 営業日（278／278 事象）も 2 日以内 25 事象 勝ち 6・中央値 −5.64pt。
- presign レーン（P-INF-43 b）の同種リスク: 2026-10-11 の試走（--dry-run・台帳に書かない・クエリ 42／42・feed 6／6）で当たった記事 14／14 本は 7 日以内 12・8〜30 日 2・180 日超 0＝現時点で古い記事の混入なし。
