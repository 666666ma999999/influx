# influx pipeline-map（配管図）

> 役割: **「何が・いつ動いて・どこに出るか」だけ**を持つ。機能の境界・道具の一覧は
> `influx-architecture.md`（X収集基盤）と `influx-stock-algo-architecture.md`（株アルゴ研究）が正本で、
> ここには書かない。**本数・時刻・入口の表は機械生成**（`~/.claude/scripts/gen_pipeline_map.py`）＝手で書き足さない。
> 人が書くのは §2 の系統分け・§3 の依存・§4 の落とし穴だけ。
> 2026-08-14 新設（グローバル運用ルール タスクE・定期実行5本以上のため必須）。**本数はここに書かない**＝§1 の機械生成表と `ls config/launchd/*.plist` / `launchctl list | grep com.influx` が正本。last_verified: 2026-08-29

## 1. スケジュール（機械生成・手で編集しない）

<!-- 再生成: /usr/bin/python3 ~/.claude/scripts/gen_pipeline_map.py ~/Desktop/biz/influx
     ズレ確認: 同コマンド + --check（rc=3 でズレ）
     旧: influx-stock-algo-architecture.md §4-1 の手書き表（2026-08-14 に本ブロックへ移設・向こうはポインタ1行に） -->

<!-- AUTOGEN:start(schedule) -->
<!-- このブロックは scripts/gen_pipeline_map.py が上書きします（手で編集しない）。件数 16・生成元= git ls-files の plist / workflows -->

| ジョブ（Label / workflow） | いつ動くか | 入口 | 定義ファイル |
|---|---|---|---|
| com.influx.edinet-tob | 月曜 18:45／火曜 18:45／水曜 18:45／木曜 18:45／金曜 18:45 | `exec /usr/bin/python3 ~/Desktop/biz/influx/scripts/edinet_fetch.py --dataset documents_all` | `config/launchd/com.influx.edinet-tob.plist` |
| com.influx.fxnia-forward | 月曜 10:30 | `exec ~/Desktop/biz/influx/scripts/fxnia_forward_launchd.sh` | `config/launchd/com.influx.fxnia-forward.plist` |
| com.influx.jsf-archive | 月曜 12:30／月曜 19:30／火曜 12:30／火曜 19:30／水曜 12:30／水曜 19:30／木曜 12:30／木曜 19:30／金曜 12:30／金曜 19:30 | `exec /usr/bin/python3 ~/Desktop/biz/influx/scripts/jsf_daily_archive.py` | `config/launchd/com.influx.jsf-archive.plist` |
| com.influx.kpi-clock-sla | 月曜 08:45／火曜 08:45／水曜 08:45／木曜 08:45／金曜 08:45 | `exec /usr/bin/python3 ~/Desktop/biz/influx/scripts/kpi_clock_sla.py` | `config/launchd/com.influx.kpi-clock-sla.plist` |
| com.influx.kpi-loop-weekly | 月曜 09:41 | `~/.claude/scripts/vault-prompt-runner.sh "~/Desktop/biz/influx/prompts/scheduled/kpi-loop- …（全文は定義ファイル）` | `config/launchd/com.influx.kpi-loop-weekly.plist` |
| com.influx.news-shock-probe | 2時間ごと | `bash ~/Desktop/biz/influx/scripts/news_shock_probe_run.sh` | `config/launchd/com.influx.news-shock-probe.plist` |
| com.influx.news-shock | 毎日 07:20／毎日 19:00 | `bash ~/Desktop/biz/influx/scripts/news_shock_run.sh` | `config/launchd/com.influx.news-shock.plist` |
| com.influx.paper-screen | 月曜 07:30／火曜 07:30／水曜 07:30／木曜 07:30／金曜 07:30 | `exec /usr/bin/python3 ~/Desktop/biz/influx/scripts/daily_screen.py` | `config/launchd/com.influx.paper-screen.plist` |
| com.influx.price-universe | 月曜 11:00 | `bash ~/Desktop/biz/influx/scripts/price_universe_run.sh` | `config/launchd/com.influx.price-universe.plist` |
| com.influx.price-watch | 毎日 22:10 | `bash ~/Desktop/biz/influx/scripts/xprice_watch_run.sh` | `config/launchd/com.influx.price-watch.plist` |
| com.influx.research-weekly | 土曜 09:00 | `exec ~/Desktop/biz/influx/scripts/research_weekly_launchd.sh` | `config/launchd/com.influx.research-weekly.plist` |
| com.influx.sedori-trend | 月曜 09:00 | `bash ~/Desktop/biz/influx/scripts/sedori_trend_run.sh` | `config/launchd/com.influx.sedori-trend.plist` |
| com.influx.tob-forward | 毎日 07:15 | `exec ~/Desktop/biz/influx/scripts/tob_forward_launchd.sh` | `config/launchd/com.influx.tob-forward.plist` |
| com.influx.tob-monthly | 毎月1日 07:30／毎月2日 07:30／毎月3日 07:30／毎月4日 07:30／毎月5日 07:30／毎月6日 07:30／毎月7日 07:30／毎月8日 07:30／毎月9日 07:30／毎月10日 07:30 | `exec /usr/bin/python3 ~/Desktop/biz/influx/scripts/kpi_tob_candidate_score.py --forward -- …（全文は定義ファイル）` | `config/launchd/com.influx.tob-monthly.plist` |
| com.influx.us-watchlist | 火曜 10:30 | `exec "$HOME/Desktop/biz/influx/scripts/us_watchlist_launchd.sh"` | `config/launchd/com.influx.us-watchlist.plist` |
| （cron）crontab.txt | cron `30 6 * * 1` | `cd ~/Desktop/biz/influx && /usr/bin/env python3 scripts/check_inactive_accounts.py >> outp …（全文は定義ファイル）` | `scripts/scheduler/crontab.txt` |

<!-- AUTOGEN:end(schedule) -->

## 2. 系統分け（人が書く・どっちの機能マップの配管か）

| 系統 | ジョブ | 機能マップ（正本） |
|---|---|---|
| **株アルゴ研究** | `paper-screen` / `tob-forward` / `tob-monthly` / `edinet-tob` / `jsf-archive` / `kpi-clock-sla` / `kpi-loop-weekly` / `price-universe` / `price-watch` / `fxnia-forward` / `research-weekly` | `influx-stock-algo-architecture.md` |
| **X収集基盤** | `us-watchlist`（インフルエンサー投稿の週次収集・**2026-08-29 に株アルゴ側から移動**＝実体は `recollect_account.py` による X 収集で、保存先も `data/influencer_candidates/`）／`sedori-trend`（語の収集系）／※ `xbuzz-*` は **claude-env 側の launchd から influx のスクリプトを叩く**（定義は `~/.claude/launchd/`） | `influx-architecture.md` |

## 3. 依存と順序（人が書く）

- **寄付前の 07:15 `tob-forward` が唯一のエントリー機会**（ここを落とすとその日は張れない）。07:30 `paper-screen` はその後。
- `price-universe`（月曜 11:00）で対象銘柄の母集団を作り、日次の `price-watch`（22:10）が追いかける＝**母集団が先・観測が後**。
- `kpi-clock-sla`（平日 08:45）は**粗い死活監視**であって、データ網羅の証明ではない（両者を混同しない）。
- `xbuzz-*`（X の収集・追跡）は **claude-env の配管図**が持つ。実行スクリプトだけ influx にある＝**定義と実体が別 repo**。

## 4. 落とし穴（実測・人が書く）

- **定義の置き場が2種**: `config/launchd/`（plist・本数は `ls config/launchd/*.plist | wc -l` で引く）・`scripts/scheduler/crontab.txt`（Linux/Docker 用の雛形・Mac では動かない）。※ `scripts/scheduler/` の plist は 2026-08-29 に旧 daily_pipeline を退役して 0 本になった。探すときは両方見る。
- **repo にある定義と、実際に動いている本数は一致しない**。未ロードは `edinet-tob` / `kpi-loop-weekly` /
  `research-weekly` / `tob-monthly` の4本（2026-08-29 に1本ずつ照合）。
  **この表は repo 側の定義を数える**（マシン非依存）ので、稼働数は `launchctl list | grep com.influx` で引く（数を書かない）。
- **plist のコメントに `--` を書くと XML として壊れ、機械が読めなくなる**。`com.influx.tob-monthly.plist` が
  この状態だった（2026-08-14 修理済み）。コメントでオプションを書くときは `- -` と離す。
- 平日指定は `Weekday` の並記（月〜金の5エントリ）で表現されている＝表では「月曜…／火曜…」と並ぶ。

## 5. 関連正本

| 情報 | 正本 |
|---|---|
| 機能の境界・道具（X収集基盤） | `influx-architecture.md`（本ファイルと一緒に毎セッション自動注入） |
| 機能の境界・道具（株アルゴ研究・14工程） | `influx-stock-algo-architecture.md` |
| 前向きレーンの台帳・事前登録 | 同上 §4-2 と `tasks/*_preregister.md` |
| 運用ルール（誰がいつ直すか） | `~/.claude/rules/05-plan-task-md.md` §architecture |

## 6. 未反映（対のファイルからの宿題・機械が積む/人が消す）

- 該当なし
