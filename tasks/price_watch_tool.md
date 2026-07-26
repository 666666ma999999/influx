# task: X値上がり検出ツール（price_watch）

- plan: `~/.claude/plans/kind-questing-scone.md`（2026-07-26 承認・price_watch 版）
- 背景: 半導体高騰メカニズム検証（tasks/research_pipeline.md 2026-07-26節）の帰結。X値上がり投稿は日本株の急騰集中より約2ヶ月先行 → 検出ツール化（投資合否システムには組み込まない・独立ツール）

## 成功基準（plan より・全達成）

1. [x] configs/x_price_watch.json 固定30クエリ（id凍結・battery_sha で実効化）
2. [x] 実走で data/x_price_watch/ledger.jsonl へ全クエリ行 append（30/30 ok・censored 0・count 0-34）
3. [x] price_watch_alert.py --selftest 5/5 PASS（spike/flat/warmup/censored/sigma0）
4. [x] ランナー手動1回成功（rc=0）＋通知経路単発テスト OK
5. [~] launchd 登録 — **plist 配置済みだが launchctl load は権限denyのためユーザー実行待ち**（下記）
6. [x] 同セッション commit

## 運用メモ

- 毎日22:10 JST に前UTC日の30クエリを収集（1回約20分・アカウント @twittora_）
- **アラートが出るのは baseline 7日蓄積後 ＝ 2026-08-02 頃から**。それまでは件数記録のみ
- Codex 実装後レビューで CONFIRMED 8件 → 6件修正（引用RT二重計上・枯渇誤判定2連続化・battery_sha 凍結・--date 一致・ok率80%閾値・アラート重複排除）。受容2件: 広告混入ノイズ（相対比較で許容・docstring 注記）・alerts before/after の並行競合（日次単発ジョブのため有界）
- 初週の観察ポイント: login_wall/blocked 率（高ければ battery 半減 or 隔日化）・generic レーンの飽和有無

## Session Handoff

- 2026-07-26: 実装完了。残作業=ユーザーが `!` で実行:
  `cp config/launchd/com.influx.price-watch.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.influx.price-watch.plist && launchctl list | grep price-watch`
