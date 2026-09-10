import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = "#1a1a2e"


def _relative_luminance(color: str) -> float:
    if len(color) == 4:
        color = "#" + "".join(channel * 2 for channel in color[1:])
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def _selector_color(template: str, selector: str) -> str:
    source = (REPO_ROOT / template).read_text(encoding="utf-8")
    rule = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}", source, re.DOTALL)
    if rule is None:
        raise AssertionError(f"Missing CSS rule for {selector} in {template}")
    color = re.search(
        r"\bcolor:\s*(#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?)\b",
        rule.group("body"),
    )
    if color is None:
        raise AssertionError(f"Missing color declaration for {selector} in {template}")
    return color.group(1)


class TemplateContrastTests(unittest.TestCase):
    def test_normal_muted_text_meets_wcag_aa_contrast(self):
        cases = (
            ("templates/view.html", ".entry .time"),
            ("templates/history.html", ".empty"),
        )

        for template, selector in cases:
            with self.subTest(template=template, selector=selector):
                foreground = _selector_color(template, selector)
                self.assertGreaterEqual(
                    _contrast_ratio(foreground, BACKGROUND),
                    4.5,
                    f"{selector} in {template} does not meet WCAG AA contrast",
                )


if __name__ == "__main__":
    unittest.main()
