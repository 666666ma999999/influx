#!/usr/bin/env python3
"""
BTC月間中央値→翌月最安値 乖離率分析テーブルチャート生成 (4分割版)

Excel (btc_monthly_deviation.xlsx) のBTC乖離分析シートを読み込み、
X (Twitter) 2x2グリッド投稿用の4枚の画像を生成する。

Image 1: 2021年 (12行)
Image 2: 2022年 (12行)
Image 3: 2023年 (12行)
Image 4: 2024-2026年 (25行) + 全期間統計
"""

import os
import sys
from datetime import datetime

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import matplotlib.font_manager as fm

try:
    import openpyxl
except ImportError:
    print("openpyxl not found. Installing...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "openpyxl", "-q"])
    import openpyxl


# ── Configuration ──────────────────────────────────────────────

EXCEL_PATH = os.environ.get(
    "BTC_EXCEL_PATH",
    "/Users/masaaki_nagasawa/Desktop/btc_monthly_deviation.xlsx"
)
# Docker mount path fallback
if not os.path.exists(EXCEL_PATH):
    EXCEL_PATH = "/host_desktop/btc_monthly_deviation.xlsx"

SHEET_NAME = "BTC乖離分析"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")

OUTPUT_FILES = [
    "btc_deviation_1_2021.png",
    "btc_deviation_2_2022.png",
    "btc_deviation_3_2023.png",
    "btc_deviation_4_2024_2026.png",
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


# ── Color Functions ────────────────────────────────────────────

def get_deviation_color(dev_pct):
    """Return color based on deviation percentage value."""
    if dev_pct > 10:
        return "#00E676"
    elif dev_pct > 0:
        return "#66BB6A"
    elif dev_pct >= -5:
        return "#B0BEC5"
    elif dev_pct >= -10:
        return "#FFB74D"
    elif dev_pct >= -15:
        return "#FF7043"
    elif dev_pct >= -25:
        return "#EF5350"
    else:
        return "#FF1744"


def get_bar_color(dev_pct):
    """Return bar fill color based on deviation percentage."""
    if dev_pct >= 0:
        t = min(abs(dev_pct) / 25.0, 1.0)
        r = int(0x66 + (0x00 - 0x66) * t)
        g = int(0xBB + (0xE6 - 0xBB) * t)
        b = int(0x6A + (0x76 - 0x6A) * t)
        return f"#{r:02X}{g:02X}{b:02X}"
    else:
        t = min(abs(dev_pct) / 50.0, 1.0)
        r = 0xEF
        g = int(0x53 + (0x17 - 0x53) * t)
        b = int(0x50 + (0x44 - 0x50) * t)
        return f"#{r:02X}{g:02X}{b:02X}"


def get_fontweight(dev_pct):
    """Return font weight based on deviation magnitude."""
    if dev_pct > 10 or dev_pct < -25:
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


# ── Data Loading ───────────────────────────────────────────────

def load_excel_data():
    """Load BTC deviation data from Excel file."""
    print(f"Reading: {EXCEL_PATH}")
    wb = openpyxl.load_workbook(EXCEL_PATH)
    ws = wb[SHEET_NAME]

    data = []
    for row in ws.iter_rows(min_row=6, max_row=66, min_col=1, max_col=8, values_only=True):
        if row[0] is None:
            continue

        base_month = str(row[0])
        median_price = row[1]
        min_price = row[3]
        min_date = str(row[4]) if row[4] else ""
        deviation = float(row[5])

        data.append({
            'base_month': base_month,
            'median': int(median_price) if median_price else 0,
            'min_price': int(min_price) if min_price else 0,
            'min_date': min_date,
            'deviation': deviation,
            'dev_pct': deviation * 100,
        })

    print(f"Loaded {len(data)} rows")
    return data


def split_data(data):
    """Split data into 4 groups by year ranges.

    Returns:
        List of (title, rows) tuples.
    """
    groups = [
        [],  # 2021
        [],  # 2022
        [],  # 2023
        [],  # 2024-2026
    ]

    for d in data:
        year = int(d['base_month'].split('/')[0])
        if year == 2021:
            groups[0].append(d)
        elif year == 2022:
            groups[1].append(d)
        elif year == 2023:
            groups[2].append(d)
        else:
            groups[3].append(d)

    titles = [
        "BTC \u6708\u9593\u4e2d\u592e\u5024 \u2192 \u7fcc\u6708\u6700\u5b89\u5024 \u4e56\u96e2\u7387 \u2776 2021\u5e74",
        "BTC \u6708\u9593\u4e2d\u592e\u5024 \u2192 \u7fcc\u6708\u6700\u5b89\u5024 \u4e56\u96e2\u7387 \u2777 2022\u5e74",
        "BTC \u6708\u9593\u4e2d\u592e\u5024 \u2192 \u7fcc\u6708\u6700\u5b89\u5024 \u4e56\u96e2\u7387 \u2778 2023\u5e74",
        "BTC \u6708\u9593\u4e2d\u592e\u5024 \u2192 \u7fcc\u6708\u6700\u5b89\u5024 \u4e56\u96e2\u7387 \u2779 2024-2026\u5e74",
    ]

    return [(titles[i], groups[i]) for i in range(4)]


def compute_stats(data):
    """Compute summary statistics for all data."""
    dev_pcts = [d['dev_pct'] for d in data]
    neg_months = [d for d in data if d['dev_pct'] < 0]
    pos_months = [d for d in data if d['dev_pct'] >= 0]

    # Max drop
    max_drop = min(data, key=lambda d: d['dev_pct'])
    max_drop_month = max_drop['base_month']
    max_drop_next_year_month = max_drop_month.split('/')[0] + '/' + str(int(max_drop_month.split('/')[1]) + 1).zfill(2)
    if max_drop_month.endswith('/12'):
        next_year = int(max_drop_month.split('/')[0]) + 1
        max_drop_next_year_month = f"{next_year}/01"
    drop_label = f"{max_drop_month}\u2192{max_drop_next_year_month.split('/')[1].lstrip('0')}\u6708"

    # Max rise
    max_rise = max(data, key=lambda d: d['dev_pct'])
    max_rise_month = max_rise['base_month']
    max_rise_next_year_month = max_rise_month.split('/')[0] + '/' + str(int(max_rise_month.split('/')[1]) + 1).zfill(2)
    if max_rise_month.endswith('/12'):
        next_year = int(max_rise_month.split('/')[0]) + 1
        max_rise_next_year_month = f"{next_year}/01"
    rise_label = f"{max_rise_month}\u2192{max_rise_next_year_month.split('/')[1].lstrip('0')}\u6708"

    import statistics
    avg_dev = statistics.mean(dev_pcts)
    median_dev = statistics.median(dev_pcts)
    neg_pct = len(neg_months) / len(data) * 100
    pos_pct = len(pos_months) / len(data) * 100

    return {
        'max_drop_pct': max_drop['dev_pct'],
        'max_drop_label': drop_label,
        'max_rise_pct': max_rise['dev_pct'],
        'max_rise_label': rise_label,
        'avg_dev': avg_dev,
        'median_dev': median_dev,
        'neg_pct': neg_pct,
        'pos_pct': pos_pct,
        'first_month': data[0]['base_month'],
        'last_month': data[-1]['base_month'],
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
    is_compact = image_index == 3  # Image 4 has more rows

    # Layout constants
    row_height = 0.30 if is_compact else 0.45
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
    col_minprice_x = 0.29
    col_dev_x = 0.50
    bar_start_x = 0.58
    bar_end_x = 0.97
    bar_width = bar_end_x - bar_start_x

    # Bar scaling (consistent across all 4 images using all_data)
    all_neg = [d['dev_pct'] for d in all_data if d['dev_pct'] < 0]
    all_pos = [d['dev_pct'] for d in all_data if d['dev_pct'] > 0]
    max_neg = max(abs(v) for v in all_neg) if all_neg else 1
    max_pos = max(all_pos) if all_pos else 1
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
    jp_text(ax, col_month_x, y_htext, "\u57fa\u6e96\u6708",
            fontsize=hfs, fontweight='bold', color=TEXT_HEADER, va='center')
    jp_text(ax, col_median_x, y_htext, "\u6708\u9593\u4e2d\u592e\u5024",
            fontsize=hfs, fontweight='bold', color=TEXT_HEADER, va='center')
    jp_text(ax, col_minprice_x, y_htext, "\u7fcc\u6708\u6700\u5b89\u5024(\u65e5\u4ed8)",
            fontsize=hfs, fontweight='bold', color=TEXT_HEADER, va='center')
    jp_text(ax, col_dev_x, y_htext, "\u4e56\u96e2\u7387",
            fontsize=hfs, fontweight='bold', color=TEXT_HEADER, va='center')
    jp_text(ax, bar_start_x + bar_width / 2, y_htext, "\u30d0\u30fc",
            fontsize=hfs, fontweight='bold', color=TEXT_HEADER,
            va='center', ha='center')

    # Scale markers
    scale_markers = [-40, -30, -20, -10, 0, 10, 20]
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
        mono_text(ax, col_month_x, y_text, month_display,
                  fontsize=data_fs, fontweight=month_weight, color=month_color,
                  va='center')

        # 月間中央値
        median_str = f"${d['median']:,}"
        mono_text(ax, col_median_x, y_text, median_str,
                  fontsize=data_fs, color=TEXT_SECONDARY, va='center')

        # 翌月最安値 (日付)
        min_str = f"${d['min_price']:,}"
        if d['min_date']:
            min_str += f" ({d['min_date']})"
        mono_text(ax, col_minprice_x, y_text, min_str,
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
            bar_len = (abs(dev_pct) / max_neg) * (zero_x - bar_start_x)
            bar_left = zero_x - bar_len
            bar_rect = patches.FancyBboxPatch(
                (bar_left, y_text - bar_h / 2), bar_len, bar_h,
                boxstyle="round,pad=0.003",
                facecolor=bar_color, edgecolor='none', alpha=0.85
            )
            ax.add_patch(bar_rect)
        elif dev_pct > 0:
            bar_len = (dev_pct / max_pos) * (bar_end_x - zero_x)
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
                f"\u25b6 \u5168\u671f\u9593\u7d71\u8a08 ({first_m}-{last_m})",
                fontsize=15, fontweight='bold', color='#58A6FF', va='center')

        y_line1 = y_stats_title - 0.45
        line1 = (f"\u6700\u5927\u4e0b\u843d: {stats['max_drop_pct']:+.1f}% ({stats['max_drop_label']})  "
                 f"|  \u6700\u5927\u4e0a\u6607: {stats['max_rise_pct']:+.1f}% ({stats['max_rise_label']})")
        jp_text(ax, 0.02, y_line1, line1,
                fontsize=13, color=TEXT_PRIMARY, va='center')

        y_line2 = y_line1 - 0.40
        line2 = (f"\u5e73\u5747\u4e56\u96e2: {stats['avg_dev']:+.1f}%  |  "
                 f"\u4e2d\u592e\u5024\u4e56\u96e2: {stats['median_dev']:+.1f}%")
        jp_text(ax, 0.02, y_line2, line2,
                fontsize=13, color=TEXT_PRIMARY, va='center')

        y_line3 = y_line2 - 0.40
        line3 = (f"\u4e0b\u843d\u6708: {stats['neg_pct']:.0f}%  |  "
                 f"\u4e0a\u6607\u6708: {stats['pos_pct']:.0f}%")
        jp_text(ax, 0.02, y_line3, line3,
                fontsize=13, color=TEXT_PRIMARY, va='center')

    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)

    return fig


# ── Main ───────────────────────────────────────────────────────

def main():
    setup_fonts()
    data = load_excel_data()

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
    print(f"Expected: 61 rows  ->  {'OK' if total_rows == 61 else 'MISMATCH!'}")


if __name__ == "__main__":
    main()
