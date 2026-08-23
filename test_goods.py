#!/usr/bin/env python3
"""Offline unit tests for goods.py -- no network."""

import unittest

from goods import (
    BY_DASHBOARD_LABEL,
    BY_GAME_NAME,
    BY_RESOURCE_ID,
    BY_STOCK_LABEL,
    BY_TICK_LABEL,
    GOODS,
    Good,
    StockpileError,
    Stockpiles,
    parse_overview_resources,
)


class GoodsTableTests(unittest.TestCase):
    def test_thirty_one_goods(self):
        # The game has exactly 31 rows in resourcedefs with is_building = 0.
        # See docs/2026-08-23-dashboard-goods-map.md.
        self.assertEqual(len(GOODS), 31)

    def test_game_names_unique(self):
        names = [good.game_name for good in GOODS]
        self.assertEqual(len(set(names)), len(names))

    def test_dashboard_labels_unique(self):
        labels = [good.dashboard_label for good in GOODS]
        self.assertEqual(len(set(labels)), len(labels))

    def test_resource_ids_unique(self):
        ids = [good.resource_id for good in GOODS]
        self.assertEqual(len(set(ids)), len(ids))

    def test_six_stock_labels_and_they_are_unique(self):
        labels = [good.stock_label for good in GOODS if good.stock_label]
        self.assertEqual(sorted(labels), ["apple", "coffee", "gems", "mpart", "oil", "vpart"])

    def test_the_four_abbreviated_dashboard_labels(self):
        # The only labels that are not the game name verbatim. Each has exactly one candidate
        # in resourcedefs, which is why the mapping is safe.
        self.assertEqual(BY_DASHBOARD_LABEL["Gas"].game_name, "Gasoline")
        self.assertEqual(BY_DASHBOARD_LABEL["M Parts"].game_name, "Machinery Parts")
        self.assertEqual(BY_DASHBOARD_LABEL["V Parts"].game_name, "Vehicle Parts")
        self.assertEqual(BY_DASHBOARD_LABEL["P Parts"].game_name, "Precision Parts")

    def test_dna_labels_reverse_the_game_word_order(self):
        self.assertEqual(
            BY_DASHBOARD_LABEL["DNA - Burro - Central"].game_name, "DNA - Central Burrozil"
        )
        self.assertEqual(
            BY_DASHBOARD_LABEL["DNA - Prze - South"].game_name, "DNA - South Przewalskia"
        )

    def test_known_resource_ids(self):
        self.assertEqual(BY_GAME_NAME["Apples"].resource_id, 3)
        self.assertEqual(BY_GAME_NAME["Machinery Parts"].resource_id, 10)
        self.assertEqual(BY_GAME_NAME["Apotheosis Serum"].resource_id, 77)
        self.assertEqual(BY_RESOURCE_ID[9].game_name, "Vehicle Parts")

    def test_stock_index_covers_only_the_six(self):
        self.assertEqual(len(BY_STOCK_LABEL), 6)
        self.assertEqual(BY_STOCK_LABEL["mpart"].game_name, "Machinery Parts")

    def test_tick_labels_cover_the_same_six_as_the_stock_block(self):
        # The sheet's "- tick" rows track exactly the goods its STOCK block tracks. If that ever
        # stops being true this test is the place it should be decided, not discovered.
        self.assertEqual(
            {good.game_name for good in GOODS if good.tick_label},
            {good.game_name for good in GOODS if good.stock_label},
        )

    def test_tick_labels_are_their_own_names(self):
        # Singular "M Part - tick", not the quantity block's plural "M Parts". They are separate
        # vocabularies and neither may be derived from the other.
        self.assertEqual(BY_TICK_LABEL["M Part - tick"].game_name, "Machinery Parts")
        self.assertEqual(BY_TICK_LABEL["Apple - tick"].game_name, "Apples")
        self.assertEqual(BY_TICK_LABEL["Gems - tick"].game_name, "Gems")
        self.assertEqual(len(BY_TICK_LABEL), 6)

    def test_good_is_frozen(self):
        with self.assertRaises(Exception):
            GOODS[0].game_name = "nope"


# A minimal overview.php Resources panel: icon cells, comma formatting, trailing centred columns,
# plus decoy panels that share its exact row shape.
RESOURCES_HTML = """
<div class="panel-heading">Resources</div>
<table class="table">
<thead>
  <tr><td></td><td style="text-align: right;">Resource</td>
    <td>Qty</td><td>Generated</td><td>Used</td><td>Loss</td><td>Net</td><td>Ticks-Worth</td>
  </tr>
</thead>
<tbody>
  <tr>
    <td style="width: 16px;"><img src="images/icons/Apples.png"/></td>
    <td style="text-align: right;">Apples</td>
    <td><span class="text-success">1,226</span></td>
    <td style="text-align: center;"><span class="text-success">0</span></td>
    <td style="text-align: center;"><span class="text-danger">14</span></td>
    <td style="text-align: center;"><span class="text-success">0</span></td>
    <td style="text-align: center;"><span class="text-danger">-14</span></td>
    <td style="text-align: center;"><span class="text-warning">87</span></td>
  </tr>
  <tr>
    <td style="text-align: right;">Gems</td>
    <td><span class="text-success">6</span></td>
    <td><span>0</span></td><td><span>0</span></td><td><span>0</span></td><td><span>0</span></td>
    <td><span class="text-success">N/A</span></td>
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
        result = parse_overview_resources(RESOURCES_HTML)
        self.assertEqual(set(result), {"Apples", "Gems"})
        self.assertNotIn("Gem Mine", result)      # buildings panel ignored
        self.assertIsNone(result.get("Oil"))      # the weapons-panel decoy is not picked up

    def test_commas_stripped(self):
        self.assertEqual(parse_overview_resources(RESOURCES_HTML)["Apples"], 1226)

    def test_row_without_icon_cell_parsed(self):
        # hideicons drops the leading <td>; the name is then the first cell.
        self.assertEqual(parse_overview_resources(RESOURCES_HTML)["Gems"], 6)

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

    def test_trailing_garbage_in_a_quantity_raises(self):
        # fullmatch, not match: "226 x" must not silently read as 226.
        html = """
<div class="panel-heading">Resources</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Apples</td><td><span>226 x</span></td></tr>
</tbody></table>
"""
        with self.assertRaises(StockpileError):
            parse_overview_resources(html)

    def test_missing_resources_panel_yields_no_rows(self):
        # The parser stays permissive; deciding an absent panel means a broken page is the
        # caller's job -- see overview.require_valid_overview.
        self.assertEqual(parse_overview_resources("<html></html>"), {})


class StockpilesTests(unittest.TestCase):
    def test_from_overview_reads_the_panel(self):
        stock = Stockpiles.from_overview(RESOURCES_HTML)
        self.assertEqual(stock["Apples"], 1226)
        self.assertEqual(stock["Gems"], 6)

    def test_absent_good_is_zero(self):
        # A good the nation holds none of is simply not rendered on the page.
        stock = Stockpiles.from_overview(RESOURCES_HTML)
        self.assertEqual(stock.get("Toys"), 0)
        self.assertEqual(stock["Toys"], 0)

    def test_contains(self):
        stock = Stockpiles.from_overview(RESOURCES_HTML)
        self.assertIn("Apples", stock)
        self.assertNotIn("Toys", stock)

    def test_as_dict_is_a_copy(self):
        stock = Stockpiles.from_overview(RESOURCES_HTML)
        snapshot = stock.as_dict()
        snapshot["Apples"] = 0
        self.assertEqual(stock["Apples"], 1226)

    def test_unknown_good_name_still_reads_zero(self):
        # Callers ask by game name; an unrecognised one is "you hold none", not an error. The
        # goods table is what decides which names are asked for.
        self.assertEqual(Stockpiles.from_overview(RESOURCES_HTML).get("Nonsense"), 0)

    def test_header_row_is_not_mistaken_for_a_resource(self):
        # The <thead> row comes through the parser like any other; it must not become a good.
        self.assertNotIn("Resource", Stockpiles.from_overview(RESOURCES_HTML).as_dict())


class TicksWorthTests(unittest.TestCase):
    def test_numeric_ticks(self):
        self.assertEqual(Stockpiles.from_overview(RESOURCES_HTML).ticks("Apples"), 87)

    def test_the_games_words_pass_through_verbatim(self):
        # "N/A" means the net is >= 0 so the stock never runs out; inventing a number would lie.
        self.assertEqual(Stockpiles.from_overview(RESOURCES_HTML).ticks("Gems"), "N/A")

    def test_absent_good_reads_as_na(self):
        # The game itself renders N/A for a good you hold none of and nothing consumes.
        self.assertEqual(Stockpiles.from_overview(RESOURCES_HTML).ticks("Toys"), "N/A")

    def test_column_found_by_heading_not_by_position(self):
        # Move Ticks-Worth to the front of the value columns and put the data in the same place.
        html = (RESOURCES_HTML
                .replace("<td>Qty</td><td>Generated</td><td>Used</td><td>Loss</td><td>Net</td>"
                         "<td>Ticks-Worth</td>",
                         "<td>Ticks-Worth</td><td>Generated</td><td>Used</td><td>Loss</td>"
                         "<td>Net</td><td>Qty</td>"))
        # Qty is now last, Ticks-Worth first -- so the first cell is the ticks value.
        self.assertEqual(Stockpiles.from_overview(html).ticks("Apples"), 1226)

    def test_no_ticks_column_means_unknown_not_zero(self):
        html = RESOURCES_HTML.replace("<td>Ticks-Worth</td>", "")
        stock = Stockpiles.from_overview(html)
        self.assertIsNone(stock.ticks_worth)
        self.assertEqual(stock["Apples"], 1226)      # quantities still read fine
        with self.assertRaises(StockpileError):
            stock.ticks("Apples")

    def test_unreadable_ticks_value_raises(self):
        html = RESOURCES_HTML.replace(
            '<span class="text-warning">87</span>', "<span>soon-ish</span>"
        )
        with self.assertRaises(StockpileError) as caught:
            Stockpiles.from_overview(html)
        self.assertIn("Ticks-Worth", str(caught.exception))

    def test_ticks_worth_is_a_copy(self):
        stock = Stockpiles.from_overview(RESOURCES_HTML)
        stock.ticks_worth["Apples"] = 0
        self.assertEqual(stock.ticks("Apples"), 87)


if __name__ == "__main__":
    unittest.main()
