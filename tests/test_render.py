"""Unit tests for age/dir formatting and the three output formats."""

import json
import unittest

from ocjump import db, render, theme
from ocjump.panes import Pane

NOW_MS = 1_000_000_000
PALETTE = theme.load("ansi")


def _record(state: str = "working", pane: bool = True) -> render.Record:
    """Build a Record with an optional bound pane for rendering tests."""
    session = db.Session(
        session_id="ses_a", title="Hello", directory="/home/u/dev", time_updated=NOW_MS - 5000
    )
    bound = (
        Pane(
            pane_id="%1",
            session_name="s",
            window_index=0,
            pane_index=0,
            current_path="/home/u/dev",
            title="OC | Hello",
            bound_session_id="ses_a",
            state=state,
            state_at=NOW_MS - 1000,
        )
        if pane
        else None
    )
    return render.Record(session=session, pane=bound, now_ms=NOW_MS)


class FormatAgeTest(unittest.TestCase):
    """Age buckets must be human-friendly across all magnitudes."""

    def test_buckets(self) -> None:
        """Each magnitude falls into its expected bucket."""
        cases = {
            5: "5s",
            90: "1m",
            3 * 3600: "3h",
            2 * 86400: "2d",
            21 * 86400: "3w",
            60 * 86400: "2mo",
            400 * 86400: "1y",
        }
        for seconds, expected in cases.items():
            self.assertEqual(render.format_age(NOW_MS, NOW_MS - seconds * 1000), expected)

    def test_clock_skew_is_clamped(self) -> None:
        """A future timestamp does not render a negative age."""
        self.assertEqual(render.format_age(NOW_MS, NOW_MS + 60_000), "0s")


class AbbreviateDirTest(unittest.TestCase):
    """Home directories collapse to ``~``; others are untouched."""

    def test_home(self) -> None:
        """A path under home keeps only the ``~`` suffix."""
        self.assertEqual(render.abbreviate_dir("/home/u/dev", home="/home/u"), "~/dev")

    def test_exact_home(self) -> None:
        """The home directory itself becomes a lone ``~``."""
        self.assertEqual(render.abbreviate_dir("/home/u", home="/home/u"), "~")

    def test_other(self) -> None:
        """An unrelated path is returned unchanged."""
        self.assertEqual(render.abbreviate_dir("/srv/x", home="/home/u"), "/srv/x")


class RecordFormatsTest(unittest.TestCase):
    """fzf, NUL and NDJSON renderings expose the same underlying data."""

    def test_fzf_line_field_layout(self) -> None:
        """Seven fields, directory before title, ids hidden in the last two."""
        record = _record()
        line = render.fzf_line(record, render.layout_for([record]), PALETTE)
        fields = line.split(render.FIELD_SEP)
        self.assertEqual(len(fields), 7)
        self.assertEqual(fields[3].strip(), "/home/u/dev")
        self.assertEqual(fields[4], "Hello")
        self.assertEqual(fields[5], "ses_a")
        self.assertEqual(fields[6], "%1")

    def test_fzf_line_color_wraps_the_glyph_only(self) -> None:
        """With color, only the first field carries ANSI codes."""
        record = _record()
        fields = render.fzf_line(
            record, render.layout_for([record]), PALETTE, color=True
        ).split(render.FIELD_SEP)
        self.assertTrue(fields[0].startswith("\x1b["))
        self.assertIn(render.ANSI_RESET, fields[0])
        self.assertNotIn("\x1b[", fields[4])

    def test_titles_start_at_the_same_column(self) -> None:
        """Label, age and directory are padded to fixed widths across rows."""
        rows = [
            _record(),
            render.Record(
                db.Session(
                    session_id="ses_b",
                    title="A much longer session title",
                    directory="/home/u/a/very/deep/project",
                    time_updated=NOW_MS - 5 * 86400_000,
                ),
                None,
                now_ms=NOW_MS,
            ),
        ]
        layout = render.layout_for(rows)
        for record in rows:
            fields = render.fzf_line(record, layout, PALETTE).split(render.FIELD_SEP)
            self.assertEqual(len(fields[0]), 1)
            self.assertEqual(len(fields[1]), layout.label)
            self.assertEqual(len(fields[2]), layout.age)
            self.assertEqual(len(fields[3]), layout.directory)

    def test_long_directory_keeps_its_tail(self) -> None:
        """A directory too long for its column is trimmed from the left."""
        record = render.Record(
            db.Session(
                session_id="s",
                title="t",
                directory="/" + "x" * 80 + "/leaf",
                time_updated=NOW_MS,
            ),
            None,
            now_ms=NOW_MS,
        )
        layout = render.layout_for([record])
        field = render.fzf_line(record, layout, PALETTE).split(render.FIELD_SEP)[3]
        self.assertEqual(layout.directory, render.DIRECTORY_MAX)
        self.assertEqual(len(field), layout.directory)
        self.assertTrue(field.startswith(render.ELLIPSIS))
        self.assertTrue(field.endswith("/leaf"))

    def test_header_matches_layout(self) -> None:
        """The header legend is padded to the same column widths."""
        layout = render.layout_for([_record()])
        fields = render.fzf_header(layout).split(render.FIELD_SEP)
        self.assertEqual(len(fields[1]), layout.label)
        self.assertEqual(len(fields[2]), layout.age)
        self.assertEqual(len(fields[3]), layout.directory)

    def test_machine_formats_are_never_colored(self) -> None:
        """NUL and NDJSON output stay free of ANSI codes."""
        record = _record()
        self.assertNotIn("\x1b[", render.nul_record(record))
        self.assertNotIn("\x1b[", render.json_record(record))

    def test_unbound_pane_has_empty_id_and_state(self) -> None:
        """A session with no pane reports an empty id and state."""
        record = _record(pane=False)
        self.assertEqual(record.pane_id, "")
        self.assertEqual(record.state, "")

    def test_json_record_roundtrips(self) -> None:
        """The NDJSON payload carries the documented fields."""
        payload = json.loads(render.json_record(_record()))
        self.assertEqual(payload["session_id"], "ses_a")
        self.assertEqual(payload["state"], "working")
        self.assertEqual(payload["pane_id"], "%1")
        self.assertAlmostEqual(payload["updated"], (NOW_MS - 5000) / 1000.0)

    def test_nul_record_is_terminated(self) -> None:
        """NUL output ends each record with a NUL byte."""
        self.assertTrue(render.nul_record(_record()).endswith("\0"))

    def test_human_table_shows_title_and_colors_only_on_request(self) -> None:
        """The table shows the title; color is opt-in and never default."""
        plain = render.human_table([_record()], PALETTE, color=False)
        self.assertIn("Hello", plain)
        self.assertNotIn("\x1b[", plain)
        self.assertIn("\x1b[", render.human_table([_record()], PALETTE, color=True))

    def test_empty_table(self) -> None:
        """An empty list prints a short notice instead of nothing."""
        self.assertEqual(render.human_table([], PALETTE), "no sessions\n")
