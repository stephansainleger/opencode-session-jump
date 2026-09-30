"""Unit tests for the color palettes."""

import unittest

from ocjump import theme


class ThemeTest(unittest.TestCase):
    """Palette lookup and color conversion."""

    def test_load_known_themes(self) -> None:
        """Both shipped themes load by name."""
        for name in theme.names():
            self.assertEqual(theme.load(name).name, name)

    def test_unknown_theme_raises(self) -> None:
        """An unknown theme name is reported, not silently ignored."""
        with self.assertRaises(theme.ThemeError):
            theme.load("does-not-exist")

    def test_hex_becomes_truecolor_sgr(self) -> None:
        """A ``#rrggbb`` color becomes a 24-bit foreground sequence."""
        self.assertEqual(theme._to_sgr("#7fd88f"), "38;2;127;216;143")

    def test_non_hex_is_passed_through(self) -> None:
        """Pre-built SGR parameters (ansi palette) are returned unchanged."""
        self.assertEqual(theme._to_sgr("1;32"), "1;32")

    def test_sgr_unknown_role_falls_back_to_muted(self) -> None:
        """An unknown role resolves to the muted color instead of raising."""
        palette = theme.load("opencode")
        self.assertEqual(palette.sgr("nonsense"), palette.sgr("muted"))

    def test_opencode_palette_is_truecolor(self) -> None:
        """The opencode palette defines 24-bit state colors."""
        palette = theme.load("opencode")
        self.assertTrue(palette.sgr("success").startswith("38;2;"))

    def test_keys_role_is_distinct_from_muted(self) -> None:
        """The shortcut legend color differs from the faded header color."""
        for name in theme.names():
            palette = theme.load(name)
            with self.subTest(theme=name):
                self.assertNotEqual(palette.sgr("keys"), palette.sgr("muted"))
