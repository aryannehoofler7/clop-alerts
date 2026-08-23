#!/usr/bin/env python3
"""Offline unit tests for overview.py -- no network.

These pin the two decisions overview.py's docstring calls out as deliberate, because both look
like brittleness a future reader might "fix": the ``panel-heading`` class is compared exactly, and
the page-finished test is ends-with rather than contains. Loosening either one silently reopens a
path to a zeroed sheet, so each has a test naming the attack it prevents.
"""

import unittest
from pathlib import Path

from overview import (
    REQUIRED_PANELS,
    OverviewError,
    panel_present,
    parse_panel,
    parse_panel_text,
    require_valid_overview,
)


def page(resources="", buildings="", end="</html>"):
    """A minimal overview page with the two panels' rows supplied by the caller."""
    return (
        '<div class="panel-heading">Resources</div>'
        f'<table class="table"><tbody>{resources}</tbody></table>'
        '<div class="panel-heading">Buildings</div>'
        f'<table class="table"><tbody>{buildings}</tbody></table>' + end
    )


APPLES = ('<tr><td style="text-align: right;">Apples</td>'
          '<td><span class="text-success">7</span></td></tr>')
BAKERY = ('<tr><td style="text-align: right;">Bakery</td>'
          '<td><span class="text-success">2</span></td></tr>')


class PanelPresentTests(unittest.TestCase):
    def test_present_and_absent(self):
        self.assertTrue(panel_present(page(APPLES, BAKERY), "Resources"))
        self.assertFalse(panel_present(page(APPLES, BAKERY), "Weapons"))

    def test_empty_panel_is_still_present(self):
        self.assertTrue(panel_present(page("", ""), "Resources"))
        self.assertEqual(parse_panel(page("", ""), "Resources"), [])

    def test_class_is_matched_exactly(self):
        # overview.php renders favourite actions on this same page as class="panel-heading h4"
        # with a label the player chooses. A token match would let one named "Resources" stand in
        # for the real panel. Failing closed is the point -- do not loosen this.
        impostor = ('<div class="panel-heading h4">Resources</div>'
                    '<table class="table"><tbody></tbody></table>')
        self.assertFalse(panel_present(impostor, "Resources"))


class RequireValidOverviewTests(unittest.TestCase):
    def test_a_good_page_is_accepted(self):
        require_valid_overview(page(APPLES, BAKERY))

    def test_either_panel_empty_alone_is_accepted(self):
        # A new nation owns no buildings; a nation can be out of everything it stockpiles.
        require_valid_overview(page(APPLES, ""))
        require_valid_overview(page("", BAKERY))

    def test_trailing_whitespace_is_accepted(self):
        require_valid_overview(page(APPLES, BAKERY, end="</html>\r\n  \n\t"))

    def test_missing_resources_panel_rejected(self):
        html = page(APPLES, BAKERY).replace(
            '<div class="panel-heading">Resources</div>', '<div class="panel-heading">Other</div>'
        )
        with self.assertRaises(OverviewError) as caught:
            require_valid_overview(html)
        self.assertIn("Resources", str(caught.exception))

    def test_missing_buildings_panel_rejected(self):
        html = page(APPLES, BAKERY).replace(
            '<div class="panel-heading">Buildings</div>', '<div class="panel-heading">Other</div>'
        )
        with self.assertRaises(OverviewError) as caught:
            require_valid_overview(html)
        self.assertIn("Buildings", str(caught.exception))

    def test_unfinished_page_rejected(self):
        with self.assertRaises(OverviewError) as caught:
            require_valid_overview(page(APPLES, BAKERY, end=""))
        self.assertIn("cut off", str(caught.exception))

    def test_closing_tag_in_a_comment_does_not_count(self):
        # 'contains' instead of 'endswith' would accept this truncated page. It must not.
        with self.assertRaises(OverviewError):
            require_valid_overview("<!-- </html> -->" + page(APPLES, BAKERY, end=""))

    def test_both_panels_empty_rejected(self):
        # One query fills both panels, so both empty at once is that query having died -- not a
        # nation that owns and holds nothing.
        with self.assertRaises(OverviewError) as caught:
            require_valid_overview(page("", ""))
        self.assertIn("no resources and no buildings", str(caught.exception))

    def test_panels_are_checked_in_page_order(self):
        # So the first complaint says how far the response actually got.
        self.assertEqual(REQUIRED_PANELS, ("Resources", "Buildings"))


GAME_SRC = Path(__file__).resolve().parent.parent / "clop"


@unittest.skipUnless(GAME_SRC.is_dir(), "game source not checked out beside this repo")
class GameSourceAssumptionsTests(unittest.TestCase):
    """Check the facts require_valid_overview relies on are still true of the game.

    Each invariant reads a property of the game's PHP. Those could stop being true without
    anything here failing -- the guard would keep passing pages it should refuse. These tests
    catch that drift on any machine with the game checked out beside this one, and skip elsewhere.
    """

    def _read(self, relative):
        return (GAME_SRC / relative).read_text(encoding="utf-8", errors="replace")

    def test_overview_renders_both_panel_headings(self):
        source = self._read("overview.php")
        for heading in REQUIRED_PANELS:
            self.assertIn(f'<div class="panel-heading">{heading}</div>', source)

    def test_footer_closes_the_document(self):
        self.assertTrue(self._read("footer.php").rstrip().endswith("EOFORM;\n?>".rstrip())
                        or "</html>" in self._read("footer.php"))
        self.assertIn("</html>", self._read("footer.php"))

    def test_resources_and_buildings_come_from_one_query(self):
        # If these are ever split into two queries, "both panels empty" stops being the signature
        # of a single failed query and the third invariant needs rethinking.
        source = self._read("backend/backend_overview.php")
        start = source.index("FROM resources r INNER JOIN resourcedefs")
        loop = source[start:start + 800]
        self.assertIn("$buildings[]", loop)
        self.assertIn("$resources[$rs['name']]", loop)


NATION_PANEL = """
<div class="panel-heading">Nation</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Government Type</td><td>Loose Despotism</td></tr>
  <tr><td style="text-align: right;">Economic Type</td><td>Poorly Defined</td></tr>
  <tr><td style="text-align: right;"><span class="text-danger">Warning:</span></td>
      <td><span class="text-danger">Your economic type is not active!</span></td></tr>
  <tr><td style="text-align: right;">Relationship with Solar Empire</td>
      <td><span class="text-danger">-120</span>
          (<span class="text-success">3</span> per tick)</td></tr>
  <tr><td style="text-align: right;">Relationship with New Lunar Republic</td>
      <td><span class="text-success">1500</span>
          (Ascending)</td></tr>
  <tr><td style="text-align: right;">Satisfaction</td>
      <td><span class="text-success">218</span> (<span class="text-danger">-5</span> per tick)</td></tr>
  <tr><td style="text-align: right;">GDP</td>
      <td><span class="text-success">60,900</span> bits per tick</td></tr>
  <tr><td style="text-align: right;">Funds</td>
      <td><span class="text-success">1,234,567</span> bits</td></tr>
</tbody></table>
"""


class ParsePanelTextTests(unittest.TestCase):
    def rows(self):
        return dict(parse_panel_text(NATION_PANEL, "Nation"))

    def test_two_span_cell_captured_whole(self):
        # parse_panel would stop at "-120" and lose the per-tick figure entirely.
        self.assertEqual(self.rows()["Relationship with Solar Empire"], "-120 (3 per tick)")

    def test_cell_whose_per_tick_is_bare_text(self):
        # Alicorn Elite / Transponyism render "(Ascending)" with no span at all.
        self.assertEqual(
            self.rows()["Relationship with New Lunar Republic"], "1500 (Ascending)"
        )

    def test_cell_with_no_span_captured(self):
        # parse_panel drops these rows entirely, because it needs a span to capture.
        self.assertEqual(self.rows()["Government Type"], "Loose Despotism")

    def test_trailing_text_after_the_span_kept(self):
        self.assertEqual(self.rows()["GDP"], "60,900 bits per tick")
        self.assertEqual(self.rows()["Funds"], "1,234,567 bits")

    def test_whitespace_collapsed(self):
        self.assertEqual(self.rows()["Satisfaction"], "218 (-5 per tick)")

    def test_name_cell_containing_a_span_still_reads_as_the_name(self):
        # The conditional "Warning:" row (rendered when active_economy is false).
        self.assertEqual(self.rows()["Warning:"], "Your economic type is not active!")

    def test_still_arms_only_on_an_exact_heading(self):
        # Favourite actions render as class="panel-heading h4" with a user-chosen label. A loose
        # match would let a favourite action named "Nation" impersonate the Nation panel.
        html = NATION_PANEL.replace('class="panel-heading"', 'class="panel-heading h4"')
        self.assertEqual(parse_panel_text(html, "Nation"), [])

    def test_parse_panel_is_unchanged_by_the_new_mode(self):
        # The span-capturing behaviour buildings.py and goods.py rely on must not shift.
        rows = dict(parse_panel(NATION_PANEL, "Nation"))
        self.assertEqual(rows["Satisfaction"], "218")
        self.assertNotIn("Government Type", rows)   # no span in that cell


if __name__ == "__main__":
    unittest.main()
