#!/usr/bin/env python3
"""BTC月間中央値→翌月最安値 乖離率ヒートマップ生成（X投稿用・ハードコードデータ版）"""

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from pathlib import Path

# ---------------------------------------------------------------------------
# Font setup (Japanese)
# ---------------------------------------------------------------------------
for font_name in ['Noto Sans CJK JP', 'Hiragino Sans', 'sans-serif']:
    try:
        matplotlib.rcParams['font.family'] = font_name
        break
    except Exception:
        continue
matplotlib.rcParams['axes.unicode_minus'] = False

# ---------------------------------------------------------------------------
# Theme constants (X posting dark theme)
# ---------------------------------------------------------------------------
BG_COLOR = '#1a1a2e'
TEXT_COLOR = '#FFFFFF'
GRID_COLOR = '#2d2d44'
SUBTITLE_COLOR = '#AAAAAA'

# ---------------------------------------------------------------------------
# Hardcoded data: deviation rates (基準月 → 翌月最安値の乖離率 %)
# ---------------------------------------------------------------------------
YEARS = [2022, 2023, 2024, 2025]

DEVIATION_DATA = {
    2022: [-7.5, -7.3, -10.5, -33.2, -44.1, -15.6, -11.4, -17.3, -5.1, -20.5, -8.3, -1.8],
    2023: [+1.4, -15.5, +1.9, -9.3, -9.1, +6.7, -12.5, -13.8, -0.4, +14.7, +9.5, -9.2],
    2024: [-3.2, +21.4, -15.7, -10.9, -8.8, -14.2, -22.2, -10.0, -0.3, +4.3, +11.5, -8.2],
    2025: [-22.0, -16.7, -10.1, +10.7, +1.0, +1.0, -6.9, -9.2, -4.4, -33.1, -10.9, -18.2],
}

# Price details: (median, min_price, min_date)
PRICE_DATA = {
    2022: [
        (40000, 37000, "2/24"), (41000, 38000, "3/7"), (42000, 37600, "4/26"),
        (40000, 26700, "5/12"), (31500, 17600, "6/18"), (22500, 19000, "7/13"),
        (22000, 19500, "8/29"), (22000, 18200, "9/21"), (19500, 18500, "10/21"),
        (19500, 15500, "11/21"), (18000, 16500, "12/30"), (16800, 16500, "1/1"),
    ],
    2023: [
        (21500, 21800, "2/13"), (23300, 19700, "3/10"), (26500, 27000, "4/20"),
        (29000, 26300, "5/25"), (27500, 25000, "6/15"), (27000, 28800, "7/11"),
        (29500, 25800, "8/17"), (29000, 25000, "9/11"), (26800, 26700, "10/7"),
        (30000, 34400, "11/3"), (37000, 40500, "12/6"), (42500, 38600, "1/23"),
    ],
    2024: [
        (43500, 42100, "2/2"), (50000, 60700, "3/5"), (67000, 56500, "4/30"),
        (64000, 57000, "5/2"), (64000, 58400, "6/24"), (65000, 55800, "7/5"),
        (63000, 49000, "8/5"), (60000, 54000, "9/6"), (60000, 59800, "10/3"),
        (65000, 67800, "11/4"), (82000, 91400, "12/20"), (97000, 89000, "1/13"),
    ],
    2025: [
        (100000, 78000, "2/28"), (92000, 76600, "3/11"), (83000, 74600, "4/7"),
        (84000, 93000, "5/1"), (99000, 100000, "6/3"), (105000, 106000, "7/1"),
        (116000, 108000, "8/25"), (119000, 108000, "9/28"), (113000, 108000, "10/25"),
        (118000, 79000, "11/25"), (92000, 82000, "12/15"), (88000, 72000, "1/28"),
    ],
}

MONTH_LABELS = ['1月', '2月', '3月', '4月', '5月', '6月',
                '7月', '8月', '9月', '10月', '11月', '12月']

# ---------------------------------------------------------------------------
# Build numpy arrays
# ---------------------------------------------------------------------------
values = np.array([DEVIATION_DATA[y] for y in YEARS])

# Monthly averages across all years
monthly_avgs = values.mean(axis=0)

# Summary stats
all_vals = values.flatten()
avg_all = all_vals.mean()

worst_idx = np.argmin(all_vals)
worst_year = YEARS[worst_idx // 12]
worst_month = (worst_idx % 12) + 1
worst_val = all_vals[worst_idx]

best_idx = np.argmax(all_vals)
best_year = YEARS[best_idx // 12]
best_month = (best_idx % 12) + 1
best_val = all_vals[best_idx]

# ---------------------------------------------------------------------------
# Create figure: 2-panel vertical layout
# ---------------------------------------------------------------------------
fig, (ax_heat, ax_bar) = plt.subplots(
    2, 1, figsize=(14, 10), dpi=150,
    gridspec_kw={'height_ratios': [3, 1], 'hspace': 0.25}
)
fig.patch.set_facecolor(BG_COLOR)

# ===== Panel 1: Heatmap =====
ax_heat.set_facecolor(BG_COLOR)

cmap = plt.cm.RdYlGn.copy()
norm = mcolors.TwoSlopeNorm(vmin=-45, vcenter=0, vmax=22)

im = ax_heat.imshow(values, cmap=cmap, norm=norm, aspect='auto')

# Cell annotations
for i in range(len(YEARS)):
    for j in range(12):
        val = values[i, j]
        # Text color: white for extreme values, black otherwise
        if abs(val) > 20:
            text_color = 'white'
            fontweight = 'bold'
            fontsize = 10
        else:
            # Determine by luminance of cell background
            rgba = cmap(norm(val))
            luminance = 0.299 * rgba[0] + 0.587 * rgba[1] + 0.114 * rgba[2]
            text_color = 'white' if luminance < 0.45 else 'black'
            fontweight = 'normal'
            fontsize = 9

        text = f'{val:+.1f}%'
        ax_heat.text(j, i, text, ha='center', va='center',
                     color=text_color, fontsize=fontsize, fontweight=fontweight)

# Axis labels
ax_heat.set_xticks(range(12))
ax_heat.set_xticklabels(MONTH_LABELS, color=TEXT_COLOR, fontsize=11)
ax_heat.set_yticks(range(len(YEARS)))
ax_heat.set_yticklabels([str(y) for y in YEARS], color=TEXT_COLOR, fontsize=13, fontweight='bold')

# Move x-axis to top
ax_heat.xaxis.set_ticks_position('top')
ax_heat.xaxis.set_label_position('top')

# Remove spines
for spine in ax_heat.spines.values():
    spine.set_visible(False)

# Cell borders (gridlines)
ax_heat.set_xticks(np.arange(-0.5, 12, 1), minor=True)
ax_heat.set_yticks(np.arange(-0.5, len(YEARS), 1), minor=True)
ax_heat.grid(which='minor', color=GRID_COLOR, linewidth=1.0)
ax_heat.tick_params(which='minor', bottom=False, left=False, top=False)

# Colorbar
cbar = plt.colorbar(im, ax=ax_heat, pad=0.02, shrink=0.8)
cbar.ax.yaxis.set_tick_params(color=TEXT_COLOR)
plt.setp(cbar.ax.yaxis.get_ticklabels(), color=TEXT_COLOR, fontsize=9)
cbar.set_label('乖離率 (%)', color=TEXT_COLOR, fontsize=10)
cbar.outline.set_visible(False)

# ===== Panel 2: Monthly average bar chart =====
ax_bar.set_facecolor(BG_COLOR)

bar_colors = ['#2ecc71' if v >= 0 else '#e74c3c' for v in monthly_avgs]
bars = ax_bar.barh(range(12), monthly_avgs, color=bar_colors, height=0.6, edgecolor='none')

# Labels on bars
for idx, (bar, val) in enumerate(zip(bars, monthly_avgs)):
    x_pos = val + (0.3 if val >= 0 else -0.3)
    ha = 'left' if val >= 0 else 'right'
    ax_bar.text(x_pos, idx, f'{val:+.1f}%', ha=ha, va='center',
                color=TEXT_COLOR, fontsize=9, fontweight='bold')

ax_bar.set_yticks(range(12))
ax_bar.set_yticklabels(MONTH_LABELS, color=TEXT_COLOR, fontsize=10)
ax_bar.invert_yaxis()
ax_bar.set_xlabel('平均乖離率 (%)', color=TEXT_COLOR, fontsize=10)
ax_bar.tick_params(axis='x', colors=TEXT_COLOR, labelsize=9)

# Zero line
ax_bar.axvline(x=0, color=TEXT_COLOR, linewidth=0.5, alpha=0.5)

# Remove spines, add subtle grid
for spine in ax_bar.spines.values():
    spine.set_visible(False)
ax_bar.grid(axis='x', color=GRID_COLOR, linewidth=0.5, alpha=0.5)

ax_bar.set_title('月別 平均乖離率（2022-2025）', color=TEXT_COLOR, fontsize=13,
                 fontweight='bold', pad=10, loc='left')

# ===== Title & Subtitle =====
title = 'BTC 月間中央値 → 翌月最安値 乖離率（2022-2025）'
subtitle = (f'平均乖離: {avg_all:+.1f}% | '
            f'最大下落: {worst_val:+.1f}%({worst_year}/{worst_month:02d}) | '
            f'最大上昇: {best_val:+.1f}%({best_year}/{best_month:02d})')

fig.suptitle(title, color=TEXT_COLOR, fontsize=18, fontweight='bold',
             y=0.98, x=0.05, ha='left')
fig.text(0.05, 0.945, subtitle, color=SUBTITLE_COLOR, fontsize=11, ha='left')

# ===== Footer =====
fig.text(0.98, 0.01, 'データ: BTC-USD 月間中央値から翌月最安値までの乖離率',
         color='#666688', fontsize=8, ha='right', va='bottom')

# ===== Save =====
output_path = Path(__file__).parent.parent / "output" / "btc_monthly_deviation_heatmap.png"
output_path.parent.mkdir(parents=True, exist_ok=True)

plt.savefig(output_path, dpi=150, facecolor=BG_COLOR, bbox_inches='tight')
plt.close()

print(f'Chart saved: {output_path}')
print(f'Summary: avg={avg_all:+.1f}%, worst={worst_val:+.1f}%({worst_year}/{worst_month:02d}), best={best_val:+.1f}%({best_year}/{best_month:02d})')
print(f'Monthly averages: {", ".join(f"{m}: {v:+.1f}%" for m, v in zip(MONTH_LABELS, monthly_avgs))}')
