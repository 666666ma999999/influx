# Lessons Learned

## 2026-03-13: xAI API はクレジット購入が必須
- xAI アカウント作成 + APIキー発行だけでは API 呼び出しできない
- チームにクレジットを購入・割り当てる必要がある
- REST API / gRPC (xai-sdk) 両方とも同じ制約
- エラーメッセージに購入URL含まれる: `https://console.x.ai/team/{team_id}`

## 2026-03-13: xAI REST API の Cloudflare 1010 対策
- Docker内の urllib.request でxAI REST APIを叩くと `403 error code: 1010` (Cloudflare) が発生
- 原因: User-Agent ヘッダーなし → Cloudflare がbot判定
- 対策: `User-Agent: influx-signal-extractor/1.0` + `Accept: application/json` ヘッダー追加で解決

## 2026-03-13: SafeXCollector の正しい API
- `SafeXCollector(profile_path=..., shared_collected_urls=set())`
- `collector.collect(search_url=..., max_scrolls=..., group_name=...)` → `CollectionResult`
- 旧API (`profile_dir`, `setup()`, `teardown()`, `collect_tweets()`) は存在しない
- macOSでは VNC Docker (`docker-compose.vnc.yml`) が必要（X11/XQuartz不要）
