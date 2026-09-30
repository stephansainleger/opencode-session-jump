"""Unit tests for the live-state vocabulary."""

import unittest

from ocjump import state


class StateStyleTest(unittest.TestCase):
    """The style table must cover every raw value the plugin can emit."""

    def test_all_documented_raw_values_have_a_style(self) -> None:
        """Every value in ALL_RAW_VALUES resolves to a style entry."""
        for raw in state.ALL_RAW_VALUES:
            self.assertIn(raw, state._STYLES)

    def test_unset_and_unknown_values_share_the_neutral_style(self) -> None:
        """An empty or unrecognised state degrades to the neutral style."""
        neutral = state.style(state.UNKNOWN)
        self.assertEqual(state.style("").label, neutral.label)
        self.assertEqual(state.style("nonsense").glyph, neutral.glyph)

    def test_priority_ranks_attention(self) -> None:
        """Waiting outranks working, which outranks finished and unknown."""
        self.assertLess(state.priority(state.WAITING_PERMISSION), state.priority(state.WORKING))
        self.assertLess(state.priority(state.WORKING), state.priority(state.DONE))
        self.assertLess(state.priority(state.DONE), state.priority(state.UNKNOWN))
        self.assertEqual(state.priority("bogus"), state.priority(state.UNKNOWN))
