"""OGP card image generator using Pillow.

Generates Open Graph Protocol style card images for embedding in
X (Twitter) posts.  Designed to look like professional summary cards
with the X dark theme aesthetic.
"""

import logging
import os
import textwrap
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


class OGPGenerator:
    """Pillow ベースの OGP カード風画像生成."""

    # OGP standard dimensions
    IMAGE_WIDTH = 1200
    IMAGE_HEIGHT = 630

    # Category color mapping
    CATEGORY_COLORS = {
        "recommended_assets": "#1d9bf0",  # blue
        "purchased_assets": "#00ba7c",    # green
        "warning_signals": "#f91880",     # red/pink
        "market_trend": "#ffd400",        # yellow
        "bullish_assets": "#00ba7c",      # green
        "bearish_assets": "#f91880",      # pink
        "ipo": "#7856ff",                 # purple
    }

    DARK_THEME = {
        "bg_color": "#15202b",
        "card_color": "#192734",
        "title_color": "#e7e9ea",
        "summary_color": "#8899a6",
        "accent_default": "#1d9bf0",
    }

    LIGHT_THEME = {
        "bg_color": "#ffffff",
        "card_color": "#f7f9f9",
        "title_color": "#0f1419",
        "summary_color": "#536471",
        "accent_default": "#1d9bf0",
    }

    # Font search paths (Linux / Docker environments)
    FONT_SEARCH_PATHS = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
        "/usr/share/fonts/noto-cjk/NotoSansCJKjp-Regular.otf",
    ]

    def __init__(
        self,
        output_dir: str = "./output/posting/images",
        theme: str = "dark",
        font_family: str = "Noto Sans CJK JP",
    ) -> None:
        """Initialize OGPGenerator.

        Args:
            output_dir: Directory to save generated images.
            theme: "dark" or "light".
            font_family: Font family name (used as hint for font search).
        """
        self.output_dir = output_dir
        self.theme = self.DARK_THEME if theme == "dark" else self.LIGHT_THEME
        self.font_family = font_family
        os.makedirs(output_dir, exist_ok=True)

        self._font_path: Optional[str] = self._find_font()

    def _find_font(self) -> Optional[str]:
        """Search for a suitable CJK font file.

        Returns:
            str or None: Path to the font file, or None if not found.
        """
        for path in self.FONT_SEARCH_PATHS:
            if os.path.exists(path):
                logger.debug("Found font at %s", path)
                return path
        logger.warning(
            "CJK font not found in search paths; falling back to default font"
        )
        return None

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont:
        """Load a font at the specified size with fallback.

        Args:
            size: Font size in pixels.

        Returns:
            ImageFont instance.
        """
        if self._font_path:
            try:
                return ImageFont.truetype(self._font_path, size)
            except (OSError, IOError):
                logger.warning(
                    "Failed to load font %s at size %d; using default",
                    self._font_path,
                    size,
                )
        return ImageFont.load_default()

    def _hex_to_rgb(self, hex_color: str) -> Tuple[int, int, int]:
        """Convert hex color string to RGB tuple.

        Args:
            hex_color: Color string like "#1d9bf0".

        Returns:
            Tuple of (R, G, B) integers.
        """
        hex_color = hex_color.lstrip("#")
        return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))

    def _draw_rounded_rect(
        self,
        draw: ImageDraw.Draw,
        xy: Tuple[int, int, int, int],
        radius: int,
        fill: str,
    ) -> None:
        """Draw a rounded rectangle.

        Args:
            draw: ImageDraw instance.
            xy: (x0, y0, x1, y1) bounding box.
            radius: Corner radius in pixels.
            fill: Fill color (hex string).
        """
        x0, y0, x1, y1 = xy
        color = self._hex_to_rgb(fill)

        # Main body rectangles
        draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=color)
        draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=color)

        # Four corner circles
        draw.pieslice(
            [x0, y0, x0 + 2 * radius, y0 + 2 * radius],
            180, 270, fill=color,
        )
        draw.pieslice(
            [x1 - 2 * radius, y0, x1, y0 + 2 * radius],
            270, 360, fill=color,
        )
        draw.pieslice(
            [x0, y1 - 2 * radius, x0 + 2 * radius, y1],
            90, 180, fill=color,
        )
        draw.pieslice(
            [x1 - 2 * radius, y1 - 2 * radius, x1, y1],
            0, 90, fill=color,
        )

    def _draw_badge(
        self,
        draw: ImageDraw.Draw,
        text: str,
        position: Tuple[int, int],
        color: str,
        font: ImageFont.FreeTypeFont,
    ) -> None:
        """Draw a colored pill-shaped badge with text.

        Args:
            draw: ImageDraw instance.
            text: Badge text.
            position: (x, y) top-left position.
            color: Badge background color (hex string).
            font: Font for the badge text.
        """
        bbox = font.getbbox(text)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        padding_x = 16
        padding_y = 8
        badge_width = text_width + padding_x * 2
        badge_height = text_height + padding_y * 2
        radius = badge_height // 2

        x, y = position
        self._draw_rounded_rect(
            draw,
            (x, y, x + badge_width, y + badge_height),
            radius,
            color,
        )

        # Center text inside badge
        text_x = x + padding_x
        text_y = y + padding_y
        draw.text(
            (text_x, text_y),
            text,
            fill=self._hex_to_rgb("#ffffff"),
            font=font,
        )

    def generate(self, data: Dict[str, Any]) -> str:
        """OGPカード風画像を生成.

        Args:
            data: Dict containing:
                - title (str): Main title text.
                - summary (str, optional): Summary / description text.
                - category (str, optional): Category key for color coding.
                - badge_text (str, optional): Text to show in the badge.
                - filename (str, optional): Base filename.

        Returns:
            str: Path to the generated image file.
        """
        title: str = data.get("title", "")
        summary: str = data.get("summary", "")
        category: str = data.get("category", "")
        badge_text: str = data.get("badge_text", "")
        filename: str = data.get("filename", "ogp_card")

        # Determine accent color from category
        accent_color = self.CATEGORY_COLORS.get(
            category, self.theme["accent_default"]
        )

        # Create base image
        bg_color = self._hex_to_rgb(self.theme["bg_color"])
        img = Image.new("RGB", (self.IMAGE_WIDTH, self.IMAGE_HEIGHT), bg_color)
        draw = ImageDraw.Draw(img)

        # Draw card area with rounded corners
        card_margin = 40
        self._draw_rounded_rect(
            draw,
            (
                card_margin,
                card_margin,
                self.IMAGE_WIDTH - card_margin,
                self.IMAGE_HEIGHT - card_margin - 20,
            ),
            20,
            self.theme["card_color"],
        )

        # Load fonts
        title_font = self._load_font(48)
        summary_font = self._load_font(28)
        badge_font = self._load_font(20)

        # Draw badge
        content_x = card_margin + 50
        current_y = card_margin + 50

        if badge_text:
            self._draw_badge(
                draw, badge_text, (content_x, current_y), accent_color,
                badge_font,
            )
            current_y += 55

        # Draw title (with text wrapping)
        title_color = self._hex_to_rgb(self.theme["title_color"])
        wrapped_title: List[str] = textwrap.wrap(title, width=22)
        for line in wrapped_title[:3]:  # Max 3 lines
            draw.text(
                (content_x, current_y),
                line,
                fill=title_color,
                font=title_font,
            )
            current_y += 60

        current_y += 15

        # Draw summary (with text wrapping)
        if summary:
            summary_color = self._hex_to_rgb(self.theme["summary_color"])
            wrapped_summary: List[str] = textwrap.wrap(summary, width=38)
            for line in wrapped_summary[:4]:  # Max 4 lines
                draw.text(
                    (content_x, current_y),
                    line,
                    fill=summary_color,
                    font=summary_font,
                )
                current_y += 38

        # Draw bottom accent bar
        bar_height = 6
        bar_y = self.IMAGE_HEIGHT - card_margin - 20 - bar_height
        accent_rgb = self._hex_to_rgb(accent_color)
        draw.rectangle(
            [
                card_margin + 20,
                bar_y,
                self.IMAGE_WIDTH - card_margin - 20,
                bar_y + bar_height,
            ],
            fill=accent_rgb,
        )

        # Save image
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_path = os.path.join(
            self.output_dir, f"{filename}_{timestamp}.png"
        )
        img.save(save_path, "PNG")

        logger.info("OGP card saved to %s", save_path)
        return save_path
