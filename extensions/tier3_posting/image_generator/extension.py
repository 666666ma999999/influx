"""Image generator extension for X posting.

Generates chart images (bar, pie, line) and OGP-style card images for
embedding in X (Twitter) posts.  Uses matplotlib for charts and Pillow
for OGP cards.
"""

import logging
from typing import Any, Dict

from core.registry import Extension

logger = logging.getLogger(__name__)


class ImageGeneratorExtension(Extension):
    """Extension that generates images for X posts.

    Dispatches to ChartGenerator or OGPGenerator based on the requested
    template_type.  Both generators are lazy-imported to avoid importing
    heavy libraries (matplotlib / Pillow) at setup time.
    """

    def __init__(self) -> None:
        self._event_bus = None
        self._config: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "tier3.image_generator"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "X投稿用の画像自動生成（チャート・OGPカード）"

    def setup(self, context: Any) -> None:
        """Extract event_bus and config from context.

        Heavy libraries (matplotlib, Pillow) are NOT imported here.
        They are lazy-imported in the generator classes when actually
        needed.

        Args:
            context: Dict with event_bus, config, and registry.
        """
        self._event_bus = (
            context.get("event_bus")
            if isinstance(context, dict)
            else getattr(context, "event_bus", None)
        )
        self._config = (
            context.get("config", {})
            if isinstance(context, dict)
            else getattr(context, "config", {})
        ) or {}

        if self._event_bus is not None:
            self._event_bus.subscribe(
                "tier3.generate_image", self.on_generate_image, priority=100
            )

        logger.info("ImageGeneratorExtension setup complete")

    def on_generate_image(
        self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate an image based on the requested template type.

        Args:
            event: Event name (tier3.generate_image).
            payload: Must contain:
                - template_type (str, required): One of "bar_chart",
                  "pie_chart", "line_chart", "ogp_card".
                - data (dict, required): Data specific to the template type.
                - title (str, optional): Override title for the image.
            meta: Event metadata (correlation_id etc.).

        Returns:
            Dict with keys:
                - success (bool): Whether image generation succeeded.
                - image_path (str): Path to the generated image file.
                - error (str | None): Error message if failed.
        """
        template_type = payload.get("template_type")
        data = payload.get("data")

        if not template_type:
            return self._fail("template_type is required in payload", meta)

        if not data or not isinstance(data, dict):
            return self._fail("data (dict) is required in payload", meta)

        # Override title if provided at top level
        title = payload.get("title")
        if title:
            data = dict(data)
            data["title"] = title

        output_dir = self._config.get("output_dir", "./output/posting/images")
        theme = self._config.get("theme", "dark")
        font_family = self._config.get("font_family", "Noto Sans CJK JP")

        try:
            if template_type in ("bar_chart", "pie_chart", "line_chart"):
                image_path = self._generate_chart(
                    template_type, data, output_dir, theme, font_family
                )
            elif template_type == "ogp_card":
                image_path = self._generate_ogp(
                    data, output_dir, theme, font_family
                )
            else:
                return self._fail(
                    f"Unknown template_type: {template_type}", meta
                )

            result = {
                "success": True,
                "image_path": image_path,
                "error": None,
            }
            if self._event_bus is not None:
                self._event_bus.publish("image.generated", result, meta)
            return result

        except Exception as exc:
            logger.exception(
                "Image generation failed for template_type=%s", template_type
            )
            return self._fail(str(exc), meta)

    def _generate_chart(
        self,
        template_type: str,
        data: Dict[str, Any],
        output_dir: str,
        theme: str,
        font_family: str,
    ) -> str:
        """Dispatch to the appropriate ChartGenerator method.

        Args:
            template_type: One of "bar_chart", "pie_chart", "line_chart".
            data: Chart-specific data dict.
            output_dir: Directory to save the generated image.
            theme: "dark" or "light".
            font_family: Font family name for text rendering.

        Returns:
            str: Path to the generated image file.
        """
        from .chart_generator import ChartGenerator

        generator = ChartGenerator(
            output_dir=output_dir, theme=theme, font_family=font_family
        )

        if template_type == "bar_chart":
            return generator.generate_bar_chart(data)
        elif template_type == "pie_chart":
            return generator.generate_pie_chart(data)
        elif template_type == "line_chart":
            return generator.generate_line_chart(data)
        else:
            raise ValueError(f"Unknown chart type: {template_type}")

    def _generate_ogp(
        self,
        data: Dict[str, Any],
        output_dir: str,
        theme: str,
        font_family: str,
    ) -> str:
        """Generate an OGP card image.

        Args:
            data: OGP card data dict.
            output_dir: Directory to save the generated image.
            theme: "dark" or "light".
            font_family: Font family name for text rendering.

        Returns:
            str: Path to the generated image file.
        """
        from .ogp_generator import OGPGenerator

        generator = OGPGenerator(
            output_dir=output_dir, theme=theme, font_family=font_family
        )
        return generator.generate(data)

    def _fail(
        self, error_message: str, meta: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build and publish an error result.

        Args:
            error_message: Human-readable error description.
            meta: Event metadata to forward with the failure event.

        Returns:
            Dict with success=False and the error message.
        """
        logger.error("ImageGeneratorExtension error: %s", error_message)
        result = {
            "success": False,
            "image_path": "",
            "error": error_message,
        }
        if self._event_bus is not None:
            self._event_bus.publish("image.failed", result, meta)
        return result

    def teardown(self) -> None:
        """Clean up resources."""
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe(
                    "tier3.generate_image", self.on_generate_image
                )
            except ValueError:
                pass
        self._event_bus = None
        self._config = {}
        logger.info("ImageGeneratorExtension teardown complete")
