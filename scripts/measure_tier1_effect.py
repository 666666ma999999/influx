#!/usr/bin/env python3
"""
Tier 1 キーワード分類修正の効果測定スクリプト

既存のmerged_all.jsonデータ（旧KW分類 + LLM分類）を使い、
新しいキーワード分類ルールで再分類した結果とLLM分類の一致率を比較する。
"""

import json
import sys
import copy
from collections import defaultdict

sys.path.insert(0, '.')

from collector.classifier import TweetClassifier


# =============================================================================
# 定数
# =============================================================================
DATA_PATH = "output/merged_all.json"
CATEGORIES = [
    "recommended_assets",
    "purchased_assets",
    "ipo",
    "market_trend",
    "bullish_assets",
    "bearish_assets",
    "warning_signals",
]
MAX_SAMPLE = 20


# =============================================================================
# メトリクス計算
# =============================================================================
def compute_metrics(tweets, kw_field, llm_field="llm_categories"):
    """KW分類とLLM分類の一致度メトリクスを計算する。

    Args:
        tweets: ツイートのリスト
        kw_field: キーワード分類カテゴリのフィールド名
        llm_field: LLM分類カテゴリのフィールド名

    Returns:
        dict: 各種メトリクス
    """
    exact_match = 0
    jaccard_sum = 0.0
    n = len(tweets)

    # カテゴリ別カウント (LLMを正解とする)
    tp = defaultdict(int)  # True Positive
    fp = defaultdict(int)  # False Positive (KWにあるがLLMにない)
    fn = defaultdict(int)  # False Negative (LLMにあるがKWにない)

    # 偽陽性・偽陰性の詳細
    fp_tweets = defaultdict(list)
    fn_tweets = defaultdict(list)

    for tweet in tweets:
        kw_cats = set(tweet.get(kw_field, []))
        llm_cats = set(tweet.get(llm_field, []))

        # 完全一致
        if kw_cats == llm_cats:
            exact_match += 1

        # Jaccard類似度
        if not kw_cats and not llm_cats:
            jaccard_sum += 1.0
        elif not kw_cats or not llm_cats:
            jaccard_sum += 0.0
        else:
            intersection = kw_cats & llm_cats
            union = kw_cats | llm_cats
            jaccard_sum += len(intersection) / len(union)

        # カテゴリ別TP/FP/FN
        for cat in CATEGORIES:
            in_kw = cat in kw_cats
            in_llm = cat in llm_cats
            if in_kw and in_llm:
                tp[cat] += 1
            elif in_kw and not in_llm:
                fp[cat] += 1
                fp_tweets[cat].append(tweet)
            elif not in_kw and in_llm:
                fn[cat] += 1
                fn_tweets[cat].append(tweet)

    # カテゴリ別Precision / Recall / F1
    per_category = {}
    for cat in CATEGORIES:
        p = tp[cat] / (tp[cat] + fp[cat]) if (tp[cat] + fp[cat]) > 0 else 0.0
        r = tp[cat] / (tp[cat] + fn[cat]) if (tp[cat] + fn[cat]) > 0 else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
        per_category[cat] = {"precision": p, "recall": r, "f1": f1}

    # Micro-F1
    total_tp = sum(tp.values())
    total_fp = sum(fp.values())
    total_fn = sum(fn.values())
    micro_p = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0.0
    micro_r = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 0.0
    micro_f1 = 2 * micro_p * micro_r / (micro_p + micro_r) if (micro_p + micro_r) > 0 else 0.0

    return {
        "exact_match_rate": exact_match / n if n > 0 else 0.0,
        "jaccard_avg": jaccard_sum / n if n > 0 else 0.0,
        "micro_precision": micro_p,
        "micro_recall": micro_r,
        "micro_f1": micro_f1,
        "per_category": per_category,
        "fp_count": dict(fp),
        "fn_count": dict(fn),
        "tp_count": dict(tp),
        "fp_tweets": dict(fp_tweets),
        "fn_tweets": dict(fn_tweets),
    }


# =============================================================================
# レポート出力
# =============================================================================
def print_report(tweets, old_metrics, new_metrics, changed_tweets):
    """比較レポートを出力する。"""
    n = len(tweets)
    contrarian_count = sum(1 for t in tweets if t.get("is_contrarian"))

    print()
    print("=" * 70)
    print("     Tier 1 修正効果測定レポート")
    print("=" * 70)

    # --- 対象データ ---
    print()
    print("【対象データ】")
    print(f"  総ツイート数（merged_all.json）:  {n}件")
    print(f"  うち contrarian ツイート:          {contrarian_count}件")
    print()

    # --- KW-LLM一致率 ---
    print("【KW-LLM一致率】")
    print(f"  {'':30s} {'修正前':>10s} {'修正後':>10s} {'改善幅':>10s}")
    print(f"  {'-'*62}")

    em_old = old_metrics["exact_match_rate"]
    em_new = new_metrics["exact_match_rate"]
    print(f"  {'完全一致率':30s} {em_old*100:9.1f}% {em_new*100:9.1f}% {(em_new-em_old)*100:+9.1f}%")

    jc_old = old_metrics["jaccard_avg"]
    jc_new = new_metrics["jaccard_avg"]
    print(f"  {'Jaccard類似度':30s} {jc_old:10.3f} {jc_new:10.3f} {jc_new-jc_old:+10.3f}")

    mf_old = old_metrics["micro_f1"]
    mf_new = new_metrics["micro_f1"]
    print(f"  {'Micro-F1':30s} {mf_old:10.3f} {mf_new:10.3f} {mf_new-mf_old:+10.3f}")

    mp_old = old_metrics["micro_precision"]
    mp_new = new_metrics["micro_precision"]
    print(f"  {'Micro-Precision':30s} {mp_old:10.3f} {mp_new:10.3f} {mp_new-mp_old:+10.3f}")

    mr_old = old_metrics["micro_recall"]
    mr_new = new_metrics["micro_recall"]
    print(f"  {'Micro-Recall':30s} {mr_old:10.3f} {mr_new:10.3f} {mr_new-mr_old:+10.3f}")
    print()

    # --- カテゴリ別F1 ---
    print("【カテゴリ別F1（LLMを正解とした場合）】")
    print(f"  {'カテゴリ':28s} {'修正前F1':>10s} {'修正後F1':>10s} {'改善幅':>10s}")
    print(f"  {'-'*60}")
    for cat in CATEGORIES:
        f1_old = old_metrics["per_category"][cat]["f1"]
        f1_new = new_metrics["per_category"][cat]["f1"]
        diff = f1_new - f1_old
        marker = " ***" if abs(diff) >= 0.01 else ""
        print(f"  {cat:28s} {f1_old:10.3f} {f1_new:10.3f} {diff:+10.3f}{marker}")
    print()

    # --- カテゴリ別Precision ---
    print("【カテゴリ別Precision（LLMを正解とした場合）】")
    print(f"  {'カテゴリ':28s} {'修正前':>10s} {'修正後':>10s} {'改善幅':>10s}")
    print(f"  {'-'*60}")
    for cat in CATEGORIES:
        p_old = old_metrics["per_category"][cat]["precision"]
        p_new = new_metrics["per_category"][cat]["precision"]
        diff = p_new - p_old
        marker = " ***" if abs(diff) >= 0.01 else ""
        print(f"  {cat:28s} {p_old:10.3f} {p_new:10.3f} {diff:+10.3f}{marker}")
    print()

    # --- カテゴリ別Recall ---
    print("【カテゴリ別Recall（LLMを正解とした場合）】")
    print(f"  {'カテゴリ':28s} {'修正前':>10s} {'修正後':>10s} {'改善幅':>10s}")
    print(f"  {'-'*60}")
    for cat in CATEGORIES:
        r_old = old_metrics["per_category"][cat]["recall"]
        r_new = new_metrics["per_category"][cat]["recall"]
        diff = r_new - r_old
        marker = " ***" if abs(diff) >= 0.01 else ""
        print(f"  {cat:28s} {r_old:10.3f} {r_new:10.3f} {diff:+10.3f}{marker}")
    print()

    # --- 偽陽性(FP)の改善 ---
    print("【偽陽性(FP)の改善 — KWが検出したがLLMにない】")
    print(f"  {'カテゴリ':28s} {'修正前FP':>10s} {'修正後FP':>10s} {'削減数':>10s}  備考")
    print(f"  {'-'*80}")
    fp_notes = {
        "recommended_assets": '「金」→複合パターン化',
        "purchased_assets": '「イン」→投資文脈パターン化',
        "warning_signals": 'contrarian判定ロジック修正',
    }
    for cat in CATEGORIES:
        fp_old = old_metrics["fp_count"].get(cat, 0)
        fp_new = new_metrics["fp_count"].get(cat, 0)
        diff = fp_new - fp_old
        note = fp_notes.get(cat, "")
        print(f"  {cat:28s} {fp_old:8d}件 {fp_new:8d}件 {diff:+8d}件  {note}")
    total_fp_old = sum(old_metrics["fp_count"].values())
    total_fp_new = sum(new_metrics["fp_count"].values())
    print(f"  {'-'*80}")
    print(f"  {'合計':28s} {total_fp_old:8d}件 {total_fp_new:8d}件 {total_fp_new-total_fp_old:+8d}件")
    print()

    # --- 偽陰性(FN)の変化 ---
    print("【偽陰性(FN)の変化 — LLMが検出したがKWにない】")
    print(f"  {'カテゴリ':28s} {'修正前FN':>10s} {'修正後FN':>10s} {'変化':>10s}")
    print(f"  {'-'*60}")
    for cat in CATEGORIES:
        fn_old = old_metrics["fn_count"].get(cat, 0)
        fn_new = new_metrics["fn_count"].get(cat, 0)
        diff = fn_new - fn_old
        marker = " ***" if abs(diff) >= 1 else ""
        print(f"  {cat:28s} {fn_old:8d}件 {fn_new:8d}件 {diff:+8d}件{marker}")
    total_fn_old = sum(old_metrics["fn_count"].values())
    total_fn_new = sum(new_metrics["fn_count"].values())
    print(f"  {'-'*60}")
    print(f"  {'合計':28s} {total_fn_old:8d}件 {total_fn_new:8d}件 {total_fn_new-total_fn_old:+8d}件")
    print()

    # --- Contrarian分析 ---
    contrarian_tweets = [t for t in tweets if t.get("is_contrarian")]
    if contrarian_tweets:
        print("【Contrarian（逆指標）ツイート分析】")
        print(f"  対象: {len(contrarian_tweets)}件")
        old_ws = sum(1 for t in contrarian_tweets if "warning_signals" in t.get("categories", []))
        new_ws = sum(1 for t in contrarian_tweets if "warning_signals" in t.get("new_categories", []))
        llm_ws = sum(1 for t in contrarian_tweets if "warning_signals" in t.get("llm_categories", []))
        print(f"  warning_signals検出数（旧KW）: {old_ws}件")
        print(f"  warning_signals検出数（新KW）: {new_ws}件")
        print(f"  warning_signals検出数（LLM）:  {llm_ws}件")

        # 新ルールでwarning_signalsが除外されたケース
        removed_ws = [
            t for t in contrarian_tweets
            if "warning_signals" in t.get("categories", [])
            and "warning_signals" not in t.get("new_categories", [])
        ]
        if removed_ws:
            print(f"\n  旧KWでwarning_signalsだったが新KWで除外: {len(removed_ws)}件")
            for i, t in enumerate(removed_ws[:5]):
                text = t["text"][:60].replace("\n", " ")
                llm = t.get("llm_categories", [])
                print(f"    [{i+1}] {text}...")
                print(f"        旧KW: {t['categories']}")
                print(f"        新KW: {t['new_categories']}")
                print(f"        LLM:  {llm}")
        print()

    # --- 分類変更サンプル ---
    print(f"【修正による分類変更の詳細サンプル（最大{MAX_SAMPLE}件 / 変更{len(changed_tweets)}件中）】")
    if not changed_tweets:
        print("  分類変更なし")
    else:
        for i, t in enumerate(changed_tweets[:MAX_SAMPLE]):
            text = t["text"][:60].replace("\n", " ")
            old_cats = set(t["categories"])
            new_cats = set(t["new_categories"])
            llm_cats = set(t.get("llm_categories", []))

            # 改善/悪化判定
            old_jaccard = _jaccard(old_cats, llm_cats)
            new_jaccard = _jaccard(new_cats, llm_cats)
            if new_jaccard > old_jaccard:
                verdict = "改善"
            elif new_jaccard < old_jaccard:
                verdict = "悪化"
            else:
                verdict = "変化なし(Jaccard同値)"

            added = new_cats - old_cats
            removed = old_cats - new_cats

            print(f"\n  [{i+1}] @{t['username']}: {text}...")
            print(f"      旧KW: {sorted(old_cats) if old_cats else '(なし)'}")
            print(f"      新KW: {sorted(new_cats) if new_cats else '(なし)'}")
            print(f"      LLM:  {sorted(llm_cats) if llm_cats else '(なし)'}")
            if added:
                print(f"      追加: {sorted(added)}")
            if removed:
                print(f"      削除: {sorted(removed)}")
            print(f"      判定: {verdict}")
    print()

    # --- サマリー ---
    improvements = sum(1 for t in changed_tweets if _jaccard(set(t["new_categories"]), set(t.get("llm_categories", []))) > _jaccard(set(t["categories"]), set(t.get("llm_categories", []))))
    degradations = sum(1 for t in changed_tweets if _jaccard(set(t["new_categories"]), set(t.get("llm_categories", []))) < _jaccard(set(t["categories"]), set(t.get("llm_categories", []))))
    neutral = len(changed_tweets) - improvements - degradations

    print("=" * 70)
    print("     総合サマリー")
    print("=" * 70)
    print(f"  分類が変わったツイート: {len(changed_tweets)}件 / {n}件")
    print(f"    改善:    {improvements}件")
    print(f"    悪化:    {degradations}件")
    print(f"    中立:    {neutral}件")
    print(f"  Micro-F1変化: {mf_old:.3f} -> {mf_new:.3f} ({mf_new-mf_old:+.3f})")
    print(f"  完全一致率変化: {em_old*100:.1f}% -> {em_new*100:.1f}% ({(em_new-em_old)*100:+.1f}%)")
    print("=" * 70)


def _jaccard(set_a, set_b):
    """2つの集合のJaccard類似度を計算する。"""
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


# =============================================================================
# メイン処理
# =============================================================================
def main():
    # 1. データ読み込み
    print("データ読み込み中...")
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        all_tweets = json.load(f)

    # KW+LLM両方あるツイートのみ
    tweets = [
        t for t in all_tweets
        if "categories" in t and "llm_categories" in t
    ]
    print(f"  merged_all.json: {len(all_tweets)}件")
    print(f"  KW+LLM分類済み: {len(tweets)}件")

    # 2. 旧KW分類でのメトリクス計算
    print("\n旧キーワードルールでのメトリクス計算中...")
    old_metrics = compute_metrics(tweets, kw_field="categories")

    # 3. 新KWルールで再分類
    print("新キーワードルールで再分類中...")
    classifier = TweetClassifier()

    for tweet in tweets:
        # deep copyして分類（classifyはtweet自体を変更するため）
        temp = copy.deepcopy(tweet)
        result = classifier.classify(temp)
        tweet["new_categories"] = result["categories"]
        tweet["new_category_details"] = result.get("category_details", {})

    # 4. 新KW分類でのメトリクス計算
    print("新キーワードルールでのメトリクス計算中...")
    new_metrics = compute_metrics(tweets, kw_field="new_categories")

    # 5. 分類が変わったツイートを特定
    changed_tweets = [
        t for t in tweets
        if set(t["categories"]) != set(t["new_categories"])
    ]

    # 6. レポート出力
    print_report(tweets, old_metrics, new_metrics, changed_tweets)


if __name__ == "__main__":
    main()
