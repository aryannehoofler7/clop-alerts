#!/usr/bin/env python3
"""Offline unit tests for buildings.py and building_map.py -- no network."""

import contextlib
import io
import unittest

import buildings
from building_map import GAME_TO_SHEET, SHEET_BUILDINGS
from stockpiles import DASHBOARD_TAB
from buildings import (
    COLUMN_SCAN_RANGE,
    BuildingError,
    Correction,
    desired_counts,
    locate_regions,
    parse_overview_buildings,
    read_columns,
    reconcile,
    sanity_check,
)


# A minimal overview.php: a decoy resources panel with the same row shape, then the Buildings panel.
# The Resources panel carries its real <thead>, because that header row is how the Ticks-Worth
# column is located -- see goods._parse_resources_panel.
OVERVIEW_HTML = """
<div class="panel-heading">Resources</div>
<table class="table">
<thead><tr><td></td><td style="text-align: right;">Resource</td>
  <td>Qty</td><td>Generated</td><td>Used</td><td>Loss</td><td>Net</td><td>Ticks-Worth</td>
</tr></thead>
<tbody>
  <tr><td style="text-align: right;">Copper</td><td><span class="text-success">201</span></td>
      <td><span>0</span></td><td><span>0</span></td><td><span>0</span></td><td><span>0</span></td>
      <td><span class="text-success">N/A</span></td></tr>
  <tr><td style="text-align: right;">Apples</td><td><span class="text-success">1,226</span></td>
      <td><span>0</span></td><td><span>14</span></td><td><span>0</span></td><td><span>-14</span></td>
      <td><span class="text-warning">87</span></td></tr>
  <tr><td style="text-align: right;">Gems</td><td><span class="text-success">6</span></td>
      <td><span>0</span></td><td><span>1</span></td><td><span>0</span></td><td><span>-1</span></td>
      <td><span class="text-warning">6</span></td></tr>
</tbody></table>
<div class="panel-heading">Buildings</div>
<table class="table"><tbody>
  <tr>
    <td style="text-align: right;">Bakery</td>
    <td><span class="text-success">2 </span></td>
    <td><form><input name="recycle"/></form></td>
  </tr>
  <tr>
    <td style="text-align: right;">Basic Copper Mine</td>
    <td><span class="text-success">10 (1 disabled)</span></td>
    <td><form><span class="input-group-btn">x</span></form></td>
  </tr>
  <tr>
    <td style="text-align: right;">Gem Mine</td>
    <td><span class="text-success">1</span></td>
  </tr>
</tbody></table>
<div class="panel-heading">Weapons</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">PRC-E8</td><td><span>1</span></td></tr>
</tbody></table>
"""


#: The alliance tab's column A, in the live order: the sheet's own "Ticks Since Recorded" formula
#: row, four status rows, the six "- tick" rows, GDP/Bits, then the 31 goods -- with a blank spacer
#: between each block. Nation "T" is column C.
DASHBOARD_COLUMN_A = (
    ["Ticks Since Recorded", "Active", "Sat", "NLR", "SE", ""]
    + ["Apple - tick", "Oil - tick", "Coffee - tick",
       "M Part - tick", "V Part - tick", "Gems - tick", ""]
    + ["GDP", "Bits", ""]
    + ["Energy", "Apples", "Coffee", "Oil", "Gas", "Gems", "Cider", "Pies", "Toys",
       "Tungsten", "Plastics", "",
       "Drugs", "Copper", "M Parts", "V Parts", "P Parts", "Composites", "",
       "Forbidden Research", "Apotheosis Serum",
       "DNA - Burro - Central", "DNA - Burro - North", "DNA - Burro - South",
       "DNA - Prze - Central", "DNA - Prze - North", "DNA - Prze - South",
       "DNA - Saddle - Central", "DNA - Saddle - North", "DNA - Saddle - South",
       "DNA - Zebrica - Central", "DNA - Zebrica - North", "DNA - Zebrica - South"]
)


def dashboard_grid(nations=("2026-08-25 10:19:48", "TOTAL (or min)", "T", "other(P)")):
    """The alliance tab as read by DASHBOARD_SCAN_RANGE: row 1 nations, column A labels."""
    return [list(nations)] + [[label] for label in DASHBOARD_COLUMN_A]


def sheet_column_a():
    """A synthetic column A: header, a few have rows, DISABLED marker, a few disabled rows."""
    col = [""] * 130
    col[7] = "Building"          # row 8 header
    have = {9: "Basic Mine", 10: "Mecha Mine", 11: "Gem Mine", 12: "Tungsten Mine",
            32: "Bakery", 23: "Cider Facility"}
    for row, name in have.items():
        col[row - 1] = name
    col[54] = "DISABLED:"        # row 55 marker
    col[56] = "Building"         # row 57 disabled header
    dis = {58: "Basic Mine", 59: "Mecha Mine", 60: "Gem Mine", 61: "Tungsten Mine",
           81: "Bakery", 72: "Cider Facility"}
    for row, name in dis.items():
        col[row - 1] = name
    return col


class FakeSheet:
    """Stand-in for GoogleSheet across all three ranges sync_sheet_step reads.

    ``A1:B130`` on the nation tab is the buildings grid, ``Q1:W60`` its STOCK block, and
    ``A1:Z60`` on the Dashboard the alliance-wide grid. ``blocks`` and ``writes`` stay keyed by A1
    alone -- the nation tab and the Dashboard never share a range, so nothing is ambiguous.
    """

    def __init__(self, column_a, column_b, stock_labels=None, dashboard=None):
        self._a = list(column_a)
        self._b = list(column_b)
        self._labels = list(stock_labels) if stock_labels is not None else [
            "apple", "oil", "coffee", "mpart", "vpart", "gems"
        ]
        self._dashboard = dashboard if dashboard is not None else dashboard_grid()
        self.writes = []   # (a1, value) from write_cell
        self.blocks = []   # (a1, values) from write
        self.reads = []    # (tab, a1) -- one entry per logical read
        self.requests = []  # one entry per ROUND TRIP to the endpoint; a batch counts once

    def batch(self, ops):
        """Run several ops in one request, as the real endpoint's `batch` action does."""
        self.requests.append(("batch", len(ops)))
        results = []
        for op in ops:
            if op["action"] == "read":
                self.reads.append((op["tab"], op["range"]))
                results.append(self._read(op["tab"], op["range"]))
            else:
                self._write(op["tab"], op["range"], op["values"])
                results.append(op["values"])
        return results

    def read(self, tab, a1):
        self.requests.append(("read", tab, a1))
        self.reads.append((tab, a1))
        return self._read(tab, a1)

    def _read(self, tab, a1):
        if tab == DASHBOARD_TAB:
            return self._dashboard
        if a1.startswith("Q"):
            # Q..W: the STOCK header at row 10 with HAVE beside it and the stamp in W, then the
            # six labels. Mirrors the live nation tab.
            grid = [["", "", "", "", "", "", ""] for _ in range(9)]
            grid.append(["STOCK", "HAVE", "NEED", "BUY", "", "", "2026-08-23 03:23:44"])
            for label in self._labels:
                grid.append([label, "", "", "", "", "", ""])
            return grid
        n = max(len(self._a), len(self._b))
        return [[self._a[i] if i < len(self._a) else "",
                 self._b[i] if i < len(self._b) else ""] for i in range(n)]

    def write(self, tab, a1, values):
        self.requests.append(("write", tab, a1))
        self._write(tab, a1, values)
        return values

    def write_cell(self, tab, a1, value):
        self.requests.append(("write", tab, a1))
        self._write(tab, a1, [[value]])
        return value

    def _write(self, tab, a1, values):
        """Apply a write without counting a round trip -- shared by write, write_cell and batch."""
        flat = [row[0] for row in values] if values and isinstance(values[0], list) else values
        if len(flat) == 1 and ":" not in a1:
            self.writes.append((a1, flat[0]))
            if a1.startswith("B"):
                row = int(a1[1:]) - 1
                while row >= len(self._b):
                    self._b.append("")
                self._b[row] = flat[0]
        else:
            self.blocks.append((a1, values))


def building_writes(sheet):
    """The column-B writes only -- the stockpile snapshot also writes W10."""
    return [(a1, value) for a1, value in sheet.writes if a1.startswith("B")]


class ParserTests(unittest.TestCase):
    def test_unreadable_count_raises_rather_than_dropping_the_row(self):
        # Dropping it would reconcile the building to 0, overwrite a correct cell, and report it
        # as an ordinary correction -- a silent wrong write dressed up as routine.
        html = ('<div class="panel-heading">Buildings</div>'
                '<table class="table"><tbody>'
                '<tr><td style="text-align: right;">Bakery</td><td><span>N/A</span></td></tr>'
                '</tbody></table>')
        with self.assertRaises(BuildingError) as caught:
            parse_overview_buildings(html)
        self.assertIn("Bakery", str(caught.exception))

    def test_only_buildings_panel_parsed(self):
        result = parse_overview_buildings(OVERVIEW_HTML)
        self.assertEqual(set(result), {"Bakery", "Basic Copper Mine", "Gem Mine"})
        self.assertNotIn("Copper", result)   # resources panel ignored
        self.assertNotIn("PRC-E8", result)   # weapons panel ignored

    def test_have_and_disabled_extracted(self):
        result = parse_overview_buildings(OVERVIEW_HTML)
        self.assertEqual(result["Basic Copper Mine"], (10, 1))
        self.assertEqual(result["Bakery"], (2, 0))
        self.assertEqual(result["Gem Mine"], (1, 0))

    def test_unknown_heading_yields_no_rows(self):
        from overview import parse_panel

        self.assertEqual(parse_panel(OVERVIEW_HTML, "Nonexistent Panel"), [])


class DesiredCountsTests(unittest.TestCase):
    def test_unowned_buildings_default_to_zero(self):
        desired = desired_counts({})
        self.assertEqual(set(desired), set(SHEET_BUILDINGS))
        self.assertTrue(all(v == (0, 0) for v in desired.values()))

    def test_rename_and_disabled_folded(self):
        desired = desired_counts({"Basic Copper Mine": (10, 1)})
        self.assertEqual(desired["Basic Mine"], (10, 1))

    def test_dna_group_summed(self):
        overview = {
            "DNA Extraction Facility - N. Zebrica": (3, 1),
            "DNA Extraction Facility - C. Zebrica": (2, 0),
        }
        self.assertEqual(desired_counts(overview)["DNA"], (5, 1))

    def test_energy_collector_group_summed(self):
        overview = {"Solar Collector": (2, 0), "Tidal Generator": (1, 1)}
        self.assertEqual(desired_counts(overview)["Energy Collector"], (3, 1))

    def test_unknown_building_raises_when_strict(self):
        with self.assertRaises(BuildingError):
            desired_counts({"Quantum Stable": (1, 0)})

    def test_unknown_building_skipped_when_not_strict(self):
        desired = desired_counts({"Quantum Stable": (1, 0)}, strict=False)
        self.assertTrue(all(v == (0, 0) for v in desired.values()))


class LocateRegionsTests(unittest.TestCase):
    def test_have_and_disabled_rows_found_by_name(self):
        regions = locate_regions(sheet_column_a())
        self.assertEqual(regions.have_header, 7)
        self.assertEqual(regions.disabled_marker, 54)
        self.assertEqual(regions.have_rows["Basic Mine"], 9)
        self.assertEqual(regions.disabled_rows["Basic Mine"], 58)
        self.assertEqual(regions.have_rows["Bakery"], 32)
        self.assertEqual(regions.disabled_rows["Bakery"], 81)


class ReconcileTests(unittest.TestCase):
    def _sheet(self):
        col_a = sheet_column_a()
        col_b = [""] * 130
        col_b[8] = 8      # B9  Basic Mine have = 8 (overview says 10)
        col_b[10] = 1     # B11 Gem Mine have = 1 (matches)
        col_b[31] = 2     # B32 Bakery have = 2 (matches)
        col_b[22] = 2     # B23 Cider Facility have = 2 (matches, unowned in this overview -> should go 0)
        return FakeSheet(col_a, col_b)

    def test_writes_only_changed_cells(self):
        sheet = self._sheet()
        overview = {"Basic Copper Mine": (10, 1), "Gem Mine": (1, 0), "Bakery": (2, 0)}
        corrections = reconcile(sheet, "T", overview)
        written = dict(sheet.writes)
        # Basic Mine have 8->10 and its disabled ''->1; Cider Facility have 2->0 (no longer owned).
        self.assertEqual(written.get("B9"), 10)
        self.assertEqual(written.get("B58"), 1)
        self.assertEqual(written.get("B23"), 0)
        # Matching cells are left alone.
        self.assertNotIn("B11", written)
        self.assertNotIn("B32", written)

    def test_corrections_reported(self):
        sheet = self._sheet()
        overview = {"Basic Copper Mine": (10, 1), "Gem Mine": (1, 0), "Bakery": (2, 0)}
        corrections = reconcile(sheet, "T", overview)
        described = {c.describe() for c in corrections}
        self.assertIn("Basic Mine have 8 -> 10", described)
        self.assertIn("Basic Mine disabled 0 -> 1", described)
        self.assertIn("Cider Facility have 2 -> 0", described)

    def test_no_changes_no_writes(self):
        col_a = sheet_column_a()
        col_b = [""] * 130
        col_b[8] = 1      # Basic Mine already 1
        sheet = FakeSheet(col_a, col_b)
        corrections = reconcile(sheet, "T", {"Basic Copper Mine": (1, 0)})
        self.assertEqual(corrections, [])
        self.assertEqual(sheet.writes, [])


class SanityCheckTests(unittest.TestCase):
    def test_clean_sheet_has_no_problems(self):
        sheet = FakeSheet(sheet_column_a(), [""] * 130)
        overview = {"Basic Copper Mine": (10, 1), "Bakery": (2, 0), "Gem Mine": (1, 0)}
        # This synthetic column A only has a handful of building rows, so most are "missing".
        problems = sanity_check(sheet, "T", overview)
        # The buildings present should NOT be flagged; the missing ones should.
        self.assertFalse(any("Basic Mine'" in p and "missing" in p for p in problems))

    def test_missing_building_flagged(self):
        col_a = sheet_column_a()
        col_a[8] = ""   # remove Basic Mine from the have region
        sheet = FakeSheet(col_a, [""] * 130)
        problems = sanity_check(sheet, "T", {})
        self.assertTrue(any("Basic Mine" in p and "missing" in p for p in problems))

    def test_duplicate_building_flagged(self):
        col_a = sheet_column_a()
        col_a[13] = "Bakery"   # a second Bakery in the have region
        sheet = FakeSheet(col_a, [""] * 130)
        problems = sanity_check(sheet, "T", {})
        self.assertTrue(any("Bakery" in p and "appears" in p for p in problems))

    def test_unmapped_overview_building_flagged(self):
        sheet = FakeSheet(sheet_column_a(), [""] * 130)
        problems = sanity_check(sheet, "T", {"Quantum Stable": (1, 0)})
        self.assertTrue(any("Quantum Stable" in p and "mapping" in p for p in problems))

    def test_missing_marker_flagged(self):
        col_a = sheet_column_a()
        col_a[54] = ""   # remove the DISABLED marker
        sheet = FakeSheet(col_a, [""] * 130)
        problems = sanity_check(sheet, "T", {})
        self.assertTrue(any("DISABLED" in p for p in problems))

    def test_disabled_without_row_flagged(self):
        # Alicornification has no disabled row in the real sheet; simulate one with a disabled count.
        col_a = [""] * 130
        col_a[7] = "Building"
        col_a[8] = "Alicornification"
        col_a[54] = "DISABLED:"
        sheet = FakeSheet(col_a, [""] * 130)
        problems = sanity_check(sheet, "T", {"Alicornification Facility": (1, 1)})
        self.assertTrue(any("Alicornification" in p and "disabled region" in p for p in problems))


class MappingIntegrityTests(unittest.TestCase):
    def test_sheet_buildings_are_the_mapping_values(self):
        self.assertEqual(SHEET_BUILDINGS, frozenset(GAME_TO_SHEET.values()))

    def test_no_blank_names(self):
        for game, sheet in GAME_TO_SHEET.items():
            self.assertTrue(game.strip())
            self.assertTrue(sheet.strip())

    def test_dna_group_size(self):
        dna = [g for g, s in GAME_TO_SHEET.items() if s == "DNA"]
        self.assertEqual(len(dna), 12)

    def test_energy_group(self):
        energy = {g for g, s in GAME_TO_SHEET.items() if s == "Energy Collector"}
        self.assertEqual(energy, {"Solar Collector", "Tidal Generator"})


# The Nation panel sync_sheet_step needs for the Dashboard's status rows.
NATION_PANEL_HTML = """
<div class="panel-heading">Nation</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Government Type</td><td>Loose Despotism</td></tr>
  <tr><td style="text-align: right;">Economic Type</td><td>Poorly Defined</td></tr>
  <tr><td style="text-align: right;">Relationship with Solar Empire</td>
      <td><span>-120</span> (<span>3</span> per tick)</td></tr>
  <tr><td style="text-align: right;">Relationship with New Lunar Republic</td>
      <td><span>1500</span> (Ascending)</td></tr>
  <tr><td style="text-align: right;">Satisfaction</td>
      <td><span>218</span> (<span>-5</span> per tick)</td></tr>
  <tr><td style="text-align: right;">GDP</td><td><span>60,900</span> bits per tick</td></tr>
  <tr><td style="text-align: right;">Funds</td><td><span>1,234,567</span> bits</td></tr>
</tbody></table>
"""

LOGGED_IN_OVERVIEW = (
    '<a href="logout.php">Logout</a><li><a>Server time: 2026-08-23 03:23:44</a></li>'
    + NATION_PANEL_HTML
    + OVERVIEW_HTML
    + "</html>"
)
LOGGED_OUT_OVERVIEW = '<a href="login.php">Login</a>' + OVERVIEW_HTML


def full_column_a():
    """A column A holding every sheet building once in the have region and once in disabled."""
    names = sorted(SHEET_BUILDINGS)
    col_a = [""] * 130
    col_a[7] = "Building"          # row 8 have header
    for i, name in enumerate(names):
        col_a[8 + i] = name        # rows 9..
    col_a[54] = "DISABLED:"        # row 55 marker
    col_a[56] = "Building"         # row 57 disabled header
    for i, name in enumerate(names):
        col_a[57 + i] = name       # rows 58..
    return col_a, names


class FakeClient:
    def __init__(self, html):
        self._html = html
        self.logins = 0

    def _open(self, path):
        return self._html

    def login(self):
        self.logins += 1


class FakeNotifier:
    def __init__(self):
        self.alerts = []
        self.failures = []

    def notify(self, message):
        self.alerts.append(message)
        return False

    def notify_failure(self, message):
        self.failures.append(message)
        return False


class SharedColumnReadTests(unittest.TestCase):
    def test_passing_columns_in_costs_no_round_trip(self):
        col_a, names = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130)
        columns = read_columns(sheet, "T")
        self.assertEqual(len(sheet.reads), 1)
        sanity_check(sheet, "T", {}, columns)
        reconcile(sheet, "T", {}, columns)
        self.assertEqual(len(sheet.reads), 1)

    def test_omitting_columns_still_reads_for_itself(self):
        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130)
        sanity_check(sheet, "T", {})
        self.assertEqual(len(sheet.reads), 1)

    def test_a_whole_sync_costs_two_round_trips(self):
        # The number that matters for reliability: every request is an independent chance to hit
        # Google's expiring-result-link fault, and this sync used to make fifteen of them.
        from clop_monitor import sync_sheet_step

        col_a, names = full_column_a()
        col_b = [""] * 130
        col_b[8 + names.index("Basic Mine")] = 8      # force building corrections too
        sheet = FakeSheet(col_a, col_b)
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", FakeNotifier())

        self.assertEqual(len(sheet.requests), 2, sheet.requests)
        self.assertEqual([kind for kind, _ in sheet.requests], ["batch", "batch"])
        # ...and the work itself is unchanged: still three reads and every write.
        self.assertEqual(len(sheet.reads), 3)
        self.assertTrue(sheet.writes)
        self.assertTrue(sheet.blocks)

    def test_nothing_is_written_before_both_steps_have_run(self):
        # Writes are held until the end, so a failure part-way leaves the sheet untouched rather
        # than half-updated.
        from clop_monitor import sync_sheet_step

        col_a, names = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130)
        seen = []
        real = sheet.batch

        def watched(ops):
            seen.append([op["action"] for op in ops])
            return real(ops)

        sheet.batch = watched
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", FakeNotifier())
        self.assertEqual(seen[0], ["read", "read", "read"])
        self.assertTrue(all(action == "write" for action in seen[1]))

    def test_sync_step_reads_the_buildings_range_once(self):
        # Two separate reads of the same range used to run back-to-back every poll. Beyond the
        # wasted execution, the check and the write judged different snapshots of the sheet.
        from clop_monitor import sync_sheet_step

        col_a, names = full_column_a()
        col_b = [""] * 130
        col_b[8 + names.index("Basic Mine")] = 8
        sheet = FakeSheet(col_a, col_b)
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", FakeNotifier())
        buildings_reads = [r for r in sheet.reads if r[1].startswith("A1:B")]
        self.assertEqual(buildings_reads, [("T", COLUMN_SCAN_RANGE)])


class SyncCacheTests(unittest.TestCase):
    """The sheet is touched when the game's numbers move, not once a minute regardless."""

    def _sheet_in_sync(self):
        # A sheet that already matches LOGGED_IN_OVERVIEW, so a first sync writes nothing but
        # still reads, and returns a digest to cache.
        col_a, names = full_column_a()
        col_b = [""] * 130
        col_b[8 + names.index("Bakery")] = 2
        col_b[8 + names.index("Basic Mine")] = 10
        col_b[8 + names.index("Gem Mine")] = 1
        col_b[57 + names.index("Basic Mine")] = 1
        return FakeSheet(col_a, col_b)

    def _sync(self, sheet, notifier=None, last=None):
        from clop_monitor import sync_sheet_step

        return sync_sheet_step(
            FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier or FakeNotifier(), None, None, last
        )

    def test_first_poll_writes_everything_and_returns_a_digest(self):
        sheet = self._sheet_in_sync()
        digest = self._sync(sheet)
        self.assertIsNotNone(digest)
        self.assertTrue(sheet.reads)
        self.assertTrue(sheet.blocks)

    def test_second_poll_with_identical_data_touches_nothing(self):
        sheet = self._sheet_in_sync()
        digest = self._sync(sheet)
        sheet.reads.clear(); sheet.writes.clear(); sheet.blocks.clear()
        again = self._sync(sheet, last=digest)
        self.assertEqual(again, digest)
        self.assertEqual(sheet.reads, [])
        self.assertEqual(sheet.writes, [])
        self.assertEqual(sheet.blocks, [])

    def test_the_server_clock_alone_does_not_count_as_a_change(self):
        # The trap that would silently disable the whole cache: server_time advances every poll,
        # so including it in the digest would make every poll look like a change.
        sheet = self._sheet_in_sync()
        digest = self._sync(sheet)
        later = LOGGED_IN_OVERVIEW.replace("2026-08-23 03:23:44", "2026-08-23 05:59:59")
        self.assertIn("05:59:59", later)   # the fixture really did change

        from clop_monitor import sync_sheet_step

        sheet.reads.clear(); sheet.writes.clear(); sheet.blocks.clear()
        again = sync_sheet_step(
            FakeClient(later), sheet, "T", FakeNotifier(), None, None, digest
        )
        self.assertEqual(again, digest)
        self.assertEqual(sheet.reads, [])

    def test_a_changed_quantity_resyncs(self):
        sheet = self._sheet_in_sync()
        digest = self._sync(sheet)
        traded = LOGGED_IN_OVERVIEW.replace(">1,226<", ">1,190<")
        self.assertNotEqual(traded, LOGGED_IN_OVERVIEW)   # the fixture really did change

        from clop_monitor import sync_sheet_step

        sheet.reads.clear(); sheet.blocks.clear()
        again = sync_sheet_step(
            FakeClient(traded), sheet, "T", FakeNotifier(), None, None, digest
        )
        self.assertNotEqual(again, digest)
        self.assertTrue(sheet.blocks)
        self.assertEqual(dict(sheet.blocks)["R11:R16"][0], [1190])

    def test_a_failed_sync_is_not_cached(self):
        # Otherwise one bad poll would convince the monitor the sheet was fine indefinitely.
        class Broken(FakeSheet):
            """Dead endpoint. Both entry points fail, because the sync uses batch now and a
            double that only breaks read() would let a 'failed' sync quietly succeed."""

            def batch(self, ops):
                from sheets import SheetError

                raise SheetError("could not reach sheet endpoint: boom")

            def read(self, tab, a1):
                from sheets import SheetError

                raise SheetError("could not reach sheet endpoint: boom")

        col_a, _ = full_column_a()
        notifier = FakeNotifier()
        self.assertIsNone(self._sync(Broken(col_a, [""] * 130), notifier, last=None))
        self.assertTrue(notifier.failures)

    def test_a_skipped_region_is_not_cached(self):
        # A layout problem means the sheet still disagrees with the game; caching that as "synced"
        # would bury the disagreement until the numbers happened to move.
        col_a, _ = full_column_a()
        col_a[3] = "not the Building header"
        broken_labels = FakeSheet(col_a, [""] * 130, stock_labels=["apple", "oil"])
        notifier = FakeNotifier()
        self.assertIsNone(self._sync(broken_labels, notifier))
        self.assertTrue(notifier.failures)


class SyncSheetStepTests(unittest.TestCase):
    def test_corrections_alert_and_write(self):
        from clop_monitor import AlertCategorySettings, sync_sheet_step

        col_a, names = full_column_a()
        col_b = [""] * 130
        col_b[8 + names.index("Basic Mine")] = 8   # overview says 10 (1 disabled)
        sheet = FakeSheet(col_a, col_b)
        notifier = FakeNotifier()
        sync_sheet_step(
            FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier,
            alerts=AlertCategorySettings(building_corrections=True),
        )
        self.assertEqual(len(notifier.alerts), 1)
        self.assertIn("Basic Mine have 8 -> 10", notifier.alerts[0])
        self.assertEqual(notifier.failures, [])
        self.assertTrue(building_writes(sheet))

    def test_a_correction_is_written_and_printed_but_does_not_pop_up(self):
        # The shipped default. A dialog is modal, so a correction the player caused a minute ago by
        # building something would stop the monitor until it was dismissed.
        from clop_monitor import sync_sheet_step

        col_a, names = full_column_a()
        col_b = [""] * 130
        col_b[8 + names.index("Basic Mine")] = 8   # overview says 10 (1 disabled)
        sheet = FakeSheet(col_a, col_b)
        notifier = FakeNotifier()
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        self.assertEqual(notifier.alerts, [])
        self.assertEqual(notifier.failures, [])
        # The cells are still corrected -- only the reporting channel changed.
        self.assertTrue(building_writes(sheet))
        # ...and the drift is still on the record, because the scrollback is now the only place
        # it appears at all.
        self.assertIn("Basic Mine have 8 -> 10", printed.getvalue())

    def test_a_poll_that_corrects_nothing_prints_nothing(self):
        from clop_monitor import sync_sheet_step

        col_a, names = full_column_a()
        col_b = [""] * 130
        col_b[8 + names.index("Bakery")] = 2
        col_b[8 + names.index("Basic Mine")] = 10
        col_b[8 + names.index("Gem Mine")] = 1
        col_b[57 + names.index("Basic Mine")] = 1
        sheet = FakeSheet(col_a, col_b)
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", FakeNotifier())
        self.assertNotIn("corrected", printed.getvalue())

    def test_switching_the_alert_on_pops_up_instead_of_printing(self):
        # Both channels would double every correction in the scrollback of whoever opted in.
        from clop_monitor import AlertCategorySettings, sync_sheet_step

        col_a, names = full_column_a()
        col_b = [""] * 130
        col_b[8 + names.index("Basic Mine")] = 8
        sheet = FakeSheet(col_a, col_b)
        notifier = FakeNotifier()
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            sync_sheet_step(
                FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier,
                alerts=AlertCategorySettings(building_corrections=True),
            )
        self.assertIn("Basic Mine have 8 -> 10", notifier.alerts[0])
        self.assertNotIn("Basic Mine have 8 -> 10", printed.getvalue())

    def test_a_layout_problem_still_pops_up_with_corrections_silenced(self):
        # Not a correction: it means cells were NOT written, so the sheet is knowingly left
        # disagreeing with the game. That is the state worth interrupting for.
        from clop_monitor import sync_sheet_step

        col_a, _ = full_column_a()
        col_a[7] = "not the Building header"      # row 8 was the header
        sheet = FakeSheet(col_a, [""] * 130)
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        self.assertEqual(len(notifier.failures), 1)
        self.assertIn("Building sync skipped", notifier.failures[0])
        self.assertEqual(building_writes(sheet), [])

    def test_stockpile_snapshot_is_stamped_but_never_popped_up(self):
        from clop_monitor import sync_sheet_step

        # A sheet where the buildings already match, so nothing but the snapshot can speak up.
        # OVERVIEW_HTML owns Bakery 2, Basic Copper Mine 10 (1 disabled), Gem Mine 1; every other
        # building is 0 on both sides ('' normalises to 0).
        col_a, names = full_column_a()
        col_b = [""] * 130
        col_b[8 + names.index("Bakery")] = 2
        col_b[8 + names.index("Basic Mine")] = 10
        col_b[8 + names.index("Gem Mine")] = 1
        col_b[57 + names.index("Basic Mine")] = 1     # its disabled row
        sheet = FakeSheet(col_a, col_b)
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        # The real quantities must reach the block -- not zeros, and not the buildings dict.
        blocks = dict(sheet.blocks)
        self.assertEqual(blocks["R11:R16"], [[1226], [0], [0], [0], [0], [6]])
        self.assertIn(("W10", "'2026-08-23 03:23:44"), sheet.writes)
        # ...and the same numbers reach the Dashboard, alongside the status rows.
        self.assertEqual(
            blocks["C3:C6"],
            [["'2026-08-23 03:23:44"], ["218 (-5)"], ["1500 (Ascending)"], ["-120 (3)"]],
        )
        self.assertEqual(blocks["C15:C16"], [[60900], [1234567]])
        # Apple, Oil, Coffee, M Part, V Part, Gems -- the Ticks-Worth column, the game's own words
        # where it prints one. Only Apples and Gems are on this page; the rest read as "N/A".
        self.assertEqual(
            blocks["C8:C13"], [[87], ["N/A"], ["N/A"], ["N/A"], ["N/A"], [6]]
        )
        # Energy, Apples, Coffee, Oil, Gas, Gems, Cider, Pies, Toys, Tungsten, Plastics
        self.assertEqual(
            blocks["C18:C28"], [[0], [1226], [0], [0], [0], [6], [0], [0], [0], [0], [0]]
        )
        # A routine snapshot is a scheduled refresh, not an event: no popup for it.
        self.assertEqual(notifier.failures, [])
        self.assertEqual(notifier.alerts, [])

    def test_bad_stock_labels_warn_and_write_no_stock_cells(self):
        from clop_monitor import sync_sheet_step

        # A label the lookup cannot find at all. Reordering no longer breaks anything -- rows are
        # found by name -- so the failure worth testing is a label that is simply gone.
        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130,
                          stock_labels=["apple", "oil", "coffee", "mpart", "vpart", "rocks"])
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        self.assertEqual(len(notifier.failures), 1)
        self.assertIn("gems", notifier.failures[0])
        self.assertEqual([a1 for a1, _ in sheet.blocks if a1.startswith("R")], [])
        self.assertNotIn("W10", [a1 for a1, _ in sheet.writes])         # and no stamp
        # The Dashboard is a different region and still updates.
        self.assertTrue([a1 for a1, _ in sheet.blocks if a1.startswith("C")])

    def test_reordered_stock_labels_are_followed_not_rejected(self):
        from clop_monitor import sync_sheet_step

        # The labels are all there, just in a different order. The old positional block would have
        # refused to write; the lookup writes each good to wherever its label now sits.
        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130,
                          stock_labels=["oil", "apple", "coffee", "mpart", "vpart", "gems"])
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        self.assertEqual(notifier.failures, [])
        # Oil first now, then apples: 0 then 1226, not the other way round.
        self.assertEqual(dict(sheet.blocks)["R11:R16"], [[0], [1226], [0], [0], [0], [6]])

    def test_missing_dashboard_column_warns_and_writes_no_dashboard_cells(self):
        from clop_monitor import sync_sheet_step

        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130,
                          dashboard=dashboard_grid(
                              nations=("2026-08-25 10:19:48", "TOTAL (or min)", "somebody(P)")))
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        self.assertEqual(len(notifier.failures), 1)
        self.assertIn("no column in row 1", notifier.failures[0])
        self.assertIn("somebody(P)", notifier.failures[0])   # row 1 is shown, to fix it by
        self.assertEqual([a1 for a1, _ in sheet.blocks if a1.startswith("C")], [])
        # The nation tab is a different region and still updates.
        self.assertIn("R11:R16", [a1 for a1, _ in sheet.blocks])

    def test_sanity_failure_warns_and_writes_no_building_cells(self):
        from clop_monitor import sync_sheet_step

        sheet = FakeSheet(sheet_column_a(), [""] * 130)  # most buildings missing -> sanity fails
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        self.assertEqual(notifier.alerts, [])
        self.assertEqual(len(notifier.failures), 1)
        self.assertIn("Building sync skipped", notifier.failures[0])
        self.assertEqual(building_writes(sheet), [])
        # The stock block is a different region of the sheet, so it is still snapshotted.
        self.assertTrue(sheet.blocks)
        self.assertIn("W10", [a1 for a1, _ in sheet.writes])

    def test_both_checks_failing_writes_nothing_and_warns_twice(self):
        from clop_monitor import sync_sheet_step

        sheet = FakeSheet(
            sheet_column_a(), [""] * 130,               # buildings missing -> sanity fails
            stock_labels=["apple", "oil", "coffee", "mpart", "vpart", "rocks"],
            dashboard=dashboard_grid(nations=("2026-08-25 10:19:48", "TOTAL (or min)")),
        )
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        self.assertEqual(len(notifier.failures), 2)
        self.assertEqual(notifier.alerts, [])
        self.assertEqual(sheet.writes, [])
        self.assertEqual(sheet.blocks, [])

    def test_unreadable_quantity_writes_nothing(self):
        from clop_monitor import sync_sheet_step

        # An unreadable Qty raises StockpileError. It must be caught before anything is written --
        # otherwise the buildings get corrected off a page we then declare unreadable.
        bad = LOGGED_IN_OVERVIEW.replace(
            '<td style="text-align: right;">Apples</td><td><span class="text-success">1,226</span></td>',
            '<td style="text-align: right;">Apples</td><td><span class="text-success">N/A</span></td>',
        )
        self.assertNotEqual(bad, LOGGED_IN_OVERVIEW)   # the replace must actually have matched
        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130)
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(bad), sheet, "T", notifier)
        self.assertEqual(sheet.writes, [])
        self.assertEqual(sheet.blocks, [])
        self.assertEqual(len(notifier.failures), 1)

    def test_page_without_panels_writes_nothing(self):
        from clop_monitor import sync_sheet_step

        # 200 OK, logged in, even a server-time stamp -- but the panel bodies never rendered.
        # A PHP fatal after header.php has flushed looks exactly like this. Reading it as "the
        # nation owns nothing and holds nothing" would zero the whole tab and stamp it verified.
        broken = ('<a href="logout.php">Logout</a>'
                  '<li><a>Server time: 2026-08-23 03:23:44</a></li>'
                  '<div id="content"><h3>Sierra Lepone</h3></div>')
        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130)
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(broken), sheet, "T", notifier)
        self.assertEqual(sheet.writes, [])
        self.assertEqual(sheet.blocks, [])
        self.assertEqual(notifier.alerts, [])
        self.assertEqual(len(notifier.failures), 1)
        self.assertIn("panel", notifier.failures[0].lower())

    def test_logged_out_overview_writes_nothing(self):
        from clop_monitor import sync_sheet_step

        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130)
        notifier = FakeNotifier()
        client = FakeClient(LOGGED_OUT_OVERVIEW)
        sync_sheet_step(client, sheet, "T", notifier)
        self.assertTrue(client.logins >= 1)          # it tried to re-login
        self.assertEqual(sheet.writes, [])           # but never wrote
        self.assertEqual(sheet.blocks, [])
        self.assertEqual(len(notifier.failures), 1)

    def test_truncated_page_writes_nothing(self):
        from clop_monitor import sync_sheet_step

        # Cut off inside the Buildings panel: both headings arrived, so the panel check passes,
        # but the building rows are missing and would be read as "you own nothing".
        cut = LOGGED_IN_OVERVIEW[:LOGGED_IN_OVERVIEW.index("Basic Copper Mine")]
        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130)
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(cut), sheet, "T", notifier)
        self.assertEqual(sheet.writes, [])
        self.assertEqual(sheet.blocks, [])
        self.assertEqual(notifier.alerts, [])
        self.assertEqual(len(notifier.failures), 1)
        self.assertIn("cut off", notifier.failures[0])

    def test_both_panels_empty_writes_nothing(self):
        from clop_monitor import sync_sheet_step

        # One query fills both panels, and on PHP 5.4 a failed query warns instead of fatalling,
        # so the page renders whole with both tables empty. That is a broken query, not a nation
        # that owns and holds nothing -- and the silent snapshot would zero R11:R16 under a fresh
        # W10 with no popup at all.
        empty = ('<a href="logout.php">Logout</a>'
                 '<li><a>Server time: 2026-08-23 03:23:44</a></li>'
                 '<div class="panel-heading">Resources</div>'
                 '<table class="table"><tbody></tbody></table>'
                 '<div class="panel-heading">Buildings</div>'
                 '<table class="table"><tbody></tbody></table></html>')
        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130)
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(empty), sheet, "T", notifier)
        self.assertEqual(sheet.writes, [])
        self.assertEqual(sheet.blocks, [])
        self.assertEqual(len(notifier.failures), 1)
        self.assertIn("no resources and no buildings", notifier.failures[0])

    def test_resources_panel_absent_writes_nothing(self):
        from clop_monitor import sync_sheet_step

        # Resources is checked first, so this is the earliest the guard can refuse a page.
        no_resources = LOGGED_IN_OVERVIEW.replace(
            '<div class="panel-heading">Resources</div>', '<div class="panel-heading">Other</div>'
        )
        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130)
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(no_resources), sheet, "T", notifier)
        self.assertEqual(sheet.writes, [])
        self.assertEqual(sheet.blocks, [])
        self.assertEqual(len(notifier.failures), 1)
        self.assertIn("no Resources panel", notifier.failures[0])

    def test_nation_with_no_buildings_still_syncs(self):
        from clop_monitor import sync_sheet_step

        # The false-positive guard. A new nation owns no buildings but holds resources; that is a
        # legitimate page and must still sync. This is the property the checks above trade against.
        no_buildings = ('<a href="logout.php">Logout</a>'
                        '<li><a>Server time: 2026-08-23 03:23:44</a></li>'
                        + NATION_PANEL_HTML +
                        '<div class="panel-heading">Resources</div>'
                        '<table class="table">'
                        '<thead><tr><td></td><td style="text-align: right;">Resource</td>'
                        '<td>Qty</td><td>Generated</td><td>Used</td><td>Loss</td><td>Net</td>'
                        '<td>Ticks-Worth</td></tr></thead><tbody>'
                        '<tr><td style="text-align: right;">Apples</td>'
                        '<td><span class="text-success">7</span></td>'
                        '<td><span>0</span></td><td><span>1</span></td><td><span>0</span></td>'
                        '<td><span>-1</span></td><td><span>7</span></td></tr>'
                        '</tbody></table>'
                        '<div class="panel-heading">Buildings</div>'
                        '<table class="table"><tbody></tbody></table></html>')
        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130)
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(no_buildings), sheet, "T", notifier)
        self.assertEqual(notifier.failures, [])
        self.assertEqual(dict(sheet.blocks)["R11:R16"], [[7], [0], [0], [0], [0], [0]])
        self.assertIn(("W10", "'2026-08-23 03:23:44"), sheet.writes)
        self.assertEqual(dict(sheet.blocks)["C15:C16"], [[60900], [1234567]])


if __name__ == "__main__":
    unittest.main()
