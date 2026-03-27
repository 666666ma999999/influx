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

## 2026-03-27: Cookie暗号化キーはホスト/Docker間で統一必須
- `cookie_crypto.py` のデフォルトキーは `username@hostname` で生成される
- ホスト（masaaki_nagasawa@host）とDocker（pwuser@container-id）で異なるキーになる
- 解決: `COOKIE_ENCRYPTION_KEY` 環境変数で共通キーを設定（.envrc + .env + docker-compose）

## 2026-03-27: X(Twitter)のService WorkerがGraphQL傍受を阻止する
- `page.on("response")` ではSW経由のGraphQLレスポンスをキャッチできない
- `context.on("response")` + `service_workers="block"` でも0件だった
- 解決: DOMスクレイピング（`[data-testid="tweet"]`）が最も確実な方法

## 2026-03-27: noVNCのindex.htmlがない問題
- Dockerfile.vnc再ビルド後もnoVNCにアクセスすると「Directory listing for /」になる
- 原因: `/usr/share/novnc/index.html` が存在せず `vnc.html` のみ
- 解決: `ln -sf vnc.html index.html`
- ログだけ見て「RUNNING」と報告するのは偽陽性。実際にHTTPアクセスで確認すること

## 2026-03-27: Playwrightの無限スクロール最大取得パターン
- `--max-scrolls` 固定ではなく、stale N回連続 + 最大実行時間の複合条件
- `time.sleep()` 固定ではなく、DOM要素数の増加を待つ方式
- 逐次JSONL保存 + checkpoint.jsonでクラッシュ耐性
- グローバルスキル化: `~/.claude/skills/max-scroll-scrape.md`

## 2026-03-27: 要件定義なしで技術的改善から始めると基礎機能が抜ける
- Codexレビューの技術的指摘から改修を始めた結果、「承認→日時選択→投稿」「カレンダービュー」が欠落
- 正しい順序: ユーザー操作フロー定義 → 要件 → 設計 → 実装
