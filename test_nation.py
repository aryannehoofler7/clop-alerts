#!/usr/bin/env python3
"""Offline unit tests for nation.py -- no network.

The cases that matter are the game's *non-numeric* per-tick values. backend_overview.php sets
$seperturn to the literal string "Fixed" for Solar Vassal / Lunar Client (lines 320-325), and
overview.php emits a bare "(Ascending)" for Alicorn Elite / Transponyism (lines 50-60). A parser
that assumed an integer there would raise on two perfectly normal governments.
"""

import unittest

from nation import NationStatus, NationStatusError, Reading, parse_server_time


HEADER = '<li><a>Server time: 2026-08-23 11:42:12</a></li>'


def panel(
    se="<span>-120</span>\n          (<span>3</span> per tick)",
    nlr="<span>1500</span>\n          (<span>-7</span> per tick)",
    satisfaction='<span class="text-success">218</span> (<span>-5</span> per tick)',
    gdp='<span class="text-success">60,900</span> bits per tick',
    funds='<span class="text-success">1,234,567</span> bits',
    extra="",
    header=HEADER,
):
    return f"""{header}
<div class="panel-heading">Nation</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Government Type</td><td>Loose Despotism</td></tr>
  <tr><td style="text-align: right;">Economic Type</td><td>Poorly Defined</td></tr>
  {extra}
  <tr><td style="text-align: right;">Relationship with Solar Empire</td><td>{se}</td></tr>
  <tr><td style="text-align: right;">Relationship with New Lunar Republic</td><td>{nlr}</td></tr>
  <tr><td style="text-align: right;">Satisfaction</td><td>{satisfaction}</td></tr>
  <tr><td style="text-align: right;">GDP</td><td>{gdp}</td></tr>
  <tr><td style="text-align: right;">Funds</td><td>{funds}</td></tr>
</tbody></table>
"""


class ReadingTests(unittest.TestCase):
    def test_display_numeric(self):
        self.assertEqual(Reading(218, -5).display(), "218 (-5)")

    def test_display_non_numeric_per_tick(self):
        self.assertEqual(Reading(1500, "Ascending").display(), "1500 (Ascending)")


class NationStatusTests(unittest.TestCase):
    def test_ordinary_page(self):
        status = NationStatus.from_overview(panel())
        self.assertEqual(status.government, "Loose Despotism")
        self.assertEqual(status.economy, "Poorly Defined")
        self.assertEqual(status.se, Reading(-120, 3))
        self.assertEqual(status.nlr, Reading(1500, -7))
        self.assertEqual(status.satisfaction, Reading(218, -5))
        self.assertEqual(status.gdp, 60900)
        self.assertEqual(status.funds, 1234567)
        self.assertEqual(status.server_time, "2026-08-23 11:42:12")

    def test_ascending_per_tick_kept_verbatim(self):
        status = NationStatus.from_overview(panel(se="<span>1500</span>\n          (Ascending)"))
        self.assertEqual(status.se, Reading(1500, "Ascending"))
        self.assertEqual(status.se.display(), "1500 (Ascending)")

    def test_fixed_per_tick_kept_verbatim(self):
        status = NationStatus.from_overview(
            panel(nlr="<span>40</span> (<span>Fixed</span> per tick)")
        )
        self.assertEqual(status.nlr, Reading(40, "Fixed"))

    def test_negative_current_with_commas(self):
        status = NationStatus.from_overview(
            panel(se="<span>-1,200</span> (<span>12</span> per tick)")
        )
        self.assertEqual(status.se, Reading(-1200, 12))

    def test_inactive_economy_warning_row_ignored(self):
        extra = (
            '<tr><td style="text-align: right;"><span class="text-danger">Warning:</span></td>'
            '<td><span class="text-danger">Your economic type is not active!</span></td></tr>'
        )
        self.assertEqual(NationStatus.from_overview(panel(extra=extra)).gdp, 60900)

    def test_corrupted_gdp_rejected(self):
        # The game's commas() helper (allfunctions.php:26-31) walks the *string* form inserting a
        # comma every three characters, so a fractional GDP renders as garbage. Never write that.
        with self.assertRaises(NationStatusError) as caught:
            NationStatus.from_overview(panel(gdp="<span>5,062,25.</span> bits per tick"))
        self.assertIn("GDP", str(caught.exception))

    def test_missing_row_raises(self):
        html = panel().replace(
            '<tr><td style="text-align: right;">Funds</td>'
            '<td><span class="text-success">1,234,567</span> bits</td></tr>',
            "",
        )
        with self.assertRaises(NationStatusError) as caught:
            NationStatus.from_overview(html)
        self.assertIn("Funds", str(caught.exception))

    def test_missing_nation_panel_raises(self):
        with self.assertRaises(NationStatusError):
            NationStatus.from_overview(HEADER + "<html></html>")

    def test_reading_without_parentheses_raises(self):
        with self.assertRaises(NationStatusError) as caught:
            NationStatus.from_overview(panel(satisfaction="<span>218</span>"))
        self.assertIn("Satisfaction", str(caught.exception))


class ServerTimeTests(unittest.TestCase):
    def test_stamp_read_verbatim(self):
        self.assertEqual(parse_server_time(HEADER), "2026-08-23 11:42:12")

    def test_missing_stamp_raises(self):
        # A page without it is not the page we think it is, and a snapshot with no staleness
        # marker is worse than no snapshot.
        with self.assertRaises(NationStatusError):
            parse_server_time("<html>nothing here</html>")


if __name__ == "__main__":
    unittest.main()
