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
- **2026-09-12 T1〜T4 実装 commit 5dd3a19**（Codex gpt-5.6-sol・Astra は容量上限2回で切替・オーナー裁定）。統括の実読で追加修正1点: ラッパーの `ANTHROPIC_API_KEY` 必須を撤去（`~/.zshrc` に無く毎回 exit 2 になる／鍵が渡ると `claude -p` が API 課金に変わる）→ 抽出前に `env -u ANTHROPIC_API_KEY`。ゲート 354件 OK。
- **試走1**（16:41〜16:51・Bash 10分上限で打切り）: ログイン確認 OK・口座ごとに収集が進行（investramza/kabu1000/kabu_kabuki のファイル更新）・打切り時に trap で Docker 撤収を実測。**34口座の収集は10分超**＝launchd 土曜 9:00 なら問題なし。
- **試走2**（16:52:40〜・nohup 切り離し・ログ `$TMPDIR/rw_trial2.log`）: 結果待ち。launchd 登録（`ln -s` + `launchctl load`）は AI 権限で却下＝**オーナーの `!` 実行待ち**（手順は本ファイル T7）。
- **試走2 結果（16:52〜19:07）**: 収集= 34口座・21ファイル・480投稿・投稿0件13口座（閾値17未満）・`RW_SUMMARY` 実測。**抽出段で fail-closed 発動 rc=5**＝`--tools ""` だけでは道具が有効のままで、claude が stdout に JSON を返さず `extraction_result_20260912.json` 書き出し＋ signals.jsonl 取込（28件・19:04）を自走。→ 修正 commit 7367d79（`--disallowedTools` 明示・指示文強化・失敗時の生出力を `~/Library/Logs/influx-research-weekly-extraction-failed.txt` へ退避）。残り段は手動で完走: `winrate_score.py` 722件中 555 評価・scoreboard 19:07・vault 鏡更新。
- **既知の穴（追加）**: 1周 約2時間15分（投稿0件口座の再試行 10/20/40秒が主因）＝土曜9:00 の無人なら実害なし／worklist は過去ファイル含む全 `tweets_*.json` を走査（今回 906件・設計どおり）／`processed_tweet_urls.jsonl` は 07-11 から未更新（誰が書くかは未確認）。
- **未確認**: 道具禁止後の抽出段が stdout に JSON 配列を返すか（単独再試験 19:07 起動・結果は次の記帳で）。
- **抽出段 単独再試験（19:07:42〜19:10・道具禁止フラグ付き・worklist 890件）**: `claude rc=0`・stdout 9,295 bytes・JSON 配列 18件を parse・末尾は ```` ``` ```` のみ（ラッパー許容内）＝**S2 合格**。`MultiEdit` は「既知の道具でない」警告のため一覧から除去（commit 次項）。
- **Session Handoff（2026-09-12 19:15）**: 実装・試走とも完了（commit 5dd3a19→7367d79→e0ee441＋本 commit）。**残= T7 登録のみ（オーナーの `!` で `ln -s` + `launchctl load`）**。ロード後に `config/launchd/README.md` の行と `docs/pipeline-map.md` §4 未ロード一覧を「稼働中」へ更新する（次セッションの先頭作業）。
- **2026-11-20 23:0x masa-2 原因と直し（オーナー「1 全部直して」11-20 22:25）**: 無人の回は登録から毎回 `line 91: docker: command not found`→exit 4（`~/Library/Logs/influx-research-weekly.log` 実読・launchctl の PATH= `/usr/bin:/bin:/usr/sbin:/sbin`・plist に EnvironmentVariables 無し・docker の実体 `/usr/local/bin/docker`）。直し= 台本 40 行目に `export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"` を 1 行。
- **確かめ**: `bash -n` rc=0／薄い PATH で `which docker` 直す前 rc=1・直した後 `/usr/local/bin/docker`／`docker compose version` v5.0.2。本体は走らせていない（次の定時 11-21 土 09:00）。
- **次の回で見る所**: 同ログに `週次完了: 収集…/抽出…/取込…`（Mac 通知と同文）か `FATAL:` の行。当たりうる所= ①claude -p（無人では一度も通っていない・exit 5）②Cookie `x_profiles/maaaki/cookies.json` 10-29 のまま（失効なら exit 4・取り込みはオーナー）③終わる時の `docker compose down` が、前から動いている xstock-vnc（11-20 時点で 3 日稼働）も消す。
- **2026-11-21 09:00 の回（11:1x 実読）**: Docker 起動 OK＝PATH の直しは効いた。10:24 に exit 4（`投稿0件が閾値以上: 20 >= 17`）。`RW_SUMMARY` accounts 34／collected 14／zero 20／tweets 283。10:11〜10:24 の 14 口座は全部 `net::ERR_NAME_NOT_RESOLVED`（x.com の名前が引けない＝回線か入れ物の DNS の一時断・Cookie 失効ではない。11:1x にホストから x.com は引ける）。claude -p の段は未到達。③は実際に起きた（前から Running の xstock-vnc を終わりに削除・11:15 時点で入れ物なし）。
- **2026-11-21 11:46 手の 1 周（オーナー「1」・launchctl kickstart）**: 13:05 に exit 4 で同じ止まり方（collected 10／zero 24／tweets 220・名前引き失敗 17 口座・11:50〜12:35 にまばら、12:23〜12:35 は続けて）。Mac 側 60 回・入れ物の中 150 回の名前引きは全部成功＝短い試しでは再現しない一時的な失敗。直し= `research_weekly.py` に一巡後の 1 回だけの取り直し（300 秒後・取れなかった口座だけ）commit 40009dd。テスト 920 件 OK（Docker）・Codex 点検「問題なし」。3 周目の手の起動は AI の権限で止められた＝オーナーの `! launchctl kickstart gui/501/com.influx.research-weekly` か 11-28 09:00 の定時で確かめる。
- **2026-11-21 16:23 オーナー起動の 1 周（取り直し入り初回）**: 収集は合格＝34 口座中 1 巡目 20 口座→取り直し 12 口座を 300 秒後に→ collected 22／zero 12（<17）／tweets 459・名前引き失敗 0 件。19:24 に `winrate_worklist.py` で exit 1（`No module named 'playwright'`）。原因= 11-20 の自分の直しで PATH の**先頭**に `/opt/homebrew/bin` を足したため python3 が `/usr/bin/python3`（3.9.6・playwright あり）→ `/opt/homebrew/bin/python3`（3.14.7・なし）にすり替わった。直し= PATH の**後ろ**に足す形へ。launchd と同じ並びで python3=/usr/bin・docker=/usr/local/bin・claude=nvm を実測、worklist 段を手で流して 854 件で成功、テスト 920 件 OK。claude の段は未通過のまま（次の 1 周で確かめる）。
- **2026-11-21 20:02 オーナー起動の 1 周（runs=5）**: 1 巡目 22 口座・名前引き失敗 0。取り直し 12 口座は全部「ログイン確認OK・0 件」＝本当に投稿が無い口座（zero 12 < 17・tweets 482）。途中 Mac が 23:08〜01:48 眠って止まり、11-22 02:12 に exit 4「Cookie 認証異常を検出」。原因= 23:05:59 に @m_kkkiii 1 口座だけログイン画面へ転送（眠る 3 分前）→ 後の 5 口座はログイン確認OK なのに、異常の行が 1 行でもあれば止める決まりで全体が停止。直し= ラッパーの確かめを「後の口座でログイン確認OK か投稿取得が出れば続行・回復なし or 異常 6 行以上（3 口座分）なら停止」に。実ログで再生して続行（collected 22）、回復の前で切った版は停止、テスト 921 OK、Codex「問題なし」。**claude の段は未通過のまま**。別件の弱い所= 1 周 3 時間超で途中に Mac が眠ると止まる（眠らない設定はオーナー判断）。

## Decision Log（仕様差分・implementation-checklist STEP 2.7）
| 項目 | 種別 | 内容 |
|---|---|---|
| 実装担当 | deviation（オーナー裁定） | オーダー「Astra で実装」→ Astra 容量上限2回 → カードで Sol（gpt-5.6-sol）に切替を裁定 |
| ANTHROPIC_API_KEY | interpreted | 叩き台に無かった変更。`~/.zshrc` に鍵が無く毎回 exit 2 になる＋鍵が渡ると `claude -p` が API 課金へ切替わるため、必須解除＋抽出前 `env -u` |
| 道具禁止 | deviation（実害起点） | 叩き台は `--tools ""` 想定→試走2で無効と判明→`--disallowedTools` 明示（7367d79・998bf43） |
| 試走の完走 | interpreted | 抽出段の fail-closed 後、残り段（採点）は統括が手動実行して1周を完成。次回の無人1周（土曜）が S5 の最終確認 |
| launchd 登録 | open-question | AI 権限で `ln -s`/`launchctl load` が却下＝オーナー操作待ち |
- **S6 達成（2026-09-13）**: オーナー `launchctl load` → `launchctl list | grep` = `-	0	com.influx.research-weekly`（実測）。S5 は初回無人実行 09-19 土 9:00 のログで判定。
- **Codex レビュー（gpt-5.6-sol・2026-09-12 19:20・Critical 0／Major 5／Minor 2）→ 採用6・open 1**: M1 証跡をラッパーで強制上書き／M2 凍結34ユニークを不変条件化（research_weekly.py `EXPECTED_RESEARCH_ACCOUNTS`・ラッパー `accounts != 34` で停止）／M3 `winrate_worklist.py` は読込失敗で rc=1／M4 `winrate_ingest.py --mark-processed` で処理済み台帳を更新（worklist の無限再投入を止める）／Minor1 通知失敗を stderr に残す／Minor2 テストの引数一致を契約検証に緩和。**M5（S5 無人1周完走・S6 登録）は未達のまま open**＝オーナーの `launchctl load` 後、次の土曜 9:00 の1周で判定。
