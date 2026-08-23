#!/usr/bin/env python3
"""Offline unit tests for sheets.py -- no network; urlopen is stubbed."""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import sheets
from sheets import (
    NATION_ENV,
    GoogleSheet,
    SheetError,
    _as_grid,
    column_letter,
    find_in_row,
    index_column,
    nation_from_env,
    startup_check,
)


class FakeResponse(io.BytesIO):
    """Minimal stand-in for the object urlopen() yields as a context manager."""

    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@contextmanager
def stub_urlopen(handler):
    """Replace sheets' urlopen with `handler(request) -> FakeResponse (or raises)`; capture calls."""
    calls = []

    def fake(request, timeout=None):
        calls.append((request, timeout))
        return handler(request)

    with mock.patch.object(sheets.urllib.request, "urlopen", fake):
        yield calls


def ok(values):
    return FakeResponse(json.dumps({"ok": True, "values": values}).encode())


class GridCoercionTests(unittest.TestCase):
    def test_scalar_becomes_1x1(self):
        self.assertEqual(_as_grid(42), [[42]])

    def test_string_is_one_cell_not_characters(self):
        self.assertEqual(_as_grid("oil"), [["oil"]])

    def test_flat_list_becomes_single_row(self):
        self.assertEqual(_as_grid([1, 2, 3]), [[1, 2, 3]])

    def test_nested_list_preserved(self):
        self.assertEqual(_as_grid([[1], [2]]), [[1], [2]])


class RequestShapeTests(unittest.TestCase):
    def test_read_posts_expected_body_and_parses_values(self):
        with stub_urlopen(lambda req: ok([[0]])) as calls:
            result = GoogleSheet().read("LePone(Z)", "R11")
        self.assertEqual(result, [[0]])
        request, timeout = calls[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, sheets.EXEC_URL)
        self.assertEqual(request.headers["Content-type"], "application/json")
        self.assertEqual(timeout, sheets.DEFAULT_TIMEOUT)
        self.assertEqual(
            json.loads(request.data.decode()),
            {"action": "read", "tab": "LePone(Z)", "range": "R11"},
        )

    def test_read_body_carries_no_values_key(self):
        with stub_urlopen(lambda req: ok([[0]])) as calls:
            GoogleSheet().read("Tab", "A1")
        self.assertNotIn("values", json.loads(calls[0][0].data.decode()))

    def test_write_coerces_scalar_and_sends_values(self):
        with stub_urlopen(lambda req: ok([[42]])) as calls:
            result = GoogleSheet().write("LePone(Z)", "R11", 42)
        self.assertEqual(result, [[42]])
        self.assertEqual(
            json.loads(calls[0][0].data.decode()),
            {"action": "write", "tab": "LePone(Z)", "range": "R11", "values": [[42]]},
        )

    def test_write_passes_through_2d_block(self):
        with stub_urlopen(lambda req: ok([[1, 2], [3, 4]])) as calls:
            GoogleSheet().write("T", "A1:B2", [[1, 2], [3, 4]])
        self.assertEqual(
            json.loads(calls[0][0].data.decode())["values"], [[1, 2], [3, 4]]
        )

    def test_custom_exec_url_and_timeout_used(self):
        with stub_urlopen(lambda req: ok([[1]])) as calls:
            GoogleSheet("https://example/exec", timeout=5).read("T", "A1")
        request, timeout = calls[0]
        self.assertEqual(request.full_url, "https://example/exec")
        self.assertEqual(timeout, 5)


class ConvenienceTests(unittest.TestCase):
    def test_read_cell_unwraps_first_value(self):
        with stub_urlopen(lambda req: ok([[0]])):
            self.assertEqual(GoogleSheet().read_cell("T", "R11"), 0)

    def test_read_cell_empty_grid_is_blank(self):
        with stub_urlopen(lambda req: ok([])):
            self.assertEqual(GoogleSheet().read_cell("T", "R11"), "")

    def test_read_cell_empty_row_is_blank(self):
        with stub_urlopen(lambda req: ok([[]])):
            self.assertEqual(GoogleSheet().read_cell("T", "R11"), "")

    def test_write_cell_unwraps_first_value(self):
        with stub_urlopen(lambda req: ok([[42]])):
            self.assertEqual(GoogleSheet().write_cell("T", "R11", 42), 42)


class ErrorHandlingTests(unittest.TestCase):
    def test_server_reported_failure_raises_with_message(self):
        resp = FakeResponse(json.dumps({"ok": False, "error": "no such tab: Nope"}).encode())
        with stub_urlopen(lambda req: resp):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet().read("Nope", "A1")
        self.assertIn("no such tab: Nope", str(ctx.exception))

    def test_ok_false_without_message_still_raises(self):
        with stub_urlopen(lambda req: FakeResponse(json.dumps({"ok": False}).encode())):
            with self.assertRaises(SheetError):
                GoogleSheet().read("T", "A1")

    def test_non_json_body_raises(self):
        with stub_urlopen(lambda req: FakeResponse(b"<html>Sign in</html>")):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet().read("T", "A1")
        self.assertIn("non-JSON", str(ctx.exception))

    def test_http_error_raises(self):
        def boom(req):
            raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, io.BytesIO(b"nope"))

        with stub_urlopen(boom):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        self.assertIn("500", str(ctx.exception))

    def test_url_error_raises(self):
        def boom(req):
            raise urllib.error.URLError("name resolution failed")

        with stub_urlopen(boom):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        self.assertIn("could not reach", str(ctx.exception))

    def test_transient_google_404_is_retried_then_succeeds(self):
        outcomes = [
            urllib.error.HTTPError(
                "https://script.google.com/exec",
                404,
                "Not Found",
                {},
                io.BytesIO(b"<html>temporary Google error</html>"),
            ),
            ok([[42]]),
        ]

        def respond(_req):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch.object(sheets.time, "sleep") as sleep:
            with stub_urlopen(respond) as calls:
                result = GoogleSheet(retry_delays=(1.0, 3.0)).read("T", "A1")
        self.assertEqual(result, [[42]])
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(1.0)

    def test_persistent_transient_error_raises_only_after_three_attempts(self):
        def boom(req):
            raise urllib.error.HTTPError(
                req.full_url, 404, "Not Found", {}, io.BytesIO(b"temporary Google error")
            )

        with mock.patch.object(sheets.time, "sleep") as sleep:
            with stub_urlopen(boom) as calls:
                with self.assertRaisesRegex(SheetError, "after 3 attempts"):
                    GoogleSheet(retry_delays=(1.0, 3.0)).read("T", "A1")
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 3.0])

    def test_protocol_error_is_not_retried(self):
        with mock.patch.object(sheets.time, "sleep") as sleep:
            with stub_urlopen(lambda req: no_such_tab("Ghost")) as calls:
                with self.assertRaisesRegex(SheetError, "no such tab: Ghost"):
                    GoogleSheet().read("Ghost", "A1")
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()


def no_such_tab(name="Nope"):
    return FakeResponse(json.dumps({"ok": False, "error": f"no such tab: {name}"}).encode())


@contextmanager
def env_file(contents):
    """Yield a Path to a temporary .env file with the given contents."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text(contents, encoding="utf-8")
        yield path


class NationFromEnvTests(unittest.TestCase):
    def test_process_env_wins_over_file(self):
        with mock.patch.dict(os.environ, {NATION_ENV: "FromEnv"}):
            with env_file(f"{NATION_ENV}=FromFile\n") as path:
                self.assertEqual(nation_from_env(path), "FromEnv")

    def test_falls_back_to_env_file(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with env_file(f"{NATION_ENV}=LePone(Z)\n") as path:
                self.assertEqual(nation_from_env(path), "LePone(Z)")

    def test_unset_raises_naming_the_variable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with env_file("CLOP_USERNAME=x\n") as path:
                with self.assertRaises(SheetError) as ctx:
                    nation_from_env(path)
        self.assertIn(NATION_ENV, str(ctx.exception))


class TabExistsTests(unittest.TestCase):
    def test_true_when_read_succeeds(self):
        with stub_urlopen(lambda req: ok([[""]])):
            self.assertTrue(GoogleSheet().tab_exists("LePone(Z)"))

    def test_false_when_endpoint_reports_no_such_tab(self):
        with stub_urlopen(lambda req: no_such_tab("Ghost")):
            self.assertFalse(GoogleSheet().tab_exists("Ghost"))

    def test_network_error_propagates_not_treated_as_missing(self):
        def boom(req):
            raise urllib.error.URLError("name resolution failed")

        with stub_urlopen(boom):
            with self.assertRaises(SheetError):
                GoogleSheet(retry_delays=()).tab_exists("LePone(Z)")

    def test_require_tab_raises_for_missing(self):
        with stub_urlopen(lambda req: no_such_tab("Ghost")):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet().require_tab("Ghost")
        self.assertIn("Ghost", str(ctx.exception))
        self.assertIn(NATION_ENV, str(ctx.exception))

    def test_require_tab_passes_for_present(self):
        with stub_urlopen(lambda req: ok([[""]])):
            GoogleSheet().require_tab("LePone(Z)")  # does not raise


class StartupCheckTests(unittest.TestCase):
    def test_returns_sheet_and_nation_when_tab_present(self):
        with mock.patch.dict(os.environ, {NATION_ENV: "LePone(Z)"}):
            with stub_urlopen(lambda req: ok([[""]])):
                sheet, nation = startup_check(env_path=Path("does-not-exist"))
        self.assertIsInstance(sheet, GoogleSheet)
        self.assertEqual(nation, "LePone(Z)")

    def test_raises_when_nation_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SheetError):
                startup_check(env_path=Path("does-not-exist"))

    def test_raises_when_tab_missing(self):
        with mock.patch.dict(os.environ, {NATION_ENV: "Ghost"}):
            with stub_urlopen(lambda req: no_such_tab("Ghost")):
                with self.assertRaises(SheetError):
                    startup_check(env_path=Path("does-not-exist"))


class CellIntTests(unittest.TestCase):
    def test_empty_cell_is_zero(self):
        self.assertEqual(sheets.cell_int(""), 0)
        self.assertEqual(sheets.cell_int(None), 0)

    def test_numbers_pass_through(self):
        self.assertEqual(sheets.cell_int(7), 7)
        self.assertEqual(sheets.cell_int(7.0), 7)

    def test_comma_formatted_text_parses(self):
        self.assertEqual(sheets.cell_int("1,204"), 1204)
        self.assertEqual(sheets.cell_int(" -3 "), -3)

    def test_non_numeric_text_is_zero(self):
        self.assertEqual(sheets.cell_int("n/a"), 0)
        self.assertEqual(sheets.cell_int("1.5"), 0)


class ColumnLetterTests(unittest.TestCase):
    def test_first_columns(self):
        self.assertEqual(column_letter(0), "A")
        self.assertEqual(column_letter(2), "C")
        self.assertEqual(column_letter(25), "Z")

    def test_past_z(self):
        # The Dashboard has eleven populated columns today, but nothing may assume it stays
        # single-letter: a tenth nation joining walks it toward AA.
        self.assertEqual(column_letter(26), "AA")
        self.assertEqual(column_letter(27), "AB")
        self.assertEqual(column_letter(51), "AZ")
        self.assertEqual(column_letter(52), "BA")

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            column_letter(-1)


class FindInRowTests(unittest.TestCase):
    def test_exact_match(self):
        row = ["READ ONLY", "TOTAL", "LePone(Z)", "quaity(P)"]
        self.assertEqual(find_in_row(row, "LePone(Z)"), 2)

    def test_surrounding_whitespace_ignored_on_both_sides(self):
        self.assertEqual(find_in_row(["  LePone(Z) "], " LePone(Z)"), 0)

    def test_not_found_is_none(self):
        self.assertIsNone(find_in_row(["TOTAL", "#N/A"], "LePone(Z)"))

    def test_substring_does_not_match(self):
        # "LePone" must not resolve to the "LePone(Z)" column: the tab name is the whole cell.
        self.assertIsNone(find_in_row(["LePone(Z)"], "LePone"))

    def test_none_cells_skipped(self):
        self.assertEqual(find_in_row([None, "", "SE"], "SE"), 2)


class IndexColumnTests(unittest.TestCase):
    def test_rows_are_one_based(self):
        grid = [["Energy"], ["Apples"], ["Coffee"]]
        self.assertEqual(index_column(grid), {"Energy": [1], "Apples": [2], "Coffee": [3]})

    def test_blank_rows_skipped_not_numbered_away(self):
        grid = [["Sat"], [""], ["GDP"]]
        self.assertEqual(index_column(grid), {"Sat": [1], "GDP": [3]})

    def test_duplicates_all_reported(self):
        # Returning every occurrence is what lets the caller refuse to write an ambiguous sheet.
        grid = [["Gems"], ["Oil"], ["Gems"]]
        self.assertEqual(index_column(grid)["Gems"], [1, 3])

    def test_short_and_empty_rows_tolerated(self):
        self.assertEqual(index_column([[], ["Oil"], [None]]), {"Oil": [2]})


if __name__ == "__main__":
    unittest.main()
