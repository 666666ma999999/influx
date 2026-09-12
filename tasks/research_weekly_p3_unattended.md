# research-weekly 無人化（spec P3・P-INF-14 裁定 A・2026-09-12）— 実装計画（叩き台 v0）

**Phase**: `docs/influencer-winrate-spec.md` §9 P3「完全自動化」（§3 非ゴール「完全無人化」は 2026-09-12 オーナー裁定 A で解除）
**裁定の正本**: vault `wiki/meta/decisions.md` 2026-09-12 P-INF-14 ／ 経緯= vault `02_Ai/influx/notes/influx-x-kpi-mining-adversarial-review-2026-09-11.md` §5-2
**状態**: 叩き台（オーナー検収前・コード未着手）

## 目的
既に repo にある無人週次サイクル（`config/launchd/com.influx.research-weekly.plist` → `scripts/research_weekly_launchd.sh` → `scripts/research_weekly.py`）を、第2周敵対レビューで一致した不備4点を直した上で launchd に登録し、凍結34口座のシグナルを毎週無人で前向き蓄積する。新設はしない。

## 成功基準
| # | 基準 | 測定 |
|---|---|---|
| S1 | 収集対象が凍結34 `research_accounts` と完全一致（37 でも 11 でもない） | 実行ログ「凍結リストから34アカウント」＋ `_frozen34_candidates.json` の username 集合 == research_accounts |
| S2 | 新規 signals 行に `extraction_model` が `<model>/prompt-v2` 形式で入り、`signal_extractor.py`（v1 haiku）は呼ばれない | `tail -n 20 output/research/signals.jsonl` の全行に prompt-v2・ラッパーのログに SignalExtractor 不在 |
| S3 | fail-closed: own_posts=0 の口座が閾値以上 or Cookie 失効時に非0終了し Mac 通知が出る | 意図的に Cookie を無効にした試走で rc≠0 ＋通知 |
| S4 | `winrate_ingest.py` が「正当な0件=0／全件拒否=非0」で終了する | `--dry-run` で空入力と壊れた入力の rc を実測 |
| S5 | E2E 1周（収集→worklist→抽出→ingest→score→scoreboard）が launchd 相当の環境（`env -i`）で完走し `weekly_log.md` に1行増える | 手動 `launchctl start` の後にログ実読 |
| S6 | launchd 登録後、`launchctl list \| grep com.influx.research-weekly` に載る | 実測 |

## 全体図
influx-architecture.md §2（機能マップ）が代替（rules/05 K-308）。本タスクの流れ: `plist(土9:00)` → `research_weekly_launchd.sh`（鍵解決・docker up・**fail-closed 判定 NEW**） → `research_weekly.py`（凍結34 **research_accounts NEW** → phase_collect） → **`winrate_worklist.py` → `claude -p` + prompt v2 → `winrate_ingest.py`（rc 修正）→ `winrate_score.py` NEW（phase_evaluate/haiku を置換）** → `weekly_scoreboard.md`。

## 影響範囲
- `scripts/research_weekly.py`（名簿・後段の置換）
- `scripts/research_weekly_launchd.sh`（fail-closed・通知・抽出の呼び出し）
- `scripts/winrate_ingest.py`（:187 終了コード）
- `config/launchd/README.md`（研究週次の状態を「稼働中」へ・解除理由の追記）
- `docs/influencer-winrate-spec.md`（§3 非ゴール・F6 に P3 実施日を注記）
- `influx-architecture.md` §4/§5 と `docs/pipeline-map.md`（定期ジョブ増＝rules/05 同セッション義務）

## 変更禁止ファイル
- `data/influencer_list_frozen_2026-07-05.json`（凍結・変更禁止と purpose に明記）
- `docs/prompts/influencer_signal_extraction_v2.md`（v2 固定。変更は v3 別ファイル）
- `collector/signal_extractor.py`（v1 経路は残置・呼ばないだけ）
- `output/research/signals.jsonl`（append のみ・手編集禁止）

## Phase 0（先に実測・裁定 A の前提「未確認」を潰す）
- **P0-1**: launchd 相当の空環境から `claude -p` が動くか。コマンド:
  `env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin <abs path>/claude -p "Reply with exactly: OK" --output-format text --model haiku`
  期待: `OK`・rc=0。**本セッションでは権限却下により未実測**（オーナーが `!` で実行するか、承認の上で AI が実行）。失敗なら抽出は「収集だけ無人・抽出はセッション内（現行ランブック §8）」に縮退＝ P2 の設計を変える。
- **P0-2**: `claude -p` の費用（購読内で 0 円か）を実測1回で確認。

## タスク（各 2〜5 分・1 ファイル・検証コマンド付き）
| # | ファイル | 変更 | 検証 |
|---|---|---|---|
| T1 | `scripts/research_weekly.py` `_to_candidates` | `research_accounts`（34・文字列配列）から candidates を作る。`_contrarian_usernames` は collection_groups から読み続け research_accounts と交差した分だけ付与 | `python3 -c` で len==34・集合一致 |
| T2 | `scripts/research_weekly.py` `main` | `phase_evaluate`（haiku v1）呼び出しを削除し、`collected_files` の own_posts=0 件数を JSON で stdout に出して終了（後段はラッパーへ） | `--dry-run` 相当で SignalExtractor が import されないこと（`grep -n SignalExtractor`） |
| T3 | `scripts/winrate_ingest.py:187` | `return 0 if result["input_records"] > 0 else 1` → 「rejected_count == input_records かつ >0 → 2／それ以外 0」 | 空 JSON と壊れた JSON で rc 実測 |
| T4 | `scripts/research_weekly_launchd.sh` | ①own_posts=0 口座数 ≥ 閾値（既定 34 の 1/2=17）or Cookie 失効文字列で rc=4＋`osascript display notification`（xprice_watch_run.sh:24 と同型）②`winrate_worklist.py` → `claude -p`（prompt v2 をファイルで渡す・`--output-format json`・model 固定・`extraction_model` を後付け）→ `winrate_ingest.py` → `winrate_score.py` を直列・各段 rc≠0 で停止＋通知 | `bash -n`・意図的失敗で rc と通知 |
| T5 | `config/launchd/README.md`・`docs/influencer-winrate-spec.md` §3/F6 | 状態更新・P3 実施 2026-09-12 と裁定リンク | 実読 |
| T6 | `influx-architecture.md` §4/§5・`docs/pipeline-map.md` | 定期ジョブ 1 本追加・vault 鏡再同期 `scripts/sync_architecture_mirror.py` | 同スクリプト rc=0 |
| T7 | 登録 | `launchctl load ~/Library/LaunchAgents/com.influx.research-weekly.plist`（symlink）・`launchctl start` で 1 周 | S5・S6 |

## 検証（一括ゲート）
`python3 -m unittest discover -s tests`（324件）＋ T3/T4 の個別 rc 実測。実装後に `/adversarial-review` 軽量（Codex 1回・`/review`）。

## 既知の穴（レビュー単独指摘・本タスクで扱わない）
- 採点器は日本株 4桁.T のみ（`winrate_score.py:85`）＝ us_forward は原理的に採点不能（凍結34 は日本株口座なので影響なし）
- KPI 化（catalog:164）は n_20bd≥10 到達まで再開後 6ヶ月以上
- 柱D の n は n_20bd を使う（生件数で早見せしない）

## Session Handoff
- 2026-09-12: 叩き台 v0 作成・コード未着手・P0-1 未実測（権限却下）。次= オーナー検収 → P0-1 → T1〜T7。
- **2026-09-12 Phase 0 実測（オーナー `!` 実行）**: `env -i HOME PATH` のみ → `Not logged in`／`USER`・`LOGNAME`・`TMPDIR` を足す → `OK`。認証は Keychain `Claude Code-credentials`（ファイル無し）で、Keychain 参照に `USER` が要る。launchd は `USER`/`HOME`/`TMPDIR` を既定で渡すため無人成立の見込み（**T7 の1周で最終確認**）。ラッパーは `USER`/`LOGNAME` 未設定なら `id -un` で補う。費用= 購読内（`--model haiku` 1回・請求なし・未計測）。
