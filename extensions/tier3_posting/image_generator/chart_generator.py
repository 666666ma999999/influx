"""Chart image generator using matplotlib.

Generates bar, pie, and line chart images suitable for embedding in
X (Twitter) posts.  Uses the Agg backend so no display server is
required.
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

logger = logging.getLogger(__name__)


class ChartGenerator:
    """matplotlib ベースのチャート画像生成."""

    DARK_THEME = {
        "bg_color": "#15202b",
        "text_color": "#e7e9ea",
        "accent_colors": [
            "#1d9bf0",
            "#00ba7c",
            "#ffd400",
            "#f91880",
            "#7856ff",
        ],
        "grid_color": "#38444d",
    }

    LIGHT_THEME = {
        "bg_color": "#ffffff",
        "text_color": "#0f1419",
        "accent_colors": [
            "#1d9bf0",
            "#00ba7c",
            "#ffd400",
            "#f91880",
            "#7856ff",
        ],
        "grid_color": "#eff3f4",
    }

    # Image dimensions: 1200x675 (16:9, X recommended)
    IMAGE_WIDTH = 1200
    IMAGE_HEIGHT = 675
    DPI = 100

    def __init__(
        self,
        output_dir: str = "./output/posting/images",
        theme: str = "dark",
        font_family: str = "Noto Sans CJK JP",
    ) -> None:
        """Initialize ChartGenerator.

        Args:
            output_dir: Directory to save generated images.
            theme: "dark" or "light".
            font_family: Font family name for text rendering.
        """
        self.output_dir = output_dir
        self.theme = self.DARK_THEME if theme == "dark" else self.LIGHT_THEME
        self.font_family = font_family
        os.makedirs(output_dir, exist_ok=True)

        # Configure matplotlib for Japanese fonts
        matplotlib.rcParams["font.family"] = font_family
        matplotlib.rcParams["axes.unicode_minus"] = False

    def _apply_theme(self, fig: plt.Figure, ax: plt.Axes) -> None:
        """Apply the current theme to figure and axes.

        Args:
            fig: matplotlib Figure to style.
            ax: matplotlib Axes to style.
        """
        fig.patch.set_facecolor(self.theme["bg_color"])
        ax.set_facecolor(self.theme["bg_color"])
        ax.tick_params(colors=self.theme["text_color"])
        ax.xaxis.label.set_color(self.theme["text_color"])
        ax.yaxis.label.set_color(self.theme["text_color"])
        ax.title.set_color(self.theme["text_color"])

        for spine in ax.spines.values():
            spine.set_color(self.theme["grid_color"])

        ax.grid(True, color=self.theme["grid_color"], alpha=0.3)

    def _save_path(self, filename: str) -> str:
        """Build the output file path with timestamp.

        Args:
            filename: Base filename (without extension).

        Returns:
            str: Full path including timestamp and .png extension.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(self.output_dir, f"{filename}_{timestamp}.png")

    def generate_bar_chart(self, data: Dict[str, Any]) -> str:
        """棒グラフ生成（勝率ランキング等）.

        Args:
            data: Dict containing:
                - labels (list[str]): Bar labels (e.g. usernames).
                - values (list[float]): Bar values (e.g. win rates).
                - title (str): Chart title.
                - ylabel (str, optional): Y-axis label.
                - filename (str, optional): Base filename.

        Returns:
            str: Path to the generated image file.
        """
        labels: List[str] = data.get("labels", [])
        values: List[float] = data.get("values", [])
        title: str = data.get("title", "")
        ylabel: str = data.get("ylabel", "")
        filename: str = data.get("filename", "bar_chart")

        fig, ax = plt.subplots(
            figsize=(
                self.IMAGE_WIDTH / self.DPI,
                self.IMAGE_HEIGHT / self.DPI,
            ),
            dpi=self.DPI,
        )
        self._apply_theme(fig, ax)

        colors = self.theme["accent_colors"]
        bar_colors = [colors[i % len(colors)] for i in range(len(labels))]

        bars = ax.barh(labels, values, color=bar_colors, edgecolor="none")

        # Add value labels on bars
        for bar, value in zip(bars, values):
            ax.text(
                bar.get_width() + max(values) * 0.01,
                bar.get_y() + bar.get_height() / 2,
                f"{value:.1f}",
                va="center",
                ha="left",
                color=self.theme["text_color"],
                fontsize=12,
            )

        ax.set_title(title, fontsize=18, fontweight="bold", pad=15)
        if ylabel:
            ax.set_xlabel(ylabel, fontsize=12)
        ax.invert_yaxis()

        fig.tight_layout()
        save_path = self._save_path(filename)
        fig.savefig(
            save_path,
            dpi=self.DPI,
            facecolor=fig.get_facecolor(),
            bbox_inches="tight",
        )
        plt.close(fig)

        logger.info("Bar chart saved to %s", save_path)
        return save_path

    def generate_pie_chart(self, data: Dict[str, Any]) -> str:
        """円グラフ生成（カテゴリ分布等）.

        Args:
            data: Dict containing:
                - labels (list[str]): Slice labels.
                - values (list[float]): Slice values.
                - title (str): Chart title.
                - filename (str, optional): Base filename.

        Returns:
            str: Path to the generated image file.
        """
        labels: List[str] = data.get("labels", [])
        values: List[float] = data.get("values", [])
        title: str = data.get("title", "")
        filename: str = data.get("filename", "pie_chart")

        fig, ax = plt.subplots(
            figsize=(
                self.IMAGE_WIDTH / self.DPI,
                self.IMAGE_HEIGHT / self.DPI,
            ),
            dpi=self.DPI,
        )
        self._apply_theme(fig, ax)
        ax.grid(False)

        colors = self.theme["accent_colors"]
        pie_colors = [colors[i % len(colors)] for i in range(len(labels))]

        wedges, texts, autotexts = ax.pie(
            values,
            labels=labels,
            colors=pie_colors,
            autopct="%1.1f%%",
            startangle=90,
            textprops={"color": self.theme["text_color"], "fontsize": 11},
        )

        for autotext in autotexts:
            autotext.set_color(self.theme["text_color"])
            autotext.set_fontsize(10)

        ax.set_title(title, fontsize=18, fontweight="bold", pad=15)

        fig.tight_layout()
        save_path = self._save_path(filename)
        fig.savefig(
            save_path,
            dpi=self.DPI,
            facecolor=fig.get_facecolor(),
            bbox_inches="tight",
        )
        plt.close(fig)

        logger.info("Pie chart saved to %s", save_path)
        return save_path

    def generate_line_chart(self, data: Dict[str, Any]) -> str:
        """折れ線グラフ生成（時系列トレンド等）.

        Args:
            data: Dict containing:
                - x_labels (list[str]): X-axis labels (e.g. dates).
                - series (list[dict]): Each dict has:
                    - label (str): Series name.
                    - values (list[float]): Data points.
                - title (str): Chart title.
                - xlabel (str, optional): X-axis label.
                - ylabel (str, optional): Y-axis label.
                - filename (str, optional): Base filename.

        Returns:
            str: Path to the generated image file.
        """
        x_labels: List[str] = data.get("x_labels", [])
        series: List[Dict[str, Any]] = data.get("series", [])
        title: str = data.get("title", "")
        xlabel: str = data.get("xlabel", "")
        ylabel: str = data.get("ylabel", "")
        filename: str = data.get("filename", "line_chart")

        fig, ax = plt.subplots(
            figsize=(
                self.IMAGE_WIDTH / self.DPI,
                self.IMAGE_HEIGHT / self.DPI,
            ),
            dpi=self.DPI,
        )
        self._apply_theme(fig, ax)

        colors = self.theme["accent_colors"]
        for i, s in enumerate(series):
            color = colors[i % len(colors)]
            ax.plot(
                x_labels,
                s.get("values", []),
                label=s.get("label", f"Series {i + 1}"),
                color=color,
                linewidth=2,
                marker="o",
                markersize=5,
            )

        ax.set_title(title, fontsize=18, fontweight="bold", pad=15)
        if xlabel:
            ax.set_xlabel(xlabel, fontsize=12)
        if ylabel:
            ax.set_ylabel(ylabel, fontsize=12)

        if series:
            legend = ax.legend(
                fontsize=10,
                facecolor=self.theme["bg_color"],
                edgecolor=self.theme["grid_color"],
            )
            for text in legend.get_texts():
                text.set_color(self.theme["text_color"])

        # Rotate x-axis labels if many
        if len(x_labels) > 7:
            plt.xticks(rotation=45, ha="right")

        fig.tight_layout()
        save_path = self._save_path(filename)
        fig.savefig(
            save_path,
            dpi=self.DPI,
            facecolor=fig.get_facecolor(),
            bbox_inches="tight",
        )
        plt.close(fig)

        logger.info("Line chart saved to %s", save_path)
        return save_path
