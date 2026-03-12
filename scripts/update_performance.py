#!/usr/bin/env python3
"""インフルエンサー推奨パフォーマンス管理スクリプト

推奨レコードの一括登録、価格スナップショット取得、スコアカード計算、HTMLレポート生成を行う。

Usage:
    # 全ツイート再スキャンして未登録推奨を一括登録
    python scripts/update_performance.py --mode register

    # 現在価格更新 + スナップショット + スコアカード再計算
    python scripts/update_performance.py --mode snapshot

    # register + snapshot（デフォルト）
    python scripts/update_performance.py --mode full

    # HTMLレポート生成
    python scripts/update_performance.py --mode report
"""

import argparse
import json
import os
import sys
from datetime import datetime

# プロジェクトルートをパスに追加
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector.ticker_extractor import TickerExtractor
from collector.price_fetcher import PriceFetcher
from extensions.tier2_classification.performance_tracker.store import RecommendationStore
from extensions.tier2_classification.performance_tracker.scorecard import ScorecardBuilder
from scripts.measure_performance import load_tweets, filter_recommendation_tweets


DEFAULT_OUTPUT_DIR = "output/performance"
DEFAULT_PRICE_CACHE = "output/price_cache.json"
DEFAULT_INPUT_PATTERNS = [
    "output/*/tweets.json",
    "output/*/classified_llm.json",
    "output/merged_all.json",
]


def mode_register(store, input_patterns, extractor, fetcher):
    """全ツイートをスキャンして未登録の推奨レコードを一括登録する。

    Args:
        store: RecommendationStore インスタンス
        input_patterns: 入力ファイルパターンリスト
        extractor: TickerExtractor インスタンス
        fetcher: PriceFetcher インスタンス

    Returns:
        新規登録件数
    """
    print("ツイートデータを読み込み中...")
    tweets = load_tweets(input_patterns)
    print(f"  読み込み完了: {len(tweets)}件")

    if not tweets:
        print("ツイートが見つかりません。")
        return 0

    rec_tweets = filter_recommendation_tweets(tweets)
    print(f"  推奨・購入ツイート: {len(rec_tweets)}件")

    if not rec_tweets:
        print("推奨・購入カテゴリのツイートが見つかりません。")
        return 0

    registered = 0
    skipped = 0
    no_ticker = 0

    for tweet in rec_tweets:
        tickers = extractor.extract(tweet)
        if not tickers:
            no_ticker += 1
            continue

        # 投稿日取得
        posted_at = tweet.get("posted_at", "")
        rec_date = _parse_date(posted_at)

        # カテゴリ
        categories = tweet.get("llm_categories", tweet.get("categories", []))
        target_cats = [c for c in categories if c in ("recommended_assets", "purchased_assets")]

        for ticker_info in tickers:
            ticker = ticker_info["ticker"]
            tweet_url = tweet.get("url", "")
            if not tweet_url:
                continue

            rec_id = store.get_rec_id(tweet_url, ticker)

            # 推奨日の価格取得
            price_at_rec = None
            if rec_date:
                price_data = fetcher.get_price_at_date(ticker, rec_date)
                price_at_rec = price_data.get("close")

            rec = {
                "rec_id": rec_id,
                "tweet_url": tweet_url,
                "ticker": ticker,
                "matched_text": ticker_info.get("matched_text", ""),
                "extraction_source": ticker_info.get("source", ""),
                "influencer": tweet.get("username", ""),
                "display_name": tweet.get("display_name", ""),
                "categories": target_cats,
                "is_contrarian": tweet.get("is_contrarian", False),
                "recommended_at": rec_date,
                "price_at_recommendation": price_at_rec,
                "registered_at": datetime.now().isoformat(),
            }

            if store.add_if_new(rec):
                registered += 1
            else:
                skipped += 1

    print(f"\n登録結果:")
    print(f"  新規登録: {registered}件")
    print(f"  既存スキップ: {skipped}件")
    print(f"  ティッカー特定不可: {no_ticker}件")
    print(f"  ストア合計: {store.count()}件")
    return registered


def mode_snapshot(store, fetcher, output_dir):
    """現在価格を取得してスナップショットとスコアカードを更新する。

    Args:
        store: RecommendationStore インスタンス
        fetcher: PriceFetcher インスタンス
        output_dir: 出力ディレクトリ

    Returns:
        スコアカード辞書
    """
    recs = store.load_all()
    if not recs:
        print("推奨レコードがありません。先に --mode register を実行してください。")
        return None

    print(f"推奨レコード: {len(recs)}件")

    # 全ティッカーの現在価格を取得
    tickers = sorted(set(r.get("ticker") for r in recs if r.get("ticker")))
    print(f"ティッカー数: {len(tickers)}件")
    print("現在価格を取得中...")

    current_prices = {}
    for i, ticker in enumerate(tickers, 1):
        price_data = fetcher.get_current_price(ticker)
        current_prices[ticker] = price_data
        status = f"${price_data['close']:.2f}" if price_data.get("close") else price_data.get("error", "N/A")
        print(f"  [{i}/{len(tickers)}] {ticker}: {status}")

    # スコアカード構築
    print("\nスコアカード構築中...")
    builder = ScorecardBuilder()
    scorecards = builder.build(recs, current_prices)

    # 保存
    scorecard_path = os.path.join(output_dir, "scorecards.json")
    builder.save(scorecards, scorecard_path)
    print(f"スコアカード保存: {scorecard_path}")

    # スナップショット保存
    snapshot_dir = os.path.join(output_dir, "snapshots")
    snapshot_path = builder.save_snapshot(recs, current_prices, snapshot_dir)
    print(f"スナップショット保存: {snapshot_path}")

    # サマリー表示
    gs = scorecards.get("global_summary", {})
    print(f"\n【グローバルサマリー】")
    print(f"  追跡可能: {gs.get('trackable', 0)}件")
    print(f"  勝率: {gs.get('win_rate', 0)}%")
    print(f"  平均リターン: {gs.get('avg_return_pct', 0):+.2f}%")

    cs = scorecards.get("contrarian_summary", {})
    if cs.get("total", 0) > 0:
        print(f"  逆指標（逆張りリターン）: {cs.get('reverse_return_pct', 0):+.2f}% ({cs['total']}件)")

    return scorecards


def mode_report(output_dir):
    """スコアカードからHTMLレポートを生成する。

    Args:
        output_dir: 出力ディレクトリ
    """
    scorecard_path = os.path.join(output_dir, "scorecards.json")
    if not os.path.exists(scorecard_path):
        print(f"スコアカードが見つかりません: {scorecard_path}")
        print("先に --mode snapshot を実行してください。")
        return

    with open(scorecard_path, "r", encoding="utf-8") as f:
        scorecards = json.load(f)

    # measure_performance.py のHTML生成を流用して拡張
    html_path = os.path.join(output_dir, "performance_report.html")
    _generate_scorecard_html(scorecards, html_path)
    print(f"HTMLレポート生成: {html_path}")


def _generate_scorecard_html(scorecards, output_path):
    """スコアカードベースのHTMLレポートを生成する。

    Args:
        scorecards: スコアカード辞書
        output_path: 出力HTMLファイルパス
    """
    embedded_data = json.dumps(scorecards, ensure_ascii=False, indent=2)

    html = f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>インフルエンサー勝率コーナー</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: #0a0a0a; color: #e7e9ea;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.5; padding: 20px;
}}
a {{ color: #1d9bf0; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.container {{ max-width: 1100px; margin: 0 auto; }}
h1 {{ font-size: 24px; margin-bottom: 8px; }}
.subtitle {{ color: #71767b; font-size: 14px; margin-bottom: 24px; }}
.dashboard {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 32px; }}
.card {{
  background: #16181c; border: 1px solid #2f3336; border-radius: 12px;
  padding: 20px; text-align: center;
}}
.card-value {{ font-size: 32px; font-weight: 700; }}
.card-label {{ font-size: 13px; color: #71767b; margin-top: 4px; }}
.positive {{ color: #00ba7c; }}
.negative {{ color: #f4212e; }}
.neutral {{ color: #ffd700; }}
.tabs {{ display: flex; gap: 0; margin-bottom: 24px; border-bottom: 1px solid #2f3336; }}
.tab {{
  padding: 12px 20px; cursor: pointer; font-size: 15px; font-weight: 500;
  color: #71767b; border: none; background: none; position: relative;
}}
.tab:hover {{ background: rgba(231,233,234,0.06); }}
.tab.active {{ color: #e7e9ea; font-weight: 700; }}
.tab.active::after {{
  content: ''; position: absolute; bottom: -1px; left: 50%; transform: translateX(-50%);
  width: 56px; height: 4px; background: #1d9bf0; border-radius: 2px;
}}
.tab-content {{ display: none; }}
.tab-content.active {{ display: block; }}
.period-tabs {{ display: flex; gap: 8px; margin-bottom: 16px; }}
.period-tab {{
  padding: 6px 16px; border-radius: 20px; cursor: pointer; font-size: 13px;
  background: #16181c; border: 1px solid #2f3336; color: #71767b;
}}
.period-tab.active {{ background: #1d9bf0; color: #fff; border-color: #1d9bf0; }}
.inf-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }}
.inf-card {{
  background: #16181c; border: 1px solid #2f3336; border-radius: 8px;
  padding: 16px;
}}
.inf-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
.inf-name {{ font-weight: 700; font-size: 15px; }}
.inf-winrate {{ font-size: 24px; font-weight: 700; }}
.inf-stats {{ display: flex; gap: 16px; font-size: 13px; color: #71767b; }}
.inf-streak {{ font-size: 12px; margin-top: 4px; }}
.picks-list {{ margin-top: 8px; font-size: 13px; }}
.pick-item {{ display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #2f3336; }}
.pick-ticker {{ font-weight: 700; color: #1d9bf0; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th {{ background: #16181c; padding: 10px 12px; text-align: left; font-weight: 600; border-bottom: 2px solid #2f3336; }}
td {{ padding: 10px 12px; border-bottom: 1px solid #2f3336; }}
tr:hover {{ background: rgba(231,233,234,0.03); }}
.win {{ color: #00ba7c; font-weight: 600; }}
.lose {{ color: #f4212e; font-weight: 600; }}
</style>
</head>
<body>
<div class="container">
  <h1>インフルエンサー勝率コーナー</h1>
  <div class="subtitle" id="subtitle"></div>
  <div class="dashboard" id="dashboard"></div>
  <div class="tabs" id="main-tabs">
    <button class="tab active" onclick="showTab('influencers')">インフルエンサー別</button>
    <button class="tab" onclick="showTab('picks')">全銘柄一覧</button>
    <button class="tab" onclick="showTab('contrarian')">逆指標</button>
  </div>
  <div id="tab-influencers" class="tab-content active"></div>
  <div id="tab-picks" class="tab-content"></div>
  <div id="tab-contrarian" class="tab-content"></div>
</div>
<script>
const DATA = {embedded_data};
let currentPeriod = 'all';

function showTab(name) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('#main-tabs .tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
}}

function setPeriod(p) {{
  currentPeriod = p;
  document.querySelectorAll('.period-tab').forEach(el => {{
    el.classList.toggle('active', el.dataset.period === p);
  }});
  renderInfluencers();
}}

function fmt(v) {{
  if (v === null || v === undefined) return 'N/A';
  return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
}}
function cls(v) {{
  if (v === null || v === undefined) return '';
  return v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral';
}}

function render() {{
  const gs = DATA.global_summary || {{}};
  document.getElementById('subtitle').textContent =
    `生成: ${{DATA.generated_at?.slice(0,10) || 'N/A'}} | 推奨: ${{DATA.total_recommendations || 0}}件 | 追跡可能: ${{DATA.total_trackable || 0}}件`;

  document.getElementById('dashboard').innerHTML = `
    <div class="card"><div class="card-value ${{cls(gs.win_rate - 50)}}">${{gs.win_rate || 0}}%</div><div class="card-label">勝率 (${{gs.winners||0}}勝/${{gs.losers||0}}敗)</div></div>
    <div class="card"><div class="card-value ${{cls(gs.avg_return_pct)}}">${{fmt(gs.avg_return_pct)}}</div><div class="card-label">平均リターン</div></div>
    <div class="card"><div class="card-value ${{cls(gs.total_return_pct)}}">${{fmt(gs.total_return_pct)}}</div><div class="card-label">トータルリターン</div></div>
    <div class="card"><div class="card-value">${{gs.trackable || 0}} / ${{DATA.total_recommendations || 0}}</div><div class="card-label">追跡可能 / 全推奨</div></div>
  `;

  renderInfluencers();
  renderPicks();
  renderContrarian();
}}

function renderInfluencers() {{
  const infs = DATA.influencers || {{}};
  let html = '<div class="period-tabs">';
  ['7d','30d','all'].forEach(p => {{
    const label = p === '7d' ? '1W' : p === '30d' ? '1M' : 'ALL';
    html += `<span class="period-tab ${{currentPeriod===p?'active':''}}" data-period="${{p}}" onclick="setPeriod('${{p}}')">${{label}}</span>`;
  }});
  html += '</div><div class="inf-grid">';

  const entries = Object.entries(infs).sort((a,b) => {{
    const sa = a[1].by_period?.[currentPeriod] || {{}};
    const sb = b[1].by_period?.[currentPeriod] || {{}};
    return (sb.avg_return_pct||0) - (sa.avg_return_pct||0);
  }});

  for (const [name, inf] of entries) {{
    const stats = inf.by_period?.[currentPeriod] || {{}};
    const wr = stats.win_rate || 0;
    const wrCls = wr >= 50 ? 'positive' : wr > 0 ? 'negative' : '';
    const streak = inf.streak || {{}};
    const streakIcon = streak.type === 'win' ? '&#x1F525;' : streak.type === 'lose' ? '&#x1F4A7;' : '';
    const streakText = streak.current > 0 ? `${{streak.current}}連${{streak.type==='win'?'勝':'敗'}}` : '';

    html += `<div class="inf-card">
      <div class="inf-header">
        <div>
          <div class="inf-name">${{inf.is_contrarian ? '[逆指標] ' : ''}}@${{name}}</div>
          <div style="color:#71767b;font-size:13px">${{inf.display_name || ''}}</div>
        </div>
        <div class="inf-winrate ${{wrCls}}">${{wr}}%</div>
      </div>
      <div class="inf-stats">
        <span>${{stats.trackable||0}}推奨</span>
        <span class="${{cls(stats.avg_return_pct)}}">平均 ${{fmt(stats.avg_return_pct)}}</span>
        <span class="inf-streak">${{streakText}}</span>
      </div>`;

    // Top 3 picks
    const picks = (inf.picks || []).slice(0, 3);
    if (picks.length > 0) {{
      html += '<div class="picks-list">';
      for (const p of picks) {{
        html += `<div class="pick-item"><span class="pick-ticker">$${{p.ticker}}</span><span class="${{p.is_winner?'win':'lose'}}">${{fmt(p.return_pct)}}</span></div>`;
      }}
      html += '</div>';
    }}
    html += '</div>';
  }}
  html += '</div>';
  document.getElementById('tab-influencers').innerHTML = html;
}}

function renderPicks() {{
  const infs = DATA.influencers || {{}};
  let allPicks = [];
  for (const [name, inf] of Object.entries(infs)) {{
    for (const p of (inf.picks || [])) {{
      allPicks.push({{...p, influencer: name}});
    }}
  }}
  allPicks.sort((a,b) => (b.return_pct||0) - (a.return_pct||0));

  let html = '<table><thead><tr><th>ティッカー</th><th>銘柄</th><th>リターン</th><th>結果</th><th>推奨者</th><th>推奨日</th></tr></thead><tbody>';
  for (const p of allPicks) {{
    const retCls = p.is_winner ? 'win' : 'lose';
    html += `<tr>
      <td style="font-weight:700;color:#1d9bf0">$${{p.ticker}}</td>
      <td>${{p.matched_text || ''}}</td>
      <td class="${{retCls}}">${{fmt(p.return_pct)}}</td>
      <td class="${{retCls}}">${{p.is_winner ? 'WIN' : 'LOSE'}}</td>
      <td>@${{p.influencer}}</td>
      <td>${{p.recommended_at || 'N/A'}}</td>
    </tr>`;
  }}
  html += '</tbody></table>';
  document.getElementById('tab-picks').innerHTML = html;
}}

function renderContrarian() {{
  const cs = DATA.contrarian_summary || {{}};
  let html = '<div style="margin-bottom:16px">';
  if (cs.total === 0) {{
    html += '<p style="color:#71767b">逆指標データがありません。</p>';
  }} else {{
    html += `<div class="dashboard" style="margin-bottom:16px">
      <div class="card"><div class="card-value">${{cs.total}}</div><div class="card-label">逆指標推奨数</div></div>
      <div class="card"><div class="card-value ${{cls(cs.reverse_win_rate - 50)}}">${{cs.reverse_win_rate}}%</div><div class="card-label">逆張り勝率</div></div>
      <div class="card"><div class="card-value ${{cls(cs.reverse_return_pct)}}">${{fmt(cs.reverse_return_pct)}}</div><div class="card-label">逆張り平均リターン</div></div>
    </div>`;
  }}
  html += '</div>';
  document.getElementById('tab-contrarian').innerHTML = html;
}}

render();
</script>
</body>
</html>'''

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


def _parse_date(posted_at):
    """投稿日をYYYY-MM-DD形式に変換する。"""
    if not posted_at:
        return None
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        if len(posted_at) >= 10:
            return posted_at[:10]
        return None


def main():
    parser = argparse.ArgumentParser(description="インフルエンサー推奨パフォーマンス管理")
    parser.add_argument(
        "--mode", "-m",
        choices=["register", "snapshot", "report", "full"],
        default="full",
        help="実行モード (default: full)",
    )
    parser.add_argument(
        "--input", "-i", nargs="+",
        default=DEFAULT_INPUT_PATTERNS,
        help="入力ファイルパス（glob対応）",
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=DEFAULT_OUTPUT_DIR,
        help="出力ディレクトリ",
    )
    parser.add_argument(
        "--price-cache",
        default=DEFAULT_PRICE_CACHE,
        help="価格キャッシュファイルパス",
    )
    args = parser.parse_args()

    store = RecommendationStore(base_dir=args.output_dir)
    extractor = TickerExtractor()
    fetcher = PriceFetcher(cache_file=args.price_cache)

    if args.mode in ("register", "full"):
        print("=" * 60)
        print("  推奨レコード登録")
        print("=" * 60)
        mode_register(store, args.input, extractor, fetcher)

    if args.mode in ("snapshot", "full"):
        print()
        print("=" * 60)
        print("  価格スナップショット & スコアカード")
        print("=" * 60)
        mode_snapshot(store, fetcher, args.output_dir)

    if args.mode in ("report", "full"):
        print()
        print("=" * 60)
        print("  HTMLレポート生成")
        print("=" * 60)
        mode_report(args.output_dir)

    print("\n完了!")


if __name__ == "__main__":
    main()
