#!/usr/bin/env python3
"""Offline unit tests for stockpiles.py -- no network."""

import unittest

from stockpiles import (
    STOCK_FIRST_ROW,
    STOCK_ROWS,
    StockpileError,
    desired_stock,
    parse_overview_resources,
    parse_server_time,
)


# A minimal overview.php: the Resources panel (with icon cells, comma formatting and the trailing
# centred columns), plus decoy panels that share its exact row shape.
OVERVIEW_HTML = """
<li><a>Server time: 2026-08-23 03:23:44</a></li>
<div class="panel-heading">Resources</div>
<table class="table"><tbody>
  <tr>
    <td style="width: 16px;"><img src="images/icons/Apples.png"/></td>
    <td style="text-align: right;">Apples</td>
    <td><span class="text-success">1,226</span></td>
    <td style="text-align: center;"><span class="text-success">0</span></td>
    <td style="text-align: center;"><span class="text-danger">14</span></td>
  </tr>
  <tr>
    <td style="width: 16px;"><img src="images/icons/Coffee.png"/></td>
    <td style="text-align: right;">Coffee</td>
    <td><span class="text-success">29</span></td>
    <td style="text-align: center;"><span class="text-danger">1</span></td>
  </tr>
  <tr>
    <td style="text-align: right;">Gems</td>
    <td><span class="text-success">6</span></td>
  </tr>
</tbody></table>
<div class="panel-heading">Buildings</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Gem Mine</td><td><span>3</span></td></tr>
</tbody></table>
<div class="panel-heading">Weapons</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Oil</td><td><span>999</span></td></tr>
</tbody></table>
"""


class ParseResourcesTests(unittest.TestCase):
    def test_only_resources_panel_parsed(self):
        result = parse_overview_resources(OVERVIEW_HTML)
        self.assertEqual(set(result), {"Apples", "Coffee", "Gems"})
        self.assertNotIn("Gem Mine", result)     # buildings panel ignored
        self.assertEqual(result.get("Oil"), None)  # the weapons-panel decoy is not picked up

    def test_commas_stripped(self):
        self.assertEqual(parse_overview_resources(OVERVIEW_HTML)["Apples"], 1226)

    def test_row_without_icon_cell_parsed(self):
        # hideicons drops the leading <td>; the name is then the first cell.
        self.assertEqual(parse_overview_resources(OVERVIEW_HTML)["Gems"], 6)

    def test_unparseable_quantity_raises(self):
        html = """
<div class="panel-heading">Resources</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Apples</td><td><span>N/A</span></td></tr>
</tbody></table>
"""
        with self.assertRaises(StockpileError) as caught:
            parse_overview_resources(html)
        self.assertIn("Apples", str(caught.exception))

    def test_missing_resources_panel_is_empty_not_an_error(self):
        # A nation can legitimately hold nothing; an absent panel is not a malformed page.
        self.assertEqual(parse_overview_resources("<html></html>"), {})


class DesiredStockTests(unittest.TestCase):
    def test_row_order_matches_the_sheet(self):
        self.assertEqual(
            [label for label, _ in STOCK_ROWS],
            ["apple", "oil", "coffee", "mpart", "vpart", "gems"],
        )
        self.assertEqual(STOCK_FIRST_ROW, 11)

    def test_absent_good_is_zero(self):
        # The nation holds no machinery or vehicle parts, so they are absent from the page.
        self.assertEqual(desired_stock({}), [0, 0, 0, 0, 0, 0])

    def test_quantities_in_row_order(self):
        resources = parse_overview_resources(OVERVIEW_HTML)
        # apple, oil, coffee, mpart, vpart, gems
        self.assertEqual(desired_stock(resources), [1226, 0, 29, 0, 0, 6])


class ServerTimeTests(unittest.TestCase):
    def test_stamp_returned_verbatim(self):
        self.assertEqual(parse_server_time(OVERVIEW_HTML), "2026-08-23 03:23:44")

    def test_stamp_found_in_the_real_header_markup(self):
        html = '<li><a>Server time: 2026-01-02 09:05:00</a></li><li><a>Next tick: 0:36:16</a></li>'
        self.assertEqual(parse_server_time(html), "2026-01-02 09:05:00")

    def test_missing_stamp_raises(self):
        with self.assertRaises(StockpileError):
            parse_server_time("<html><body>Please log in.</body></html>")


class MappingIntegrityTests(unittest.TestCase):
    def test_six_distinct_goods(self):
        self.assertEqual(len(STOCK_ROWS), 6)
        self.assertEqual(len({label for label, _ in STOCK_ROWS}), 6)
        self.assertEqual(len({game for _, game in STOCK_ROWS}), 6)

    def test_game_names_are_real_resourcedefs_names(self):
        # The non-building resourcedefs names, hand-copied from clop/tables with data.sql. This
        # guards against a typo in STOCK_ROWS, not against the game renaming a resource -- that
        # would only show up on a live run.
        known = {
            "Oil", "Copper", "Apples", "Energy", "Vehicle Parts", "Machinery Parts", "Pies",
            "Cider", "Coffee", "Gasoline", "Gems", "Tungsten", "Plastics", "Precision Parts",
            "Composites", "Drugs", "Toys", "Forbidden Research", "Apotheosis Serum",
        }
        for _, game_name in STOCK_ROWS:
            self.assertIn(game_name, known)


if __name__ == "__main__":
    unittest.main()
