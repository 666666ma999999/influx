<!-- ARCHIVE: 新規追記禁止。CLAUDE.md L120 廃止 (2026-05-11) により本ファイルは静的保管。新規教訓は /save で wiki/meta/ に保存。 -->

# Lessons Learned

## 2026-04-23: plan.md の数式例は単位を明示する / 実装時は実装側の単位に合わせて解釈
- plan.md T0.3 の例 `score = win_rate * min(trackable/10, 1.0) * 100` — win_rate が 0-100% (パーセント) なら末尾 `* 100` で 10,000 スケールになり Exit Criteria の `score ≥ 70` と整合しない
- `_calc_horizon_stats` は `win_rate = round(len(winners)/len(trackable)*100, 1)` で 0-100 を返している → 実装では `* 100` を省略して 0-100 スケールに正規化
- 教訓: plan.md に数式例を書くときは「win_rate は 0-1 想定 / 0-100 想定」を明記。実装者は実装側の単位で読み替える
- 横展開: 他の KPI 式（engagement_rate 重み・noise_filter 閾値 等）も 0-1 / 0-100 / 0-1000 のどれか、docstring で単位を明示

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

## 2026-04-24: バッチパイプラインの jsonl ログは run_id + pipeline_start で多重実行耐性を確保する
- `scripts/daily_pipeline.py` 初版は `collect_start_at` を `_finish` 内で決めていたため、collect 途中でクラッシュすると開始時刻が残らず、classify→collect の所要時間が集計できなかった
- 同日再実行時、jsonl には複数 run のレコードが混在し、`_summarize_log` が過去 run の duration も合算して誤集計していた（Codex Stage 1 MUST 指摘）
- 対策: `uuid4().hex` の `run_id` を各レコードに付与し、集計関数で `rec["run_id"] != run_id` を必ずフィルタ。起動直後に `pipeline_start` レコードを `_append_log` で即保存し、途中クラッシュでも `collect_start_at` が必ず残るようにした
- 教訓 (バッチ基盤汎用): (1) append-only jsonl は `run_id` 必須 (2) 起動時の即時記録で「長時間処理の途中で落ちても開始時刻は残る」を担保 (3) 集計は必ず `run_id` でフィルタ
- 横展開チェック: 他の長時間バッチ（scheduler, impression_tracker 等）で jsonl ログを集計するコードが `run_id` を無視して duration を合算していないか grep

## 2026-04-24: T0.2「XQuartz/VNC DISPLAY 整備」は実態として不要だった（headless モードで解決）
- phase-tracker の記述「#2 TOP5 の 4 名以上アクティブ → T0.2 保留（XQuartz/VNC DISPLAY 未整備）」が M1 残課題として残っていた
- 実際のコード確認で `collector/inactive_checker.py:226` が `p.chromium.launch(headless=headless)` を受け取り、`check_inactive_accounts.py` に `--headless` フラグが存在
- `docker-compose.yml` も `./x_profile:/app/x_profile` を mount 済み。DISPLAY 不要な経路が既に成立していた
- 実行: `docker compose run --rm -T xstock python3 scripts/check_inactive_accounts.py --headless --no-cache` → 32/32 巡回成功、当日付 `output/2026-04-24/inactive_check_result.json` 生成
- 教訓: phase-tracker の「保留理由」は定期的に実コード状態と照合する。「環境未整備」と書かれていても `--headless` 等の回避路が実装済みだと気づきにくい
- 横展開チェック: 他の「保留」項目も同様に、現行コードで既に解決できる経路がないか確認する癖をつける

## 2026-04-24: 監視ログの書き込み失敗はパイプライン本処理を止めない（degraded warning で継続）
- `daily_pipeline.py` の `_append_log` / `_notify_pending` / `_summarize_log` で OSError 未捕捉のため、`/output` の permission/disk full で本処理ごと落ちる設計になっていた（Codex Stage 2 MUST 指摘）
- 対策: 各関数で `try/except OSError` して `[pipeline] <context> 失敗: {e}` を stderr に出して継続。書き込み失敗 → 当該レコードのみ欠落。読み取り失敗 → 空サマリで fallback
- 原則: **「監視するものが監視対象を壊してはいけない」(monitoring must not crash the thing it monitors)**。observability 層は本処理より壊れやすくて構わない、ただし壊れても本処理を道連れにしない
- 横展開: 同様のログ系（viewer 更新、drafts.jsonl の読み書き等）も I/O 失敗で本処理を止めていないか確認

## Session: 2026-04-24 12:47:51 (from: tasks/kabuki_strategy_sync.md)
### Decisions
## Decision Log
- 2026-04-24: task.md 起票。M4 の日次頻度を SSoT の週2に上書き（アカウント別頻度は SSoT が決定）
### Failures/Stuck
## Failures / Stuck Context
（未記録）

## 2026-07-06: 同一データセットへのフェッチャー並行起動で .tmp 衝突リスク（自己検出・実害なし）
- 症状: サブエージェントが自セッション内で jq_fetch を起動中（ps で 2 プロセス）なのに気づかず、親セッションからも nohup で同一 dataset(shortsale) のフェッチを追加起動 → 一時 3 本並走
- リスク: write_json_gz の一時ファイルパスが `<target>.tmp` 固定のため、同一ターゲットへの同時書き込みで tmp が混線 → os.replace 後に破損 gzip が残り、読み側は path.exists() のみで skip するため**破損が永続化**する
- 対処: pkill で全停止 → gunzip -t で全件整合性検査（破損ゼロ確認）→ 単独プロセスで再開
- 教訓: (1) **フェッチャー起動前に `ps aux | grep <script>` で既走プロセス確認を必須化** (2) 排他が必要な長時間ジョブは flock 等のロックファイルを実装に入れるべき（jq_fetch 将来改修時の TODO） (3) サブエージェントに「フェッチ実行」まで委譲したら、親は同じフェッチを起動しない（所有権を明確に）

## 2026-07-06: launchd から Desktop 配下のスクリプトを直接実行すると TCC でブロックされる
- 症状: `launchctl` 登録済みジョブが初回定時実行で exit 2。ログに `can't open file '...Desktop/biz/influx/...': [Errno 1] Operation not permitted`
- 原因: macOS のプライバシー保護(TCC)。launchd 起動プロセスはデスクトップ等の保護フォルダへのアクセス権を継承しない。手動実行(ターミナル経由)では Terminal の権限で通るため気づきにくい
- 解決: ProgramArguments を `/bin/bash -c 'exec /usr/bin/python3 <script>'` のラッパー方式に変更（この Mac では bash に既にフルディスクアクセス相当の許可があり、autopost/make-article の既存 LaunchAgents が同方式で exit 0 稼働中という実績を確認してから踏襲）
- 教訓: (1) Desktop/Documents 配下を触る launchd ジョブは最初から bash ラッパーで書く (2) 新規 LaunchAgent は登録後に `launchctl kickstart gui/$UID/<label>` で即時テストし、定時を待たずに exit code とログを確認する (3) FDA の GUI 付与はファイル選択でグレーアウトすることがあり、既存の動いている方式の踏襲が速い

## 2026-07-07: バックグラウンド実行の `| tail` パイプが exit code をマスクし「失敗が completed と誤通知」される（同日2回発生）
- 症状1: `cmd 2>&1 | tail -N` のバックグラウンド実行で、cmd が引数エラー/OOM で死んでもパイプ全体の exit code は tail の 0 になり、タスク通知が completed になる（volshock holdout 観測の --defer-entry 引数エラー、第15周 T2 の未完了がこれで見逃されかけた）
- 症状2: Docker 3 並列で各コンテナ約2.84GB × 3 > Docker Desktop 割当7.65GB → exit 137(OOMキル)。並列度はメモリ割当から逆算が必要
- 対処: (1) 長時間ジョブは `> log 2>&1` の直接リダイレクトにして exit code を保存 (2) 完了判定は通知でなく**成果物の実在**（report.md/台帳行）で行う (3) 重い Docker ジョブは逐次 or 2並列まで（docker stats で実測してから決める）
- 検知: 「completed 通知なのに台帳/レポートが無い」は本パターンをまず疑う

## 2026-08-01: Cookie再取得でラベル通りに抽出したら別人（@twittora_）だった — ハンドル照合ゲートが検出・ただし旧合鍵は上書き消失
- 症状: スキル記載の成功実績「Profile 2 → kabuki666999」を根拠に masa-2 の Chrome Profile 2 から抽出 → Docker側ハンドル照合で実体が @twittora_ と判明（identity-verify-before-use の実害3例目・maaaki 7/23事故と同型）。masa-2 の全プロフィール（Default/Profile 2）とも X ログインは twittora_ で、kabuki666999 のログインは現存しない
- 実害: import_chrome_cookies.py はバックアップを取らず上書きするため、旧 kabuki 合鍵（7/12取得）が復元不可で消失（gitignore域・TimeMachineなし）。※旧合鍵も同じ Profile 2 由来なら以前から twittora_ だった可能性あり（未確認・要データ帰属調査）
- 対処: (1) 誤合鍵は隔離 `cookies.json.WRONG_ACCOUNT_twittora_20260801`（削除しない・沈黙誤収集より可視故障） (2) `x_profiles/kabuki666999/expected_account.json` 新設（maaaki と同じガード） (3) スキルに「抽出→保存の前にハンドル照合」を必須手順化
- 再発防止の型: 抽出は必ず〈プロフィール実測→抽出→**ハンドル照合（AppTabBar_Profile_Link href）**→OKなら保存先へ、NGなら隔離〉。成功実績のプロフィール対応は Mac ごとに別物（ラベル・過去実績を信用しない）

## 2026-08-15: ベースレートに「過熱3ヶ月のローカル値9.5%」を全期間の市場基準として誤用し、17イベント中16本を誤って死に筋判定していた
- 症状: `scripts/tdnet_event_profile.py:211` に「市場ベースレート（既存実測 9.5%）」がハードコードされ、TDnet表題イベント13〜17種の採否判定が全部これで割られていた。実際の 9.5% の出所は `tasks/stock_algo_kpi_loop.md:669` の**2026-02〜04の3ヶ月**（3月13.0%/4月14.8%の過熱相場）のローカル値。同定義（終値+20%/20bd）の全期間確定値は catalog §6 の **4.1%**（2026-07-06・118ヶ月・n=58,497）
- 実害: 2.3倍厳しい基準で判定した結果、2026-08-02 に「表題レベルの無料データに入場ゲートを越えるイベントは無い」と結論。正しい4.1%で割り直すと**上位6本が lift 1.5倍超**（暗号資産3.22・株式分割2.37・MSワラント2.07・TOB意見表明1.78・第三者割当1.71・資本提携1.61）＝結論が覆る。自社株買いの死に筋判定だけは lift 0.61<1 で不変
- 根因: ①ローカル値（特定3ヶ月・過熱相場）と全期間確定値を区別せずコードに焼いた ②「既存実測」というラベルだけで出所を再確認しなかった ③同じ数字が event_profile / winrate-ev-summary / influencer_discovery_preregister へ横展開され、誰も出所を辿らなかった
- 対処: (1) 該当行を 4.1%（+高値タッチ版と比べる時のみ 8.5%）へ訂正しコメントで誤用の経緯を明記 (2) catalog #12 の lift 記述を訂正 (3) event_profile.md を再生成
- 再発防止の型: **比較基準（ベースレート・ベンチマーク）をコードや文書に書く時は、必ず〈定義・期間・n・出所ファイル:行〉の4点を同じ場所に併記する**。「既存実測」「確定値」だけのラベルは出所を辿れず、ローカル値の全期間流用を検知できない
