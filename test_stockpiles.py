#!/usr/bin/env python3
"""Offline unit tests for stockpiles.py -- no network.

These exist to prove "lookup, not hardcode" is real. Each test moves something on the fake sheet
that a hardcoded address would have got wrong.

The overview parsing these used to cover now lives in test_goods.py and test_nation.py.
"""

import unittest

from goods import Stockpiles
from nation import NationStatus, Reading
from stockpiles import (
    DASHBOARD_LABELS,
    DASHBOARD_TAB,
    STATUS_LABELS,
    DashboardBlock,
    NationBlock,
    Report,
    as_sheet_text,
    contiguous_runs,
    locate_dashboard_block,
    locate_nation_block,
    snapshot,
    status_values,
)


# Column Q onward of a nation tab, as read by NATION_SCAN_RANGE ("Q1:W60"). Index 0 is Q.
def nation_grid(header_row=10, have_at=1, labels=None, stamp="2026-08-23 11:42:12", tail=True):
    """Build the Q..W grid: blank rows, a STOCK header, the label run, then the COST block."""
    labels = ["apple", "oil", "coffee", "mpart", "vpart", "gems"] if labels is None else labels
    grid = [["", "", "", "", "", "", ""] for _ in range(header_row - 1)]
    header = ["STOCK", "", "", "", "", "", ""]
    header[have_at] = "HAVE"
    header[6] = stamp
    grid.append(header)
    for label in labels:
        grid.append([label, "0", "", "", "", "", ""])
    if tail:
        grid.append(["", "", "", "", "", "", ""])
        grid.append(["", "", "", "", "", "", ""])
        grid.append(["COST", "Bits", "", "", "", "", ""])
        grid.append(["Copper", "200", "", "", "", "", ""])
        grid.append(["M Part", "10", "", "", "", "", ""])
    return grid


GOODS_LABELS = [
    "Energy", "Apples", "Coffee", "Oil", "Gas", "Gems", "Cider", "Pies", "Toys",
    "Tungsten", "Plastics",
    "",  # spacer, row 28
    "Drugs", "Copper", "M Parts", "V Parts", "P Parts", "Composites",
    "",  # spacer, row 35
    "Forbidden Research", "Apotheosis Serum",
    "DNA - Burro - Central", "DNA - Burro - North", "DNA - Burro - South",
    "DNA - Prze - Central", "DNA - Prze - North", "DNA - Prze - South",
    "DNA - Saddle - Central", "DNA - Saddle - North", "DNA - Saddle - South",
    "DNA - Zebrica - Central", "DNA - Zebrica - North", "DNA - Zebrica - South",
]

NATIONS = ["READ ONLY", "TOTAL", "LePone(Z)", "quaity(P)", "Pure Apple Acres(B)", "#N/A"]

#: The blank rows in the live layout. Nothing may ever be written into one of these.
SPACER_ROWS = (6, 13, 16, 28, 35)


def dashboard_grid(nations=None, labels=None, offset=0):
    """The alliance tab as read by DASHBOARD_SCAN_RANGE: row 1 nations, column A labels.

    This is the live layout as of 2026-08-24: status rows 2-5, the tick block 7-12, GDP/Bits 14-15,
    then the goods from 17. ``offset`` inserts that many blank rows below row 1, moving the whole
    block down -- which nothing should notice, because every row is found by its label.
    """
    nations = NATIONS if nations is None else nations
    labels = GOODS_LABELS if labels is None else labels
    grid = [list(nations)]
    grid.extend([[""] for _ in range(offset)])
    grid.append(["Active"])
    grid.append(["Sat"])
    grid.append(["NLR"])
    grid.append(["SE"])
    grid.append([""])          # spacer row 6
    grid.append(["Apple - tick"])
    grid.append(["Oil - tick"])
    grid.append(["Coffee - tick"])
    grid.append(["M Part - tick"])
    grid.append(["V Part - tick"])
    grid.append(["Gems - tick"])
    grid.append([""])          # spacer row 13
    grid.append(["GDP"])
    grid.append(["Bits"])
    grid.append([""])          # spacer row 16
    grid.extend([[label] for label in labels])
    return grid


class LocateNationBlockTests(unittest.TestCase):
    def test_todays_layout(self):
        block, problems = locate_nation_block(nation_grid())
        self.assertEqual(problems, [])
        self.assertEqual(block.header_row, 10)
        self.assertEqual(block.value_column, "R")
        self.assertEqual(block.timestamp_cell, "W10")
        self.assertEqual(
            block.rows,
            {"apple": 11, "oil": 12, "coffee": 13, "mpart": 14, "vpart": 15, "gems": 16},
        )

    def test_inserted_row_shifts_the_block(self):
        # Somebody adds a row above the STOCK header. A hardcoded Q11:Q16 would now write apples
        # into the oil row; the lookup just returns different rows.
        block, problems = locate_nation_block(nation_grid(header_row=12))
        self.assertEqual(problems, [])
        self.assertEqual(block.header_row, 12)
        self.assertEqual(block.rows["apple"], 13)
        self.assertEqual(block.timestamp_cell, "W12")

    def test_have_column_moved(self):
        # HAVE is found in the header row, so moving it moves the values with it.
        block, problems = locate_nation_block(nation_grid(have_at=3))
        self.assertEqual(problems, [])
        self.assertEqual(block.value_column, "T")

    def test_cost_block_below_is_not_picked_up(self):
        block, _ = locate_nation_block(nation_grid())
        self.assertNotIn("Copper", block.rows)
        self.assertNotIn("M Part", block.rows)
        self.assertNotIn("COST", block.rows)

    def test_missing_stock_header(self):
        grid = [["", "", "", "", "", "", ""] for _ in range(5)]
        block, problems = locate_nation_block(grid)
        self.assertIsNone(block)
        self.assertEqual(len(problems), 1)
        self.assertIn("STOCK", problems[0])

    def test_missing_have_header(self):
        grid = nation_grid()
        grid[9][1] = ""
        block, problems = locate_nation_block(grid)
        self.assertIsNone(block)
        self.assertIn("HAVE", problems[0])

    def test_missing_stock_label(self):
        block, problems = locate_nation_block(
            nation_grid(labels=["apple", "oil", "coffee", "mpart", "vpart"])
        )
        self.assertIsNone(block)
        self.assertEqual(len(problems), 1)
        self.assertIn("gems", problems[0])

    def test_all_missing_labels_reported_not_just_the_first(self):
        block, problems = locate_nation_block(nation_grid(labels=["apple", "oil"]))
        self.assertIsNone(block)
        self.assertEqual(len(problems), 4)

    def test_duplicate_label_in_the_run(self):
        block, problems = locate_nation_block(
            nation_grid(labels=["apple", "apple", "oil", "coffee", "mpart", "vpart", "gems"])
        )
        self.assertIsNone(block)
        self.assertTrue(any("apple" in problem and "twice" in problem for problem in problems))

    def test_run_ending_at_the_grid_edge(self):
        # No COST block below and no trailing blank: the run must end at the last row, not raise.
        block, problems = locate_nation_block(nation_grid(tail=False))
        self.assertEqual(problems, [])
        self.assertEqual(block.rows["gems"], 16)


class ContiguousRunsTests(unittest.TestCase):
    def test_single_run(self):
        self.assertEqual(
            contiguous_runs({"a": 11, "b": 12, "c": 13}),
            [[(11, "a"), (12, "b"), (13, "c")]],
        )

    def test_gaps_split_runs(self):
        self.assertEqual(
            contiguous_runs({"a": 2, "b": 3, "c": 7, "d": 8}),
            [[(2, "a"), (3, "b")], [(7, "c"), (8, "d")]],
        )

    def test_lone_row_is_its_own_run(self):
        self.assertEqual(contiguous_runs({"a": 5}), [[(5, "a")]])

    def test_unordered_input_sorted(self):
        self.assertEqual(contiguous_runs({"b": 3, "a": 2}), [[(2, "a"), (3, "b")]])

    def test_empty(self):
        self.assertEqual(contiguous_runs({}), [])


class LocateDashboardBlockTests(unittest.TestCase):
    def test_todays_layout(self):
        block, problems = locate_dashboard_block(dashboard_grid(), "LePone(Z)")
        self.assertEqual(problems, [])
        self.assertEqual(block.column, "C")
        self.assertEqual(block.rows["Active"], 2)
        self.assertEqual(block.rows["Apple - tick"], 7)
        self.assertEqual(block.rows["Gems - tick"], 12)
        self.assertEqual(block.rows["GDP"], 14)
        self.assertEqual(block.rows["Bits"], 15)
        self.assertEqual(block.rows["Energy"], 17)
        self.assertEqual(block.rows["Plastics"], 27)
        self.assertEqual(block.rows["Drugs"], 29)
        self.assertEqual(block.rows["Composites"], 34)
        self.assertEqual(block.rows["Forbidden Research"], 36)
        self.assertEqual(block.rows["DNA - Zebrica - South"], 49)

    def test_all_forty_three_labels_located(self):
        block, _ = locate_dashboard_block(dashboard_grid(), "LePone(Z)")
        self.assertEqual(len(block.rows), 43)   # 6 status + 6 tick + 31 goods
        self.assertEqual(set(block.rows), set(DASHBOARD_LABELS))

    def test_another_nations_column(self):
        block, _ = locate_dashboard_block(dashboard_grid(), "quaity(P)")
        self.assertEqual(block.column, "D")

    def test_inserted_row_shifts_every_row(self):
        block, problems = locate_dashboard_block(dashboard_grid(offset=3), "LePone(Z)")
        self.assertEqual(problems, [])
        self.assertEqual(block.rows["Active"], 5)
        self.assertEqual(block.rows["Energy"], 20)

    def test_nation_not_found_names_row_one(self):
        block, problems = locate_dashboard_block(dashboard_grid(), "Nowhere(X)")
        self.assertIsNone(block)
        self.assertEqual(len(problems), 1)
        self.assertIn("Nowhere(X)", problems[0])
        self.assertIn("LePone(Z)", problems[0])   # row 1's contents are shown

    def test_a_real_nation_never_resolves_to_the_na_spare(self):
        block, _ = locate_dashboard_block(dashboard_grid(), "LePone(Z)")
        self.assertEqual(block.column, "C")       # not F, where the "#N/A" sits

    def test_missing_label(self):
        labels = list(GOODS_LABELS)
        labels[labels.index("Toys")] = ""
        block, problems = locate_dashboard_block(dashboard_grid(labels=labels), "LePone(Z)")
        self.assertIsNone(block)
        self.assertEqual(len(problems), 1)
        self.assertIn("Toys", problems[0])

    def test_duplicate_label(self):
        labels = list(GOODS_LABELS)
        labels[labels.index("Cider")] = "Gems"
        block, problems = locate_dashboard_block(dashboard_grid(labels=labels), "LePone(Z)")
        self.assertIsNone(block)
        self.assertTrue(any("Gems" in problem for problem in problems))
        self.assertTrue(any("Cider" in problem for problem in problems))

    def test_spacer_rows_are_not_labels(self):
        block, _ = locate_dashboard_block(dashboard_grid(), "LePone(Z)")
        located = set(block.rows.values())
        for spacer in SPACER_ROWS:
            self.assertNotIn(spacer, located)

    def test_runs_from_todays_layout_skip_the_spacers(self):
        block, _ = locate_dashboard_block(dashboard_grid(), "LePone(Z)")
        spans = [(run[0][0], run[-1][0]) for run in contiguous_runs(block.rows)]
        self.assertEqual(
            spans, [(2, 5), (7, 12), (14, 15), (17, 27), (29, 34), (36, 49)]
        )


STATUS = NationStatus(
    government="Loose Despotism",
    economy="Poorly Defined",
    satisfaction=Reading(218, -5),
    se=Reading(-120, 3),
    nlr=Reading(1500, "Ascending"),
    gdp=60900,
    funds=1234567,
    server_time="2026-08-23 11:42:12",
)

STOCK = Stockpiles(
    {"Apples": 1226, "Oil": 80, "Gems": 6, "Toys": 4},
    # Ticks-Worth as the game prints it: a number, or one of its two words.
    {"Apples": 13, "Oil": "N/A", "Gems": "NONE", "Toys": 2},
)

#: A page with no Ticks-Worth column at all -- ticks unknown rather than zero.
STOCK_NO_TICKS = Stockpiles({"Apples": 1226, "Oil": 80, "Gems": 6, "Toys": 4})


class FakeSheet:
    """Stand-in for GoogleSheet: serves a canned grid per tab and records every write."""

    def __init__(self, nation_tab="LePone(Z)", nation=None, dashboard=None):
        self.grids = {
            nation_tab: nation if nation is not None else nation_grid(),
            DASHBOARD_TAB: dashboard if dashboard is not None else dashboard_grid(),
        }
        self.blocks = []   # (tab, a1, values) from write()
        self.cells = []    # (tab, a1, value) from write_cell()

    def read(self, tab, a1):
        return self.grids[tab]

    def write(self, tab, a1, values):
        self.blocks.append((tab, a1, values))
        return values

    def write_cell(self, tab, a1, value):
        self.cells.append((tab, a1, value))
        return value


class StatusValuesTests(unittest.TestCase):
    def test_every_status_label_covered(self):
        self.assertEqual(set(status_values(STATUS)), set(STATUS_LABELS))

    def test_active_is_the_server_time_forced_to_text(self):
        self.assertEqual(status_values(STATUS)["Active"], as_sheet_text("2026-08-23 11:42:12"))

    def test_readings_rendered_with_the_per_tick_in_parentheses(self):
        values = status_values(STATUS)
        self.assertEqual(values["Sat"], "218 (-5)")
        self.assertEqual(values["SE"], "-120 (3)")
        self.assertEqual(values["NLR"], "1500 (Ascending)")

    def test_gdp_and_bits_are_numbers_so_total_can_sum_them(self):
        values = status_values(STATUS)
        self.assertEqual(values["GDP"], 60900)
        self.assertEqual(values["Bits"], 1234567)
        self.assertIsInstance(values["GDP"], int)
        self.assertIsInstance(values["Bits"], int)


class SnapshotTests(unittest.TestCase):
    def run_snapshot(self, sheet):
        return snapshot(sheet, "LePone(Z)", STOCK, STATUS)

    def test_no_problems_on_todays_layout(self):
        sheet = FakeSheet()
        _report, problems = self.run_snapshot(sheet)
        self.assertEqual(problems, [])

    def test_nation_tab_values_in_sheet_row_order(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        nation_blocks = [block for block in sheet.blocks if block[0] == "LePone(Z)"]
        self.assertEqual(len(nation_blocks), 1)
        _tab, a1, values = nation_blocks[0]
        self.assertEqual(a1, "R11:R16")
        # apple, oil, coffee, mpart, vpart, gems -- the sheet's order, not the goods table's
        self.assertEqual(values, [[1226], [80], [0], [0], [0], [6]])

    def test_timestamp_written_after_the_values(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        self.assertEqual(sheet.cells, [("LePone(Z)", "W10", as_sheet_text(STATUS.server_time))])

    def test_dashboard_written_as_six_runs(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        ranges = [block[1] for block in sheet.blocks if block[0] == DASHBOARD_TAB]
        self.assertEqual(
            ranges, ["C2:C5", "C7:C12", "C14:C15", "C17:C27", "C29:C34", "C36:C49"]
        )

    def test_dashboard_status_run_payload(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        payload = dict((b[1], b[2]) for b in sheet.blocks if b[0] == DASHBOARD_TAB)
        self.assertEqual(
            payload["C2:C5"],
            [
                [as_sheet_text("2026-08-23 11:42:12")],
                ["218 (-5)"],
                ["1500 (Ascending)"],
                ["-120 (3)"],
            ],
        )
        self.assertEqual(payload["C14:C15"], [[60900], [1234567]])

    def test_dashboard_tick_run_payload(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        payload = dict((b[1], b[2]) for b in sheet.blocks if b[0] == DASHBOARD_TAB)
        # Apple, Oil, Coffee, M Part, V Part, Gems -- the sheet's order, and the game's own words
        # where it does not print a number. Coffee/M Part/V Part are not on the page at all, which
        # the game itself would render as "N/A".
        self.assertEqual(
            payload["C7:C12"], [[13], ["N/A"], ["N/A"], ["N/A"], ["N/A"], ["NONE"]]
        )

    def test_dashboard_goods_run_payload(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        payload = dict((b[1], b[2]) for b in sheet.blocks if b[0] == DASHBOARD_TAB)
        # Energy, Apples, Coffee, Oil, Gas, Gems, Cider, Pies, Toys, Tungsten, Plastics
        self.assertEqual(
            payload["C17:C27"],
            [[0], [1226], [0], [80], [0], [6], [0], [0], [4], [0], [0]],
        )

    def test_absent_good_written_as_zero(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        payload = dict((b[1], b[2]) for b in sheet.blocks if b[0] == DASHBOARD_TAB)
        self.assertEqual(payload["C36:C49"], [[0]] * 14)

    def test_no_ticks_column_leaves_the_tick_rows_alone(self):
        # "We could not read it" and "you have none" are different claims. The tick rows keep
        # whatever they held, everything else is still written, and a problem is reported.
        sheet = FakeSheet()
        _report, problems = snapshot(sheet, "LePone(Z)", STOCK_NO_TICKS, STATUS)
        ranges = [block[1] for block in sheet.blocks if block[0] == DASHBOARD_TAB]
        self.assertNotIn("C7:C12", ranges)
        self.assertIn("C2:C5", ranges)
        self.assertIn("C17:C27", ranges)
        self.assertEqual(len(problems), 1)
        self.assertIn("Ticks-Worth", problems[0])

    def test_spacer_rows_never_written(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        touched = set()
        for tab, a1, _values in sheet.blocks:
            if tab != DASHBOARD_TAB:
                continue          # the nation tab has its own rows; SPACER_ROWS is this tab's
            first, last = a1.split(":")
            touched.update(range(int(first[1:]), int(last[1:]) + 1))
        for spacer in SPACER_ROWS:
            self.assertNotIn(spacer, touched)
        self.assertTrue(touched)  # ...and it did write something, so this is not vacuously true

    def test_dashboard_problem_does_not_stop_the_nation_tab(self):
        sheet = FakeSheet(dashboard=dashboard_grid(nations=["READ ONLY", "TOTAL"]))
        _report, problems = self.run_snapshot(sheet)
        self.assertTrue(problems)
        self.assertTrue(any(block[0] == "LePone(Z)" for block in sheet.blocks))
        self.assertFalse(any(block[0] == DASHBOARD_TAB for block in sheet.blocks))

    def test_nation_problem_does_not_stop_the_dashboard(self):
        sheet = FakeSheet(nation=nation_grid(labels=["apple", "oil"]))
        _report, problems = self.run_snapshot(sheet)
        self.assertTrue(problems)
        self.assertFalse(any(block[0] == "LePone(Z)" for block in sheet.blocks))
        self.assertTrue(any(block[0] == DASHBOARD_TAB for block in sheet.blocks))

    def test_nation_problem_withholds_the_timestamp_too(self):
        # A fresh stamp over stale numbers is worse than an obviously stale one.
        sheet = FakeSheet(nation=nation_grid(labels=["apple", "oil"]))
        self.run_snapshot(sheet)
        self.assertEqual(sheet.cells, [])

    def test_junk_in_the_timestamp_cell_blocks_the_whole_nation_tab(self):
        sheet = FakeSheet(nation=nation_grid(stamp="NEED BY"))
        _report, problems = self.run_snapshot(sheet)
        self.assertTrue(any("W10" in problem for problem in problems))
        self.assertFalse(any(block[0] == "LePone(Z)" for block in sheet.blocks))
        self.assertEqual(sheet.cells, [])

    def test_empty_timestamp_cell_is_fine(self):
        sheet = FakeSheet(nation=nation_grid(stamp=""))
        _report, problems = self.run_snapshot(sheet)
        self.assertEqual(problems, [])
        self.assertEqual(len(sheet.cells), 1)

    def test_report_records_what_was_written(self):
        sheet = FakeSheet()
        report, _problems = self.run_snapshot(sheet)
        self.assertEqual(report.timestamp, "2026-08-23 11:42:12")
        self.assertEqual(len(report.nation_writes), 1)
        self.assertEqual(len(report.dashboard_writes), 6)


if __name__ == "__main__":
    unittest.main()
