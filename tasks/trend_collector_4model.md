# トレンドの自動収集（株と関係なく）— 作り方の 4 モデル議論（P-INF-50）

- Phase: 品薄予兆の源さがし（P-INF-44 の後続・株の判断からは切り離す）
- 起票: 2026-10-12（masa-2）

## 答える問い
> そうしたらGoogleトレンドや他のトレンドの自動収集する仕組みだけは作ってください、株と関係なく。ありとあらゆるトレンドを収集したいのでやり方を4モデル（fable5.1 統括・opus・GPT-6 Astra・codex Sol）で議論して、一致した結論と割れた論点を分けて報告して。

## 冒頭 ask の回答（2026-10-12 オーナー・カード）
- 集めたトレンドの使い道（4 つとも選択）= まず広く貯めるだけ／X 投稿・記事のネタ探し／広告・LP の題材／商品・仕入れ・品薄の語
- 統括の読み（カード内で宣言）= 今回は 4 モデルの議論の報告まで。仕組みを作るのは報告を見てもらった後。masa-2 に定期便を足す時は作る直前に yes／no

## 対象と数え方
- 起案者 4 体（互いの案を見せない）= Opus 5.5／Fable 5.1（別文脈の Claude 2 体）／Codex gpt-6-astra／Codex gpt-6.1-sol。渡した数 4 ／返った数は Handoff に書く
- 統合= Fable 5.1（同じ文脈へ他 3 体の全文を渡して〈一致／1 体だけ／割れ〉に分ける）。Astra と Sol だけの一致は 1 票に割り引く
- 進行役（本セッション・Opus 5.5）= 取り次ぎと機械検証（「無い／効いていない」型の主張は自分で確かめてから採る）
- 出所の候補は「提供手段がある」と「定期取得・保存してよい」を別に数える（規約・機械向け案内を起案者が自分で開いた物だけ「確認済み」）

## 成功基準
- vault の報告 1 枚に〈背景の復唱（証拠つき）／一致した結論／1 体だけの着眼／割れた論点／作る物の 5 点セット／選択肢 2 つ以上〉がある
- 各体の実モデル名を transcript・ログで確かめて書く
- 定期便・登録・課金は 0 件（作らない）

## 非ゴール
- 仕組みの実装・launchd 登録（報告の裁定後）／株の売買サインの再測定（P-INF-44 b で決着）／価格.com の自動取得（規約で不可）

## 素材の置き場
- 依頼文と各体の返答= `output/gtrends_x/trend_panel/`（gitignore・`brief_common.md`・`out_{astra,sol,fable,opus}.md`・`log_{astra,sol}.txt`）

## Session Handoff
- 2026-10-12 15:02 起案 4 体を起動（masa-2）。15:05 Astra・Sol 返却、15:07 Fable 返却（transcript の model= claude-fable-5-1）。Opus は実行中。
- 進行役の機械検証（15:08〜15:09）: 分野つき書き出し 6 本は md5 が 3 組で一致（買い物＝技術／ビジネス＝ゲーム／食＝趣味）＝分野の絞りは効いていない／`config/launchd/*.plist` は 19 本／HF 保存庫 README 65〜70 行に「Marketing or commercial targeting (intended for academic research only)」を実読／Google の robots は禁止 2 行のみ（http 200・93 bytes）／公式 RSS は 10 語／Wikipedia 上位は 1,000 記事。
- 2026-10-12 15:10 Opus 返却（transcript の model= claude-opus-5-5）＝渡した 4 ／返った 4。15:14 Fable の仕分け返却: 一致 8・1 体だけの着眼 12・割れ 4（置き場／Google の書き出しボタンの自動操作と頻度／X を入れるか／保存期限）。置き場は 3 体が「influx の外」、Fable も自案（中）を取り下げて外へ。
- 進行役の追加の確かめ: Google ニュースの robots は一般向け `Disallow: /`・`rss` を含む行 0 件（既存便 `scripts/news_shock_collect.py:246` がこの RSS を取っている＝範囲外の所見・今回は触らない）／X の robots は `Disallow: /`／GitHub `fdciabdul/Google-Trends-Keywords-Scraper` は `license None`。
- 報告（叩き台）= vault `02_Ai/trends/reports/trends-collector-4model-2026-10-12.md`（235 行）。台帳= P-INF-50 を新規記帳・P-INF-44 行の 6 回目の 3 案は失効と記載・調査ノートの該当表に失効の注意書き 1 行。
- 判断待ち（オーナー）: ①置き場〈a 新しい小さな置き場（推奨）／b influx の中／c 画面の自動操作なし／d 作らない〉②Google の書き出しを自動で毎日 1 回押すか ③X を入れるか ④登録が要る無料窓口と公式 API の申請。既定値= 日本だけ・masa-2・有料なし・保存庫は広告用途に渡さない。
- 未確認（作る前に読む）: YouTube の保存制限の原文／Google・X の規約の該当文／Wikipedia の API の利用条件／所要の見積り（Fable の見立てのみ）。
- 作った物: 0（定期便・登録・課金なし）。
