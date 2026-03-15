#!/usr/bin/env python3
"""BTC月間乖離率チャート生成スクリプト（X投稿用）

yfinanceでBTC-USDの日足データを取得し、月次の中央値(Close)と
翌月の最安値(Low)から乖離率を算出。ダークテーマの棒グラフで
可視化し、X投稿用の画像とJSONデータを出力する。
"""

import sys
import argparse
import json
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

# プロジェクトルートをパスに追加
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# --- ダークテーマカラー定義 ---
COLOR_BG = '#1a1a2e'
COLOR_TEXT = '#ffffff'
COLOR_GRID = '#2d2d44'
COLOR_POSITIVE = '#51cf66'
COLOR_NEGATIVE = '#ff6b6b'
COLOR_ZERO_LINE = '#555577'
COLOR_ANNOTATION = '#ffd43b'


# --- Layer 1: Statistical Profile ---
def compute_statistical_profile(values: np.ndarray) -> Dict[str, float]:
    """統計プロファイル算出

    Args:
        values: 乖離率の配列

    Returns:
        統計指標の辞書
    """
    mean = float(np.mean(values))
    std = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    median = float(np.median(values))
    cv = abs(std / mean) if mean != 0 else 0.0
    negative_ratio = float(np.sum(values < 0) / len(values))

    # 歪度
    if std > 0 and len(values) > 2:
        skewness = float(pd.Series(values).skew())
    else:
        skewness = 0.0

    return {
        'mean': mean,
        'std': std,
        'cv': cv,
        'skewness': skewness,
        'min': float(np.min(values)),
        'max': float(np.max(values)),
        'median': median,
        'negative_ratio': negative_ratio,
    }


# --- Layer 3: Impact Optimization ---
def detect_impact(values: np.ndarray, stats: Dict[str, float]) -> Dict[str, Any]:
    """インパクト検出（極値・トレンド）

    Args:
        values: 乖離率の配列
        stats: 統計プロファイル

    Returns:
        インパクト情報の辞書
    """
    latest = float(values[-1])
    mean = stats['mean']
    std = stats['std']

    z_score = (latest - mean) / std if std > 0 else 0.0
    is_extreme = abs(z_score) > 2.0

    # 直近3ヶ月のトレンド
    recent = values[-3:] if len(values) >= 3 else values
    trend = 'rising' if recent[-1] > recent[0] else 'falling'

    return {
        'latest': latest,
        'z_score': z_score,
        'is_extreme': is_extreme,
        'trend': trend,
    }


def generate_title(currency: str, impact: Dict[str, Any]) -> str:
    """タイトル自動生成

    Args:
        currency: 通貨単位
        impact: インパクト情報

    Returns:
        チャートタイトル文字列
    """
    base = f'BTC({currency.upper()}) 月間乖離率'

    if impact['is_extreme']:
        if impact['latest'] > 0:
            return f'{base} \U0001f525'  # 🔥
        else:
            return f'{base} \u26a0\ufe0f'  # ⚠️
    return base


# --- Data Pipeline ---
def fetch_btc_data(years: int = 4) -> pd.DataFrame:
    """yfinanceでBTC日足データ取得

    Args:
        years: 取得年数（デフォルト4年）

    Returns:
        OHLCV日足データ
    """
    end = datetime.now()
    start = end.replace(year=end.year - years, day=1)

    print(f"BTC-USD データ取得中: {start.strftime('%Y-%m-%d')} 〜 {end.strftime('%Y-%m-%d')}")
    ticker = yf.Ticker('BTC-USD')
    df = ticker.history(start=start.strftime('%Y-%m-%d'), end=end.strftime('%Y-%m-%d'))

    if df.empty:
        raise RuntimeError("BTC-USD データの取得に失敗しました")

    print(f"  取得件数: {len(df)}日分")
    return df


def calculate_monthly_divergence(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """月次乖離率算出

    各月のClose中央値と翌月のLow最安値から乖離率を計算する。

    Args:
        df: OHLCV日足データ

    Returns:
        月次乖離率データのリスト
    """
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    # タイムゾーン情報を除去してgroupby互換にする
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df['year_month'] = df.index.to_period('M')

    # 月次集計: 中央値(Close)、最安値(Low)とその日付
    monthly_groups = df.groupby('year_month')
    monthly_data = []

    for period, group in monthly_groups:
        median_close = float(group['Close'].median())
        min_low = float(group['Low'].min())
        min_low_date = group['Low'].idxmin().strftime('%Y-%m-%d')

        monthly_data.append({
            'period': period,
            'month': str(period),
            'median_close': round(median_close, 2),
            'min_low': round(min_low, 2),
            'min_low_date': min_low_date,
        })

    # 乖離率算出: (翌月最安値 - 当月中央値) / 当月中央値 * 100
    results: List[Dict[str, Any]] = []
    for i, current in enumerate(monthly_data):
        record: Dict[str, Any] = {
            'month': current['month'],
            'median_close': current['median_close'],
        }

        if i + 1 < len(monthly_data):
            next_month = monthly_data[i + 1]
            divergence = (next_month['min_low'] - current['median_close']) / current['median_close'] * 100
            record['next_month_min'] = next_month['min_low']
            record['next_month_min_date'] = next_month['min_low_date']
            record['divergence_pct'] = round(divergence, 2)
        else:
            record['next_month_min'] = None
            record['next_month_min_date'] = None
            record['divergence_pct'] = None

        results.append(record)

    return results


# --- Chart Rendering ---
def _format_price_short(price: float) -> str:
    """価格を短縮フォーマット（$42.1K, $101K 等）

    Args:
        price: USD価格

    Returns:
        短縮表記の文字列
    """
    if price >= 100_000:
        return f"${price / 1000:.0f}K"
    elif price >= 10_000:
        return f"${price / 1000:.1f}K"
    else:
        return f"${price:,.0f}"


def render_divergence_chart(
    data: List[Dict[str, Any]],
    output_path: Path,
    currency: str = 'usd',
) -> None:
    """ダークテーマ棒グラフ描画（価格情報付き2パネルレイアウト）

    左パネル: 当月中央値 → 翌月最安値のテキスト表示
    右パネル: 乖離率の横棒グラフ

    Args:
        data: 月次乖離率データ
        output_path: 画像出力パス
        currency: 通貨単位
    """
    # 乖離率ありのレコードのみ
    valid = [d for d in data if d['divergence_pct'] is not None]
    if not valid:
        print("描画可能なデータがありません")
        return

    labels = [d['month'].replace('-', '/')[2:] for d in valid]  # 22/03 形式
    values = np.array([d['divergence_pct'] for d in valid])
    colors = [COLOR_POSITIVE if v >= 0 else COLOR_NEGATIVE for v in values]

    stats = compute_statistical_profile(values)
    impact = detect_impact(values, stats)
    title = generate_title(currency, impact)

    # フォント設定
    try:
        matplotlib.rcParams['font.family'] = 'Noto Sans CJK JP'
    except Exception:
        matplotlib.rcParams['font.family'] = 'sans-serif'
    matplotlib.rcParams['axes.unicode_minus'] = False

    fig, (ax_text, ax_bar) = plt.subplots(
        1, 2, figsize=(16, 14),
        gridspec_kw={'width_ratios': [3, 5]},
        sharey=True,
    )
    fig.patch.set_facecolor(COLOR_BG)
    ax_text.set_facecolor(COLOR_BG)
    ax_bar.set_facecolor(COLOR_BG)

    # --- 共通Y軸 ---
    y = np.arange(len(labels))

    # =============================================
    # 左パネル: 価格情報テキスト
    # =============================================
    ax_text.set_xlim(0, 1)
    ax_text.set_yticks(y)
    ax_text.set_yticklabels(labels, fontsize=8)
    ax_text.invert_yaxis()

    # 軸・目盛りを非表示にしてテキストのみ表示
    ax_text.tick_params(axis='x', which='both', bottom=False, top=False, labelbottom=False)
    ax_text.tick_params(axis='y', colors=COLOR_TEXT, labelsize=9)
    for spine in ax_text.spines.values():
        spine.set_color(COLOR_GRID)

    # 水平グリッド（行の視認性向上）
    for i in range(len(valid)):
        if i % 2 == 0:
            ax_text.axhspan(i - 0.4, i + 0.4, color='#222240', alpha=0.3)

    # ヘッダー
    ax_text.text(
        0.5, -0.8, '当月中央値 → 翌月最安値',
        fontsize=8, color='#888899',
        ha='center', va='center',
        fontstyle='italic',
    )

    # 各行の価格テキスト
    for i, d in enumerate(valid):
        median_str = _format_price_short(d['median_close'])
        next_min = d.get('next_month_min')
        if next_min is not None:
            next_str = _format_price_short(next_min)
            price_label = f"{median_str} → {next_str}"
        else:
            price_label = f"{median_str} → N/A"

        text_color = colors[i]
        ax_text.text(
            0.95, i, price_label,
            fontsize=7.5, color=text_color,
            ha='right', va='center',
            family='monospace',
            alpha=0.9,
        )

    # =============================================
    # 右パネル: 横棒グラフ（既存ロジック）
    # =============================================
    bars = ax_bar.barh(y, values, color=colors, height=0.7, edgecolor='none', alpha=0.9)

    # バー末端に乖離率%ラベル
    for i, (val, bar) in enumerate(zip(values, bars)):
        pct_label = f"{val:+.1f}%"
        if val >= 0:
            x_pos = val + 0.3
            ha = 'left'
        else:
            x_pos = val - 0.3
            ha = 'right'
        ax_bar.text(
            x_pos, i, pct_label,
            fontsize=7, color=colors[i],
            ha=ha, va='center',
            alpha=0.85,
        )

    # ゼロライン
    ax_bar.axvline(x=0, color=COLOR_ZERO_LINE, linewidth=1.0, linestyle='-')

    # 平均ライン
    ax_bar.axvline(
        x=stats['mean'], color=COLOR_ANNOTATION, linewidth=0.8,
        linestyle='--', alpha=0.6, label=f"平均: {stats['mean']:.1f}%",
    )

    # --- アノテーション ---
    max_idx = int(np.argmax(values))
    min_idx = int(np.argmin(values))
    latest_idx = len(values) - 1

    annotation_indices = {max_idx: '最大', min_idx: '最小', latest_idx: '直近'}
    # 重複除去（同じインデックスならラベル結合）
    unique_annotations: Dict[int, str] = {}
    for idx, label in annotation_indices.items():
        if idx in unique_annotations:
            unique_annotations[idx] += f'/{label}'
        else:
            unique_annotations[idx] = label

    for idx, label in unique_annotations.items():
        val = values[idx]
        offset_x = 1.5 if val >= 0 else -1.5
        ax_bar.annotate(
            f'{label}',
            xy=(val, idx),
            xytext=(offset_x * 10, 0),
            textcoords='offset points',
            fontsize=9,
            color=COLOR_ANNOTATION,
            fontweight='bold',
            ha='left' if val >= 0 else 'right',
            va='center',
            arrowprops=dict(arrowstyle='->', color=COLOR_ANNOTATION, lw=0.8),
        )

    # Y軸設定（右パネルのY軸目盛りは非表示、左パネルで表示済み）
    ax_bar.tick_params(axis='y', which='both', left=False, labelleft=False)
    ax_bar.invert_yaxis()

    # X軸設定: %表示
    ax_bar.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'{v:.0f}%'))

    # グリッド（X軸方向 = 縦グリッド線）
    ax_bar.grid(axis='x', color=COLOR_GRID, linewidth=0.5, alpha=0.5)
    ax_bar.set_axisbelow(True)

    # 水平グリッド（右パネルも行の視認性向上）
    for i in range(len(valid)):
        if i % 2 == 0:
            ax_bar.axhspan(i - 0.4, i + 0.4, color='#222240', alpha=0.3)
    ax_bar.set_axisbelow(True)

    # 軸・スパインの色
    ax_bar.tick_params(colors=COLOR_TEXT, labelsize=9)
    for spine in ax_bar.spines.values():
        spine.set_color(COLOR_GRID)

    # タイトル（fig全体のスーパータイトル）
    fig.suptitle(title, color=COLOR_TEXT, fontsize=16, fontweight='bold', y=0.98)

    # サブタイトル
    subtitle = (
        f"平均: {stats['mean']:.1f}% | "
        f"最大: {stats['max']:.1f}% | "
        f"直近: {impact['latest']:.1f}%"
    )
    fig.text(
        0.5, 0.96, subtitle,
        fontsize=11, color='#aaaaaa',
        ha='center', va='top',
    )

    # 凡例（右パネル）
    ax_bar.legend(loc='lower right', fontsize=9, facecolor=COLOR_BG, edgecolor=COLOR_GRID, labelcolor=COLOR_TEXT)

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_path), dpi=150, facecolor=COLOR_BG, bbox_inches='tight')
    plt.close(fig)
    print(f"チャート保存: {output_path}")


def render_interactive_chart(
    data: List[Dict[str, Any]],
    output_path: Path,
    currency: str = 'usd',
) -> None:
    """Plotlyインタラクティブチャート描画

    Args:
        data: 月次乖離率データ
        output_path: HTML出力パス
        currency: 通貨単位
    """
    import plotly.graph_objects as go

    valid = [d for d in data if d['divergence_pct'] is not None]
    if not valid:
        print("描画可能なデータがありません")
        return

    labels = [d['month'] for d in valid]  # 2022-03 format for readability
    values = [d['divergence_pct'] for d in valid]
    colors = [COLOR_POSITIVE if v >= 0 else COLOR_NEGATIVE for v in values]

    stats = compute_statistical_profile(np.array(values))
    impact = detect_impact(np.array(values), stats)
    title = generate_title(currency, impact)

    # Currency symbol
    symbol = '$' if currency.lower() == 'usd' else '¥' if currency.lower() == 'jpy' else ''

    # Build hover text
    hover_texts = []
    for d in valid:
        median_str = f"{symbol}{d['median_close']:,.2f}"
        if d['next_month_min'] is not None:
            next_min_str = f"{symbol}{d['next_month_min']:,.2f}"
            next_date_str = d['next_month_min_date']
            hover_texts.append(
                f"<b>{d['month']}</b><br>"
                f"乖離率: {d['divergence_pct']:+.2f}%<br>"
                f"当月中央値: {median_str}<br>"
                f"翌月最安値: {next_min_str} ({next_date_str})"
            )
        else:
            hover_texts.append(
                f"<b>{d['month']}</b><br>"
                f"当月中央値: {median_str}<br>"
                f"翌月データなし"
            )

    fig = go.Figure()

    fig.add_trace(go.Bar(
        y=labels,
        x=values,
        orientation='h',
        marker_color=colors,
        hovertext=hover_texts,
        hoverinfo='text',
        opacity=0.9,
    ))

    # Zero line (vertical)
    fig.add_vline(x=0, line_color=COLOR_ZERO_LINE, line_width=1)

    # Mean line
    fig.add_vline(
        x=stats['mean'], line_color=COLOR_ANNOTATION, line_width=1,
        line_dash='dash', opacity=0.6,
        annotation_text=f"平均: {stats['mean']:.1f}%",
        annotation_position="top",
        annotation_font_color=COLOR_ANNOTATION,
        annotation_font_size=11,
    )

    # Annotations for max, min, latest
    max_idx = int(np.argmax(values))
    min_idx = int(np.argmin(values))
    latest_idx = len(values) - 1

    annotation_map = {max_idx: '最大', min_idx: '最小', latest_idx: '直近'}
    unique: Dict[int, List[str]] = {}
    for idx, lbl in annotation_map.items():
        unique.setdefault(idx, []).append(lbl)

    annotations = []
    for idx, lbls in unique.items():
        label = '/'.join(lbls)
        val = values[idx]
        annotations.append(dict(
            x=val, y=labels[idx],
            text=f"{label} {val:+.1f}%",
            showarrow=True,
            arrowhead=2,
            arrowcolor=COLOR_ANNOTATION,
            font=dict(color=COLOR_ANNOTATION, size=11, family='Noto Sans CJK JP, sans-serif'),
            ax=40 if val >= 0 else -40,
            ay=0,
        ))

    subtitle = (
        f"平均: {stats['mean']:.1f}% | "
        f"最大: {stats['max']:.1f}% | "
        f"直近: {impact['latest']:.1f}%"
    )

    fig.update_layout(
        title=dict(
            text=f"{title}<br><span style='font-size:12px;color:#aaaaaa'>{subtitle}</span>",
            font=dict(color=COLOR_TEXT, size=18, family='Noto Sans CJK JP, sans-serif'),
            x=0.5,
        ),
        plot_bgcolor=COLOR_BG,
        paper_bgcolor=COLOR_BG,
        font=dict(color=COLOR_TEXT, family='Noto Sans CJK JP, sans-serif'),
        width=1200,
        height=1400,
        xaxis=dict(
            ticksuffix='%',
            gridcolor=COLOR_GRID,
            zerolinecolor=COLOR_ZERO_LINE,
            tickfont=dict(size=10),
        ),
        yaxis=dict(
            autorange='reversed',  # oldest at top
            tickfont=dict(size=9),
            gridcolor=COLOR_GRID,
        ),
        annotations=annotations,
        showlegend=False,
        margin=dict(l=80, r=60, t=80, b=40),
        hoverlabel=dict(
            bgcolor='#2d2d44',
            font_size=13,
            font_family='Noto Sans CJK JP, sans-serif',
            font_color=COLOR_TEXT,
            bordercolor='#555577',
        ),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(output_path), include_plotlyjs='cdn')
    print(f"インタラクティブチャート保存: {output_path}")


# --- Main ---
def main() -> None:
    parser = argparse.ArgumentParser(
        description='BTC月間乖離率チャート生成（X投稿用）',
    )
    parser.add_argument(
        '--years', type=int, default=4,
        help='取得年数（デフォルト: 4）',
    )
    parser.add_argument(
        '--currency', type=str, default='usd',
        help='通貨単位（デフォルト: usd）',
    )
    parser.add_argument(
        '--output', type=str, default='output/',
        help='出力ディレクトリ（デフォルト: output/）',
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # データ取得
    df = fetch_btc_data(years=args.years)

    # 月次乖離率算出
    monthly_data = calculate_monthly_divergence(df)
    valid_count = sum(1 for d in monthly_data if d['divergence_pct'] is not None)
    print(f"月次データ: {len(monthly_data)}ヶ月（乖離率算出: {valid_count}件）")

    # JSON保存
    json_path = output_dir / 'btc_divergence_data.json'
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(monthly_data, f, ensure_ascii=False, indent=2)
    print(f"JSONデータ保存: {json_path}")

    # チャート描画
    chart_path = output_dir / 'btc_divergence_chart.png'
    render_divergence_chart(monthly_data, chart_path, currency=args.currency)

    # インタラクティブチャート描画
    interactive_path = output_dir / 'btc_divergence_interactive.html'
    render_interactive_chart(monthly_data, interactive_path, currency=args.currency)

    print("完了")


if __name__ == '__main__':
    main()
