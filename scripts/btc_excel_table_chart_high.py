#!/usr/bin/env python3
"""
BTC月間中央値→翌月最高値 乖離率分析テーブルチャート生成 (4分割版・逆時系列)

yfinanceデータからBTC乖離分析を行い、
X (Twitter) 2x2グリッド投稿用の4枚の画像を生成する。

Image 1: 2025-2026年 (14行) — 最新月が先頭
Image 2: 2024年 (12行)
Image 3: 2023年 (12行)
Image 4: 2022年 (12行) + 全期間統計
"""

import os
import sys
import statistics
from datetime import datetime

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import matplotlib.font_manager as fm


# ── Configuration ──────────────────────────────────────────────

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")

OUTPUT_FILES = [
    "btc_deviation_high_1_2025_2026.png",
    "btc_deviation_high_2_2024.png",
    "btc_deviation_high_3_2023.png",
    "btc_deviation_high_4_2022.png",
]

# Colors
BG_COLOR = "#0D1117"
HEADER_BG = "#1A1A2E"
ROW_EVEN = "#0D1117"
ROW_ODD = "#161B22"
TEXT_PRIMARY = "#E6EDF3"
TEXT_SECONDARY = "#8B949E"
TEXT_HEADER = "#FFFFFF"

# Font properties (set in setup_fonts)
JP_FONT = None
MONO_FONT = None


# ── Data Loading ───────────────────────────────────────────────

def load_data():
    """Load hardcoded BTC max-price deviation data from yfinance."""
    data = [
        {'base_month': '2022/01', 'median': 41821, 'next_month': '2022/02', 'max_price': 45661, 'max_date': '2/10', 'deviation': 0.092},
        {'base_month': '2022/02', 'median': 40990, 'next_month': '2022/03', 'max_price': 48087, 'max_date': '3/28', 'deviation': 0.173},
        {'base_month': '2022/03', 'median': 41801, 'next_month': '2022/04', 'max_price': 47313, 'max_date': '4/3', 'deviation': 0.132},
        {'base_month': '2022/04', 'median': 40540, 'next_month': '2022/05', 'max_price': 39903, 'max_date': '5/4', 'deviation': -0.016},
        {'base_month': '2022/05', 'median': 30297, 'next_month': '2022/06', 'max_price': 31957, 'max_date': '6/1', 'deviation': 0.055},
        {'base_month': '2022/06', 'median': 21855, 'next_month': '2022/07', 'max_price': 24573, 'max_date': '7/30', 'deviation': 0.124},
        {'base_month': '2022/07', 'median': 21362, 'next_month': '2022/08', 'max_price': 25136, 'max_date': '8/15', 'deviation': 0.177},
        {'base_month': '2022/08', 'median': 22961, 'next_month': '2022/09', 'max_price': 22674, 'max_date': '9/13', 'deviation': -0.013},
        {'base_month': '2022/09', 'median': 19559, 'next_month': '2022/10', 'max_price': 20988, 'max_date': '10/29', 'deviation': 0.073},
        {'base_month': '2022/10', 'median': 19417, 'next_month': '2022/11', 'max_price': 21447, 'max_date': '11/5', 'deviation': 0.105},
        {'base_month': '2022/11', 'median': 16693, 'next_month': '2022/12', 'max_price': 18319, 'max_date': '12/14', 'deviation': 0.097},
        {'base_month': '2022/12', 'median': 16906, 'next_month': '2023/01', 'max_price': 23920, 'max_date': '1/29', 'deviation': 0.415},
        {'base_month': '2023/01', 'median': 20976, 'next_month': '2023/02', 'max_price': 25134, 'max_date': '2/16', 'deviation': 0.198},
        {'base_month': '2023/02', 'median': 23391, 'next_month': '2023/03', 'max_price': 29160, 'max_date': '3/30', 'deviation': 0.247},
        {'base_month': '2023/03', 'median': 25053, 'next_month': '2023/04', 'max_price': 31006, 'max_date': '4/14', 'deviation': 0.238},
        {'base_month': '2023/04', 'median': 28417, 'next_month': '2023/05', 'max_price': 29820, 'max_date': '5/6', 'deviation': 0.049},
        {'base_month': '2023/05', 'median': 27220, 'next_month': '2023/06', 'max_price': 31390, 'max_date': '6/23', 'deviation': 0.153},
        {'base_month': '2023/06', 'median': 26963, 'next_month': '2023/07', 'max_price': 31815, 'max_date': '7/13', 'deviation': 0.180},
        {'base_month': '2023/07', 'median': 30146, 'next_month': '2023/08', 'max_price': 30177, 'max_date': '8/8', 'deviation': 0.001},
        {'base_month': '2023/08', 'median': 28702, 'next_month': '2023/09', 'max_price': 27489, 'max_date': '9/19', 'deviation': -0.042},
        {'base_month': '2023/09', 'median': 26278, 'next_month': '2023/10', 'max_price': 35150, 'max_date': '10/24', 'deviation': 0.338},
        {'base_month': '2023/10', 'median': 28328, 'next_month': '2023/11', 'max_price': 38415, 'max_date': '11/24', 'deviation': 0.356},
        {'base_month': '2023/11', 'median': 36874, 'next_month': '2023/12', 'max_price': 44706, 'max_date': '12/8', 'deviation': 0.212},
        {'base_month': '2023/12', 'median': 42628, 'next_month': '2024/01', 'max_price': 48969, 'max_date': '1/11', 'deviation': 0.149},
        {'base_month': '2024/01', 'median': 42842, 'next_month': '2024/02', 'max_price': 63913, 'max_date': '2/28', 'deviation': 0.492},
        {'base_month': '2024/02', 'median': 51305, 'next_month': '2024/03', 'max_price': 73750, 'max_date': '3/14', 'deviation': 0.437},
        {'base_month': '2024/03', 'median': 68330, 'next_month': '2024/04', 'max_price': 72715, 'max_date': '4/8', 'deviation': 0.064},
        {'base_month': '2024/04', 'median': 65221, 'next_month': '2024/05', 'max_price': 71946, 'max_date': '5/21', 'deviation': 0.103},
        {'base_month': '2024/05', 'median': 66267, 'next_month': '2024/06', 'max_price': 71908, 'max_date': '6/7', 'deviation': 0.085},
        {'base_month': '2024/06', 'median': 66341, 'next_month': '2024/07', 'max_price': 69988, 'max_date': '7/29', 'deviation': 0.055},
        {'base_month': '2024/07', 'median': 64119, 'next_month': '2024/08', 'max_price': 65593, 'max_date': '8/1', 'deviation': 0.023},
        {'base_month': '2024/08', 'median': 59479, 'next_month': '2024/09', 'max_price': 66481, 'max_date': '9/27', 'deviation': 0.118},
        {'base_month': '2024/09', 'median': 60157, 'next_month': '2024/10', 'max_price': 73577, 'max_date': '10/29', 'deviation': 0.223},
        {'base_month': '2024/10', 'median': 66642, 'next_month': '2024/11', 'max_price': 99656, 'max_date': '11/22', 'deviation': 0.495},
        {'base_month': '2024/11', 'median': 90551, 'next_month': '2024/12', 'max_price': 108268, 'max_date': '12/17', 'deviation': 0.196},
        {'base_month': '2024/12', 'median': 97491, 'next_month': '2025/01', 'max_price': 109115, 'max_date': '1/20', 'deviation': 0.119},
        {'base_month': '2025/01', 'median': 101090, 'next_month': '2025/02', 'max_price': 102756, 'max_date': '2/1', 'deviation': 0.016},
        {'base_month': '2025/02', 'median': 96553, 'next_month': '2025/03', 'max_price': 95043, 'max_date': '3/2', 'deviation': -0.016},
        {'base_month': '2025/03', 'median': 84343, 'next_month': '2025/04', 'max_price': 95768, 'max_date': '4/25', 'deviation': 0.135},
        {'base_month': '2025/04', 'median': 84719, 'next_month': '2025/05', 'max_price': 111970, 'max_date': '5/22', 'deviation': 0.322},
        {'base_month': '2025/05', 'median': 104106, 'next_month': '2025/06', 'max_price': 110561, 'max_date': '6/9', 'deviation': 0.062},
        {'base_month': '2025/06', 'median': 105723, 'next_month': '2025/07', 'max_price': 123092, 'max_date': '7/14', 'deviation': 0.164},
        {'base_month': '2025/07', 'median': 117636, 'next_month': '2025/08', 'max_price': 124457, 'max_date': '8/14', 'deviation': 0.058},
        {'base_month': '2025/08', 'median': 115028, 'next_month': '2025/09', 'max_price': 117912, 'max_date': '9/18', 'deviation': 0.025},
        {'base_month': '2025/09', 'median': 113039, 'next_month': '2025/10', 'max_price': 126198, 'max_date': '10/6', 'deviation': 0.116},
        {'base_month': '2025/10', 'median': 112956, 'next_month': '2025/11', 'max_price': 111167, 'max_date': '11/2', 'deviation': -0.016},
        {'base_month': '2025/11', 'median': 94287, 'next_month': '2025/12', 'max_price': 94602, 'max_date': '12/9', 'deviation': 0.003},
        {'base_month': '2025/12', 'median': 88344, 'next_month': '2026/01', 'max_price': 97861, 'max_date': '1/14', 'deviation': 0.108},
        {'base_month': '2026/01', 'median': 90513, 'next_month': '2026/02', 'max_price': 79323, 'max_date': '2/1', 'deviation': -0.124},
        {'base_month': '2026/02', 'median': 68005, 'next_month': '2026/03', 'max_price': 74052, 'max_date': '3/4', 'deviation': 0.089},
    ]
    # Add note for last row
    data[-1]['note'] = '※3月は16日まで'

    # Add dev_pct field
    for d in data:
        d['dev_pct'] = d['deviation'] * 100

    # Reverse for descending order (newest first)
    data.reverse()
    print(f"Loaded {len(data)} rows (2022-2026, reverse chronological)")
    return data


# ── Color Functions ────────────────────────────────────────────

def get_deviation_color(dev_pct):
    """Return color based on deviation percentage value.

    Inverted scheme for max-price data (most values are positive):
    - > +30%: bright green (extreme upside)
    - > +15%: green
    - > +5%: light green
    - 0% to +5%: light gray (minimal move)
    - -1% to 0%: orange (slight negative)
    - < -1%: red (rare negative)
    - < -5%: bright red (very rare strong negative)
    """
    if dev_pct > 30:
        return "#00E676"
    elif dev_pct > 15:
        return "#66BB6A"
    elif dev_pct > 5:
        return "#81C784"
    elif dev_pct >= 0:
        return "#B0BEC5"
    elif dev_pct >= -1:
        return "#FFB74D"
    elif dev_pct >= -5:
        return "#FF5252"
    else:
        return "#FF1744"


def get_bar_color(dev_pct):
    """Return bar fill color based on deviation percentage.

    Positive bars: green (#66BB6A), brighter for larger values.
    Negative bars: red (#EF5350).
    """
    if dev_pct >= 0:
        t = min(abs(dev_pct) / 50.0, 1.0)
        r = int(0x66 + (0x00 - 0x66) * t)
        g = int(0xBB + (0xE6 - 0xBB) * t)
        b = int(0x6A + (0x76 - 0x6A) * t)
        return f"#{r:02X}{g:02X}{b:02X}"
    else:
        return "#EF5350"


def get_fontweight(dev_pct):
    """Return font weight based on deviation magnitude."""
    if dev_pct > 30 or dev_pct < -5:
        return "bold"
    return "normal"


# ── Font Setup ─────────────────────────────────────────────────

def setup_fonts():
    """Configure Japanese font and monospace font for matplotlib."""
    global JP_FONT, MONO_FONT

    jp_candidates = ['Noto Sans CJK JP', 'Hiragino Sans', 'IPAPGothic',
                     'IPAGothic', 'Yu Gothic', 'Meiryo']
    available = {f.name: f.fname for f in fm.fontManager.ttflist}

    jp_font_name = None
    for name in jp_candidates:
        if name in available:
            jp_font_name = name
            break

    if jp_font_name:
        plt.rcParams['font.family'] = jp_font_name
        print(f"Using JP font: {jp_font_name}")
    else:
        plt.rcParams['font.family'] = 'sans-serif'
        print("Warning: No Japanese font found")

    JP_FONT = jp_font_name or 'sans-serif'

    mono_candidates = ['DejaVu Sans Mono', 'Consolas', 'Courier New', 'monospace']
    MONO_FONT = None
    for name in mono_candidates:
        if name in available:
            MONO_FONT = name
            break
    if not MONO_FONT:
        MONO_FONT = 'monospace'
    print(f"Using mono font: {MONO_FONT}")

    plt.rcParams['axes.unicode_minus'] = False


def jp_text(ax, x, y, text, **kwargs):
    """Render text using the Japanese font."""
    kwargs.setdefault('fontfamily', JP_FONT)
    return ax.text(x, y, text, **kwargs)


def mono_text(ax, x, y, text, **kwargs):
    """Render text using the monospace font (ASCII only)."""
    kwargs.setdefault('fontfamily', MONO_FONT)
    return ax.text(x, y, text, **kwargs)


# ── Data Splitting ─────────────────────────────────────────────

def split_data(data):
    """Split into 4 balanced groups (data is already reversed: newest first).

    Group 1: 2025-2026 (newest first: 2026/02, 2026/01, 2025/12...2025/01) = 14 rows
    Group 2: 2024 (2024/12...2024/01) = 12 rows
    Group 3: 2023 (2023/12...2023/01) = 12 rows
    Group 4: 2022 (2022/12...2022/01) = 12 rows + statistics

    Returns:
        List of (title, rows) tuples.
    """
    g1 = [d for d in data if d['base_month'].startswith('2026') or d['base_month'].startswith('2025')]
    g2 = [d for d in data if d['base_month'].startswith('2024')]
    g3 = [d for d in data if d['base_month'].startswith('2023')]
    g4 = [d for d in data if d['base_month'].startswith('2022')]

    groups = [g1, g2, g3, g4]

    titles = [
        "BTC 月間中央値 → 翌月最高値 乖離率 ❶ 2025-2026年",
        "BTC 月間中央値 → 翌月最高値 乖離率 ❷ 2024年",
        "BTC 月間中央値 → 翌月最高値 乖離率 ❸ 2023年",
        "BTC 月間中央値 → 翌月最高値 乖離率 ❹ 2022年",
    ]

    return [(titles[i], groups[i]) for i in range(4)]


def compute_stats(data):
    """Compute summary statistics for all data."""
    dev_pcts = [d['dev_pct'] for d in data]
    neg_months = [d for d in data if d['dev_pct'] < 0]
    pos_months = [d for d in data if d['dev_pct'] >= 0]

    # Max rise (primary stat for max-price version)
    max_rise = max(data, key=lambda d: d['dev_pct'])
    max_rise_month = max_rise['base_month']
    max_rise_next_year_month = max_rise_month.split('/')[0] + '/' + str(int(max_rise_month.split('/')[1]) + 1).zfill(2)
    if max_rise_month.endswith('/12'):
        next_year = int(max_rise_month.split('/')[0]) + 1
        max_rise_next_year_month = f"{next_year}/01"
    rise_label = f"{max_rise_month}\u2192{max_rise_next_year_month.split('/')[1].lstrip('0')}\u6708"

    # Max drop
    max_drop = min(data, key=lambda d: d['dev_pct'])
    max_drop_month = max_drop['base_month']
    max_drop_next_year_month = max_drop_month.split('/')[0] + '/' + str(int(max_drop_month.split('/')[1]) + 1).zfill(2)
    if max_drop_month.endswith('/12'):
        next_year = int(max_drop_month.split('/')[0]) + 1
        max_drop_next_year_month = f"{next_year}/01"
    drop_label = f"{max_drop_month}\u2192{max_drop_next_year_month.split('/')[1].lstrip('0')}\u6708"

    avg_dev = statistics.mean(dev_pcts)
    median_dev = statistics.median(dev_pcts)
    pos_pct = len(pos_months) / len(data) * 100
    neg_pct = len(neg_months) / len(data) * 100

    return {
        'max_rise_pct': max_rise['dev_pct'],
        'max_rise_label': rise_label,
        'max_drop_pct': max_drop['dev_pct'],
        'max_drop_label': drop_label,
        'avg_dev': avg_dev,
        'median_dev': median_dev,
        'pos_pct': pos_pct,
        'neg_pct': neg_pct,
        'first_month': data[-1]['base_month'],
        'last_month': data[0]['base_month'],
    }


# ── Chart Rendering ────────────────────────────────────────────

def render_image(title, rows, all_data, image_index, stats=None):
    """Render one table image.

    Args:
        title: Image title string
        rows: Data rows for this image
        all_data: All data (for consistent bar scaling)
        image_index: 0-3 (which image)
        stats: Statistics dict for image 4 (index 3), or None

    Returns:
        matplotlib Figure
    """
    n = len(rows)

    # Layout constants — same for all 4 images (max 14 rows fits comfortably)
    row_height = 0.45
    header_height = 0.60
    title_height = 1.2
    stats_height = 2.0 if stats else 0.0
    bottom_pad = 0.3

    content_height = title_height + header_height + n * row_height + stats_height + bottom_pad

    fig_width = 12
    fig_height = 9
    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=150)
    fig.patch.set_facecolor(BG_COLOR)
    ax.set_facecolor(BG_COLOR)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, content_height)
    ax.axis('off')

    # Column positions (as fraction of width)
    col_month_x = 0.02
    col_median_x = 0.13
    col_maxprice_x = 0.29
    col_dev_x = 0.50
    bar_start_x = 0.58
    bar_end_x = 0.97
    bar_width = bar_end_x - bar_start_x

    # Bar scaling: asymmetric for max-price data
    # Negative side: accommodate up to ~15% negative
    # Positive side: accommodate up to ~50% positive
    max_neg = 15.0   # scale negative side to 15%
    max_pos = 50.0   # scale positive side to 50%
    total_range = max_neg + max_pos
    zero_frac = max_neg / total_range
    zero_x = bar_start_x + bar_width * zero_frac

    # ── Title ──
    y_title = content_height - 0.3
    jp_text(ax, 0.5, y_title, title,
            ha='center', va='top', fontsize=20, fontweight='bold',
            color=TEXT_HEADER)

    # ── Header Row ──
    y_header_top = content_height - title_height
    y_header_bot = y_header_top - header_height

    header_rect = patches.Rectangle(
        (0, y_header_bot), 1, header_height,
        facecolor=HEADER_BG, edgecolor='none'
    )
    ax.add_patch(header_rect)

    y_htext = y_header_bot + header_height * 0.55
    hfs = 16
    jp_text(ax, col_month_x, y_htext, "基準月",
            fontsize=hfs, fontweight='bold', color=TEXT_HEADER, va='center')
    jp_text(ax, col_median_x, y_htext, "月間中央値",
            fontsize=hfs, fontweight='bold', color=TEXT_HEADER, va='center')
    jp_text(ax, col_maxprice_x, y_htext, "翌月最高値(日付)",
            fontsize=hfs, fontweight='bold', color=TEXT_HEADER, va='center')
    jp_text(ax, col_dev_x, y_htext, "乖離率",
            fontsize=hfs, fontweight='bold', color=TEXT_HEADER, va='center')
    jp_text(ax, bar_start_x + bar_width / 2, y_htext, "バー",
            fontsize=hfs, fontweight='bold', color=TEXT_HEADER,
            va='center', ha='center')

    # Scale markers
    scale_markers = [-10, 0, 10, 20, 30, 40, 50]
    for marker in scale_markers:
        if marker < 0:
            mx = zero_x - (abs(marker) / max_neg) * (zero_x - bar_start_x)
        elif marker > 0:
            mx = zero_x + (marker / max_pos) * (bar_end_x - zero_x)
        else:
            mx = zero_x
        if bar_start_x - 0.01 <= mx <= bar_end_x + 0.01:
            mono_text(ax, mx, y_header_bot + 0.04, f"{marker}%",
                      fontsize=8, color='#555555', ha='center', va='bottom')

    # ── Data Rows ──
    y_data_top = y_header_bot

    for i, d in enumerate(rows):
        y_row_top = y_data_top - i * row_height
        y_row_bot = y_row_top - row_height
        y_text = y_row_bot + row_height / 2

        # Row background
        row_bg = ROW_EVEN if i % 2 == 0 else ROW_ODD
        row_rect = patches.Rectangle(
            (0, y_row_bot), 1, row_height,
            facecolor=row_bg, edgecolor='none'
        )
        ax.add_patch(row_rect)

        dev_pct = d['dev_pct']
        dev_color = get_deviation_color(dev_pct)
        dev_weight = get_fontweight(dev_pct)

        data_fs = 13
        dev_fs = 14

        # 基準月
        month_display = d['base_month']
        is_jan = month_display.endswith('/01')
        month_weight = 'bold' if is_jan else 'normal'
        month_color = TEXT_PRIMARY if is_jan else TEXT_SECONDARY
        note = d.get('note', '')
        if note:
            month_display += ' *'
        mono_text(ax, col_month_x, y_text, month_display,
                  fontsize=data_fs, fontweight=month_weight, color=month_color,
                  va='center')

        # 月間中央値
        median_str = f"${d['median']:,}"
        mono_text(ax, col_median_x, y_text, median_str,
                  fontsize=data_fs, color=TEXT_SECONDARY, va='center')

        # 翌月最高値 (日付)
        max_str = f"${d['max_price']:,}"
        if d['max_date']:
            max_str += f" ({d['max_date']})"
        mono_text(ax, col_maxprice_x, y_text, max_str,
                  fontsize=data_fs, color=TEXT_SECONDARY, va='center')

        # 乖離率
        dev_str = f"{dev_pct:+.1f}%"
        mono_text(ax, col_dev_x, y_text, dev_str,
                  fontsize=dev_fs, fontweight=dev_weight, color=dev_color,
                  va='center')

        # Horizontal bar
        bar_color = get_bar_color(dev_pct)
        bar_h = row_height * 0.55

        if dev_pct < 0:
            bar_len = min((abs(dev_pct) / max_neg) * (zero_x - bar_start_x), zero_x - bar_start_x)
            bar_left = zero_x - bar_len
            bar_rect = patches.FancyBboxPatch(
                (bar_left, y_text - bar_h / 2), bar_len, bar_h,
                boxstyle="round,pad=0.003",
                facecolor=bar_color, edgecolor='none', alpha=0.85
            )
            ax.add_patch(bar_rect)
        elif dev_pct > 0:
            bar_len = min((dev_pct / max_pos) * (bar_end_x - zero_x), bar_end_x - zero_x)
            bar_rect = patches.FancyBboxPatch(
                (zero_x, y_text - bar_h / 2), bar_len, bar_h,
                boxstyle="round,pad=0.003",
                facecolor=bar_color, edgecolor='none', alpha=0.85
            )
            ax.add_patch(bar_rect)

    # ── Zero Line (vertical dashed line) ──
    y_first_row_top = y_data_top
    y_last_row_bot = y_data_top - n * row_height
    ax.plot([zero_x, zero_x], [y_first_row_top, y_last_row_bot],
            color='#FFFFFF', linewidth=0.7, linestyle='--', alpha=0.4)

    # ── Footnote for rows with notes ──
    notes = [(d['base_month'], d['note']) for d in rows if d.get('note')]
    if notes:
        y_note = y_last_row_bot + 0.02
        for _month, note_text in notes:
            jp_text(ax, 0.02, y_note, f"* {note_text}",
                    fontsize=10, color='#8B949E', va='top')

    # ── Statistics Summary (Image 4 only) ──
    if stats:
        y_stats_top = y_last_row_bot - 0.15

        # Separator line
        ax.plot([0.02, 0.98], [y_stats_top, y_stats_top],
                color='#2D333B', linewidth=2)

        y_stats_title = y_stats_top - 0.35
        first_m = stats['first_month']
        last_m = stats['last_month']
        jp_text(ax, 0.02, y_stats_title,
                f"\u25b6 全期間統計 ({first_m}-{last_m})",
                fontsize=15, fontweight='bold', color='#58A6FF', va='center')

        y_line1 = y_stats_title - 0.45
        line1 = (f"最大上昇: {stats['max_rise_pct']:+.1f}% ({stats['max_rise_label']})  "
                 f"|  最大下落: {stats['max_drop_pct']:+.1f}% ({stats['max_drop_label']})")
        jp_text(ax, 0.02, y_line1, line1,
                fontsize=13, color=TEXT_PRIMARY, va='center')

        y_line2 = y_line1 - 0.40
        line2 = (f"平均乖離: {stats['avg_dev']:+.1f}%  |  "
                 f"中央値乖離: {stats['median_dev']:+.1f}%")
        jp_text(ax, 0.02, y_line2, line2,
                fontsize=13, color=TEXT_PRIMARY, va='center')

        y_line3 = y_line2 - 0.40
        line3 = (f"上昇月: {stats['pos_pct']:.0f}%  |  "
                 f"下落月: {stats['neg_pct']:.0f}%")
        jp_text(ax, 0.02, y_line3, line3,
                fontsize=13, color=TEXT_PRIMARY, va='center')

    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)

    return fig


# ── Main ───────────────────────────────────────────────────────

def main():
    setup_fonts()
    data = load_data()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    groups = split_data(data)
    stats = compute_stats(data)

    total_rows = 0
    for i, (title, rows) in enumerate(groups):
        is_last = (i == 3)
        img_stats = stats if is_last else None

        fig = render_image(title, rows, data, i, stats=img_stats)

        output_path = os.path.join(OUTPUT_DIR, OUTPUT_FILES[i])
        fig.savefig(output_path, facecolor=fig.get_facecolor(),
                    bbox_inches='tight', pad_inches=0.3)
        plt.close(fig)

        file_size = os.path.getsize(output_path)
        print(f"Saved: {output_path}")
        print(f"  Rows: {len(rows)}, Size: {file_size / 1024:.0f} KB ({file_size / 1024 / 1024:.1f} MB)")
        total_rows += len(rows)

    print(f"\nTotal rows across 4 images: {total_rows}")
    print(f"Expected: 50 rows  ->  {'OK' if total_rows == 50 else 'MISMATCH!'}")


if __name__ == "__main__":
    main()
