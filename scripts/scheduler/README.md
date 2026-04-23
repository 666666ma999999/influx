# Scheduler 設定雛形（plan.md M4）

`daily_pipeline.py` を自動起動する設定ファイル集。

## ファイル一覧

| ファイル | 用途 |
|---|---|
| `com.influx.daily_pipeline.plist` | macOS launchd（ユーザーエージェント） |
| `crontab.txt` | Linux / Docker 環境用 crontab エントリ |

## セットアップ（macOS）

```bash
# インストール
cp scripts/scheduler/com.influx.daily_pipeline.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.influx.daily_pipeline.plist

# 状態確認
launchctl list | grep com.influx

# 次回実行を即実行（テスト）
launchctl start com.influx.daily_pipeline

# ログ確認
tail -f output/pipeline_log/launchd.log
tail -f output/pipeline_log/launchd.err.log

# アンインストール
launchctl unload ~/Library/LaunchAgents/com.influx.daily_pipeline.plist
rm ~/Library/LaunchAgents/com.influx.daily_pipeline.plist
```

## セットアップ（Linux / Docker）

```bash
# 既存 crontab を退避 + 追記
crontab -l > /tmp/cur.cron 2>/dev/null || true
cat scripts/scheduler/crontab.txt >> /tmp/cur.cron
crontab /tmp/cur.cron

# 確認
crontab -l | grep influx

# ログ確認
tail -f output/pipeline_log/cron.log
```

## 運用上の注意

- **Cookie 期限切れ時**: `daily_pipeline.py` の `collect` ステップで `CookieExpiredError` 相当が発生 → stderr にログ残す（M1 T1.5）→ 翌日以降 VNC 経由で手動更新
- **LLM API Key**: デフォルト `--no-llm-compose` で LLM 呼ばない軽量モード。本番運用では Key 設定後 flag を外す
- **TZ**: plist/crontab 双方で `Asia/Tokyo` 固定。日次モニタリングログ `output/collection_metrics.jsonl` の `collected_at` と一致
- **0 件フォールバック**: compose.py M4 T4.1 で 0 件時は前日高 ER 投稿を候補に追加（安全網）
- **実行履歴**: `output/pipeline_log/{YYYY-MM-DD}.jsonl` に各ステップの exit code・所要秒を記録

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| launchctl load で "Load failed" | plist のパス記述（python3 絶対パス）を `/usr/local/bin/python3` 等に調整 |
| Cookie 期限切れで collect 失敗 | `scripts/import_chrome_cookies.py --chrome-profile "..." --account <id>` で Chrome から再抽出（refresh-x-cookies スキル参照） |
| impression_tracker が動かない | `track.py --from-schedule` を別 cron で 15 分毎起動（scheduled 予約を消費） |
| 承認待ちドラフトが溜まる | `python -m extensions.tier3_posting.cli.manage status` で整理 |

## 次期追加予定

- `track.py --from-schedule` を 15 分毎 cron（M1 T1.2 follow-up）
- `scripts/measure_f1.py` を月次 cron（M6 T6.2）
- `scripts/audit_routing.py` を月次 cron（M6 T6.5）
