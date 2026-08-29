# launchd ジョブ一覧（新規作成・2026-07-11 監査O-4）

このディレクトリの3つのplistの現況を記録する（監査O-4: 「research-weekly.plistが未インストールのまま意図不明で放置」への対応。設置場所自体は`~/Library/LaunchAgents/`へのシンボリックリンク/コピーが必要で、このディレクトリのファイルは正本）。

| plist | 状態（2026-07-11実測: `launchctl list \| grep com.influx`） | スケジュール | 実行内容 |
|---|---|---|---|
| `com.influx.jsf-archive.plist` | **稼働中**（`launchctl list`に登録あり） | 月〜金 12:30/19:30 JST（計10回/週） | `scripts/jsf_daily_archive.py`（日証金の日次アーカイブ蓄積。§7-I I3で言及の日次データ収集元） |
| `com.influx.paper-screen.plist` | **稼働中**（`launchctl list`に登録あり） | 月〜金 7:30 JST | `scripts/daily_screen.py`（毎朝スクリーニング・ペーパートレード観察） |
| `com.influx.research-weekly.plist` | **意図的に未インストール**（`launchctl list`に登録なし。実測確認済み） | 土曜 9:00 JST（設定のみ・未有効化） | `scripts/research_weekly_launchd.sh`（インフルエンサー週次サイクル: `docs/influencer-winrate-spec.md` §8のwinrate_worklist→抽出→ingest→score一式を無人実行する想定） |
| `com.influx.price-watch.plist` | **稼働中**（2026-07-26 登録） | 毎日 22:10 JST | `scripts/xprice_watch_run.sh`（X値上がり検出: 固定30クエリ日次収集→zスコア判定→検知時Mac通知。台帳 `data/x_price_watch/ledger.jsonl`） |
| `com.influx.okasira-forward.plist` | **登録 2026-08-29**（P-INF-07・オーナー裁定「今すぐ専用 capture を追加」） | 毎週月曜 11:15 JST（fxnia の後） | `scripts/fxnia_forward_launchd.sh` を `FWD_ACCOUNT=okasira_kanki FWD_START=20260830 FWD_LABEL=okasira` で起動（同スクリプト共用・コピーなし）。出力 `data/influencer_candidates/forward/okasira_kanki.json`・`output/influencer_candidates/okasira_forward/`・台帳 `okasira_forward_ledger.tsv` |

## `com.influx.research-weekly` が未インストールである理由

`docs/influencer-winrate-spec.md` の非ゴール（§3）およびF6運用トリガーで、本フェーズ(P1)は
**ユーザーが週1回「インフルエンサー週次回して」と明示的に言うセッション内実行が正式**と定義されている。
完全無人化（launchd化）はF6で **P3（任意・後日）** と位置づけられており、P1受入完了時点では
意図的にスコープ外としている。plistファイル自体は将来のP3判断のためにリポジトリへ用意してあるが、
**ユーザーがP3実施を決定するまでインストール（`launchctl load`）しない**。

P3実施を決定した場合は、このREADMEの本行を「稼働中」に更新し、影響ファイル（`docs/influencer-winrate-spec.md`
§9 Phase分解のP3欄）とあわせて同一セッション内で更新すること（`rules/41-vault-project-structure.md`
同期義務に準拠）。

## 運用メモ

- `launchctl list | grep com.influx` で3ジョブの現況を随時確認できる（ロード済みならラベルが表示される）
- ログ出力先はいずれも `~/Library/Logs/influx-<name>.log`
- plist正本は本ディレクトリ（`config/launchd/`）。実際にlaunchdへ登録する際は`~/Library/LaunchAgents/`へ配置してから`launchctl load`する

## com.influx.price-universe（週次B2B価格チェッカー）

- **毎週月曜 8:30** に `scripts/price_universe_run.sh` を実行（34系列の価格取得＋発火判定）
- 設置: `cp config/launchd/com.influx.price-universe.plist ~/Library/LaunchAgents/` →
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.influx.price-universe.plist`
- ログ: `~/.claude/state/price-universe.{out,err}.log`

**2026-07-29 の事故**: `launchctl load` でその場のパスから読み込んだだけで
`~/Library/LaunchAgents/` に置いていなかったため、**登録が消えていた**
（`launchctl list` にヒット0・plist も不在）。日次の `com.influx.price-watch` は
正しく `~/Library/LaunchAgents/` にあったため生き残っていた。
→ **plist は必ず `~/Library/LaunchAgents/` に置いてから bootstrap すること。**

## xstock-vnc コンテナの扱い（常駐させない）

X の収集は `xstock-vnc` コンテナが必要だが、**restart ポリシーは意図的に `no` のまま**にしている。
実測でメモリ **1.79GiB** を占有するため、日次の数分のために常時2GB近くを使うのは割に合わない。

代わりに `scripts/xprice_watch_run.sh` が**実行時にコンテナが無ければ自分で起こす**
（最大120秒待機＋Xvfb立ち上げに15秒）。2026-07-27 と 07-29 の2回コンテナが消え、
22:10 の自動実行が `ERROR: xstock-vnc コンテナが稼働していない` で失敗した実害への対策。

常駐させたくなった場合は `docker-compose.vnc.yml` に `restart: unless-stopped` を1行足すだけ。
