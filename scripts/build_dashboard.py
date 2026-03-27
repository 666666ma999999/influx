#!/usr/bin/env python3
"""パイプライン統合ダッシュボードのデータ収集・HTML埋め込みスクリプト。"""

import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EMBED_PATTERN = re.compile(r"const EMBEDDED_DATA = \{.*?\};", re.DOTALL)
POSTING_EMBED_PATTERN = re.compile(r"var POSTING_DATA = \[.*?\];", re.DOTALL)

JST = timezone(timedelta(hours=9))


def scan_date_directories(output_dir: str, days: int = 14) -> list:
    """output/YYYY-MM-DD/ ディレクトリをスキャンし、各日の収集・分類件数を返す。

    Args:
        output_dir: outputディレクトリパス
        days: 遡る日数

    Returns:
        [{date, collected, classified, has_tweets, has_classified}] のリスト（日付降順）
    """
    results = []
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    for name in sorted(os.listdir(output_dir), reverse=True):
        if not date_pattern.match(name):
            continue
        dir_path = os.path.join(output_dir, name)
        if not os.path.isdir(dir_path):
            continue

        tweets_path = os.path.join(dir_path, "tweets.json")
        classified_path = os.path.join(dir_path, "classified_llm.json")

        collected = 0
        classified = 0

        if os.path.exists(tweets_path):
            try:
                with open(tweets_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                collected = len(data) if isinstance(data, list) else 0
            except (json.JSONDecodeError, OSError):
                pass

        if os.path.exists(classified_path):
            try:
                with open(classified_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                classified = len(data) if isinstance(data, list) else 0
            except (json.JSONDecodeError, OSError):
                pass

        results.append({
            "date": name,
            "collected": collected,
            "classified": classified,
            "has_tweets": os.path.exists(tweets_path),
            "has_classified": os.path.exists(classified_path),
        })

        if len(results) >= days:
            break

    return results


def collect_posting_data(data_dir: str) -> dict:
    """PostStoreからドラフト・履歴・インプレッションデータを収集する。

    Args:
        data_dir: PostStoreのbase_dir

    Returns:
        {drafts, history, impressions, status_counts} の辞書
    """
    result = {
        "drafts": [],
        "history": [],
        "impressions": {},
        "status_counts": {
            "draft": 0, "approved": 0, "scheduled": 0,
            "posted": 0, "failed": 0, "rejected": 0, "archived": 0,
        },
    }

    try:
        from extensions.tier3_posting.x_poster.post_store import PostStore
        store = PostStore(base_dir=data_dir)
        result["drafts"] = store.load_drafts()
        result["history"] = store.load_history()
        result["impressions"] = store.get_latest_impressions()
    except (ImportError, Exception) as e:
        print(f"  PostStore読み込みスキップ: {e}")
        return result

    # ステータス集計
    for d in result["drafts"]:
        status = d.get("status", "draft")
        if status in result["status_counts"]:
            result["status_counts"][status] += 1

    # 履歴からposted（dry_run除外）をカウント
    posted_ids = set()
    failed_ids = set()
    for h in result["history"]:
        nid = h.get("news_id")
        if not nid:
            continue
        if h.get("status") == "posted" and not h.get("dry_run", False):
            posted_ids.add(nid)
        elif h.get("status") == "failed":
            failed_ids.add(nid)

    result["real_posted_count"] = len(posted_ids)
    result["failed_history_ids"] = failed_ids

    return result


def collect_btc_data(output_dir: str) -> dict:
    """BTC関連ファイルの存在・更新日時を確認する。

    Args:
        output_dir: outputディレクトリパス

    Returns:
        {data_updated_at, chart_exists, viewer_exists, image_count}
    """
    result = {
        "data_updated_at": None,
        "chart_exists": False,
        "viewer_exists": False,
        "image_count": 0,
    }

    data_json = os.path.join(output_dir, "btc_divergence_data.json")
    if os.path.exists(data_json):
        mtime = os.path.getmtime(data_json)
        result["data_updated_at"] = datetime.fromtimestamp(mtime, tz=JST).strftime("%Y-%m-%d %H:%M")

    result["chart_exists"] = os.path.exists(os.path.join(output_dir, "btc_divergence_chart.png"))
    result["viewer_exists"] = os.path.exists(os.path.join(output_dir, "btc_deviation_viewer.html"))

    # btc_deviation_*.png をカウント
    for fname in os.listdir(output_dir):
        if fname.startswith("btc_deviation_") and fname.endswith(".png"):
            result["image_count"] += 1

    return result


def encode_images_base64(drafts: list) -> list:
    """ドラフトの画像ファイルをBase64エンコードしてインライン化する。"""
    for draft in drafts:
        images = draft.get("images", [])
        images_base64 = []
        for img in images:
            img_path = img.get("path", "")
            if os.path.exists(img_path):
                try:
                    with open(img_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    ext = os.path.splitext(img_path)[1].lower()
                    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext.lstrip("."), "image/png")
                    images_base64.append({
                        "data": f"data:{mime};base64,{b64}",
                        "type": img.get("type", ""),
                        "description": img.get("description", ""),
                    })
                except Exception as e:
                    print(f"  画像エンコードエラー ({img_path}): {e}")
        if images_base64:
            draft["images_base64"] = images_base64
    return drafts


def build_posting_data(data_dir: str) -> list:
    """PostStoreからドラフト一覧を取得し、履歴・インプレッション・画像をマージして返す。

    Args:
        data_dir: PostStoreのbase_dir

    Returns:
        ドラフトのリスト（履歴・インプレッション・画像base64マージ済み）
    """
    try:
        from extensions.tier3_posting.x_poster.post_store import PostStore
        store = PostStore(base_dir=data_dir)
        drafts = store.load_drafts()
        history = store.load_history()

        # 履歴をnews_idでインデックス化してマージ
        history_map = {}
        for rec in history:
            nid = rec.get("news_id")
            if nid:
                history_map[nid] = rec

        for draft in drafts:
            nid = draft.get("news_id")
            if nid and nid in history_map:
                h = history_map[nid]
                if h.get("posted_url"):
                    draft["posted_url"] = h["posted_url"]
                if h.get("posted_at"):
                    draft["posted_at"] = h["posted_at"]
                if h.get("error"):
                    draft["error"] = h["error"]

        # インプレッションデータのマージ
        latest_impressions = store.get_latest_impressions()
        for draft in drafts:
            nid = draft.get("news_id")
            if nid and nid in latest_impressions:
                draft["impressions"] = latest_impressions[nid]

        # 画像データのBase64エンコード
        drafts = encode_images_base64(drafts)

        return drafts
    except (ImportError, Exception) as e:
        print(f"  PostStore読み込みスキップ (posting_data): {e}")
        return []


def build_overview(date_runs: list, posting: dict) -> dict:
    """KPIサマリーとヘルス判定を構築する。"""
    counts = {
        "collected": 0,
        "classified": 0,
        "drafts": posting["status_counts"].get("draft", 0),
        "approved": posting["status_counts"].get("approved", 0) + posting["status_counts"].get("scheduled", 0),
        "posted": posting.get("real_posted_count", 0),
        "tracked": len(posting.get("impressions", {})),
        "failed": posting["status_counts"].get("failed", 0),
    }

    latest_run_date = None
    if date_runs:
        latest_run_date = date_runs[0]["date"]
        counts["collected"] = date_runs[0]["collected"]
        counts["classified"] = date_runs[0]["classified"]

    # ヘルス判定
    health = "healthy"
    if latest_run_date:
        try:
            latest_dt = datetime.strptime(latest_run_date, "%Y-%m-%d")
            now = datetime.now()
            delta = (now - latest_dt).days
            if delta > 7:
                health = "critical"
            elif delta > 3 or counts["failed"] > 0:
                health = "warning"
        except ValueError:
            health = "warning"
    else:
        health = "critical"

    return {
        "latest_run_date": latest_run_date,
        "health": health,
        "counts": counts,
    }


def build_stages(date_runs: list, posting: dict, overview: dict) -> list:
    """6ステージの状態を構築する。"""
    c = overview["counts"]
    latest = date_runs[0] if date_runs else None

    def stage_status(has_data: bool, count: int) -> str:
        if count > 0:
            return "completed"
        if has_data:
            return "ready"
        return "pending"

    stages = [
        {
            "key": "collect",
            "label": "収集",
            "status": "completed" if latest and latest["has_tweets"] else "pending",
            "count": c["collected"],
            "command": "docker compose run xstock python scripts/collect_tweets.py",
        },
        {
            "key": "classify",
            "label": "分類",
            "status": "completed" if latest and latest["has_classified"] else ("ready" if c["collected"] > 0 else "pending"),
            "count": c["classified"],
            "command": "docker compose run xstock python scripts/classify_tweets.py",
        },
        {
            "key": "draft",
            "label": "ドラフト",
            "status": stage_status(c["classified"] > 0, c["drafts"]),
            "count": c["drafts"],
            "command": "",
        },
        {
            "key": "review",
            "label": "レビュー",
            "status": stage_status(c["drafts"] > 0, c["approved"]),
            "count": c["approved"],
            "command": "",
        },
        {
            "key": "post",
            "label": "投稿",
            "status": "completed" if c["posted"] > 0 else ("ready" if c["approved"] > 0 else "pending"),
            "count": c["posted"],
            "command": "",
        },
        {
            "key": "track",
            "label": "追跡",
            "status": "completed" if c["tracked"] > 0 else ("ready" if c["posted"] > 0 else "pending"),
            "count": c["tracked"],
            "command": "",
        },
    ]

    # エラー判定
    if c["failed"] > 0:
        stages[4]["status"] = "error"

    return stages


def build_lifecycle(posting: dict) -> dict:
    """ドラフトのステータス分布を返す。"""
    return posting["status_counts"]


def build_run_history(date_runs: list, posting: dict) -> list:
    """日次履歴テーブルデータを構築する。"""
    rows = []
    for run in date_runs:
        date_str = run["date"]

        # この日付のposting関連データは簡略化（全体カウントのみ）
        status = "healthy"
        if not run["has_tweets"]:
            status = "warning"

        rows.append({
            "date": date_str,
            "collected": run["collected"],
            "classified": run["classified"],
            "drafts": "-",
            "approved": "-",
            "posted": "-",
            "tracked": "-",
            "status": status,
        })

    return rows


def build_actions(overview: dict, stages: list, posting: dict, btc: dict) -> list:
    """推奨アクション（優先度順）を構築する。"""
    actions = []
    c = overview["counts"]

    # 1. 投稿エラーがある場合
    if c["failed"] > 0:
        actions.append({
            "priority": 1,
            "label": "投稿エラーを確認",
            "reason": f"failed {c['failed']}件 — review.htmlでエラー詳細を確認",
            "command": "",
        })

    # 2. ツイート収集（3日以上経過）
    health = overview["health"]
    if health in ("warning", "critical"):
        actions.append({
            "priority": 2,
            "label": "ツイート収集",
            "reason": "最新収集日が3日以上前",
            "command": "docker compose run xstock python scripts/collect_tweets.py",
        })

    # 3. LLM分類（未分類あり）
    if c["collected"] > 0 and c["classified"] == 0:
        actions.append({
            "priority": 3,
            "label": "LLM分類実行",
            "reason": f"収集済み{c['collected']}件が未分類",
            "command": "docker compose run xstock python scripts/classify_tweets.py",
        })

    # 4. 承認済みを投稿
    if c["approved"] > 0:
        actions.append({
            "priority": 5,
            "label": "投稿実行",
            "reason": f"承認済み{c['approved']}件が未投稿",
            "command": "",
        })

    # 5. インプレッション追跡
    if c["posted"] > 0 and c["tracked"] == 0:
        actions.append({
            "priority": 6,
            "label": "インプレッション追跡",
            "reason": f"投稿済み{c['posted']}件が未追跡",
            "command": "",
        })

    # 6. BTC乖離率更新
    if btc.get("data_updated_at"):
        try:
            btc_dt = datetime.strptime(btc["data_updated_at"], "%Y-%m-%d %H:%M")
            if (datetime.now() - btc_dt).days > 7:
                actions.append({
                    "priority": 7,
                    "label": "BTC乖離率データ更新",
                    "reason": f"最終更新: {btc['data_updated_at']}（7日以上前）",
                    "command": "python scripts/btc_excel_table_chart.py",
                })
        except ValueError:
            pass
    elif not btc.get("data_updated_at"):
        actions.append({
            "priority": 7,
            "label": "BTC乖離率データ作成",
            "reason": "btc_divergence_data.json が存在しない",
            "command": "python scripts/btc_excel_table_chart.py",
        })

    actions.sort(key=lambda a: a["priority"])
    return actions


def build_pages(output_dir: str) -> list:
    """既存ページリンク（存在確認付き）を返す。"""
    pages_def = [
        ("Dashboard", "dashboard.html"),
        ("Viewer", "viewer.html"),
        ("Review", "http://localhost:8080"),
        ("Performance", "performance_report.html"),
        ("Annotator", "annotator.html"),
        ("BTC Viewer", "btc_deviation_viewer.html"),
    ]
    pages = []
    for label, href in pages_def:
        full = os.path.join(output_dir, href)
        pages.append({
            "label": label,
            "href": href,
            "exists": os.path.exists(full),
        })
    return pages


def build_alerts(overview: dict, stages: list, posting: dict) -> list:
    """アラートを生成する。"""
    alerts = []
    c = overview["counts"]

    if overview["health"] == "critical":
        alerts.append({"level": "error", "text": "最新のツイート収集が7日以上前です"})
    elif overview["health"] == "warning" and c["failed"] == 0:
        alerts.append({"level": "warning", "text": "最新のツイート収集が3日以上前です"})

    if c["failed"] > 0:
        alerts.append({"level": "error", "text": f"投稿失敗が{c['failed']}件あります"})

    if c["approved"] > 0:
        alerts.append({"level": "warning", "text": f"承認済み{c['approved']}件が未投稿です"})

    # dry_runのみで実投稿なしの場合
    if posting.get("real_posted_count", 0) == 0 and len(posting.get("history", [])) > 0:
        alerts.append({"level": "info", "text": "実投稿はまだありません（dry_runのみ）"})

    if len(alerts) == 0:
        alerts.append({"level": "info", "text": "問題はありません"})

    return alerts


def assemble_embedded_data(
    overview: dict, stages: list, lifecycle: dict,
    run_history: list, actions: list, pages: list,
    btc: dict, alerts: list,
) -> dict:
    """全データを統合してEMBEDDED_DATA構造を返す。"""
    return {
        "generated_at": datetime.now(JST).isoformat(),
        "overview": overview,
        "stages": stages,
        "lifecycle": lifecycle,
        "run_history": run_history,
        "actions": actions,
        "pages": pages,
        "btc_divergence": btc,
        "alerts": alerts,
    }


def embed_into_html(html_path: str, data: dict, output_path: str = None,
                    posting_data: list = None) -> None:
    """EMBEDDED_DATA・POSTING_DATAを置換してHTMLに埋め込む。"""
    if not os.path.exists(html_path):
        print(f"ERROR: HTMLファイルが見つかりません: {html_path}")
        sys.exit(1)

    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    # EMBEDDED_DATA 置換
    data_json = json.dumps(data, ensure_ascii=False, indent=None)
    data_json = data_json.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

    match = EMBED_PATTERN.search(html)
    if not match:
        print("ERROR: EMBEDDED_DATA placeholder not found in HTML")
        sys.exit(1)

    new_snippet = f"const EMBEDDED_DATA = {data_json};"
    new_html = html[:match.start()] + new_snippet + html[match.end():]

    # POSTING_DATA 置換
    if posting_data is not None:
        posting_json = json.dumps(posting_data, ensure_ascii=False, indent=None)
        posting_json = posting_json.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        posting_match = POSTING_EMBED_PATTERN.search(new_html)
        if posting_match:
            posting_snippet = f"var POSTING_DATA = {posting_json};"
            new_html = new_html[:posting_match.start()] + posting_snippet + new_html[posting_match.end():]
            print(f"  POSTING_DATA: {len(posting_data)}件埋め込み")
        else:
            print("WARNING: POSTING_DATA placeholder not found in HTML (skipping)")

    out = output_path or html_path
    with open(out, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"埋め込み完了: {out}")


def main():
    parser = argparse.ArgumentParser(description="influx統合ダッシュボードをビルド")
    parser.add_argument(
        "--html", default="output/dashboard.html",
        help="テンプレートHTMLパス (default: output/dashboard.html)",
    )
    parser.add_argument(
        "--output", default=None,
        help="出力HTMLパス (デフォルト: --htmlと同じ)",
    )
    parser.add_argument(
        "--output-dir", default="output",
        help="outputディレクトリ (default: output)",
    )
    parser.add_argument(
        "--data-dir", default="output/posting",
        help="PostStoreのbase_dir (default: output/posting)",
    )
    args = parser.parse_args()

    print("=== influx Dashboard Builder ===")

    # 1. 日付ディレクトリスキャン
    print("1. Scanning date directories...")
    date_runs = scan_date_directories(args.output_dir)
    print(f"   Found {len(date_runs)} date directories")

    # 2. PostStoreデータ収集
    print("2. Collecting posting data...")
    posting = collect_posting_data(args.data_dir)
    print(f"   Drafts: {len(posting['drafts'])}, History: {len(posting['history'])}")

    # 3. BTCデータ収集
    print("3. Collecting BTC data...")
    btc = collect_btc_data(args.output_dir)
    print(f"   Images: {btc['image_count']}, Data: {'Yes' if btc['data_updated_at'] else 'No'}")

    # 4. 各セクション構築
    print("4. Building sections...")
    overview = build_overview(date_runs, posting)
    stages = build_stages(date_runs, posting, overview)
    lifecycle = build_lifecycle(posting)
    run_history = build_run_history(date_runs, posting)
    actions = build_actions(overview, stages, posting, btc)
    pages = build_pages(args.output_dir)
    alerts = build_alerts(overview, stages, posting)

    print(f"   Health: {overview['health']}")
    print(f"   Actions: {len(actions)}")
    print(f"   Alerts: {len(alerts)}")

    # 5. 投稿キューデータ構築
    print("5. Building posting queue data...")
    posting_queue = build_posting_data(args.data_dir)
    print(f"   Posting queue items: {len(posting_queue)}")

    # 6. データ統合 & HTML埋め込み
    print("6. Embedding into HTML...")
    data = assemble_embedded_data(
        overview, stages, lifecycle, run_history,
        actions, pages, btc, alerts,
    )
    embed_into_html(args.html, data, args.output, posting_data=posting_queue)

    print("=== Done ===")


if __name__ == "__main__":
    main()
