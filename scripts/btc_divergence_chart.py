#!/usr/bin/env python3
"""BTC月間乖離率ヒートマップ生成（X投稿用）"""

import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from pathlib import Path

# Read data
data_path = Path(__file__).parent.parent / "output" / "btc_divergence_data.json"
with open(data_path) as f:
    data = json.load(f)

matrix_data = data["matrix"]
years = matrix_data["years"]
months = matrix_data["months"]

# Build values array, replacing None with NaN
raw_values = matrix_data["values"]
values = np.array([[np.nan if v is None else v for v in row] for row in raw_values])

# Chart config
BG_COLOR = '#1a1a2e'
TEXT_COLOR = '#FFFFFF'
GRID_COLOR = '#2d2d44'
SUBTITLE_COLOR = '#888888'

# Font setup
try:
    matplotlib.rcParams['font.family'] = 'Noto Sans CJK JP'
except:
    matplotlib.rcParams['font.family'] = 'sans-serif'
matplotlib.rcParams['axes.unicode_minus'] = False

# Month labels in Japanese
month_labels = ['1月', '2月', '3月', '4月', '5月', '6月',
                '7月', '8月', '9月', '10月', '11月', '12月']
year_labels = [str(y) for y in years]

# Create figure
fig, ax = plt.subplots(figsize=(14, 6))
fig.patch.set_facecolor(BG_COLOR)
ax.set_facecolor(BG_COLOR)

# Create diverging colormap centered at 0
# Use RdYlGn: Red (negative/danger) -> Yellow (neutral) -> Green (positive/opportunity)
cmap = plt.cm.RdYlGn.copy()
norm = mcolors.TwoSlopeNorm(vmin=-40, vcenter=0, vmax=25)

# Set NaN cells to dark background
cmap.set_bad(color=BG_COLOR)

# Create masked array for NaN values
masked_values = np.ma.masked_invalid(values)

# Draw heatmap
im = ax.imshow(masked_values, cmap=cmap, norm=norm, aspect='auto')

# Cell value annotations
for i in range(len(years)):
    for j in range(12):
        val = values[i, j]
        if np.isnan(val):
            continue
        # Determine text color based on background brightness
        rgba = cmap(norm(val))
        luminance = 0.299 * rgba[0] + 0.587 * rgba[1] + 0.114 * rgba[2]
        text_color = 'white' if luminance < 0.5 else 'black'

        # Format: show sign and 1 decimal
        text = f'{val:+.1f}%'
        fontweight = 'bold' if abs(val) > 20 else 'normal'
        fontsize = 9 if abs(val) > 20 else 8

        ax.text(j, i, text, ha='center', va='center',
                color=text_color, fontsize=fontsize, fontweight=fontweight)

# Axis labels
ax.set_xticks(range(12))
ax.set_xticklabels(month_labels, color=TEXT_COLOR, fontsize=11)
ax.set_yticks(range(len(years)))
ax.set_yticklabels(year_labels, color=TEXT_COLOR, fontsize=12, fontweight='bold')

# Move x-axis labels to top
ax.xaxis.set_ticks_position('top')
ax.xaxis.set_label_position('top')

# Remove spines
for spine in ax.spines.values():
    spine.set_visible(False)

# Colorbar
cbar = plt.colorbar(im, ax=ax, pad=0.02, shrink=0.8)
cbar.ax.yaxis.set_tick_params(color=TEXT_COLOR)
plt.setp(cbar.ax.yaxis.get_ticklabels(), color=TEXT_COLOR, fontsize=9)
cbar.set_label('乖離率 (%)', color=TEXT_COLOR, fontsize=10)
cbar.outline.set_visible(False)

# Title with impact
summary = data["summary"]
worst = summary["worst_divergence"]
best = summary["best_divergence"]

title = 'BTC 月間中央値→翌月最安値 乖離率マトリクス'
subtitle = (f'平均: {summary["avg_divergence"]:+.1f}% | '
            f'最大下落: {worst["value"]:+.1f}% ({worst["label"]}) | '
            f'最大上昇: {best["value"]:+.1f}% ({best["label"]}) | '
            f'負の月: {summary["negative_count"]}/{summary["total_months"]}')

ax.set_title(title, color=TEXT_COLOR, fontsize=16, fontweight='bold', pad=25, loc='left')
fig.text(0.05, 0.92, subtitle, color=SUBTITLE_COLOR, fontsize=10, ha='left')

# Add gridlines between cells
ax.set_xticks(np.arange(-0.5, 12, 1), minor=True)
ax.set_yticks(np.arange(-0.5, len(years), 1), minor=True)
ax.grid(which='minor', color=GRID_COLOR, linewidth=0.5)
ax.tick_params(which='minor', bottom=False, left=False, top=False)

# Watermark
fig.text(0.98, 0.02, '@influx_bot', color='#444466', fontsize=8,
         ha='right', va='bottom', alpha=0.5)

# Save
output_path = Path(__file__).parent.parent / "output" / "posting" / "images" / "btc_divergence_heatmap.png"
output_path.parent.mkdir(parents=True, exist_ok=True)
plt.tight_layout(rect=[0, 0, 1, 0.90])
plt.savefig(output_path, dpi=150, facecolor=BG_COLOR, bbox_inches='tight')
plt.close()
print(f'✅ Chart saved: {output_path}')
