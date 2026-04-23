# Lessons Learned

## 2026-04-19: 統計量のラベルは計算対象と一致させる（`f1_95ci` vs `recall_95ci`）
- measure_f1.py で Wilson CI を F1 のラベルで出力していたが、実計算は recall ベース（k=TP, n=TP+FN）
- レビューで「ラベルと意味が一致しない」と HIGH 指摘。`recall_95ci` にリネーム + コメントで近似根拠明示
- 教訓: 統計量の出力キー名は「何を計算しているか」と厳密に一致させる。F1 の正確な CI が欲しい場合はブートストラップ
- 横展開: 他の `_ci`/`_confidence`/`_interval` サフィックスも指標名と一致しているか確認

## 2026-04-19: フォールバックガードは「空コンテンツが生成される前」に入れる（後付けでは遅い）
- compose.py で weekly_report が「全カテゴリ 0 件」でも空テンプレドラフトを生成 → fallback_previous_high_er が発動しない
- レビューで「有用コンテンツなしドラフトが承認待ちに登録される」指摘
- 対策: generate_weekly_report 内で `sum(category_counts.values()) == 0` なら空 list を返す
- 教訓: 各 generator は「有意なコンテンツがあるか」で空判定し、上流のフォールバック判定を正しく発火させる

## 2026-04-19: 多義語 bare keyword 事前レビューの標準化
- 追加: ロング/ショート/レバ/スワップ/塩漬け の 5 語で FP テスト実施 → 10/20 ケースで false positive 検出
- 対策: 全て bare keyword 削除し、金融文脈付き双方向パターン化（"(株|銘柄|円|ドル|日経|FX|BTC|...)".*ロング / ロング.*(持|乗|エントリー|仕込|利確|損切|爆益) 等）
- 結果: 25 ケース全 PASS（FP=0、FN=0）
- テンプレ化: 新 keyword 追加時は「金融意図の肯定 5 ケース + 非金融での同単語使用 5 ケース」を書いてから追加する（test-driven keyword addition）

## 2026-04-19: 汎用キーワード ("ホールド" 等) は単独ではなく文脈付きパターンで追加する
- purchased_assets に "ホールド" を bare keyword で追加したところ "ホールド仕様の車" で false positive を起こした
- 対策: bare キーワード削除、patterns 側で `(株|銘柄|FX|BTC|コイン|ポジション|ガチ|長期)\w*ホールド` と `ホールド\w*(株|銘柄|ポジ|BTC|ETH|コイン|投資)` の双方向文脈マッチに変更
- 教訓: 多義語 (汎用名詞・一般動詞) を keyword に入れる前に必ず文脈付きパターンで囲む。テストケースは「金融意図」と「非金融での同じ単語使用」の両方を書く
- 横展開: "ロング"、"ショート"、"レバ" など他の多義語も再レビュー必要 → 本日実施、5 語全対応済み

## 2026-04-19: LLM プロンプトで主観語 (強気/弱気) を使うと実装差が出るので、ドメイン定数で正規化
- 逆指標 (gihuboy) の強気発言 → warning_signals ルールを llm_classifier プロンプトに書いていたが、LLM は「買った」を「強気」と判定せず漏れる（67% カバー）
- 修正: `config.py` に `CONTRARIAN_TRIGGER_CATEGORIES = {6 投資カテゴリ}` を定義し、`apply_contrarian_override()` ヘルパーで classifier/llm_classifier 両経路を強制統一（2026-04-19 ユーザー指示で「逆神」運用に拡大）
- 教訓: LLM に主観判定させる箇所があっても、ドメイン定数で決定的に post-process する SST 層を用意するとプロンプトブレに強くなる
- 横展開: LLM 出力の他のフィールド（confidence 閾値・カテゴリ優先度等）も同様に後処理で強制できるか検討

## 2026-04-19: ステータスフィルタは「拒否リスト」ではなく「許可リスト」で書く
- M1 T1.4 レビューで `get_latest_impressions` が `status=='scheduled'` のみ除外していたが、`rate_limited` / `login_required` / `error` 等の失敗レコードが UI に漏れる指摘を受けた
- 最初の実装: `if rec.get("status") == "scheduled": continue`
- 修正後: `if status is not None and status != "ok": continue`（正方向フィルタ + 後方互換）
- 教訓: 表示対象の判定は「拒否する status リスト」ではなく「許可する status リスト」で書く。新しい失敗ステータスが追加されるたびに UI が壊れるのを防げる
- 横展開: 他の `_api_get_*` / `load_*` 関数で同様のフィルタがないか定期確認

## 2026-04-18: 例外型は新設しただけでは不十分（既存の dict 返却経路も全て raise に揃える）
- T1.5 で `CookieExpiredError` を追加した最初の実装では `XPoster.post()` の Cookie 読込失敗のみ raise に変えたが、`_check_login_status` の失敗パスが依然として error dict を返していた
- レビュー Stage 2 で「Cookie 失効の主経路がバイパスされ、`error_type: cookie_expired` が記録されない」と HIGH 指摘
- 教訓: 新例外型を導入する際は (1) `grep` で同じ意味の dict 返却 / `return False` を全て洗い出す、(2) 各失敗経路を raise に揃える、(3) caller 側に try/except + 構造化 error_type 伝搬を追加、(4) early break / バッチ停止判定を行う、までが必須セット
- パラメータ追加検証パターン: `build_failure_history` のシグネチャに `error_type` を追加 → `Grep` で全 caller を検索 → 渡している箇所を確認

## 2026-04-18: Single Source of Truth は「定数化」だけでは足りない（呼び出し側を移行するまで Dual-Path）
- M1 T1.0 で `collector/config.py` に `CATEGORY_TEMPLATE_MAP` を定数化したが、最初の実装では `compose.py` の filter は依然ハードコードのままだった
- レビュー Stage 1/2 双方で「対応表が実装側で参照されていない」(Exit Criteria 6 未達) と HIGH 指摘
- 修正: `compose.py` に `CATEGORY_TEMPLATE_MAP` を import し、`_categories_for_template()` ヘルパーで動的に filter set を導出
- 教訓: Canonical Module 原則 (グローバル `20-code-quality.md`) は「定数を作る」ではなく「呼び出し元の重複を消す」までが必須セット。新定数を追加した際は必ず呼び出し側の Grep → 移行を同一コミットで行うこと
- 検証パターン: `inspect.getsource()` で関数本文に新定数名（`_categories_for_template` 等）が含まれるかを assert する単体テスト

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

## 2026-04-21: X bot 検知強化で Playwright 手動ログインが通らなくなった
- 3/22 に通った同一スクリプト（`scripts/refresh_cookies_vnc.py`）が 4/21 時点で「次へ」押下後にフォームがクリアされログイン不能
- 原因: Playwright の `--enable-automation` フラグ + `navigator.webdriver=true` が X の bot 検知に引っかかる
- 対処: `ignore_default_args=["--enable-automation"]` + `--disable-blink-features=AutomationControlled` + `add_init_script` で navigator.webdriver / languages / window.chrome を偽装 + 実 Chrome 風 user-agent
- 補足: セッション汚染されるとその後も通らないので、再試行前に `Default/Cookies*` と `Singleton*` を削除
- スキル化: `.claude/skills/refresh-x-cookies/SKILL.md` に統合（Bot 検知回避節追加）
- 教訓: 自動化フラグ除去だけでなく、「過去に通った」だけを根拠にせず X 側の検知強化を疑うこと。UA のドリフト（Chrome のバージョン更新）も fingerprint 不整合の原因になる

## 2026-04-21: X bot 検知が VNC Playwright ログインを完全阻止 → ホスト Chrome 抽出経路で迂回
- 症状: email/username 入力→次へでフォームが空に戻る無限ループ。stealth フラグ追加（`ignore_default_args=["--enable-automation"]`, `navigator.webdriver` 偽装）でも突破不可
- 原因推定: X の bot 検知が TLS fingerprint / IP reputation / Cookie 履歴まで見ている。自動化ブラウザは原理的に通らない可能性
- 解決: **ホスト Mac の Chrome で既にログイン済みなら、そこから Cookie を直接抽出**して `x_profiles/<account>/cookies.json` に保存する経路を追加
- 実装: `scripts/import_chrome_cookies.py`（macOS Keychain + openssl CLI 経由、cryptography 依存なし）
- 教訓 1: **Chrome 128+ は SHA256(host) の 32-byte プレフィックス**を Cookie 暗号値に付ける（ドメイン入れ替え対策）。復号後に剥がさないと値先頭が binary garbage になる
- 教訓 2: LIKE パターン `%x.com` は `.dropbox.com` 等「末尾が x.com」な全ドメインに誤マッチする。厳密ドメインフィルタ必須
- 教訓 3: openssl CLI の `-K $key` はプロセステーブル露出リスク（複数ユーザー Mac では注意）
- 教訓 4: `cookie_crypto.load_cookies_encrypted` は Fernet 復号失敗時に平文 JSON フォールバックするので、Docker 内 cryptography に頼らずホスト側で平文 JSON 保存で OK
- スキル: `.claude/skills/refresh-x-cookies/SKILL.md` に「最速経路」節追加
- 検証: 両アカウント（@kabuki666999, @maaaki）で `x.com/home` の `AppTabBar_Home_Link` 検知成功

## 2026-04-21: VNC Playwright ログイン経路は全廃、Chrome Cookie 抽出に一本化
- 削除: `scripts/refresh_cookies_vnc.py` / `scripts/setup_profile.py` / `scripts/setup_from_chrome.py`
- 理由: いずれも Playwright の `launch_persistent_context` + ユーザー手動ログインを前提としているが、X の bot 検知強化（`--enable-automation` / `navigator.webdriver` / TLS fingerprint）で 2026-04-21 時点で突破不能
- 確立した唯一の経路: `scripts/import_chrome_cookies.py`（openssl + macOS Keychain、Chrome 128+ の SHA256 host prefix strip 対応）
- 追従参照更新: `collector/x_collector.py`, `scripts/{fetch_bookmarks, fetch_engagement, collect_tweets, scheduler/README.md}`, `CLAUDE.md`, `README.md`, `plan.md` 全て新スクリプト参照に変更
- plan.md 恒久対策節: M0 完了後に 3 Batch (cookie_health_check / refresh_all_cookies / import_safari_cookies) を実装する計画として記録
- 教訓: X 検知強化で「手動ログイン」経路が塞がれたら、既存の別ブラウザ（普段使い Chrome）のログイン状態を再利用する経路に切り替える。Playwright で再現しようとしてはならない
