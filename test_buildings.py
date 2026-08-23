#!/usr/bin/env python3
"""Offline unit tests for buildings.py and building_map.py -- no network."""

import unittest

import buildings
from building_map import GAME_TO_SHEET, SHEET_BUILDINGS
from buildings import (
    BuildingError,
    Correction,
    desired_counts,
    locate_regions,
    parse_overview_buildings,
    reconcile,
    sanity_check,
)


# A minimal overview.php: a decoy resources panel with the same row shape, then the Buildings panel.
OVERVIEW_HTML = """
<div class="panel-heading">Resources</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Copper</td><td><span class="text-success">201</span></td></tr>
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
    """Stand-in for GoogleSheet: serves a fixed A/B grid and the Q stock labels, records writes."""

    def __init__(self, column_a, column_b, stock_labels=None):
        self._a = list(column_a)
        self._b = list(column_b)
        self._labels = list(stock_labels) if stock_labels is not None else [
            "apple", "oil", "coffee", "mpart", "vpart", "gems"
        ]
        self.writes = []   # (a1, value) from write_cell
        self.blocks = []   # (a1, values) from write

    def read(self, tab, a1):
        if a1.startswith("Q"):
            return [[label] for label in self._labels]
        n = max(len(self._a), len(self._b))
        return [[self._a[i] if i < len(self._a) else "",
                 self._b[i] if i < len(self._b) else ""] for i in range(n)]

    def write(self, tab, a1, values):
        self.blocks.append((a1, values))
        return values

    def write_cell(self, tab, a1, value):
        self.writes.append((a1, value))
        if a1.startswith("B"):
            row = int(a1[1:]) - 1
            while row >= len(self._b):
                self._b.append("")
            self._b[row] = value
        return value


def building_writes(sheet):
    """The column-B writes only -- the stockpile snapshot also writes W10."""
    return [(a1, value) for a1, value in sheet.writes if a1.startswith("B")]


class ParserTests(unittest.TestCase):
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


LOGGED_IN_OVERVIEW = (
    '<a href="logout.php">Logout</a><li><a>Server time: 2026-08-23 03:23:44</a></li>'
    + OVERVIEW_HTML
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


class SyncSheetStepTests(unittest.TestCase):
    def test_corrections_alert_and_write(self):
        from clop_monitor import sync_sheet_step

        col_a, names = full_column_a()
        col_b = [""] * 130
        col_b[8 + names.index("Basic Mine")] = 8   # overview says 10 (1 disabled)
        sheet = FakeSheet(col_a, col_b)
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        self.assertEqual(len(notifier.alerts), 1)
        self.assertIn("Basic Mine have 8 -> 10", notifier.alerts[0])
        self.assertEqual(notifier.failures, [])
        self.assertTrue(building_writes(sheet))

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
        self.assertIn(("W10", "2026-08-23 03:23:44"), sheet.writes)
        # A routine snapshot is a scheduled refresh, not an event: no popup for it.
        self.assertEqual(notifier.failures, [])
        self.assertEqual(notifier.alerts, [])

    def test_bad_stock_labels_warn_and_write_no_stock_cells(self):
        from clop_monitor import sync_sheet_step

        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130,
                          stock_labels=["oil", "apple", "coffee", "mpart", "vpart", "gems"])
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        self.assertEqual(len(notifier.failures), 1)
        self.assertIn("Stockpile snapshot skipped", notifier.failures[0])
        self.assertEqual(sheet.blocks, [])                              # no R block
        self.assertNotIn("W10", [a1 for a1, _ in sheet.writes])         # and no stamp

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


if __name__ == "__main__":
    unittest.main()
