#!/usr/bin/env python3
"""追加アノテーション後の一括再訓練パイプライン

手動アノテーション追加後に、ML分類器・few-shot事例・メタ分類器を
順番に再訓練し、最後に精度評価を実行する。

使い方:
    docker compose run --rm xstock python3 scripts/retrain_pipeline.py \
        --gold output/annotations.json \
        --tweets output/merged_all.json

    # few-shot改善をスキップ
    docker compose run --rm xstock python3 scripts/retrain_pipeline.py \
        --gold output/annotations.json \
        --skip-few-shot
"""

import argparse
import subprocess
import sys
import time

STEPS = [
    {
        "name": "ML分類器 再訓練",
        "cmd": ["python3", "scripts/train_classifier.py", "--gold", "{gold}", "--tweets", "{tweets}"],
    },
    {
        "name": "few-shot事例 改善",
        "cmd": ["python3", "scripts/improve_few_shots.py", "--gold", "{gold}", "--tweets", "{tweets}"],
        "skip_flag": "skip_few_shot",
    },
    {
        "name": "メタ分類器 再訓練",
        "cmd": ["python3", "scripts/train_meta.py", "--gold", "{gold}", "--tweets", "{tweets}"],
    },
    {
        "name": "精度評価",
        "cmd": ["python3", "scripts/measure_human_accuracy.py", "--gold", "{gold}", "--tweets", "{tweets}", "--component", "all"],
    },
]


def run_step(step: dict, gold: str, tweets: str) -> dict:
    """1ステップを実行して結果を返す。

    Args:
        step: ステップ定義辞書
        gold: アノテーションJSONパス
        tweets: ツイートJSONパス

    Returns:
        {"name": str, "success": bool, "elapsed": float, "returncode": int}
    """
    cmd = [
        arg.replace("{gold}", gold).replace("{tweets}", tweets)
        for arg in step["cmd"]
    ]

    name = step["name"]
    print(f"\n{'='*60}")
    print(f"[STEP] {name}")
    print(f"  CMD: {' '.join(cmd)}")
    print(f"{'='*60}")

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=False,
            text=True,
        )
        elapsed = time.time() - start
        success = result.returncode == 0

        if success:
            print(f"\n  [OK] {name} 完了 ({elapsed:.1f}秒)")
        else:
            print(f"\n  [FAIL] {name} 失敗 (exit={result.returncode}, {elapsed:.1f}秒)")

        return {
            "name": name,
            "success": success,
            "elapsed": elapsed,
            "returncode": result.returncode,
        }
    except FileNotFoundError as e:
        elapsed = time.time() - start
        print(f"\n  [FAIL] {name} コマンド実行不可: {e}")
        return {
            "name": name,
            "success": False,
            "elapsed": elapsed,
            "returncode": -1,
        }


def print_summary(results: list, total_elapsed: float):
    """全ステップのサマリーを出力する。

    Args:
        results: 各ステップの結果リスト
        total_elapsed: パイプライン全体の所要時間
    """
    print(f"\n{'='*60}")
    print("パイプライン サマリー")
    print(f"{'='*60}")

    success_count = 0
    fail_count = 0
    skip_count = 0

    for r in results:
        if r.get("skipped"):
            status = "SKIP"
            skip_count += 1
            print(f"  [{status}] {r['name']}")
        elif r["success"]:
            status = "OK"
            success_count += 1
            print(f"  [{status}]   {r['name']} ({r['elapsed']:.1f}秒)")
        else:
            status = "FAIL"
            fail_count += 1
            print(f"  [{status}] {r['name']} (exit={r['returncode']}, {r['elapsed']:.1f}秒)")

    print(f"\n  合計: {success_count}成功 / {fail_count}失敗 / {skip_count}スキップ")
    print(f"  所要時間: {total_elapsed:.1f}秒")

    if fail_count > 0:
        print("\n  [WARN] 一部ステップが失敗しました")


def main():
    parser = argparse.ArgumentParser(
        description="追加アノテーション後の一括再訓練パイプライン"
    )
    parser.add_argument("--gold", required=True, help="アノテーションJSONパス")
    parser.add_argument(
        "--tweets", default="output/merged_all.json",
        help="ツイートJSONパス (default: output/merged_all.json)"
    )
    parser.add_argument(
        "--skip-few-shot", action="store_true",
        help="few-shot事例改善をスキップ"
    )
    args = parser.parse_args()

    print(f"Gold: {args.gold}")
    print(f"Tweets: {args.tweets}")
    if args.skip_few_shot:
        print("few-shot改善: スキップ")

    results = []
    total_start = time.time()

    for step in STEPS:
        # スキップフラグの確認
        skip_flag = step.get("skip_flag")
        if skip_flag and getattr(args, skip_flag, False):
            print(f"\n  [SKIP] {step['name']} (--{skip_flag.replace('_', '-')} 指定)")
            results.append({"name": step["name"], "skipped": True})
            continue

        r = run_step(step, args.gold, args.tweets)
        results.append(r)

        # 精度評価以外のステップが失敗したら中断
        if not r["success"] and step["name"] != "精度評価":
            print(f"\n[ABORT] {step['name']} が失敗したためパイプラインを中断します",
                  file=sys.stderr)
            break

    total_elapsed = time.time() - total_start
    print_summary(results, total_elapsed)

    # 失敗があれば非ゼロ終了
    has_failure = any(
        not r.get("skipped") and not r.get("success")
        for r in results
    )
    sys.exit(1 if has_failure else 0)


if __name__ == "__main__":
    main()
