#!/usr/bin/env python3
"""手動アノテーション(Gold Standard)とKW分類/LLM分類/ML分類の精度比較スクリプト"""

import argparse
import csv
import json
import re
from collections import Counter, defaultdict

CATEGORIES = [
    "recommended_assets",
    "purchased_assets",
    "sold_assets",
    "winning_trades",
    "ipo",
    "market_trend",
    "bullish_assets",
    "bearish_assets",
    "warning_signals",
]

# コンポーネント定義: (表示名, 予測フィールド名)
COMPONENTS = {
    "regex": ("KW分類(regex)", "categories"),
    "llm": ("LLM分類", "llm_categories"),
    "ml": ("ML分類", "ml_categories"),
}


def evaluate(gold_dict, tweet_dict, pred_field):
    """Gold Standardに対する予測の精度を評価する。

    Args:
        gold_dict: URL → set(human_categories) の辞書
        tweet_dict: URL → ツイート辞書
        pred_field: 予測カテゴリのフィールド名 ("categories" or "llm_categories")

    Returns:
        dict: 評価メトリクス
    """
    tp = defaultdict(int)
    fp = defaultdict(int)
    fn = defaultdict(int)
    tn = defaultdict(int)

    fp_samples = defaultdict(list)
    fn_samples = defaultdict(list)

    exact_match = 0
    jaccard_sum = 0.0
    n = 0

    for url, human_cats in gold_dict.items():
        tweet = tweet_dict.get(url)
        if tweet is None:
            continue
        n += 1

        pred_cats = set(tweet.get(pred_field, []))

        # 完全一致
        if human_cats == pred_cats:
            exact_match += 1

        # Jaccard類似度
        if not human_cats and not pred_cats:
            jaccard_sum += 1.0
        elif not human_cats or not pred_cats:
            jaccard_sum += 0.0
        else:
            intersection = human_cats & pred_cats
            union = human_cats | pred_cats
            jaccard_sum += len(intersection) / len(union)

        # カテゴリ別TP/FP/FN/TN
        for cat in CATEGORIES:
            in_pred = cat in pred_cats
            in_gold = cat in human_cats
            if in_pred and in_gold:
                tp[cat] += 1
            elif in_pred and not in_gold:
                fp[cat] += 1
                fp_samples[cat].append({
                    "url": url,
                    "username": tweet.get("username", ""),
                    "text": tweet.get("text", ""),
                    "gold_categories": sorted(human_cats),
                    "predicted_categories": sorted(pred_cats),
                })
            elif not in_pred and in_gold:
                fn[cat] += 1
                fn_samples[cat].append({
                    "url": url,
                    "username": tweet.get("username", ""),
                    "text": tweet.get("text", ""),
                    "gold_categories": sorted(human_cats),
                    "predicted_categories": sorted(pred_cats),
                })
            else:
                tn[cat] += 1

    # カテゴリ別Precision / Recall / F1
    per_category = {}
    for cat in CATEGORIES:
        p = tp[cat] / (tp[cat] + fp[cat]) if (tp[cat] + fp[cat]) > 0 else 0.0
        r = tp[cat] / (tp[cat] + fn[cat]) if (tp[cat] + fn[cat]) > 0 else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
        per_category[cat] = {
            "precision": p,
            "recall": r,
            "f1": f1,
            "tp": tp[cat],
            "fp": fp[cat],
            "fn": fn[cat],
            "tn": tn[cat],
        }

    # Micro集計
    total_tp = sum(tp.values())
    total_fp = sum(fp.values())
    total_fn = sum(fn.values())
    total_tn = sum(tn.values())
    micro_p = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0.0
    micro_r = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 0.0
    micro_f1 = 2 * micro_p * micro_r / (micro_p + micro_r) if (micro_p + micro_r) > 0 else 0.0

    return {
        "n": n,
        "exact_match_rate": exact_match / n if n > 0 else 0.0,
        "jaccard_avg": jaccard_sum / n if n > 0 else 0.0,
        "micro_precision": micro_p,
        "micro_recall": micro_r,
        "micro_f1": micro_f1,
        "per_category": per_category,
        "total_tp": total_tp,
        "total_fp": total_fp,
        "total_fn": total_fn,
        "total_tn": total_tn,
        "fp_samples": dict(fp_samples),
        "fn_samples": dict(fn_samples),
    }


def analyze_errors(metrics):
    """FP/FNが多いカテゴリのエラーサンプルを出力する。

    Args:
        metrics: evaluate関数の戻り値
    """
    # FPが多い順にソート
    fp_sorted = sorted(
        CATEGORIES,
        key=lambda c: metrics["per_category"][c]["fp"],
        reverse=True,
    )
    fn_sorted = sorted(
        CATEGORIES,
        key=lambda c: metrics["per_category"][c]["fn"],
        reverse=True,
    )

    print("\n  --- エラー分析: FP上位カテゴリ ---")
    for cat in fp_sorted:
        fp_count = metrics["per_category"][cat]["fp"]
        if fp_count == 0:
            continue
        samples = metrics["fp_samples"].get(cat, [])[:5]
        print(f"\n  {cat} (FP={fp_count}件)")
        for i, s in enumerate(samples):
            print(f"    [{i+1}] {s['text'][:50]}...")
            print(f"        {s['url']}")

    print("\n  --- エラー分析: FN上位カテゴリ ---")
    for cat in fn_sorted:
        fn_count = metrics["per_category"][cat]["fn"]
        if fn_count == 0:
            continue
        samples = metrics["fn_samples"].get(cat, [])[:5]
        print(f"\n  {cat} (FN={fn_count}件)")
        for i, s in enumerate(samples):
            print(f"    [{i+1}] {s['text'][:50]}...")
            print(f"        {s['url']}")


def print_confusion_matrix(metrics):
    """カテゴリ別混同行列(TP/FP/FN/TN)を表示する。

    Args:
        metrics: evaluate関数の戻り値
    """
    print(f"\n  --- カテゴリ別混同行列 ---")
    header = f"  {'カテゴリ':20s} | {'TP':>5s} | {'FP':>5s} | {'FN':>5s} | {'TN':>5s}"
    separator = f"  {'-'*20}-+-{'-'*5}-+-{'-'*5}-+-{'-'*5}-+-{'-'*5}"
    print(header)
    print(separator)

    for cat in CATEGORIES:
        m = metrics["per_category"][cat]
        print(
            f"  {cat:20s} | {m['tp']:5d} | {m['fp']:5d} | {m['fn']:5d} | {m['tn']:5d}"
        )

    print(separator)
    print(
        f"  {'合計':20s} | {metrics['total_tp']:5d} | {metrics['total_fp']:5d} "
        f"| {metrics['total_fn']:5d} | {metrics['total_tn']:5d}"
    )


def extract_ngrams(text, n=2):
    """テキストからn-gramを抽出する。

    Args:
        text: 入力テキスト
        n: n-gramのn

    Returns:
        list[str]: n-gramのリスト
    """
    # URLを除去
    text = re.sub(r'https?://\S+', '', text)
    # メンション・ハッシュタグの記号を除去（テキスト部分は残す）
    text = re.sub(r'[@#]', '', text)
    # 句読点・特殊文字を除去
    text = re.sub(r'[、。！？!?\s　\n\r\t]+', ' ', text)
    text = text.strip()

    if not text:
        return []

    chars = list(text.replace(' ', ''))
    if len(chars) < n:
        return []

    return [''.join(chars[i:i+n]) for i in range(len(chars) - n + 1)]


def analyze_error_patterns(metrics):
    """FP/FNツイートのテキストから頻出2-gramパターンを抽出して表示する。

    Args:
        metrics: evaluate関数の戻り値
    """
    print(f"\n  --- 誤分類パターン分析 (頻出2-gram) ---")

    for cat in CATEGORIES:
        fp_texts = [s["text"] for s in metrics["fp_samples"].get(cat, [])]
        fn_texts = [s["text"] for s in metrics["fn_samples"].get(cat, [])]

        if not fp_texts and not fn_texts:
            continue

        print(f"\n  [{cat}]")

        if fp_texts:
            fp_counter = Counter()
            for text in fp_texts:
                ngrams = extract_ngrams(text)
                fp_counter.update(set(ngrams))  # 同一ツイート内の重複を除外
            top_fp = fp_counter.most_common(5)
            if top_fp:
                print(f"    FP頻出パターン (偽陽性 {len(fp_texts)}件):")
                for gram, count in top_fp:
                    print(f"      '{gram}' x {count}")

        if fn_texts:
            fn_counter = Counter()
            for text in fn_texts:
                ngrams = extract_ngrams(text)
                fn_counter.update(set(ngrams))
            top_fn = fn_counter.most_common(5)
            if top_fn:
                print(f"    FN頻出パターン (偽陰性 {len(fn_texts)}件):")
                for gram, count in top_fn:
                    print(f"      '{gram}' x {count}")


def print_table(title, metrics):
    """評価結果を表形式で出力する。

    Args:
        title: テーブルのタイトル
        metrics: evaluate関数の戻り値
    """
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print(f"{'=' * 72}")
    print(f"  突合件数: {metrics['n']}件")
    print()

    header = f"  {'カテゴリ':20s} | {'Precision':>9s} | {'Recall':>6s} | {'F1':>5s} | {'TP':>4s} | {'FP':>4s} | {'FN':>4s}"
    separator = f"  {'-'*20}-+-{'-'*9}-+-{'-'*6}-+-{'-'*5}-+-{'-'*4}-+-{'-'*4}-+-{'-'*4}"
    print(header)
    print(separator)

    for cat in CATEGORIES:
        m = metrics["per_category"][cat]
        print(
            f"  {cat:20s} | {m['precision']:9.3f} | {m['recall']:6.3f} | {m['f1']:5.3f} "
            f"| {m['tp']:4d} | {m['fp']:4d} | {m['fn']:4d}"
        )

    print(separator)
    print(
        f"  {'Micro':20s} | {metrics['micro_precision']:9.3f} | {metrics['micro_recall']:6.3f} "
        f"| {metrics['micro_f1']:5.3f} | {metrics['total_tp']:4d} | {metrics['total_fp']:4d} "
        f"| {metrics['total_fn']:4d}"
    )

    print()
    print(f"  完全一致率: {metrics['exact_match_rate']*100:.1f}%")
    print(f"  Jaccard類似度: {metrics['jaccard_avg']:.3f}")

    # 混同行列
    print_confusion_matrix(metrics)

    # エラー分析
    analyze_errors(metrics)

    # 誤分類パターン分析
    analyze_error_patterns(metrics)


def print_comparison_table(all_metrics):
    """複数コンポーネントの比較表を出力する。

    Args:
        all_metrics: {component_key: (display_name, metrics)} の辞書
    """
    print(f"\n{'=' * 90}")
    print(f"  === コンポーネント比較表 ===")
    print(f"{'=' * 90}")

    # ヘッダー
    comp_names = [name for name, _ in all_metrics.values()]
    header = f"  {'カテゴリ':20s}"
    for name in comp_names:
        header += f" | {name:>16s}"
    print(header)

    separator = f"  {'-'*20}"
    for _ in comp_names:
        separator += f"-+-{'-'*16}"
    print(separator)

    # カテゴリ別F1
    for cat in CATEGORIES:
        row = f"  {cat:20s}"
        for _, metrics in all_metrics.values():
            f1 = metrics["per_category"][cat]["f1"]
            row += f" | {f1:16.3f}"
        print(row)

    print(separator)

    # Micro F1
    row = f"  {'Micro F1':20s}"
    for _, metrics in all_metrics.values():
        row += f" | {metrics['micro_f1']:16.3f}"
    print(row)

    # 完全一致率
    row = f"  {'完全一致率':20s}"
    for _, metrics in all_metrics.values():
        row += f" | {metrics['exact_match_rate']*100:15.1f}%"
    print(row)

    # Jaccard
    row = f"  {'Jaccard':20s}"
    for _, metrics in all_metrics.values():
        row += f" | {metrics['jaccard_avg']:16.3f}"
    print(row)


def dump_errors_csv(metrics, dump_path, component_name):
    """FP/FNのツイートをCSVに出力する。

    Args:
        metrics: evaluate関数の戻り値
        dump_path: 出力CSVパス
        component_name: コンポーネント名（ファイル名に付加）
    """
    rows = []

    for cat in CATEGORIES:
        for sample in metrics["fp_samples"].get(cat, []):
            rows.append({
                "url": sample["url"],
                "username": sample["username"],
                "text": sample["text"],
                "gold_categories": "|".join(sample["gold_categories"]),
                "predicted_categories": "|".join(sample["predicted_categories"]),
                "error_type": "FP",
                "error_category": cat,
            })
        for sample in metrics["fn_samples"].get(cat, []):
            rows.append({
                "url": sample["url"],
                "username": sample["username"],
                "text": sample["text"],
                "gold_categories": "|".join(sample["gold_categories"]),
                "predicted_categories": "|".join(sample["predicted_categories"]),
                "error_type": "FN",
                "error_category": cat,
            })

    # 重複排除（同じURL+error_type+error_categoryの組み合わせ）
    seen = set()
    unique_rows = []
    for row in rows:
        key = (row["url"], row["error_type"], row["error_category"])
        if key not in seen:
            seen.add(key)
            unique_rows.append(row)

    fieldnames = ["url", "username", "text", "gold_categories", "predicted_categories", "error_type", "error_category"]
    with open(dump_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(unique_rows)

    print(f"\n  エラーダンプ出力: {dump_path} ({len(unique_rows)}件)")

    # カテゴリ別FP/FNサマリ
    print(f"\n  --- FP/FN件数サマリ ({component_name}) ---")
    print(f"  {'カテゴリ':20s} | {'FP':>5s} | {'FN':>5s} | {'合計':>5s}")
    print(f"  {'-'*20}-+-{'-'*5}-+-{'-'*5}-+-{'-'*5}")
    total_fp = 0
    total_fn = 0
    for cat in CATEGORIES:
        fp_count = metrics["per_category"][cat]["fp"]
        fn_count = metrics["per_category"][cat]["fn"]
        total_fp += fp_count
        total_fn += fn_count
        if fp_count > 0 or fn_count > 0:
            print(f"  {cat:20s} | {fp_count:5d} | {fn_count:5d} | {fp_count + fn_count:5d}")
    print(f"  {'-'*20}-+-{'-'*5}-+-{'-'*5}-+-{'-'*5}")
    print(f"  {'合計':20s} | {total_fp:5d} | {total_fn:5d} | {total_fp + total_fn:5d}")


def main():
    parser = argparse.ArgumentParser(description="手動アノテーション vs KW/LLM/ML分類の精度測定")
    parser.add_argument("--gold", required=True, help="手動アノテーションJSONパス")
    parser.add_argument("--tweets", default="output/merged_all.json", help="全ツイートJSONパス")
    parser.add_argument(
        "--component",
        default="all",
        choices=["regex", "llm", "ml", "all"],
        help="評価対象コンポーネント (default: all)",
    )
    parser.add_argument("--dump", default=None, help="FP/FNエラーのCSV出力先パス")
    args = parser.parse_args()

    # Gold Standard読み込み
    with open(args.gold, "r", encoding="utf-8") as f:
        gold_data = json.load(f)

    annotations = gold_data if isinstance(gold_data, list) else gold_data.get("annotations", [])
    gold_dict = {}
    for ann in annotations:
        url = ann.get("url", "")
        human_cats = set(ann.get("human_categories", []))
        gold_dict[url] = human_cats

    print(f"Gold Standard: {len(gold_dict)}件のアノテーション")

    # ツイートデータ読み込み
    with open(args.tweets, "r", encoding="utf-8") as f:
        all_tweets = json.load(f)

    tweet_dict = {}
    for t in all_tweets:
        url = t.get("url", "")
        if url:
            tweet_dict[url] = t

    print(f"ツイートデータ: {len(tweet_dict)}件")

    # マッチ件数の確認
    matched = sum(1 for url in gold_dict if url in tweet_dict)
    print(f"突合可能: {matched}件 / {len(gold_dict)}件")

    if matched == 0:
        print("ERROR: 突合可能なツイートが0件です。URLが一致しているか確認してください。")
        raise SystemExit(1)

    # 評価対象コンポーネントの決定
    if args.component == "all":
        target_components = list(COMPONENTS.keys())
    else:
        target_components = [args.component]

    # 各コンポーネントの評価
    all_metrics = {}
    for comp_key in target_components:
        display_name, pred_field = COMPONENTS[comp_key]
        metrics = evaluate(gold_dict, tweet_dict, pred_field)
        all_metrics[comp_key] = (display_name, metrics)
        print_table(f"=== 手動ラベル vs {display_name} ===", metrics)

        # --dump が指定されている場合、CSVを出力
        if args.dump:
            if len(target_components) == 1:
                dump_path = args.dump
            else:
                # 複数コンポーネント時はファイル名にコンポーネント名を付加
                base = args.dump
                if base.endswith(".csv"):
                    base = base[:-4]
                dump_path = f"{base}_{comp_key}.csv"
            dump_errors_csv(metrics, dump_path, display_name)

    # allモード時は比較表を出力
    if args.component == "all" and len(all_metrics) > 1:
        print_comparison_table(all_metrics)


if __name__ == "__main__":
    main()
