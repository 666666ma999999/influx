#!/usr/bin/env python3
"""インフルエンサー推奨銘柄パフォーマンス測定スクリプト

ツイートデータから銘柄を抽出し、推奨/購入時点からのリターンを算出する。
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime
from typing import List, Dict, Any

# プロジェクトルートをパスに追加
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector.ticker_extractor import TickerExtractor
from collector.price_fetcher import PriceFetcher


def load_tweets(input_patterns: List[str]) -> List[Dict[str, Any]]:
    """入力ファイルからツイートを読み込む。

    Args:
        input_patterns: 入力ファイルパスのリスト（glob対応）

    Returns:
        ツイートデータのリスト（重複除去済み）
    """
    tweets = []
    seen_urls = set()

    for pattern in input_patterns:
        files = glob.glob(pattern)
        for filepath in sorted(files):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    for tweet in data:
                        url = tweet.get("url", "")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            tweets.append(tweet)
            except (json.JSONDecodeError, OSError) as e:
                print(f"警告: {filepath} の読み込みに失敗: {e}")

    return tweets


def filter_recommendation_tweets(tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """推奨・購入カテゴリのツイートをフィルタする。

    Args:
        tweets: 全ツイートリスト

    Returns:
        recommended_assets or purchased_assets に分類されたツイート
    """
    target_categories = {"recommended_assets", "purchased_assets"}
    result = []

    for tweet in tweets:
        # LLM分類を優先、なければキーワード分類
        categories = set(tweet.get("llm_categories", []))
        if not categories:
            categories = set(tweet.get("categories", []))

        if categories & target_categories:
            result.append(tweet)

    return result


def build_recommendations(
    tweets: List[Dict[str, Any]],
    extractor: TickerExtractor,
    fetcher: PriceFetcher,
) -> tuple:
    """推奨データを構築する。

    Args:
        tweets: フィルタ済みツイートリスト
        extractor: TickerExtractorインスタンス
        fetcher: PriceFetcherインスタンス

    Returns:
        (recommendations, untrackable_tweets) のタプル
    """
    recommendations = []
    untrackable = []

    for tweet in tweets:
        tickers = extractor.extract(tweet)

        if not tickers:
            untrackable.append({
                "text": tweet.get("text", "")[:200],
                "username": tweet.get("username", ""),
                "url": tweet.get("url", ""),
                "categories": tweet.get("llm_categories", tweet.get("categories", [])),
                "reason": "ティッカー特定不可",
            })
            continue

        # 投稿日の取得
        posted_at = tweet.get("posted_at", "")
        if posted_at:
            try:
                rec_date = datetime.fromisoformat(posted_at.replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except (ValueError, AttributeError):
                rec_date = posted_at[:10] if len(posted_at) >= 10 else None
        else:
            rec_date = None

        for ticker_info in tickers:
            ticker = ticker_info["ticker"]

            # 価格取得
            price_at_rec = None
            current_price = None
            return_pct = None
            is_winner = None

            if rec_date:
                rec_price_data = fetcher.get_price_at_date(ticker, rec_date)
                price_at_rec = rec_price_data.get("close")

            current_data = fetcher.get_current_price(ticker)
            current_price = current_data.get("close")

            # リターン算出
            if price_at_rec and current_price:
                return_pct = round((current_price - price_at_rec) / price_at_rec * 100, 2)
                is_winner = return_pct > 0

            recommendations.append({
                "ticker": ticker,
                "matched_text": ticker_info.get("matched_text", ""),
                "influencer": tweet.get("username", ""),
                "display_name": tweet.get("display_name", ""),
                "tweet_text": tweet.get("text", "")[:200],
                "tweet_url": tweet.get("url", ""),
                "category": ticker_info.get("context", "不明"),
                "recommended_at": rec_date,
                "price_at_recommendation": price_at_rec,
                "current_price": current_price,
                "price_date": current_data.get("date"),
                "return_pct": return_pct,
                "is_winner": is_winner,
                "is_contrarian": tweet.get("is_contrarian", False),
            })

    return recommendations, untrackable


def build_summary(
    recommendations: List[Dict[str, Any]],
    untrackable: List[Dict[str, Any]],
    total_tweets: int,
    rec_tweets: int,
) -> Dict[str, Any]:
    """サマリーを構築する。

    Args:
        recommendations: 推奨データリスト
        untrackable: 追跡不能ツイートリスト
        total_tweets: 全ツイート数
        rec_tweets: 推奨・購入カテゴリのツイート数

    Returns:
        サマリー辞書
    """
    # 価格が取得できたもののみ
    trackable = [r for r in recommendations if r["return_pct"] is not None]
    winners = [r for r in trackable if r["is_winner"]]
    losers = [r for r in trackable if not r["is_winner"]]

    # 期間算出
    dates = [r["recommended_at"] for r in recommendations if r["recommended_at"]]
    date_from = min(dates) if dates else "N/A"
    date_to = max(dates) if dates else "N/A"

    # 勝率・リターン
    win_rate = round(len(winners) / len(trackable) * 100, 1) if trackable else 0.0
    avg_return = round(sum(r["return_pct"] for r in trackable) / len(trackable), 2) if trackable else 0.0
    total_return = round(sum(r["return_pct"] for r in trackable), 2) if trackable else 0.0

    # ベスト・ワースト
    best = max(trackable, key=lambda r: r["return_pct"]) if trackable else None
    worst = min(trackable, key=lambda r: r["return_pct"]) if trackable else None

    # インフルエンサー別
    by_influencer = {}
    for r in trackable:
        inf = r["influencer"]
        if inf not in by_influencer:
            by_influencer[inf] = {"wins": 0, "total": 0, "returns": []}
        by_influencer[inf]["total"] += 1
        by_influencer[inf]["returns"].append(r["return_pct"])
        if r["is_winner"]:
            by_influencer[inf]["wins"] += 1

    influencer_stats = {}
    for inf, data in by_influencer.items():
        influencer_stats[inf] = {
            "win_rate": round(data["wins"] / data["total"] * 100, 1) if data["total"] else 0,
            "avg_return": round(sum(data["returns"]) / len(data["returns"]), 2),
            "picks": data["total"],
        }

    # 逆指標パフォーマンス
    contrarian_recs = [r for r in trackable if r["is_contrarian"]]
    contrarian_perf = {
        "total": len(contrarian_recs),
        "reverse_return_pct": round(
            sum(-r["return_pct"] for r in contrarian_recs) / len(contrarian_recs), 2
        ) if contrarian_recs else 0.0,
    }

    return {
        "generated_at": datetime.now().isoformat(),
        "data_period": {"from": date_from, "to": date_to},
        "total_tweets_analyzed": total_tweets,
        "recommendation_tweets": rec_tweets,
        "trackable_recommendations": len(trackable),
        "total_extracted": len(recommendations),
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": win_rate,
        "avg_return_pct": avg_return,
        "total_return_pct": total_return,
        "best_pick": {
            "ticker": best["ticker"], "return_pct": best["return_pct"],
            "influencer": best["influencer"],
        } if best else None,
        "worst_pick": {
            "ticker": worst["ticker"], "return_pct": worst["return_pct"],
            "influencer": worst["influencer"],
        } if worst else None,
        "by_influencer": influencer_stats,
        "contrarian_performance": contrarian_perf,
        "untrackable_count": len(untrackable),
    }


def generate_html_report(
    summary: Dict[str, Any],
    recommendations: List[Dict[str, Any]],
    untrackable: List[Dict[str, Any]],
    output_path: str,
) -> None:
    """HTMLレポートを生成する。

    Args:
        summary: サマリーデータ
        recommendations: 推奨データリスト
        untrackable: 追跡不能ツイートリスト
        output_path: 出力HTMLファイルパス
    """
    # データをJSONとして埋め込む
    embedded_data = json.dumps({
        "summary": summary,
        "recommendations": recommendations,
        "untrackable": untrackable,
    }, ensure_ascii=False, indent=2)

    html = f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>インフルエンサー推奨銘柄パフォーマンスレポート</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: #0a0a0a; color: #e7e9ea;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.5; padding: 20px;
}}
a {{ color: #1d9bf0; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}

.container {{ max-width: 1000px; margin: 0 auto; }}
h1 {{ font-size: 24px; margin-bottom: 8px; }}
.subtitle {{ color: #71767b; font-size: 14px; margin-bottom: 24px; }}

/* Dashboard cards */
.dashboard {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 32px; }}
.card {{
  background: #16181c; border: 1px solid #2f3336; border-radius: 12px;
  padding: 20px; text-align: center;
}}
.card-value {{ font-size: 36px; font-weight: 700; }}
.card-label {{ font-size: 13px; color: #71767b; margin-top: 4px; }}
.positive {{ color: #00ba7c; }}
.negative {{ color: #f4212e; }}
.neutral {{ color: #ffd700; }}

/* Section */
.section {{ margin-bottom: 32px; }}
.section-title {{ font-size: 18px; font-weight: 700; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #2f3336; }}

/* Table */
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th {{ background: #16181c; padding: 10px 12px; text-align: left; font-weight: 600; border-bottom: 2px solid #2f3336; cursor: pointer; white-space: nowrap; }}
th:hover {{ background: #1e2024; }}
td {{ padding: 10px 12px; border-bottom: 1px solid #2f3336; }}
tr:hover {{ background: rgba(231,233,234,0.03); }}
.win {{ color: #00ba7c; font-weight: 600; }}
.lose {{ color: #f4212e; font-weight: 600; }}
.ticker {{ font-weight: 700; color: #1d9bf0; }}
.influencer {{ color: #e7e9ea; }}
.tweet-preview {{ color: #71767b; font-size: 12px; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

/* Influencer cards */
.inf-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }}
.inf-card {{
  background: #16181c; border: 1px solid #2f3336; border-radius: 8px;
  padding: 16px; display: flex; justify-content: space-between; align-items: center;
}}
.inf-name {{ font-weight: 700; font-size: 15px; }}
.inf-stats {{ text-align: right; font-size: 13px; color: #71767b; }}
.inf-winrate {{ font-size: 20px; font-weight: 700; }}

/* Untrackable */
.untrackable-item {{
  background: #16181c; border: 1px solid #2f3336; border-radius: 8px;
  padding: 12px 16px; margin-bottom: 8px; font-size: 13px;
}}
.untrackable-user {{ color: #1d9bf0; font-weight: 600; }}
.untrackable-text {{ color: #71767b; margin-top: 4px; }}

/* Tab nav */
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
</style>
</head>
<body>
<div class="container">
  <h1>インフルエンサー推奨銘柄パフォーマンスレポート</h1>
  <div class="subtitle" id="subtitle"></div>

  <!-- Dashboard -->
  <div class="dashboard" id="dashboard"></div>

  <!-- Tabs -->
  <div class="tabs">
    <button class="tab active" onclick="showTab('picks')">銘柄別</button>
    <button class="tab" onclick="showTab('influencers')">インフルエンサー別</button>
    <button class="tab" onclick="showTab('contrarian')">逆指標</button>
    <button class="tab" onclick="showTab('untrackable')">追跡不能</button>
  </div>

  <div id="tab-picks" class="tab-content active"></div>
  <div id="tab-influencers" class="tab-content"></div>
  <div id="tab-contrarian" class="tab-content"></div>
  <div id="tab-untrackable" class="tab-content"></div>
</div>

<script>
const EMBEDDED_DATA = {embedded_data};

function showTab(name) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
}}

function fmt(v, suffix) {{
  if (v === null || v === undefined) return 'N/A';
  const s = v >= 0 ? '+' + v.toFixed(2) : v.toFixed(2);
  return s + (suffix || '');
}}

function cls(v) {{
  if (v === null || v === undefined) return '';
  return v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral';
}}

function render() {{
  const s = EMBEDDED_DATA.summary;
  const recs = EMBEDDED_DATA.recommendations;
  const untr = EMBEDDED_DATA.untrackable;

  // Subtitle
  document.getElementById('subtitle').textContent =
    `対象期間: ${{s.data_period.from}} 〜 ${{s.data_period.to}} | 生成: ${{s.generated_at.slice(0,10)}}`;

  // Dashboard
  const dash = document.getElementById('dashboard');
  dash.innerHTML = `
    <div class="card">
      <div class="card-value ${{cls(s.win_rate - 50)}}">${{s.win_rate}}%</div>
      <div class="card-label">勝率 (${{s.winners}}勝 / ${{s.losers}}敗)</div>
    </div>
    <div class="card">
      <div class="card-value ${{cls(s.avg_return_pct)}}">${{fmt(s.avg_return_pct, '%')}}</div>
      <div class="card-label">平均リターン</div>
    </div>
    <div class="card">
      <div class="card-value ${{cls(s.total_return_pct)}}">${{fmt(s.total_return_pct, '%')}}</div>
      <div class="card-label">トータルリターン</div>
    </div>
    <div class="card">
      <div class="card-value">${{s.trackable_recommendations}} / ${{s.recommendation_tweets}}</div>
      <div class="card-label">追跡可能 / 推奨ツイート</div>
    </div>
  `;

  // Picks table
  const trackable = recs.filter(r => r.return_pct !== null);
  trackable.sort((a, b) => (b.return_pct || 0) - (a.return_pct || 0));

  let picksHtml = '<div class="section"><table><thead><tr>';
  picksHtml += '<th>ティッカー</th><th>リターン</th><th>結果</th>';
  picksHtml += '<th>推奨者</th><th>推奨日</th><th>推奨時価格</th><th>現在価格</th><th>ツイート</th>';
  picksHtml += '</tr></thead><tbody>';

  for (const r of trackable) {{
    const retCls = r.return_pct > 0 ? 'win' : 'lose';
    const result = r.return_pct > 0 ? 'WIN' : 'LOSE';
    picksHtml += `<tr>
      <td class="ticker">$${{r.ticker}}</td>
      <td class="${{retCls}}">${{fmt(r.return_pct, '%')}}</td>
      <td class="${{retCls}}">${{result}}</td>
      <td class="influencer">@${{r.influencer}}</td>
      <td>${{r.recommended_at || 'N/A'}}</td>
      <td>${{r.price_at_recommendation ? '$' + r.price_at_recommendation.toFixed(2) : 'N/A'}}</td>
      <td>${{r.current_price ? '$' + r.current_price.toFixed(2) : 'N/A'}}</td>
      <td class="tweet-preview"><a href="${{r.tweet_url}}" target="_blank">${{r.tweet_text}}</a></td>
    </tr>`;
  }}
  picksHtml += '</tbody></table></div>';

  // 価格未取得の推奨も表示
  const noPrice = recs.filter(r => r.return_pct === null);
  if (noPrice.length > 0) {{
    picksHtml += '<div class="section"><div class="section-title">価格未取得（' + noPrice.length + '件）</div><table><thead><tr>';
    picksHtml += '<th>ティッカー</th><th>推奨者</th><th>推奨日</th><th>ツイート</th>';
    picksHtml += '</tr></thead><tbody>';
    for (const r of noPrice) {{
      picksHtml += `<tr>
        <td class="ticker">$${{r.ticker}}</td>
        <td class="influencer">@${{r.influencer}}</td>
        <td>${{r.recommended_at || 'N/A'}}</td>
        <td class="tweet-preview"><a href="${{r.tweet_url}}" target="_blank">${{r.tweet_text}}</a></td>
      </tr>`;
    }}
    picksHtml += '</tbody></table></div>';
  }}

  document.getElementById('tab-picks').innerHTML = picksHtml;

  // Influencer cards
  let infHtml = '<div class="section"><div class="inf-grid">';
  const infEntries = Object.entries(s.by_influencer).sort((a, b) => b[1].avg_return - a[1].avg_return);
  for (const [name, data] of infEntries) {{
    const wrCls = data.win_rate >= 50 ? 'positive' : 'negative';
    const retCls = data.avg_return >= 0 ? 'positive' : 'negative';
    infHtml += `<div class="inf-card">
      <div>
        <div class="inf-name">@${{name}}</div>
        <div style="color:#71767b;font-size:13px">${{data.picks}}推奨</div>
      </div>
      <div class="inf-stats">
        <div class="inf-winrate ${{wrCls}}">${{data.win_rate}}%</div>
        <div class="${{retCls}}">平均 ${{fmt(data.avg_return, '%')}}</div>
      </div>
    </div>`;
  }}
  infHtml += '</div></div>';
  document.getElementById('tab-influencers').innerHTML = infHtml;

  // Contrarian
  const contrarians = recs.filter(r => r.is_contrarian && r.return_pct !== null);
  let contHtml = '<div class="section">';
  if (contrarians.length === 0) {{
    contHtml += '<p style="color:#71767b">逆指標アカウントの推奨銘柄データがありません。</p>';
  }} else {{
    contHtml += `<div class="dashboard" style="margin-bottom:16px">
      <div class="card">
        <div class="card-value">${{s.contrarian_performance.total}}</div>
        <div class="card-label">逆指標推奨数</div>
      </div>
      <div class="card">
        <div class="card-value ${{cls(s.contrarian_performance.reverse_return_pct)}}">${{fmt(s.contrarian_performance.reverse_return_pct, '%')}}</div>
        <div class="card-label">逆張りリターン</div>
      </div>
    </div>`;
    contHtml += '<table><thead><tr><th>ティッカー</th><th>推奨者</th><th>順張りリターン</th><th>逆張りリターン</th><th>ツイート</th></tr></thead><tbody>';
    for (const r of contrarians) {{
      contHtml += `<tr>
        <td class="ticker">$${{r.ticker}}</td>
        <td class="influencer">@${{r.influencer}}</td>
        <td class="${{r.return_pct > 0 ? 'win' : 'lose'}}">${{fmt(r.return_pct, '%')}}</td>
        <td class="${{-r.return_pct > 0 ? 'win' : 'lose'}}">${{fmt(-r.return_pct, '%')}}</td>
        <td class="tweet-preview">${{r.tweet_text}}</td>
      </tr>`;
    }}
    contHtml += '</tbody></table>';
  }}
  contHtml += '</div>';
  document.getElementById('tab-contrarian').innerHTML = contHtml;

  // Untrackable
  let untrHtml = '<div class="section">';
  untrHtml += `<div class="section-title">追跡不能ツイート (${{untr.length}}件)</div>`;
  if (untr.length === 0) {{
    untrHtml += '<p style="color:#71767b">全てのツイートから銘柄を特定できました。</p>';
  }} else {{
    for (const u of untr) {{
      untrHtml += `<div class="untrackable-item">
        <span class="untrackable-user">@${{u.username}}</span>
        <span style="color:#71767b"> — ${{u.categories.join(', ')}}</span>
        <div class="untrackable-text">${{u.text}}</div>
        ${{u.url ? '<div><a href="' + u.url + '" target="_blank" style="font-size:12px">ツイートを見る</a></div>' : ''}}
      </div>`;
    }}
  }}
  untrHtml += '</div>';
  document.getElementById('tab-untrackable').innerHTML = untrHtml;
}}

render();
</script>
</body>
</html>'''

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTMLレポート生成: {output_path}")


def print_summary(summary: Dict[str, Any], recommendations: List[Dict[str, Any]]) -> None:
    """コンソールにサマリーを出力する。"""
    s = summary
    print()
    print("=" * 60)
    print("  インフルエンサー推奨銘柄パフォーマンスレポート")
    print("=" * 60)
    print(f"対象期間: {s['data_period']['from']} 〜 {s['data_period']['to']}")
    print(f"分析ツイート数: {s['total_tweets_analyzed']}件")
    print(f"推奨・購入ツイート: {s['recommendation_tweets']}件")
    print(f"追跡可能推奨: {s['trackable_recommendations']}件 / {s['total_extracted']}件抽出")
    print()

    print("【サマリー】")
    print(f"  勝率: {s['win_rate']}% ({s['winners']}勝 / {s['losers']}敗)")
    ret_sign = "+" if s["avg_return_pct"] >= 0 else ""
    print(f"  平均リターン: {ret_sign}{s['avg_return_pct']}%")
    tot_sign = "+" if s["total_return_pct"] >= 0 else ""
    print(f"  トータルリターン: {tot_sign}{s['total_return_pct']}%")

    if s["best_pick"]:
        print(f"  ベストピック: ${s['best_pick']['ticker']} +{s['best_pick']['return_pct']}% (@{s['best_pick']['influencer']})")
    if s["worst_pick"]:
        print(f"  ワーストピック: ${s['worst_pick']['ticker']} {s['worst_pick']['return_pct']}% (@{s['worst_pick']['influencer']})")
    print()

    # 銘柄別
    trackable = [r for r in recommendations if r["return_pct"] is not None]
    trackable.sort(key=lambda r: r["return_pct"], reverse=True)

    if trackable:
        print("【銘柄別パフォーマンス】")
        for r in trackable:
            sign = "+" if r["return_pct"] >= 0 else ""
            result = "WIN " if r["is_winner"] else "LOSE"
            price_rec = f"${r['price_at_recommendation']:.2f}" if r["price_at_recommendation"] else "N/A"
            price_cur = f"${r['current_price']:.2f}" if r["current_price"] else "N/A"
            print(f"  ${r['ticker']:<6} {sign}{r['return_pct']:>6.2f}%  @{r['influencer']:<18} {r['recommended_at'] or 'N/A'}  {price_rec} -> {price_cur}  {result}")
        print()

    # インフルエンサー別
    if s["by_influencer"]:
        print("【インフルエンサー別勝率】")
        for inf, data in sorted(s["by_influencer"].items(), key=lambda x: x[1]["avg_return"], reverse=True):
            sign = "+" if data["avg_return"] >= 0 else ""
            print(f"  @{inf:<18} {data['picks']}推奨  勝率{data['win_rate']}%  平均{sign}{data['avg_return']}%")
        print()

    # 逆指標
    if s["contrarian_performance"]["total"] > 0:
        cp = s["contrarian_performance"]
        sign = "+" if cp["reverse_return_pct"] >= 0 else ""
        print(f"【逆指標】逆張りリターン: {sign}{cp['reverse_return_pct']}% ({cp['total']}件)")
        print()

    print(f"追跡不能ツイート: {s['untrackable_count']}件")
    print()


def main():
    parser = argparse.ArgumentParser(description="インフルエンサー推奨銘柄パフォーマンス測定")
    parser.add_argument(
        "--input", "-i", nargs="+",
        default=["output/*/tweets.json", "output/*/classified_llm.json", "output/merged_all.json"],
        help="入力ファイルパス（glob対応、複数指定可）",
    )
    parser.add_argument(
        "--output", "-o", default="output",
        help="出力ディレクトリ",
    )
    args = parser.parse_args()

    # 1. ツイート読込
    print("ツイートデータを読み込み中...")
    tweets = load_tweets(args.input)
    print(f"  読み込み完了: {len(tweets)}件（重複除去済み）")

    if not tweets:
        print("エラー: ツイートが見つかりません。--input で入力ファイルを指定してください。")
        sys.exit(1)

    # 2. フィルタ
    rec_tweets = filter_recommendation_tweets(tweets)
    print(f"  推奨・購入ツイート: {len(rec_tweets)}件")

    if not rec_tweets:
        print("推奨・購入カテゴリのツイートが見つかりません。")
        sys.exit(0)

    # 3. ティッカー抽出 + 価格取得 + リターン算出
    print("ティッカー抽出・価格取得中...")
    extractor = TickerExtractor()
    fetcher = PriceFetcher(cache_file=os.path.join(args.output, "price_cache.json"))

    recommendations, untrackable = build_recommendations(rec_tweets, extractor, fetcher)
    print(f"  抽出ティッカー: {len(recommendations)}件")
    print(f"  追跡不能: {len(untrackable)}件")

    # 4. サマリー算出
    summary = build_summary(recommendations, untrackable, len(tweets), len(rec_tweets))

    # 5. コンソール出力
    print_summary(summary, recommendations)

    # 6. JSON保存
    result_data = {
        "summary": summary,
        "recommendations": recommendations,
        "untrackable": untrackable,
    }
    json_path = os.path.join(args.output, "performance_result.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result_data, f, ensure_ascii=False, indent=2)
    print(f"JSON保存: {json_path}")

    # 7. HTMLレポート生成
    html_path = os.path.join(args.output, "performance_report.html")
    generate_html_report(summary, recommendations, untrackable, html_path)


if __name__ == "__main__":
    main()
