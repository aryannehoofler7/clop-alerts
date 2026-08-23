#!/usr/bin/env python3
"""Offline unit tests for stockpiles.py -- no network."""

import unittest

from stockpiles import (
    as_sheet_text,
    STOCK_FIRST_ROW,
    STOCK_ROWS,
    TIMESTAMP_CELL,
    VALUE_RANGE,
    StockpileError,
    check_labels,
    desired_stock,
    parse_overview_resources,
    parse_server_time,
    snapshot,
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


EXPECTED_LABELS = ["apple", "oil", "coffee", "mpart", "vpart", "gems"]


class FakeSheet:
    """Stand-in for GoogleSheet: serves the Q label column and records every write."""

    def __init__(self, labels=None):
        self.labels = list(labels) if labels is not None else list(EXPECTED_LABELS)
        self.blocks = []   # (a1, values) from write()
        self.cells = []    # (a1, value) from write_cell()

    def read(self, tab, a1):
        return [[label] for label in self.labels]

    def write(self, tab, a1, values):
        self.blocks.append((a1, values))
        return values

    def write_cell(self, tab, a1, value):
        self.cells.append((a1, value))
        return value


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

    def test_missing_resources_panel_yields_no_rows(self):
        # The parser itself stays permissive and just reports no rows. Deciding that an absent
        # panel means a broken page is the caller's job -- see overview.require_valid_overview.
        self.assertEqual(parse_overview_resources("<html></html>"), {})

    def test_present_panel_with_no_rows_is_empty(self):
        # A nation that holds nothing renders the heading with an empty table. That is valid;
        # it is a *missing* heading that means a broken page, and sync_sheet_step guards that.
        html = '<div class="panel-heading">Resources</div><table class="table"><tbody></tbody></table>'
        self.assertEqual(parse_overview_resources(html), {})

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

    def test_negative_quantity_accepted(self):
        html = """
<div class="panel-heading">Resources</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Apples</td><td><span>-5</span></td></tr>
</tbody></table>
"""
        self.assertEqual(parse_overview_resources(html), {"Apples": -5})


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
        with self.assertRaises(StockpileError) as caught:
            parse_server_time("<html><body>Please log in.</body></html>")
        self.assertIn("Server time", str(caught.exception))


class CheckLabelsTests(unittest.TestCase):
    def test_expected_labels_have_no_problems(self):
        self.assertEqual(check_labels(FakeSheet(), "T"), [])

    def test_labels_are_case_and_space_insensitive(self):
        problems = check_labels(FakeSheet(labels=[" Apple ", "OIL", "coffee",
                                                  "mpart", "vpart", "gems"]), "T")
        self.assertEqual(problems, [])

    def test_reordered_labels_flagged_with_their_cell(self):
        problems = check_labels(FakeSheet(labels=["oil", "apple", "coffee",
                                                  "mpart", "vpart", "gems"]), "T")
        self.assertEqual(len(problems), 2)
        self.assertIn("Q11", problems[0])
        self.assertIn("'apple'", problems[0])
        self.assertIn("'oil'", problems[0])

    def test_renamed_label_flagged(self):
        problems = check_labels(FakeSheet(labels=["apple", "oil", "coffee",
                                                  "mpart", "vpart", "diamonds"]), "T")
        self.assertEqual(len(problems), 1)
        self.assertIn("Q16", problems[0])

    def test_short_grid_flagged_rather_than_crashing(self):
        problems = check_labels(FakeSheet(labels=["apple", "oil"]), "T")
        self.assertEqual(len(problems), 4)     # rows 13-16 read as blank

    def test_reads_only_the_label_column(self):
        # The R values are deliberately not read: they are overwritten regardless.
        class RecordingSheet(FakeSheet):
            def read(self, tab, a1):
                self.read_range = a1
                return super().read(tab, a1)

        sheet = RecordingSheet()
        check_labels(sheet, "T")
        self.assertEqual(sheet.read_range, "Q11:Q16")

    def test_blank_cell_is_reported_as_empty_not_as_none(self):
        class NoneCellSheet(FakeSheet):
            def read(self, tab, a1):
                return [[None] for _ in self.labels]

        problems = check_labels(NoneCellSheet(), "T")
        self.assertEqual(len(problems), 6)
        self.assertNotIn("None", problems[0])
        self.assertIn("''", problems[0])

    def test_empty_grid_flags_every_row(self):
        # A brand-new tab can come back with nothing at all.
        self.assertEqual(len(check_labels(FakeSheet(labels=[]), "T")), 6)

    def test_middle_row_mismatch_names_its_own_cell(self):
        problems = check_labels(FakeSheet(labels=["apple", "oil", "coffee",
                                                  "widgets", "vpart", "gems"]), "T")
        self.assertEqual(len(problems), 1)
        self.assertIn("Q14", problems[0])


class SnapshotTests(unittest.TestCase):
    def _resources(self):
        return parse_overview_resources(OVERVIEW_HTML)   # Apples 1226, Coffee 29, Gems 6

    def test_writes_the_value_block_and_the_timestamp(self):
        sheet = FakeSheet()
        written = snapshot(sheet, "T", self._resources(), "2026-08-23 03:23:44")
        self.assertEqual(
            sheet.blocks,
            [(VALUE_RANGE, [[1226], [0], [29], [0], [0], [6]])],
        )
        # Apostrophe-prefixed: Sheets consumes it and stores the stamp as text rather than
        # parsing it into a date in the spreadsheet's timezone. See as_sheet_text.
        self.assertEqual(sheet.cells, [(TIMESTAMP_CELL, "'2026-08-23 03:23:44")])
        self.assertEqual(
            written,
            [("apple", 1226), ("oil", 0), ("coffee", 29),
             ("mpart", 0), ("vpart", 0), ("gems", 6)],
        )

    def test_a_good_no_longer_held_is_written_back_to_zero(self):
        sheet = FakeSheet()
        snapshot(sheet, "T", {}, "2026-08-23 03:23:44")
        self.assertEqual(sheet.blocks, [(VALUE_RANGE, [[0], [0], [0], [0], [0], [0]])])

    def test_nothing_is_read(self):
        # The snapshot overwrites unconditionally, so it must not depend on the sheet's contents.
        class NoReadSheet(FakeSheet):
            def read(self, tab, a1):
                raise AssertionError("snapshot must not read the sheet")

        snapshot(NoReadSheet(), "T", self._resources(), "2026-08-23 03:23:44")

    def test_timestamp_written_last(self):
        # W10 claims the numbers beside it are fresh, so it must never land before they do.
        sheet = FakeSheet()
        order = []
        sheet.write = lambda tab, a1, values: order.append("values")
        sheet.write_cell = lambda tab, a1, value: order.append("stamp")
        snapshot(sheet, "T", self._resources(), "2026-08-23 03:23:44")
        self.assertEqual(order, ["values", "stamp"])


class AsSheetTextTests(unittest.TestCase):
    """The stamp must reach the sheet as text, not as a date.

    Verified against the live sheet: written bare, ``2026-08-23 07:12:30`` came back as
    ``2026-08-23T07:12:30.000Z`` -- Sheets had parsed it into a date value, reinterpreting the
    game's clock in the spreadsheet's timezone. That is exactly the assumption this feature
    refuses to make anywhere else, so it must not make it here either.
    """

    def test_prefixes_with_the_force_text_marker(self):
        self.assertEqual(as_sheet_text("2026-08-23 07:12:30"), "'2026-08-23 07:12:30")

    def test_snapshot_uses_it(self):
        sheet = FakeSheet()
        snapshot(sheet, "T", {}, "2026-08-23 07:12:30")
        self.assertEqual(sheet.cells, [(TIMESTAMP_CELL, "'2026-08-23 07:12:30")])


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
