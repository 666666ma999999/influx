#!/usr/bin/env python3
"""X(Twitter)投稿URLのエンゲージメント指標を取得してJSONLに出力する。

出力スキーマ（1URL = 1行のJSONL・T0 スキーマ）:

  成功:
    {"url": "https://x.com/...", "status": "ok", "likes": 123, "views": 4567,
     "retweets": 8, "replies": 2, "bookmarks": 10, "scraped_at": "2026-06-11T12:34:56+09:00"}

  失敗:
    {"url": "https://x.com/...", "status": "deleted", "error_detail": "...",
     "scraped_at": "2026-06-11T12:34:56+09:00"}
    status は "deleted" | "protected" | "login_required" | "rate_limited" | "other" のいずれか

使い方（influxディレクトリで実行、またはDockerコンテナ内 cwd=/app）:
  python3 scripts/fetch_engagement.py --url https://x.com/jack/status/20 --out /tmp/out.jsonl
  python3 scripts/fetch_engagement.py --urls-file /tmp/urls.txt --out /tmp/out.jsonl
  python3 scripts/fetch_engagement.py --profile x_profiles/maaaki --urls-file /tmp/urls.txt --out /tmp/out.jsonl
  docker exec -e DISPLAY=:99 xstock-vnc python3 scripts/fetch_engagement.py --url <url> --out /tmp/out.jsonl

Cookie期限切れ時は exit code 3 で終了し、re-loginコマンドを stderr に出力する。

履歴: 2026-04-18 新設 → 2026-05-01 phase4 物理分離クリーンアップ(commit 6e7fb3b)で
誤って削除 → 2026-06-11 復元。移管後の import パス(tier3_posting.*)と、make_article
ラッパー fetch_engagement_via_influx.sh が渡す --profile 引数に追従。
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 2026-05-01 phase4 で extensions/tier3_posting/ は /app/tier3_posting/ へ物理分離済み。
from tier3_posting.impression_tracker.scraper import (  # noqa: E402
    CookieExpiredError,
    ImpressionScraper,
)

COOKIE_EXPIRED_MSG = """\
[ERROR] X Cookie期限切れを検出しました。ホスト Chrome から再抽出してください（VNC Playwright 経路は X bot 検知で廃止済み）:

  python3 scripts/import_chrome_cookies.py --chrome-profile "Profile 2" --account kabuki666999
  python3 scripts/import_chrome_cookies.py --chrome-profile "Default"   --account maaaki

詳細は refresh-x-cookies スキル参照。Chrome に X ログイン済みが前提。
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="X投稿URLのエンゲージメント指標を取得してJSONLに出力する"
    )
    url_group = parser.add_mutually_exclusive_group(required=True)
    url_group.add_argument("--url", metavar="URL", help="単一ツイートURL")
    url_group.add_argument(
        "--urls-file", metavar="PATH", help="URLリストファイル（1行1URL、#コメント行無視）"
    )
    parser.add_argument("--out", required=True, metavar="PATH", help="JSONL出力先")
    parser.add_argument(
        "--screenshot-dir",
        default="/app/output/posting",
        metavar="PATH",
        help="エラー時スクリーンショット保存先（デフォルト: /app/output/posting）",
    )
    # make_article ラッパーは --profile を渡す（cookies.json はこのディレクトリ直下）。
    # 旧 --profile-path は後方互換のエイリアスとして残す。
    parser.add_argument(
        "--profile",
        "--profile-path",
        dest="profile",
        default="./x_profile",
        metavar="PATH",
        help="ブラウザプロファイルパス（cookies.json の親・デフォルト: ./x_profile）",
    )
    return parser.parse_args()


def load_urls(args: argparse.Namespace):
    if args.url:
        return [args.url.strip()]

    urls_file = Path(args.urls_file)
    if not urls_file.exists():
        print(f"[ERROR] URLファイルが見つかりません: {urls_file}", file=sys.stderr)
        sys.exit(1)

    urls = []
    with open(urls_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)

    if not urls:
        print(f"[ERROR] URLファイルにURLが1件もありません: {urls_file}", file=sys.stderr)
        sys.exit(1)

    return urls


def main() -> None:
    args = parse_args()

    urls = load_urls(args)
    logger.info("処理対象: %d件のURL", len(urls))

    scraper = ImpressionScraper(
        profile_path=args.profile,
        screenshot_dir=args.screenshot_dir,
    )

    start = time.time()

    try:
        results = scraper.scrape_batch(urls, screenshot_dir=args.screenshot_dir)
    except CookieExpiredError:
        print(COOKIE_EXPIRED_MSG, file=sys.stderr)
        sys.exit(3)
    except Exception as exc:
        logger.exception("scrape_batch 実行中に予期しないエラー: %s", exc)
        sys.exit(2)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ok_count = 0
    fail_count = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            if r.get("status") == "ok":
                ok_count += 1
            else:
                fail_count += 1

    elapsed = time.time() - start
    print(
        f"DONE: total={len(results)}, ok={ok_count}, failed={fail_count}, elapsed={elapsed:.1f}s"
    )


if __name__ == "__main__":
    main()
