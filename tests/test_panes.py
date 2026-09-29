"""Unit tests for pane parsing and the two-tier session mapping."""

import unittest

import support
from ocjump import db, panes
from ocjump.runner import Result


def _session(session_id: str, title: str, directory: str = "/p") -> db.Session:
    """Build an in-memory Session for mapping tests."""
    return db.Session(session_id=session_id, title=title, directory=directory, time_updated=0)


def _pane(title: str, path: str = "/p", bound: str = "", state: str = "") -> panes.Pane:
    """Build a Pane directly for mapping tests."""
    return panes.Pane(
        pane_id="%1",
        session_name="s",
        window_index=0,
        pane_index=0,
        current_path=path,
        title=title,
        bound_session_id=bound,
        state=state,
        state_at=None,
    )


class ParsePanesTest(unittest.TestCase):
    """Parsing must keep OpenCode panes and skip malformed lines."""

    def test_keeps_opencode_and_parses_optionals(self) -> None:
        """A non-OpenCode pane and a malformed line are dropped."""
        output = "\n".join(
            [
                support.pane_line("%1", "s", "OC | Hello", "/p", state="working", state_at="99"),
                support.pane_line("%2", "s", "shell", "/p", command="bash"),
                "garbage",
            ]
        )
        result = panes.parse_panes(output)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].state, "working")
        self.assertEqual(result[0].state_at, 99)

    def test_empty_state_at_becomes_none(self) -> None:
        """An unset @oc_state_at parses to None, not an error."""
        result = panes.parse_panes(support.pane_line("%1", "s", "OC | X", "/p"))
        self.assertIsNone(result[0].state_at)

    def test_list_panes_returns_empty_when_tmux_fails(self) -> None:
        """A failing tmux yields an empty list rather than raising."""

        def failing_runner(argv):  # noqa: ANN001, ANN202 - test seam
            return Result(tuple(argv), 1, "", "")

        self.assertEqual(panes.list_panes(failing_runner), [])


class SplitPaneTitleTest(unittest.TestCase):
    """Title splitting handles the prefix, ellipsis and foreign titles."""

    def test_title_cases(self) -> None:
        """Full, unicode/ascii-truncated and foreign titles are handled."""
        cases = {
            "OC | Hello": ("Hello", False),
            "OC | Hel\u2026": ("Hel", True),
            "OC | Hel...": ("Hel", True),
            "bash": ("", False),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(panes.split_pane_title(title), expected)


class MatchSessionTest(unittest.TestCase):
    """The tiers degrade gracefully: bound id, unique title, else None."""

    def test_bound_id_wins(self) -> None:
        """The plugin-published id is trusted over the title."""
        sessions = [_session("ses_a", "Hello"), _session("ses_b", "Hello")]
        by_id = {s.session_id: s for s in sessions}
        self.assertEqual(
            panes.match_session(_pane("OC | other", bound="ses_b"), sessions, by_id), "ses_b"
        )

    def test_unique_truncated_title_in_same_dir(self) -> None:
        """A truncated title matching one session in the same dir resolves."""
        sessions = [_session("ses_a", "Hello world"), _session("ses_b", "Other", "/q")]
        by_id = {s.session_id: s for s in sessions}
        self.assertEqual(
            panes.match_session(_pane("OC | Hello\u2026", path="/p"), sessions, by_id), "ses_a"
        )

    def test_ambiguous_title_is_none(self) -> None:
        """Two candidate sessions make the tier-2 match refuse to guess."""
        sessions = [_session("ses_a", "Same one"), _session("ses_b", "Same two")]
        by_id = {s.session_id: s for s in sessions}
        self.assertIsNone(panes.match_session(_pane("OC | Same\u2026"), sessions, by_id))

    def test_directory_must_match(self) -> None:
        """A title match in another directory is rejected."""
        sessions = [_session("ses_a", "Hello", "/elsewhere")]
        by_id = {s.session_id: s for s in sessions}
        self.assertIsNone(panes.match_session(_pane("OC | Hello", path="/p"), sessions, by_id))

    def test_exact_untruncated_match(self) -> None:
        """An untruncated title must match exactly."""
        sessions = [_session("ses_a", "Hello")]
        by_id = {s.session_id: s for s in sessions}
        self.assertEqual(panes.match_session(_pane("OC | Hello"), sessions, by_id), "ses_a")


class ResolveTest(unittest.TestCase):
    """Resolution indexes sessions to their first matching pane."""

    def test_maps_bound_pane(self) -> None:
        """A bound pane is indexed under its session id."""
        sessions = [_session("ses_a", "Hello")]
        result = panes.resolve(sessions, [_pane("OC | Hello", bound="ses_a")])
        self.assertEqual(result["ses_a"].pane_id, "%1")
