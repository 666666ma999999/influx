"""
Claude モデル比較テスト: 3.5 Haiku vs Haiku 4.5 vs Sonnet 4.5

merged_all.json からランダム30件のツイート（llm_categories設定済み）を抽出し、
Claude Haiku 4.5 と Claude Sonnet 4.5 で同じプロンプトを使って分類した結果を
既存の Claude 3.5 Haiku の結果と比較する。
"""

import os
import sys
import json
import random
import urllib.request
import urllib.error
import time

# プロジェクトルートをパスに追加
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector.config import CLASSIFICATION_RULES


# テスト対象モデル（APIで呼び出す分のみ）
TEST_MODELS = [
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5"},
    {"id": "claude-sonnet-4-5-20250929", "label": "Sonnet 4.5"},
]

# 既存データのモデル（API呼び出しなし、データから取得）
BASELINE_LABEL = "3.5 Haiku"

# コスト概算 (USD per 1M tokens)  ※2025年時点の公開価格
COST_TABLE = {
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
}


def load_few_shot_examples(few_shot_path: str) -> str:
    """Few-shot例をJSONファイルから読み込んでテキスト形式に変換"""
    if not os.path.exists(few_shot_path):
        print(f"警告: Few-shot例ファイルが見つかりません: {few_shot_path}")
        return ""

    with open(few_shot_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        examples = data.get("examples", data) if isinstance(data, dict) else data

    formatted_examples = []
    for ex in examples:
        example_text = f"""例{len(formatted_examples) + 1}:
ユーザー: {ex.get('username', 'unknown')}
ツイート: {ex['text']}
逆指標: {ex.get('is_contrarian', False)}
→ カテゴリ: {', '.join(ex['categories'])}
→ 理由: {ex['reasoning']}"""
        formatted_examples.append(example_text)

    return "\n\n".join(formatted_examples)


def build_system_prompt(few_shot_examples: str) -> str:
    """
    LLMClassifierと同じロジックでシステムプロンプトを構築。
    出力形式はJSON配列（LLMClassifier準拠）。
    """
    # カテゴリ定義
    categories_desc = []
    for cat_key, cat_info in CLASSIFICATION_RULES.items():
        categories_desc.append(f"- {cat_key}: {cat_info['name']}")
    categories_text = "\n".join(categories_desc)

    base_prompt = f"""あなたは日本語の株式投資ツイートを分類する専門家です。

【分類カテゴリ】
{categories_text}

【分類ルール】
1. 各ツイートは複数のカテゴリに該当する可能性があります
2. 該当するカテゴリが1つもない場合は空配列を返します
3. is_contrarian=true（逆指標アカウント）のツイートで強気内容の場合:
   - bullish_assets（高騰している資産）には分類しない
   - warning_signals（警戒すべき動き）に分類する
   - 理由: 逆指標アカウントの強気発言は市場の天井シグナルとして機能
4. is_contrarian=true のツイートで弱気内容の場合:
   - bearish_assets（下落している資産）には分類しない
   - 逆指標の弱気発言は逆張りシグナルではないため、該当なし（空配列）とする
   - ただし市況分析を含む場合は market_trend に分類可
5. 「オススメしている資産」と「購入した資産」は明確に区別:
   - 「〜がいい」「おすすめ」→ recommended_assets
   - 「買った」「購入」「イン」→ purchased_assets
6. 信頼度（confidence）は以下の基準で設定:
   - 0.9-1.0: カテゴリが明確、根拠が確実
   - 0.7-0.9: カテゴリが妥当、根拠が十分
   - 0.5-0.7: カテゴリが推測、根拠がやや弱い
   - 0.3-0.5: カテゴリが不明瞭、根拠が不十分
7. confidence 0.5未満のカテゴリは出力に含めないこと
8. 以下のツイートは分類対象外（空配列を返す）:
   - 投資・金融・経済に全く関係ない内容（日常報告、食事、天気、挨拶等）
   - ニュースの単純な引用で投資判断や見解を含まないもの
   - フォロワー向け感謝メッセージ、自己紹介

【カテゴリ詳細】
- recommended_assets: 他者に推奨している資産。キーワード例: 割安、おすすめ、一択、〜がいい
- purchased_assets: 本人が実際に購入・保有した資産。キーワード例: 買った、購入、イン、エントリー、保有
- ipo: IPOに関する内容。キーワード例: IPO、新規公開、抽選、当選
- market_trend: 市況全体のトレンド分析。キーワード例: 相場、地合い、トレンド、利上げ、円安
- bullish_assets: 高騰している資産の報告。キーワード例: 爆上げ、急騰、ストップ高、絶好調
- bearish_assets: 下落している資産の報告。キーワード例: 暴落、急落、ストップ安、が弱い
- warning_signals: 警戒すべき動き。逆指標アカウントの強気発言、バブルサイン等

【出力形式】
JSON配列で以下の形式で返してください:
[
  {{
    "id": <ツイートID>,
    "categories": ["category1", "category2"],
    "reasoning": "分類の理由を日本語で簡潔に説明",
    "confidence": 0.85
  }}
]"""

    # Few-shot例を追加
    if few_shot_examples:
        base_prompt += f"\n\n【分類例】\n{few_shot_examples}"

    base_prompt += "\n\n必ずJSON配列のみを返してください。余計な説明は不要です。"

    return base_prompt


def call_anthropic_api(api_key: str, model: str, system_prompt: str,
                       user_content: str, max_retries: int = 3) -> dict:
    """Anthropic Messages APIを呼び出し"""
    for attempt in range(max_retries):
        try:
            request_body = {
                "model": model,
                "max_tokens": 4096,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_content}]
            }

            data = json.dumps(request_body).encode('utf-8')

            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": api_key,
                    "anthropic-version": "2023-06-01"
                }
            )

            with urllib.request.urlopen(req, timeout=120) as response:
                response_data = json.loads(response.read().decode('utf-8'))
                return response_data

        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            print(f"  API HTTP error (attempt {attempt + 1}/{max_retries}): {e.code}")
            print(f"    Error: {error_body[:300]}")

            if e.code in [429, 500, 502, 503, 504] and attempt < max_retries - 1:
                wait_time = 2 ** (attempt + 1)
                print(f"    {wait_time}秒待機してリトライ...")
                time.sleep(wait_time)
                continue
            else:
                raise Exception(f"API呼び出し失敗: {e.code} - {error_body[:300]}")

        except urllib.error.URLError as e:
            print(f"  ネットワークエラー (attempt {attempt + 1}/{max_retries}): {e.reason}")
            if attempt < max_retries - 1:
                wait_time = 2 ** (attempt + 1)
                print(f"    {wait_time}秒待機してリトライ...")
                time.sleep(wait_time)
                continue
            else:
                raise Exception(f"ネットワークエラー: {e.reason}")

    raise Exception("最大リトライ回数に到達")


def parse_anthropic_response(response: dict) -> list:
    """Anthropic APIレスポンスからJSON分類結果を抽出・パース"""
    content_blocks = response.get("content", [])
    if not content_blocks:
        print("  警告: APIレスポンスにcontentが含まれていません")
        return []

    # テキストブロックを探す
    response_text = ""
    for block in content_blocks:
        if block.get("type") == "text":
            response_text = block.get("text", "")
            break

    if not response_text:
        print("  警告: APIレスポンスにテキストが含まれていません")
        return []

    # ```json ブロックで囲まれている場合は除去
    response_text = response_text.strip()
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        # 最初の行（```json等）と最後の行（```）を除去
        if len(lines) >= 3:
            response_text = "\n".join(lines[1:-1])
        else:
            response_text = "\n".join(lines[1:])

    # JSONパース
    result = json.loads(response_text)

    # results配列のラッパーの場合
    if isinstance(result, dict) and "results" in result:
        return result["results"]
    elif isinstance(result, list):
        return result
    else:
        print(f"  警告: 想定外のレスポンス構造: {type(result)}")
        return []


def jaccard_similarity(set_a: set, set_b: set) -> float:
    """Jaccard類似度を計算"""
    if not set_a and not set_b:
        return 1.0  # 両方空なら完全一致
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def compute_pairwise_stats(sample_tweets: list, results_a: dict, results_b: dict,
                           label_a: str, label_b: str) -> dict:
    """2つのモデル結果間のペアワイズ統計を計算"""
    exact_matches = 0
    jaccard_scores = []
    a_only_count = 0
    b_only_count = 0
    both_empty = 0

    all_categories = list(CLASSIFICATION_RULES.keys())
    category_stats = {cat: {"agree": 0, "a_only": 0, "b_only": 0} for cat in all_categories}

    for i in range(len(sample_tweets)):
        cats_a = set(results_a.get(i, []))
        cats_b = set(results_b.get(i, []))

        if cats_a == cats_b:
            exact_matches += 1

        jaccard_scores.append(jaccard_similarity(cats_a, cats_b))

        if cats_a and not cats_b:
            a_only_count += 1
        elif cats_b and not cats_a:
            b_only_count += 1
        elif not cats_a and not cats_b:
            both_empty += 1

        for cat in all_categories:
            in_a = cat in cats_a
            in_b = cat in cats_b
            if in_a and in_b:
                category_stats[cat]["agree"] += 1
            elif in_a and not in_b:
                category_stats[cat]["a_only"] += 1
            elif not in_a and in_b:
                category_stats[cat]["b_only"] += 1

    n = len(sample_tweets)
    avg_jaccard = sum(jaccard_scores) / n if n > 0 else 0.0

    return {
        "exact_matches": exact_matches,
        "exact_rate": exact_matches / n if n > 0 else 0.0,
        "avg_jaccard": avg_jaccard,
        "a_only": a_only_count,
        "b_only": b_only_count,
        "both_empty": both_empty,
        "category_stats": category_stats,
        "label_a": label_a,
        "label_b": label_b,
        "n": n,
    }


def print_pairwise_summary(stats: dict):
    """ペアワイズ比較のサマリーを表示"""
    label_a = stats["label_a"]
    label_b = stats["label_b"]
    n = stats["n"]

    print(f"\n{'=' * 80}")
    print(f"  {label_a} vs {label_b}")
    print(f"{'=' * 80}")
    print(f"  サンプル数:                {n}")
    print(f"  完全一致:                  {stats['exact_matches']}/{n} ({stats['exact_rate']*100:.1f}%)")
    print(f"  平均Jaccard類似度:         {stats['avg_jaccard']:.3f}")
    print(f"  両方カテゴリなし:          {stats['both_empty']}")
    print(f"  {label_a}のみカテゴリあり: {stats['a_only']}")
    print(f"  {label_b}のみカテゴリあり: {stats['b_only']}")
    print()

    # カテゴリ別一致数
    print(f"  カテゴリ別一致数:")
    print(f"  {'カテゴリ':35s} | {'一致':>4s} | {label_a+'のみ':>10s} | {label_b+'のみ':>10s}")
    print(f"  {'-' * 70}")
    all_categories = list(CLASSIFICATION_RULES.keys())
    for cat in all_categories:
        cs = stats["category_stats"][cat]
        cat_display = f"{cat} ({CLASSIFICATION_RULES[cat]['name']})"
        if len(cat_display) > 33:
            cat_display = cat_display[:33]
        print(f"  {cat_display:35s} | {cs['agree']:4d} | {cs['a_only']:10d} | {cs['b_only']:10d}")


def main():
    # APIキー確認
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("エラー: ANTHROPIC_API_KEY 環境変数が設定されていません")
        sys.exit(1)

    # データロード
    merged_path = "output/merged_all.json"
    if not os.path.exists(merged_path):
        print(f"エラー: {merged_path} が見つかりません")
        sys.exit(1)

    with open(merged_path, 'r', encoding='utf-8') as f:
        all_tweets = json.load(f)

    print(f"全ツイート数: {len(all_tweets)}")

    # llm_categories が設定済みのツイートのみ抽出
    classified_tweets = [t for t in all_tweets if t.get("llm_categories") is not None]
    print(f"Claude 3.5 Haiku 分類済みツイート数: {len(classified_tweets)}")

    if len(classified_tweets) < 30:
        print(f"警告: 分類済みツイートが30件未満 ({len(classified_tweets)}件)。全件使用します。")
        sample_size = len(classified_tweets)
    else:
        sample_size = 30

    # 再現可能なランダムサンプリング
    random.seed(42)
    sample_tweets = random.sample(classified_tweets, sample_size)

    print(f"サンプルサイズ: {sample_size}件")
    print()

    # Few-shot例ロード
    few_shot_path = "data/few_shot_examples.json"
    few_shot_examples = load_few_shot_examples(few_shot_path)
    if few_shot_examples:
        print(f"Few-shot例をロード: {few_shot_path}")
    else:
        print("警告: Few-shot例なしで実行します")

    # システムプロンプト構築
    system_prompt = build_system_prompt(few_shot_examples)
    print(f"システムプロンプト長: {len(system_prompt)} 文字")
    print()

    # ユーザーメッセージを構築（30件一括）
    tweets_for_llm = []
    for i, tweet in enumerate(sample_tweets):
        tweets_for_llm.append({
            "id": i,
            "username": tweet.get("username", "unknown"),
            "text": tweet.get("text", ""),
            "is_contrarian": tweet.get("is_contrarian", False)
        })

    user_content = f"以下のツイートを分類してください:\n\n{json.dumps(tweets_for_llm, ensure_ascii=False, indent=2)}"

    # ===== 既存データからベースライン (3.5 Haiku) を取得 =====
    baseline_by_id = {}
    for i, tweet in enumerate(sample_tweets):
        baseline_by_id[i] = tweet.get("llm_categories", [])

    # ===== 各モデルでAPI呼び出し =====
    model_results = {}  # model_label -> {id: categories_list}
    model_usage = {}    # model_label -> usage dict

    for model_info in TEST_MODELS:
        model_id = model_info["id"]
        model_label = model_info["label"]

        print("=" * 80)
        print(f"{model_label} ({model_id}) API呼び出し中...")
        print("=" * 80)

        try:
            start_time = time.time()
            response = call_anthropic_api(api_key, model_id, system_prompt, user_content)
            elapsed = time.time() - start_time

            # 使用トークン情報
            usage = response.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            model_usage[model_label] = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "elapsed": elapsed,
            }

            print(f"  応答時間: {elapsed:.1f}秒")
            print(f"  使用トークン: input={input_tokens}, output={output_tokens}")

            # レスポンスパース
            classifications = parse_anthropic_response(response)
            print(f"  分類結果: {len(classifications)}件")

            # IDでインデックス化
            results_by_id = {}
            for cls in classifications:
                try:
                    cls_id = int(cls.get("id", -1))
                except (ValueError, TypeError):
                    continue
                results_by_id[cls_id] = cls.get("categories", [])

            model_results[model_label] = results_by_id

        except Exception as e:
            print(f"  エラー: {model_label} のAPI呼び出しに失敗しました: {e}")
            print(f"  このモデルの結果はスキップします。")
            model_results[model_label] = {}
            model_usage[model_label] = {"input_tokens": 0, "output_tokens": 0, "elapsed": 0}

        print()

    # ===== 比較テーブル（全3モデル） =====
    print()
    print("=" * 140)
    print("比較結果: 全モデル ツイート別分類")
    print("=" * 140)

    # ヘッダー
    col_labels = [BASELINE_LABEL] + [m["label"] for m in TEST_MODELS if m["label"] in model_results]
    header = f"{'#':>3} | {'ツイート (40文字)':40s}"
    for label in col_labels:
        header += f" | {label:25s}"
    header += " | 一致状況"
    print(header)
    print("-" * 140)

    for i, tweet in enumerate(sample_tweets):
        text = tweet.get("text", "").replace("\n", " ")
        if len(text) > 40:
            text_display = text[:40] + "..."
        else:
            text_display = text

        row = f"{i:3d} | {text_display:43s}"

        # ベースライン
        baseline_cats = set(baseline_by_id.get(i, []))
        baseline_str = ", ".join(sorted(baseline_cats)) if baseline_cats else "(none)"
        row += f" | {baseline_str:25s}"

        # 各テストモデル
        all_cats_sets = [baseline_cats]
        for model_info in TEST_MODELS:
            label = model_info["label"]
            if label not in model_results:
                row += f" | {'(error)':25s}"
                continue
            cats = set(model_results[label].get(i, []))
            all_cats_sets.append(cats)
            cats_str = ", ".join(sorted(cats)) if cats else "(none)"
            row += f" | {cats_str:25s}"

        # 一致判定（全モデルが同一か）
        if len(set(frozenset(s) for s in all_cats_sets)) == 1:
            match_str = "ALL_MATCH"
        else:
            # ベースラインとの最大Jaccard
            jaccards = []
            for s in all_cats_sets[1:]:
                jaccards.append(jaccard_similarity(baseline_cats, s))
            max_j = max(jaccards) if jaccards else 0.0
            if max_j == 1.0:
                match_str = "partial"
            elif max_j > 0:
                match_str = f"partial({max_j:.2f})"
            else:
                match_str = "DIFF"

        row += f" | {match_str}"
        print(row)

    # ===== ペアワイズ比較サマリー =====
    print()
    print()
    print("#" * 80)
    print("  ペアワイズ比較サマリー")
    print("#" * 80)

    # ベースライン vs 各モデル
    pairwise_results = []
    for model_info in TEST_MODELS:
        label = model_info["label"]
        if label not in model_results or not model_results[label]:
            continue
        stats = compute_pairwise_stats(
            sample_tweets, baseline_by_id, model_results[label],
            BASELINE_LABEL, label
        )
        pairwise_results.append(stats)
        print_pairwise_summary(stats)

    # テストモデル同士の比較（2モデル以上ある場合）
    active_models = [m for m in TEST_MODELS if m["label"] in model_results and model_results[m["label"]]]
    if len(active_models) >= 2:
        for idx_a in range(len(active_models)):
            for idx_b in range(idx_a + 1, len(active_models)):
                label_a = active_models[idx_a]["label"]
                label_b = active_models[idx_b]["label"]
                stats = compute_pairwise_stats(
                    sample_tweets, model_results[label_a], model_results[label_b],
                    label_a, label_b
                )
                pairwise_results.append(stats)
                print_pairwise_summary(stats)

    # ===== コスト概算 =====
    print()
    print("=" * 80)
    print("  コスト概算")
    print("=" * 80)
    print(f"  {'モデル':20s} | {'Input Tokens':>14s} | {'Output Tokens':>14s} | {'応答時間':>8s} | {'概算コスト (USD)':>16s}")
    print(f"  {'-' * 80}")

    for model_info in TEST_MODELS:
        label = model_info["label"]
        model_id = model_info["id"]
        u = model_usage.get(label, {})
        input_t = u.get("input_tokens", 0)
        output_t = u.get("output_tokens", 0)
        elapsed = u.get("elapsed", 0)

        cost_info = COST_TABLE.get(model_id, {"input": 0, "output": 0})
        cost = (input_t / 1_000_000 * cost_info["input"]) + (output_t / 1_000_000 * cost_info["output"])

        print(f"  {label:20s} | {input_t:14d} | {output_t:14d} | {elapsed:7.1f}s | ${cost:15.6f}")

    print()
    print("  ※ 3.5 Haiku (ベースライン) は既存データを使用しているためAPI呼び出しなし")
    print("  ※ コストは Anthropic 公開価格に基づく概算（実際のコストは利用プランにより変動）")

    # ===== 不一致ツイートの詳細 =====
    print()
    print("=" * 80)
    print("  不一致ツイートの詳細 (上位10件、全モデルが不一致のもの優先)")
    print("=" * 80)

    diff_tweets = []
    for i, tweet in enumerate(sample_tweets):
        baseline_cats = set(baseline_by_id.get(i, []))
        all_same = True
        for model_info in TEST_MODELS:
            label = model_info["label"]
            if label in model_results and model_results[label]:
                test_cats = set(model_results[label].get(i, []))
                if test_cats != baseline_cats:
                    all_same = False
                    break
        if not all_same:
            diff_tweets.append(i)

    for count, i in enumerate(diff_tweets[:10]):
        tweet = sample_tweets[i]
        text = tweet.get("text", "").replace("\n", " ")

        print(f"\n  [#{i}] @{tweet.get('username', '?')} (contrarian={tweet.get('is_contrarian', False)})")
        print(f"    テキスト: {text[:80]}{'...' if len(text) > 80 else ''}")

        baseline_cats = sorted(baseline_by_id.get(i, []))
        print(f"    {BASELINE_LABEL:12s}: {baseline_cats if baseline_cats else '(none)'}")
        print(f"      理由: {tweet.get('llm_reasoning', 'N/A')}")
        print(f"      信頼度: {tweet.get('llm_confidence', 'N/A')}")

        for model_info in TEST_MODELS:
            label = model_info["label"]
            if label in model_results and model_results[label]:
                test_cats = sorted(model_results[label].get(i, []))
                print(f"    {label:12s}: {test_cats if test_cats else '(none)'}")

    if len(diff_tweets) > 10:
        print(f"\n  ... 他 {len(diff_tweets) - 10} 件の不一致あり")

    # ===== 最終推奨 =====
    print()
    print("=" * 80)
    print("  最終推奨")
    print("=" * 80)

    if not pairwise_results:
        print("  API呼び出しが全て失敗したため、推奨を出せません。")
    else:
        # ベースラインとの比較結果のみ抽出
        baseline_comparisons = [s for s in pairwise_results if s["label_a"] == BASELINE_LABEL]

        print()
        print("  モデル別スコアまとめ:")
        print(f"    {'モデル':15s} | {'完全一致率':>10s} | {'平均Jaccard':>12s} | {'概算コスト':>12s}")
        print(f"    {'-' * 60}")

        # ベースライン行
        print(f"    {BASELINE_LABEL:15s} | {'(基準)':>10s} | {'(基準)':>12s} | {'$0 (既存)':>12s}")

        best_model = None
        best_score = -1

        for stats in baseline_comparisons:
            label = stats["label_b"]
            model_id = next((m["id"] for m in TEST_MODELS if m["label"] == label), "")
            u = model_usage.get(label, {})
            input_t = u.get("input_tokens", 0)
            output_t = u.get("output_tokens", 0)
            cost_info = COST_TABLE.get(model_id, {"input": 0, "output": 0})
            cost = (input_t / 1_000_000 * cost_info["input"]) + (output_t / 1_000_000 * cost_info["output"])

            print(f"    {label:15s} | {stats['exact_rate']*100:9.1f}% | {stats['avg_jaccard']:12.3f} | ${cost:11.6f}")

            # スコア = Jaccard (精度) を重視
            score = stats["avg_jaccard"]
            if score > best_score:
                best_score = score
                best_model = label

        print()
        if best_model:
            print(f"  推奨: {best_model}")
            best_stats = next(s for s in baseline_comparisons if s["label_b"] == best_model)
            print(f"    - ベースライン (3.5 Haiku) との平均Jaccard類似度: {best_stats['avg_jaccard']:.3f}")
            print(f"    - 完全一致率: {best_stats['exact_rate']*100:.1f}%")
            print()
            if best_score >= 0.9:
                print("    評価: ベースラインとの一致度が非常に高く、分類品質は同等以上と判断。")
            elif best_score >= 0.7:
                print("    評価: ベースラインとの一致度が高い。一部の境界ケースで分類が異なるが概ね同等。")
            elif best_score >= 0.5:
                print("    評価: ベースラインとの一致度は中程度。分類傾向に差異が見られる。")
            else:
                print("    評価: ベースラインとの一致度が低い。分類基準の解釈に大きな差異がある可能性。")

            print()
            print("    ※ この比較は30件のサンプルに基づく暫定評価です。")
            print("      本格採用前にはより大きなサンプルでの検証を推奨します。")

    print()
    print("=" * 80)
    print("テスト完了")
    print("=" * 80)


if __name__ == "__main__":
    main()
