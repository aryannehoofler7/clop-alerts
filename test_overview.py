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


if __name__ == "__main__":
    unittest.main()
