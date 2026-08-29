# scripts/scheduler/

Linux / Docker で動かす場合の **cron 雛形置き場**（`crontab.txt` 1枚だけ）。

> 🪦 2026-08-29: `com.influx.daily_pipeline.plist` と `daily_pipeline.py` は退役した（旧2段階分類
> パイプライン）。**macOS の定期実行は `config/launchd/` が正本**で、ここには置かない。
> 何がいつ動くかは `docs/pipeline-map.md`（機械生成）を見る。

| ファイル | 用途 |
|---|---|
| `crontab.txt` | Linux/Docker 用の雛形。現在の中身は Cookie 期限の週次チェック（`check_inactive_accounts.py`）のみ。**Mac では使わない** |

## 使い方（Linux/Docker のみ）

```bash
crontab -l > /tmp/cur.cron 2>/dev/null || true
cat scripts/scheduler/crontab.txt >> /tmp/cur.cron
crontab /tmp/cur.cron
crontab -l | grep influx      # 確認
```

## つまずいたら

| 症状 | 対処 |
|---|---|
| Cookie 期限切れで収集が失敗 | `python3 scripts/import_chrome_cookies.py --chrome-profile "<profile>" --account <id>`（詳細= `.claude/skills/refresh-x-cookies/SKILL.md`） |
| macOS で定期実行が動かない | ここではなく `config/launchd/` と `~/.claude/launchd/` を見る（`docs/pipeline-map.md` §4 に落とし穴） |
