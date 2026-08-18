# task: X検索精度強化 — 打ち止め再検証（最小実行版）

- plan: `~/.claude/plans/kind-questing-scone.md`（承認済 2026-07-26）
- branch: `analysis/x-search-precision`（worktree `../influx-x-search-precision`）
- 実行ノート: `_tmp_` スクリプトは**入力データ（output/・未追跡 data/）が main ツリーにしか無い**ため main リポジトリ直下に置いて実行（`_tmp_*.py` は gitignore 済＝git 汚染ゼロ）。worktree には task.md・昇格物のみ。

## 成功基準

plan の成功基準 1〜5 を参照（C0 censoring 表 / A-lite 遷移表 / D-lite 手読み / C-lite カバレッジ差分 / research_pipeline.md 昇格1コミット）。

## Scope

- Batch 1: T0 正本保護 + T1 `_tmp_c0_censoring.py`（early-exit ① 判定）
- Batch 2: T2 `_tmp_a_lite.py` + T3 手読み（early-exit ② 判定）
- Batch 3: T4 `_tmp_c_lite.py` + T5 昇格

## Progress

- [x] T0 正本保護: `data/reverse_lookup/surge_episodes_1y.csv` 複製・diff 空・sha256=6cc0e1ee0ed3f3f9…・main commit 11888e3
- [x] T1 C0 censoring: join 426/426・full観測 = current 307 / origin_10bd 208 / origin_21bd 190 / onset 199（+partial 110）→ **early-exit ① 不発動**（見逃し説は手元で検証可能）。スクリプト= main `scripts/_tmp_c0_censoring.py`
- [x] T2 A-lite（main `scripts/_tmp_a_lite.py`）: uncovered 150アカの遷移 = PASS→FAIL 10 / PASS→PASS 3 / FAIL→FAIL 137。**散弾群の before PASS 10 → after 0（全滅）**・非散弾群 3→3。spam アカ 8（template 6/styled 4）。補助指標: スレッド型散弾（同日 codes≥5）29 アカ。premise 訂正: corpus max codes/post は direct のみ=2 だが社名解決込み=4（shotgun 6 投稿）
- [x] T3 手読み（D-lite）: 生存3アカ（bruceikegold=製錬所訪問回想 / harunokabu=優待報告 / inubashiri55=利確事後報告）**全て事前コールではない** → **early-exit ② 発動＝偽陽性説確定**。C-lite のみ続行
- [x] T4 C-lite（main `scripts/_tmp_c_lite.py`）: セルフチェック一致（current 27銘柄/770言及 = scorecard）。完全観測 primary: current 307ep→28cover / **origin_10bd 208ep→0 cover** / origin_21bd 190ep→2 / onset 199ep→37（新規8銘柄・手読みでは大半がニュース転載/結果報告/列挙スレ、真の事前テーゼは drdebuneko エンプラス等ごく少数）
- [x] Codex 実装後レビュー: C0/A-lite 全数値を独立再現・C-lite onset の暦日→営業日丸めバグ1件 CONFIRMED → 修正済（36/32/7 → 37/33/8）
- [x] T5 昇格: tasks/research_pipeline.md へ確定知見追記（main commit 782bcdc）。結論=打ち止め維持を支持（偽陽性説確定・起点近傍の見逃し説不支持）

## Session Handoff

- 2026-07-26: plan 承認（敵対レビュー2系統で原4レーン案を最小版へ縮小。round15c ANDゲート廃止→遷移表・二項検定削除・恒久実装は復活シグナル後の別プランへ分離）。実測前提: t0 は episode_start より中央値117日前・t0≥20251001 は 222/426。
- 2026-07-26: **全タスク完了**。次アクションはユーザー裁定待ち（打ち止め維持 / 採点器の防御的恒久修正のみ / onset 帯の標的新規収集）。裁定内容は research_pipeline.md §次アクション参照。worktree は裁定後に撤去可（`git worktree remove ../influx-x-search-precision`）。
