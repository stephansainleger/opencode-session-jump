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

    def test_colorize_wraps_text_with_a_role_color(self) -> None:
        """The colorize helper applies the palette color and resets after."""
        out = render.colorize("hi", PALETTE, "success")
        self.assertTrue(out.startswith("\x1b["))
        self.assertIn("hi", out)
        self.assertTrue(out.endswith(render.ANSI_RESET))

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


def _state_record(state: str, updated: int) -> render.Record:
    """Build a Record bound to a pane carrying ``state``."""
    session = db.Session(
        session_id=f"ses_{updated}", title="t", directory="/p", time_updated=updated
    )
    pane = Pane(
        pane_id="%1",
        session_name="s",
        window_index=0,
        pane_index=0,
        current_path="/p",
        title="OC | t",
        bound_session_id=session.session_id,
        state=state,
        state_at=None,
    )
    return render.Record(session, pane, now_ms=NOW_MS)


class SortRecordsTest(unittest.TestCase):
    """Attention ordering surfaces waiting/working first, then recency."""

    def test_recent_keeps_the_given_order(self) -> None:
        """The default mode preserves the newest-first input order."""
        records = [_state_record("done", 3), _state_record("working", 2)]
        self.assertEqual(render.sort_records(records, "recent"), records)

    def test_attention_ranks_waiting_then_working(self) -> None:
        """Waiting outranks working, which outranks done and unknown."""
        records = [
            _state_record("done", 5),
            _state_record("unknown", 6),
            _state_record("working", 1),
            _state_record("waiting-permission", 2),
        ]
        ordered = [record.state for record in render.sort_records(records, "attention")]
        self.assertEqual(ordered, ["waiting-permission", "working", "done", "unknown"])

    def test_attention_breaks_ties_by_recency(self) -> None:
        """Within the same priority, the most recent session comes first."""
        records = [_state_record("working", 1), _state_record("working", 9)]
        ordered = [r.session.time_updated for r in render.sort_records(records, "attention")]
        self.assertEqual(ordered, [9, 1])


class ArchivedRenderingTest(unittest.TestCase):
    """Archived sessions are marked in human views but keep their raw title."""

    def _archived(self) -> render.Record:
        session = db.Session(
            session_id="ses_arch",
            title="Old",
            directory="/p",
            time_updated=NOW_MS,
            archived=True,
        )
        return render.Record(session, None, now_ms=NOW_MS)

    def test_visible_views_add_the_marker(self) -> None:
        """fzf, NUL and the human table carry ``(arch)`` in the title field."""
        record = self._archived()
        line = render.fzf_line(record, render.layout_for([record]), PALETTE)
        self.assertEqual(line.split(render.FIELD_SEP)[4], "Old (arch)")
        self.assertIn("Old (arch)", render.nul_record(record))
        self.assertIn("Old (arch)", render.human_table([record], PALETTE))

    def test_json_keeps_the_raw_title_and_a_flag(self) -> None:
        """NDJSON exposes the untouched title next to ``archived: true``."""
        payload = json.loads(render.json_record(self._archived()))
        self.assertEqual(payload["title"], "Old")
        self.assertTrue(payload["archived"])

    def test_live_session_is_not_archived(self) -> None:
        """A normal session has no marker and ``archived`` is false."""
        record = _record()
        self.assertEqual(render.display_title(record.session), "Hello")
        self.assertFalse(json.loads(render.json_record(record))["archived"])


class StatusLineTest(unittest.TestCase):
    """The compact status segment counts live states and omits empty buckets."""

    def test_counts_in_fixed_order(self) -> None:
        """Working, both waiting kinds and done are reported in fixed order."""
        records = [
            _state_record("working", 1),
            _state_record("waiting-permission", 2),
            _state_record("waiting-question", 3),
            _state_record("done", 4),
            _state_record("done", 5),
        ]
        self.assertEqual(render.status_line(records, color=False), "OC:5 ●1 ⏸1 ?1 ✓2")

    def test_unknown_and_error_are_reported(self) -> None:
        """Unbound sessions count as unknown; errors have their own glyph."""
        records = [_state_record("", 1), _state_record("error", 2)]
        self.assertEqual(render.status_line(records, color=False), "OC:2 ✗1 ·1")

    def test_empty_list_is_a_zero_total(self) -> None:
        """No sessions still yields a parseable segment."""
        self.assertEqual(render.status_line([], color=False), "OC:0")

    def test_hex_palette_colors_each_glyph(self) -> None:
        """The default palette wraps glyphs in tmux styles and resets them."""
        line = render.status_line([_state_record("working", 1)], theme.load("opencode"))
        self.assertIn("#[fg=#7fd88f]●#[default]1", line)

    def test_ansi_palette_stays_plain(self) -> None:
        """The ANSI palette stores SGR codes, which tmux styles cannot use."""
        line = render.status_line([_state_record("working", 1)], PALETTE)
        self.assertNotIn("#[", line)

    def test_color_false_is_plain_even_with_a_hex_palette(self) -> None:
        """Disabling color yields no tmux style sequence at all."""
        line = render.status_line(
            [_state_record("working", 1)], theme.load("opencode"), color=False
        )
        self.assertEqual(line, "OC:1 ●1")


class PreviewLinesTest(unittest.TestCase):
    """The preview card separates metadata, roles and message transitions."""

    NOW = 10_000
    SESSION = db.Session(
        session_id="ses_a",
        title="Hello",
        directory="/home/u/dev",
        time_updated=1_000,
        agent="plan",
        model='{"id": "m", "providerID": "prov"}',
    )

    def _part(self, kind: str, text: str = "", tool: str = "", status: str = "") -> db.PreviewPart:
        """Build one preview part."""
        return db.PreviewPart(kind, text, tool, status)

    def _message(self, role: str, created: int, parts: list[db.PreviewPart]) -> db.PreviewMessage:
        """Build one preview message."""
        return db.PreviewMessage(role, created, tuple(parts))

    def _preview(self, total: int = 2) -> db.Preview:
        """Build a two-message preview with text, thinking and a tool call."""
        messages = (
            self._message("user", 5_000, [self._part("text", "question")]),
            self._message(
                "assistant",
                8_000,
                [
                    self._part("reasoning", "thinking hard"),
                    self._part("tool", tool="bash", status="completed"),
                    self._part("text", "answer"),
                ],
            ),
        )
        return db.Preview(messages=messages, total=total, omitted=total - len(messages))

    def _render(self, preview: db.Preview | None = None, **kwargs) -> str:
        """Render the standard preview with deterministic defaults."""
        return render.preview_lines(
            self.SESSION,
            preview or self._preview(),
            PALETTE,
            now_ms=self.NOW,
            **kwargs,
        )

    def test_header_is_six_fixed_lines(self) -> None:
        """The card and summary occupy exactly the six pinned header lines."""
        lines = self._render(color=False).splitlines()
        header = lines[:6]
        self.assertEqual(len(header), 6)
        self.assertIn("Hello", header[0])
        self.assertIn("ses_a", header[1])
        self.assertIn("/home/u/dev", header[2])
        self.assertIn("plan", header[3])
        self.assertEqual(header[5], "last 2 direct messages")

    def test_header_reports_omitted_messages(self) -> None:
        """A larger total explains that older messages are not shown."""
        lines = self._render(self._preview(total=9), color=False).splitlines()
        self.assertEqual(
            lines[5], "last 2 direct messages · 7 older messages omitted"
        )

    def test_messages_have_rules_and_role_badges(self) -> None:
        """Each visible message starts with a full-width role rule."""
        out = self._render(color=False)
        self.assertIn("── user · 5s ", out)
        self.assertIn("── assistant · 2s ", out)
        # A blank line precedes every rule, so two messages never touch.
        self.assertIn("\n\n── assistant", out)

    def test_intermediate_assistants_are_merged_and_summarized(self) -> None:
        """A run of text-less assistant steps becomes one summary line."""
        preview = db.Preview(
            messages=(
                self._message("assistant", 5_000, [self._part("reasoning", "plan")]),
                self._message(
                    "assistant", 6_000, [self._part("tool", tool="bash", status="completed")]
                ),
                self._message(
                    "assistant", 7_000, [self._part("tool", tool="edit", status="completed")]
                ),
            ),
            total=3,
            omitted=0,
        )
        out = self._render(preview, color=False)
        self.assertEqual(out.count("── assistant"), 1)
        self.assertIn("⋯ 3 intermediate steps", out)
        self.assertIn("bash×1, edit×1", out)
        self.assertNotIn("thinking", out)
        self.assertNotIn("bash completed", out)

    def test_direct_answer_hides_its_steps(self) -> None:
        """Only the text of a direct assistant reply is shown."""
        out = self._render(color=False)
        self.assertIn("answer", out)
        self.assertNotIn("thinking", out)
        self.assertNotIn("bash completed", out)

    def test_color_wraps_roles_and_labels(self) -> None:
        """Color mode emits ANSI codes; plain mode emits none."""
        colored = self._render(color=True)
        plain = self._render(color=False)
        self.assertIn("\x1b[", colored)
        self.assertNotIn("\x1b[", plain)

    def test_rules_fit_the_requested_width(self) -> None:
        """Header and rules never exceed the preview width."""
        lines = self._render(color=False, width=40).splitlines()
        for line in lines[:6]:
            self.assertLessEqual(len(line), 40)
        for line in lines:
            if line.startswith("── "):
                self.assertEqual(len(line), 40)

    def test_long_text_is_clipped_with_a_marker(self) -> None:
        """A long message is cut at a word boundary and marked."""
        long_text = "word " * 200
        preview = db.Preview(
            messages=(
                self._message("user", 5_000, [self._part("text", long_text)]),
            ),
            total=1,
            omitted=0,
        )
        out = self._render(preview, color=False)
        self.assertIn(render.ELLIPSIS, out)
        self.assertNotIn(long_text.strip(), out)

    def test_empty_preview(self) -> None:
        """A session with no messages says so in the header."""
        empty = db.Preview(messages=(), total=0, omitted=0)
        self.assertIn("no messages", self._render(empty, color=False))

    def test_messages_without_parts_are_skipped(self) -> None:
        """Empty messages do not produce a bare rule."""
        preview = db.Preview(
            messages=(self._message("assistant", 5_000, []),),
            total=1,
            omitted=0,
        )
        out = self._render(preview, color=False)
        self.assertNotIn("── assistant", out)
        self.assertIn("(no text content)", out)
