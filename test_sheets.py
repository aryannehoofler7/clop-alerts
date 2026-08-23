#!/usr/bin/env python3
"""Offline unit tests for sheets.py -- no network; urlopen is stubbed."""

import io
import json
import unittest
import urllib.error
from contextlib import contextmanager
from unittest import mock

import sheets
from sheets import GoogleSheet, SheetError, _as_grid


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
                GoogleSheet().read("T", "A1")
        self.assertIn("500", str(ctx.exception))

    def test_url_error_raises(self):
        def boom(req):
            raise urllib.error.URLError("name resolution failed")

        with stub_urlopen(boom):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet().read("T", "A1")
        self.assertIn("could not reach", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
