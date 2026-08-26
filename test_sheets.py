#!/usr/bin/env python3
"""Offline unit tests for sheets.py -- no network; urlopen is stubbed."""

import http.client
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
    column_index,
    column_letter,
    find_in_row,
    index_column,
    nation_from_env,
    startup_check,
)


class FakeResponse(io.BytesIO):
    """Minimal stand-in for the object urlopen() yields as a context manager."""

    def __init__(self, body: bytes, status: int = 200, content_type="application/json"):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FailingReadResponse(FakeResponse):
    """A response whose headers arrive but whose body dies mid-transfer.

    This is the shape that matters: the failure lands inside the ``with urlopen(...)`` block on
    ``read()``, not on the call that opened it, so it is only caught if the handler wraps both.
    """

    def __init__(self, error: BaseException):
        super().__init__(b"")
        self.error = error

    def read(self, *args):
        raise self.error


def html_page(body=b"<html><body>Sorry, unable to open the file at this time.</body></html>"):
    """The 200-with-an-HTML-page reply Google Apps Script serves during a hiccup."""
    return FakeResponse(body, content_type="text/html; charset=utf-8")


def apps_script_error_page(message="Script function not found: doGet"):
    """The real Apps Script error page, trimmed to the parts the reader keys off.

    Captured from the live endpoint: ~5KB of ppConfig telemetry boilerplate wrapping one line of
    monospace body text. The boilerplate is what makes a first-200-characters quote useless, so it
    is kept here rather than tidied away.
    """
    return html_page(
        b"<!DOCTYPE html><html><head><script nonce=\"cQ9OZR9PSnEF4FD8UuMogg\">"
        b"window['ppConfig'] = {productName: '26981ed0d57bbad37e728ff58134270c', "
        b"deleteIsEnforced:  false , sealIsEnforced:  false , heartbeatRate:  0.5 , "
        b"periodicReportingRateMillis:  60000.0};</script>"
        b"<link rel=\"shortcut icon\" href=\"//ssl.gstatic.com/docs/script/images/favicon.ico\">"
        b"<title>Error</title><style type=\"text/css\">.errorMessage {font-weight: bold;}</style>"
        b"</head><body style=\"margin:20px\"><div>"
        b"<img alt=\"Google Apps Script\" src=\"//ssl.gstatic.com/docs/script/images/logo.png\">"
        b"</div><div style=\"text-align:center;font-family:monospace\">"
        + message.encode()
        + b"</div></body></html>"
    )


def replies(*outcomes):
    """Return a stub handler that plays `outcomes` in order, raising any that are exceptions."""
    queued = list(outcomes)

    def respond(_request):
        outcome = queued.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return respond


@contextmanager
def stub_urlopen(handler):
    """Replace sheets' HTTP with `handler(request) -> FakeResponse (or raises)`; capture calls.

    Both seams are covered: the no-redirect opener used for the first hop (POST to /exec) and the
    plain urlopen used for the second (fetching the result link). A handler that returns an
    ordinary response settles it on the first hop, so `calls` stays one entry per round trip --
    which is what every count assertion in this file means.
    """
    calls = []

    def fake(request, timeout=None):
        calls.append((request, timeout))
        return handler(request)

    class FakeOpener:
        def open(self, request, timeout=None):
            return fake(request, timeout)

    with mock.patch.object(sheets.urllib.request, "urlopen", fake):
        with mock.patch.object(sheets, "_build_opener", FakeOpener):
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


ECHO_URL = "https://script.googleusercontent.com/macros/echo?user_content_key=abc&lib=xyz"


def redirect_to(location=ECHO_URL, code=302):
    """The 302 that /exec answers a POST with, carrying the one-shot result link."""
    return urllib.error.HTTPError(
        "https://script.google.com/exec", code, "Found",
        {"Location": location}, io.BytesIO(b""),
    )


def two_hop(second, location=ECHO_URL):
    """Handler that redirects the first hop and answers the second with `second`."""
    def respond(request):
        if isinstance(request, str):          # hop 2 is fetched by URL
            return second() if callable(second) else second
        raise redirect_to(location)           # hop 1 is a Request object
    return respond


class TwoHopTests(unittest.TestCase):
    """/exec answers a POST with a redirect; the hops are fetched and capped separately."""

    def test_redirect_is_followed_and_its_body_parsed(self):
        with stub_urlopen(two_hop(ok([[11]]))) as calls:
            self.assertEqual(GoogleSheet().read("T", "A1"), [[11]])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0], ECHO_URL)

    def test_each_hop_gets_its_own_timeout(self):
        # The whole point of splitting them: the script may legitimately run for half a minute,
        # but the result link is dead within seconds, so waiting 30s on it collects a corpse.
        with stub_urlopen(two_hop(ok([[1]]))) as calls:
            GoogleSheet(timeout=30.0, content_timeout=12.0).read("T", "A1")
        self.assertEqual(calls[0][1], 30.0)
        self.assertEqual(calls[1][1], 12.0)
        self.assertLess(sheets.CONTENT_TIMEOUT, sheets.DEFAULT_TIMEOUT)

    def test_a_direct_answer_needs_no_second_hop(self):
        with stub_urlopen(lambda req: ok([[2]])) as calls:
            self.assertEqual(GoogleSheet().read("T", "A1"), [[2]])
        self.assertEqual(len(calls), 1)

    def test_a_redirect_without_a_location_is_not_swallowed(self):
        with stub_urlopen(lambda req: (_ for _ in ()).throw(
            urllib.error.HTTPError("u", 302, "Found", {}, io.BytesIO(b"")))):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        self.assertIn("302", str(ctx.exception))

    def test_slow_second_hop_times_out_and_retries_with_a_fresh_link(self):
        # A fresh POST means a fresh link, which is the actual remedy for an expired one.
        hops = [TimeoutError("timed out"), ok([[3]])]

        def second():
            outcome = hops.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(sheets.time, "sleep"):
            with stub_urlopen(two_hop(second)) as calls:
                self.assertEqual(GoogleSheet(retry_delays=(1.0,)).read("T", "A1"), [[3]])
        self.assertEqual(len(calls), 4)   # two full round trips, two hops each


class WhichHopFailedTests(unittest.TestCase):
    """A failure must name the hop it happened on.

    "timed out reading the sheet endpoint's reply" was true of both hops, which made a production
    dialog impossible to diagnose from its own text -- the only way to tell was to go and re-time
    the hops by hand.
    """

    def test_a_hop_1_timeout_says_running_the_script(self):
        def boom(_req):
            return FailingReadResponse(TimeoutError("The read operation timed out"))

        with stub_urlopen(boom):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        self.assertIn(sheets.HOP_SCRIPT, str(ctx.exception))
        self.assertNotIn(sheets.HOP_RESULT, str(ctx.exception))

    def test_a_hop_2_timeout_says_fetching_the_result_link(self):
        def respond(request):
            if isinstance(request, str):
                return FailingReadResponse(TimeoutError("The read operation timed out"))
            raise redirect_to()

        with stub_urlopen(respond):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        self.assertIn(sheets.HOP_RESULT, str(ctx.exception))
        self.assertNotIn(sheets.HOP_SCRIPT, str(ctx.exception))

    def test_the_two_hops_are_labelled_differently(self):
        self.assertNotEqual(sheets.HOP_SCRIPT, sheets.HOP_RESULT)


class TimeoutsAgainstMeasuredRangesTests(unittest.TestCase):
    """Both caps are pinned against what has actually been observed on the live endpoint.

    The numbers below are measurements, not guesses, and they are the reason the two hops are
    treated so differently. If someone re-measures and finds different ranges, change these
    constants and these numbers together.
    """

    #: Slowest hop 1 seen on a call that returned good JSON, across ~40 samples on three days.
    #: Successive samples kept raising this (3.2 -> 7.51 -> 9.56), which is why the caps are set as
    #: generous bounds rather than snugly around whatever the latest run happened to show.
    SLOWEST_GOOD_HOP1 = 9.56
    #: Same for hop 2. An early two-sample read said this was always ~0.5s; it is not.
    SLOWEST_GOOD_HOP2 = 2.81

    #: How much room a cap must leave above the slowest known success. Cutting early throws away a
    #: call that would have worked and costs a full extra round trip; cutting late wastes a few
    #: seconds of a 180-second budget. The asymmetry is the whole argument for erring generous.
    MARGIN = 2.0

    def test_hop_1_leaves_real_headroom_over_the_slowest_known_success(self):
        self.assertGreaterEqual(
            sheets.DEFAULT_TIMEOUT, self.SLOWEST_GOOD_HOP1 * self.MARGIN
        )

    def test_hop_2_leaves_real_headroom_over_the_slowest_known_success(self):
        self.assertGreaterEqual(
            sheets.CONTENT_TIMEOUT, self.SLOWEST_GOOD_HOP2 * self.MARGIN
        )

    def test_the_retry_schedule_starts_fast_and_ends_slow(self):
        # Two failure modes, two halves of the schedule: immediate retries beat independent
        # blips, a stretched tail outlasts a sustained bad patch. Nine attempts packed into 70
        # seconds all failed together, which is what the tail exists to prevent.
        delays = sheets.DEFAULT_RETRY_DELAYS
        self.assertLessEqual(delays[0], 0.5)
        self.assertGreaterEqual(delays[-1], 20.0)
        self.assertEqual(list(delays), sorted(delays), "delays must not decrease")

    def test_enough_attempts_fit_the_deadline_even_when_all_are_doomed(self):
        # Not all of them -- the tail is deliberately longer than the deadline allows, and the
        # deadline clamps it. But a useful number must fit before that happens.
        doomed = sheets.DEFAULT_TIMEOUT + sheets.CONTENT_TIMEOUT
        spent = fitted = 0
        for delay in (0.0,) + tuple(sheets.DEFAULT_RETRY_DELAYS):
            spent += delay + doomed
            if spent > sheets.DEFAULT_DEADLINE:
                break
            fitted += 1
        self.assertGreaterEqual(fitted, 5)


class DeadlineTests(unittest.TestCase):
    """Retries stop on the clock as well as the count -- a stalled poll is not monitoring."""

    @staticmethod
    def _clock(step):
        state = {"now": 0.0}

        def monotonic():
            state["now"] += step
            return state["now"]

        return monotonic

    def test_slow_attempts_stop_retrying_before_the_attempts_run_out(self):
        with mock.patch.object(sheets.time, "sleep"):
            with mock.patch.object(sheets.time, "monotonic", self._clock(1.0)):
                with stub_urlopen(lambda req: html_page()) as calls:
                    with self.assertRaises(SheetError) as ctx:
                        GoogleSheet(retry_delays=(1.0, 3.0, 8.0), deadline=10.0).read("T", "A1")
        message = str(ctx.exception)
        self.assertIn("stopped at the 10s budget", message)
        self.assertLess(len(calls), 4)

    def test_each_hop_is_clamped_to_what_is_left_of_the_budget(self):
        # Without this the deadline only stops the *next* retry, and the attempt already running
        # can overshoot it by another 42 seconds. Measured live at 50.7s against a 45s budget.
        with stub_urlopen(two_hop(ok([[1]]))) as calls:
            GoogleSheet(timeout=30.0, content_timeout=12.0, deadline=5.0).read("T", "A1")
        self.assertLessEqual(calls[0][1], 5.0)
        self.assertLessEqual(calls[1][1], 5.0)

    def test_an_exhausted_budget_still_leaves_a_usable_timeout(self):
        # Clamping to zero would mean "non-blocking" and fail instantly with a confusing error.
        with stub_urlopen(two_hop(ok([[1]]))) as calls:
            GoogleSheet(deadline=0.0).read("T", "A1")
        self.assertEqual(calls[0][1], sheets.MIN_TIMEOUT)

    def test_fast_attempts_still_use_the_whole_retry_budget(self):
        with mock.patch.object(sheets.time, "sleep"):
            with stub_urlopen(lambda req: html_page()) as calls:
                with self.assertRaises(SheetError):
                    GoogleSheet(retry_delays=(1.0, 3.0, 8.0), deadline=45.0).read("T", "A1")
        self.assertEqual(len(calls), 4)

    def test_the_final_message_reports_attempts_and_time(self):
        with mock.patch.object(sheets.time, "sleep"):
            with mock.patch.object(sheets.time, "monotonic", self._clock(1.0)):
                with stub_urlopen(lambda req: html_page()):
                    with self.assertRaises(SheetError) as ctx:
                        GoogleSheet(retry_delays=(1.0, 3.0, 8.0), deadline=10.0).read("T", "A1")
        self.assertRegex(str(ctx.exception), r"after \d+ attempts in \d+s")


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
                GoogleSheet(retry_delays=()).read("T", "A1")
        self.assertIn("non-JSON", str(ctx.exception))

    def test_transient_non_json_body_is_retried_then_succeeds(self):
        # A 200 carrying an HTML page is how Apps Script reports a passing hiccup on the
        # googleusercontent redirect hop. It looks nothing like a transport error, so it used to
        # skip the retries entirely and pop a dialog on the very first occurrence.
        with mock.patch.object(sheets.time, "sleep") as sleep:
            with stub_urlopen(replies(html_page(), ok([[42]]))) as calls:
                result = GoogleSheet(retry_delays=(1.0, 3.0)).read("T", "A1")
        self.assertEqual(result, [[42]])
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(1.0)

    def test_empty_body_is_retried_then_succeeds(self):
        with mock.patch.object(sheets.time, "sleep"):
            with stub_urlopen(replies(FakeResponse(b""), ok([[7]]))) as calls:
                self.assertEqual(GoogleSheet(retry_delays=(1.0,)).read("T", "A1"), [[7]])
        self.assertEqual(len(calls), 2)

    def test_persistent_non_json_body_raises_only_after_three_attempts(self):
        with mock.patch.object(sheets.time, "sleep") as sleep:
            with stub_urlopen(lambda req: html_page()) as calls:
                with self.assertRaisesRegex(SheetError, "after 3 attempts"):
                    GoogleSheet(retry_delays=(1.0, 3.0)).read("T", "A1")
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 3.0])

    def test_non_json_error_quotes_what_actually_arrived(self):
        # The message must carry evidence rather than assert a cause: a live check after the first
        # such dialog found the deployment perfectly healthy, so "check the access setting" was a
        # wrong guess presented as a diagnosis.
        with stub_urlopen(lambda req: html_page()):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        message = str(ctx.exception)
        self.assertIn("text/html", message)
        self.assertIn("Sorry, unable to open the file", message)

    def test_empty_body_error_says_so_rather_than_quoting_nothing(self):
        with stub_urlopen(lambda req: FakeResponse(b"")):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        self.assertIn("empty body", str(ctx.exception))

    def test_read_timeout_becomes_a_sheet_error_and_is_retried(self):
        # A socket timeout during response.read() escapes as a bare TimeoutError, not a URLError.
        # Nothing above sync_sheet_step catches that, so uncaught it kills the monitor outright --
        # with a traceback and no dialog, which is the one outcome this project refuses to ship.
        timeout = TimeoutError("The read operation timed out")
        with mock.patch.object(sheets.time, "sleep"):
            with stub_urlopen(replies(FailingReadResponse(timeout), ok([[1]]))) as calls:
                self.assertEqual(GoogleSheet(retry_delays=(1.0,)).read("T", "A1"), [[1]])
        self.assertEqual(len(calls), 2)

    def test_persistent_read_timeout_raises_sheet_error(self):
        def boom(_req):
            return FailingReadResponse(TimeoutError("The read operation timed out"))

        with stub_urlopen(boom):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        self.assertIn("timed out", str(ctx.exception).lower())

    def test_body_dying_mid_transfer_becomes_a_sheet_error_and_is_retried(self):
        # IncompleteRead is an http.client.HTTPException -- neither an HTTPError nor a URLError,
        # so it too used to escape every handler here.
        cut_short = http.client.IncompleteRead(b"half a payload")
        with mock.patch.object(sheets.time, "sleep"):
            with stub_urlopen(replies(FailingReadResponse(cut_short), ok([[1]]))) as calls:
                self.assertEqual(GoogleSheet(retry_delays=(1.0,)).read("T", "A1"), [[1]])
        self.assertEqual(len(calls), 2)

    def test_persistent_mid_transfer_failure_raises_sheet_error(self):
        def boom(_req):
            return FailingReadResponse(http.client.IncompleteRead(b"half a payload"))

        with stub_urlopen(boom):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        self.assertIn("broke off part-way", str(ctx.exception))

    def test_expired_result_link_is_named_not_dumped(self):
        # The 5KB page quotes as nothing but ppConfig boilerplate, so the reader must pull out the
        # one line of body text that says what actually went wrong.
        with stub_urlopen(lambda req: apps_script_error_page()):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        message = str(ctx.exception)
        self.assertIn("Script function not found: doGet", message)
        self.assertNotIn("ppConfig", message)

    def test_expired_result_link_does_not_blame_the_deployment(self):
        # The whole point of the change: this fault is Google being slow, and the dialog must not
        # send the reader off to redeploy a healthy Apps Script.
        with stub_urlopen(lambda req: apps_script_error_page()):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        message = str(ctx.exception)
        self.assertIn("clears on its own", message)
        self.assertNotIn("'Anyone' access", message)

    def test_expired_result_link_is_retried(self):
        with mock.patch.object(sheets.time, "sleep"):
            with stub_urlopen(replies(apps_script_error_page(), ok([[5]]))) as calls:
                self.assertEqual(GoogleSheet(retry_delays=(1.0,)).read("T", "A1"), [[5]])
        self.assertEqual(len(calls), 2)

    def test_other_apps_script_error_pages_are_reported_verbatim(self):
        page = apps_script_error_page("Authorization is required to perform that action.")
        with stub_urlopen(lambda req: page):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        message = str(ctx.exception)
        self.assertIn("Authorization is required", message)
        # Not the expiring-link fault, so it must not carry that fault's reassurance.
        self.assertNotIn("clears on its own", message)

    def test_sign_in_page_still_points_at_the_deployment(self):
        # A page that is *not* an Apps Script error page keeps the old advice, which is right for it.
        with stub_urlopen(lambda req: html_page(b"<html><body>Sign in to continue</body></html>")):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        self.assertIn("'Anyone' access", str(ctx.exception))

    def test_google_error_pages_are_quoted_as_words_not_boilerplate(self):
        # Drive's "Page not found", which is what an expired result link looks like when Google
        # gets to it fractionally sooner. Real capture: 7805 bytes whose first 200 characters are
        # entirely window['ppConfig'] telemetry, so a byte-quote showed the reader nothing.
        drive_404 = (
            b"<!DOCTYPE html><html lang=\"en\"><head>"
            b"<script nonce=\"HwUR6ZoM0zatdET2VpdZWw\">window['ppConfig'] = {productName: "
            b"'26981ed0d57bbad37e728ff58134270c', deleteIsEnforced: false, sealIsEnforced: false,"
            b" heartbeatRate: 0.5};</script><title>Page not found</title>"
            b"<style>.errorMessage {font-weight:bold}</style></head><body><div>Drive</div>"
            b"<div>Sorry, unable to open the file at present. "
            b"Please check the address and try again.</div></body></html>"
        )

        def boom(req):
            raise urllib.error.HTTPError(
                "https://script.googleusercontent.com/macros/echo",
                404, "Not Found", {}, io.BytesIO(drive_404),
            )

        with mock.patch.object(sheets.time, "sleep"):
            with stub_urlopen(boom):
                with self.assertRaises(SheetError) as ctx:
                    GoogleSheet(retry_delays=()).read("T", "A1")
        message = str(ctx.exception)
        self.assertIn("unable to open the file at present", message)
        self.assertNotIn("ppConfig", message)
        # It is a 404, so it is retryable -- the same expired-link fault, caught a beat earlier.
        self.assertIn(404, sheets.RETRYABLE_HTTP_STATUSES)

    def test_apps_script_error_reader_ignores_unrelated_html(self):
        self.assertIsNone(sheets.apps_script_error(b"<html><body>Sign in</body></html>"))
        self.assertIsNone(sheets.apps_script_error(b"<title>Error</title>"))  # marker alone
        self.assertIsNone(sheets.apps_script_error(b"\xff\xfe not utf-8 at all"))

    def test_a_write_retried_after_a_garbled_reply_repeats_the_same_payload(self):
        # Retrying a write is only safe because the payload is an assignment, not an increment.
        # If this ever stops holding, the retry has to go.
        with mock.patch.object(sheets.time, "sleep"):
            with stub_urlopen(replies(html_page(), ok([[42]]))) as calls:
                GoogleSheet(retry_delays=(1.0,)).write("T", "R11", 42)
        first, second = (json.loads(call[0].data.decode()) for call in calls)
        self.assertEqual(first, second)
        self.assertEqual(second["values"], [[42]])

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

    def test_retry_flagged_protocol_error_is_retried(self):
        # What the redeployed doGet answers when Google's one-shot result link has expired. It is
        # {ok: false}, but it is the one {ok: false} that is worth asking again.
        expired = FakeResponse(
            json.dumps(
                {"ok": False, "retry": True, "error": "this endpoint answers POST only."}
            ).encode()
        )
        with mock.patch.object(sheets.time, "sleep"):
            with stub_urlopen(replies(expired, ok([[9]]))) as calls:
                self.assertEqual(GoogleSheet(retry_delays=(1.0,)).read("T", "A1"), [[9]])
        self.assertEqual(len(calls), 2)

    def test_retry_flagged_error_still_reports_its_message_when_persistent(self):
        def boom(_req):
            return FakeResponse(
                json.dumps({"ok": False, "retry": True, "error": "answers POST only"}).encode()
            )

        with stub_urlopen(boom):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        self.assertIn("answers POST only", str(ctx.exception))

    def test_protocol_error_without_the_flag_is_still_definitive(self):
        # The flag must be opt-in: 'no such tab' will be just as true in three seconds.
        with mock.patch.object(sheets.time, "sleep") as sleep:
            with stub_urlopen(lambda req: no_such_tab("Ghost")) as calls:
                with self.assertRaises(SheetError):
                    GoogleSheet(retry_delays=(1.0, 3.0)).read("Ghost", "A1")
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

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


class A1GeometryTests(unittest.TestCase):
    def test_column_index_round_trips(self):
        for i in (0, 1, 25, 26, 27, 51, 52, 701):
            self.assertEqual(column_index(column_letter(i)), i)

    def test_bounds_of_a_single_cell(self):
        self.assertEqual(sheets.a1_bounds("W10"), (22, 22, 10, 10))

    def test_bounds_of_a_range(self):
        self.assertEqual(sheets.a1_bounds("Q1:W60"), (16, 22, 1, 60))

    def test_reversed_corners_are_normalised(self):
        self.assertEqual(sheets.a1_bounds("W60:Q1"), (16, 22, 1, 60))

    def test_unparseable_is_none(self):
        for bad in ("", "A", "1", "A1:B2:C3", "$A$1", "Sheet!A1"):
            self.assertIsNone(sheets.a1_bounds(bad), bad)

    def test_overlap(self):
        self.assertTrue(sheets.ranges_overlap("A1:B130", "B11"))
        self.assertTrue(sheets.ranges_overlap("Q1:W60", "R11:R16"))
        self.assertTrue(sheets.ranges_overlap("Q1:W60", "W10"))

    def test_no_overlap(self):
        # The one that matters: buildings write column B, stockpiles read Q:W.
        self.assertFalse(sheets.ranges_overlap("B11", "Q1:W60"))
        self.assertFalse(sheets.ranges_overlap("A1:B130", "Q1:W60"))
        self.assertFalse(sheets.ranges_overlap("R11:R16", "A1:B130"))

    def test_rows_matter_as_well_as_columns(self):
        self.assertFalse(sheets.ranges_overlap("B1:B10", "B11:B20"))
        self.assertTrue(sheets.ranges_overlap("B1:B10", "B10:B20"))

    def test_an_unparseable_range_is_assumed_to_overlap(self):
        # Guessing "no overlap" here would serve stale cells; guessing "overlap" only costs a
        # round trip.
        self.assertTrue(sheets.ranges_overlap("nonsense", "A1"))


class BatchTests(unittest.TestCase):
    def test_one_request_carries_every_op(self):
        reply = FakeResponse(json.dumps({
            "ok": True,
            "results": [{"ok": True, "values": [[1]]}, {"ok": True, "values": [[2]]}],
        }).encode())
        ops = [
            {"action": "read", "tab": "T", "range": "A1"},
            {"action": "write", "tab": "T", "range": "B1", "values": [[2]]},
        ]
        with stub_urlopen(lambda req: reply) as calls:
            self.assertEqual(GoogleSheet().batch(ops), [[[1]], [[2]]])
        self.assertEqual(len(calls), 1)
        sent = json.loads(calls[0][0].data.decode())
        self.assertEqual(sent["action"], "batch")
        self.assertEqual(sent["ops"], ops)

    def test_a_failed_op_raises_naming_the_range(self):
        reply = FakeResponse(json.dumps({
            "ok": True,
            "results": [{"ok": True, "values": []}, {"ok": False, "error": "bad range"}],
        }).encode())
        with stub_urlopen(lambda req: reply):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet().batch([
                    {"action": "read", "tab": "T", "range": "A1"},
                    {"action": "read", "tab": "T", "range": "ZZ"},
                ])
        self.assertIn("bad range", str(ctx.exception))
        self.assertIn("ZZ", str(ctx.exception))

    def test_a_missing_tab_inside_a_batch_keeps_its_type(self):
        reply = FakeResponse(json.dumps({
            "ok": True, "results": [{"ok": False, "error": "no such tab: Ghost"}],
        }).encode())
        with stub_urlopen(lambda req: reply):
            with self.assertRaises(sheets.SheetTabMissing):
                GoogleSheet().batch([{"action": "read", "tab": "Ghost", "range": "A1"}])

    def test_a_short_results_list_is_refused(self):
        reply = FakeResponse(json.dumps({"ok": True, "results": [{"ok": True, "values": []}]}).encode())
        with stub_urlopen(lambda req: reply):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet().batch([
                    {"action": "read", "tab": "T", "range": "A1"},
                    {"action": "read", "tab": "T", "range": "A2"},
                ])
        self.assertIn("for 2 operations", str(ctx.exception))

    def test_an_old_deployment_falls_back_to_one_call_per_op(self):
        # The client must work against a deployment that has never heard of batch, and improve by
        # itself the moment the new script goes live -- no setting to remember to flip.
        replies = [
            FakeResponse(json.dumps({"ok": False, "error": "unknown action: batch"}).encode()),
            ok([[1]]),
            ok([[2]]),
        ]
        with stub_urlopen(replies_from(replies)) as calls:
            sheet = GoogleSheet()
            result = sheet.batch([
                {"action": "read", "tab": "T", "range": "A1"},
                {"action": "read", "tab": "T", "range": "A2"},
            ])
        self.assertEqual(result, [[[1]], [[2]]])
        self.assertEqual(len(calls), 3)   # the rejected batch, then one call per op
        self.assertFalse(sheet._batch_supported)

    def test_the_real_old_deployment_reply_triggers_the_fallback(self):
        # The reply the LIVE un-redeployed script actually gives a batch payload. It reads
        # request.tab before request.action, so tab is undefined and it answers "no such tab:
        # undefined" -- which this client classifies as SheetTabMissing, its one *definitive*
        # error. Keying the fallback off the wording "unknown action" would have let that
        # propagate and switched sheet sync off permanently against exactly the deployment the
        # fallback exists for.
        replies = [
            FakeResponse(json.dumps({"ok": False, "error": "no such tab: undefined"}).encode()),
            ok([[296]]),
        ]
        with stub_urlopen(replies_from(replies)) as calls:
            sheet = GoogleSheet()
            result = sheet.batch([{"action": "read", "tab": "LePone(Z)", "range": "R11"}])
        self.assertEqual(result, [[[296]]])
        self.assertFalse(sheet._batch_supported)
        self.assertEqual(len(calls), 2)

    def test_a_per_op_failure_is_not_mistaken_for_an_old_deployment(self):
        # Once the outer reply proves batch is understood, a bad op must surface as itself rather
        # than triggering a pointless replay of every op one at a time.
        reply = FakeResponse(json.dumps({
            "ok": True, "results": [{"ok": False, "error": "no such tab: Ghost"}],
        }).encode())
        with stub_urlopen(lambda req: reply) as calls:
            sheet = GoogleSheet()
            with self.assertRaises(sheets.SheetTabMissing):
                sheet.batch([{"action": "read", "tab": "Ghost", "range": "A1"}])
        self.assertTrue(sheet._batch_supported)
        self.assertEqual(len(calls), 1)   # no fallback replay

    def test_a_transport_failure_says_nothing_about_batch_support(self):
        # A timeout is weather. Marking the deployment as batch-less over one would give up the
        # reliability win permanently over a passing outage.
        def boom(_req):
            return FailingReadResponse(TimeoutError("The read operation timed out"))

        with stub_urlopen(boom):
            sheet = GoogleSheet(retry_delays=())
            with self.assertRaises(SheetError):
                sheet.batch([{"action": "read", "tab": "T", "range": "A1"}])
        self.assertIsNone(sheet._batch_supported)

    def test_the_fallback_is_remembered_so_it_costs_one_wasted_request_only(self):
        replies = [
            FakeResponse(json.dumps({"ok": False, "error": "unknown action: batch"}).encode()),
            ok([[1]]), ok([[9]]),
        ]
        with stub_urlopen(replies_from(replies)) as calls:
            sheet = GoogleSheet()
            sheet.batch([{"action": "read", "tab": "T", "range": "A1"}])
            self.assertEqual(len(calls), 2)
            sheet.batch([{"action": "read", "tab": "T", "range": "A2"}])
        self.assertEqual(len(calls), 3)   # no second rejected batch

    def test_an_empty_batch_costs_nothing(self):
        with stub_urlopen(lambda req: ok([[1]])) as calls:
            self.assertEqual(GoogleSheet().batch([]), [])
        self.assertEqual(calls, [])


class BatchedSheetTests(unittest.TestCase):
    """The wrapper that turns a sync's eleven round trips into two."""

    class Recorder:
        def __init__(self, grids=None):
            self.requests = []
            self._grids = grids or {}

        def batch(self, ops):
            self.requests.append(("batch", [(o["action"], o["tab"], o["range"]) for o in ops]))
            return [self._grids.get((o["tab"], o["range"]), [[o["action"]]]) for o in ops]

        def read(self, tab, a1):
            self.requests.append(("read", tab, a1))
            return self._grids.get((tab, a1), [["live"]])

    def test_prefetched_reads_cost_one_request_and_are_served_from_it(self):
        inner = self.Recorder({("T", "A1:B130"): [["a"]], ("T", "Q1:W60"): [["q"]]})
        sheet = sheets.BatchedSheet(inner, prefetch=[("T", "A1:B130"), ("T", "Q1:W60")])
        self.assertEqual(len(inner.requests), 1)
        self.assertEqual(sheet.read("T", "A1:B130"), [["a"]])
        self.assertEqual(sheet.read("T", "Q1:W60"), [["q"]])
        self.assertEqual(len(inner.requests), 1)   # no further traffic

    def test_an_unprefetched_read_falls_through_live(self):
        inner = self.Recorder()
        sheet = sheets.BatchedSheet(inner, prefetch=[("T", "A1")])
        sheet.read("T", "ZZ1")
        self.assertEqual(inner.requests[-1], ("read", "T", "ZZ1"))

    def test_writes_are_queued_and_sent_as_one_request(self):
        inner = self.Recorder()
        sheet = sheets.BatchedSheet(inner)
        sheet.write("T", "R11:R16", [[1], [2]])
        sheet.write_cell("T", "W10", "stamp")
        sheet.write_cell("T", "B9", 10)
        self.assertEqual(inner.requests, [])       # nothing sent yet
        self.assertEqual(sheet.pending_writes, 3)
        sheet.flush()
        self.assertEqual(len(inner.requests), 1)
        self.assertEqual(
            inner.requests[0][1],
            [("write", "T", "R11:R16"), ("write", "T", "W10"), ("write", "T", "B9")],
        )

    def test_flush_preserves_order_so_the_timestamp_stays_last(self):
        # The stockpile snapshot queues its values then its timestamp precisely so the sheet can
        # never claim a freshness it does not have. Reordering would break that silently.
        inner = self.Recorder()
        sheet = sheets.BatchedSheet(inner)
        sheet.write("T", "R11:R16", [[1]])
        sheet.write_cell("T", "W10", "stamp")
        sheet.flush()
        ranges = [r for _a, _t, r in inner.requests[0][1]]
        self.assertEqual(ranges, ["R11:R16", "W10"])

    def test_flushing_nothing_makes_no_request(self):
        inner = self.Recorder()
        sheets.BatchedSheet(inner).flush()
        self.assertEqual(inner.requests, [])

    def test_reading_a_range_with_a_queued_write_flushes_first(self):
        inner = self.Recorder({("T", "B1:B5"): [["fresh"]]})
        sheet = sheets.BatchedSheet(inner, prefetch=[("T", "B1:B5")])
        sheet.write_cell("T", "B3", 7)
        got = sheet.read("T", "B1:B5")
        self.assertEqual(inner.requests[1][0], "batch")     # the flush
        self.assertEqual(inner.requests[2], ("read", "T", "B1:B5"))
        self.assertEqual(got, [["fresh"]])

    def test_a_non_overlapping_queued_write_does_not_force_a_flush(self):
        # Buildings write column B; the stockpile step then reads Q:W. No shared cell, so the
        # prefetched grid is still correct and no round trip is owed.
        inner = self.Recorder({("T", "Q1:W60"): [["q"]]})
        sheet = sheets.BatchedSheet(inner, prefetch=[("T", "Q1:W60")])
        sheet.write_cell("T", "B9", 10)
        self.assertEqual(sheet.read("T", "Q1:W60"), [["q"]])
        self.assertEqual(len(inner.requests), 1)
        self.assertEqual(sheet.pending_writes, 1)   # still queued, not flushed


def replies_from(items):
    queue = list(items)

    def respond(_request):
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return respond


class TabMissingClassificationTests(unittest.TestCase):
    """A missing tab is a configuration fault; everything else is weather. Callers act on that."""

    def test_no_such_tab_is_its_own_exception_type(self):
        with stub_urlopen(lambda req: no_such_tab("Ghost")):
            with self.assertRaises(sheets.SheetTabMissing):
                GoogleSheet().read("Ghost", "A1")

    def test_it_is_still_a_sheet_error_so_existing_handlers_keep_working(self):
        self.assertTrue(issubclass(sheets.SheetTabMissing, SheetError))

    def test_a_timeout_is_not_classified_as_a_missing_tab(self):
        # The distinction that decides whether sheet sync survives a passing Google outage.
        def boom(_req):
            return FailingReadResponse(TimeoutError("The read operation timed out"))

        with stub_urlopen(boom):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).read("T", "A1")
        self.assertNotIsInstance(ctx.exception, sheets.SheetTabMissing)

    def test_other_protocol_errors_are_not_classified_as_a_missing_tab(self):
        page = FakeResponse(json.dumps({"ok": False, "error": "unknown action: frobnicate"}).encode())
        with stub_urlopen(lambda req: page):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet().read("T", "A1")
        self.assertNotIsInstance(ctx.exception, sheets.SheetTabMissing)

    def test_require_tab_raises_the_specific_type(self):
        with stub_urlopen(lambda req: no_such_tab("Ghost")):
            with self.assertRaises(sheets.SheetTabMissing):
                GoogleSheet().require_tab("Ghost")

    def test_require_tab_lets_an_outage_through_unchanged(self):
        def boom(req):
            raise urllib.error.URLError("name resolution failed")

        with stub_urlopen(boom):
            with self.assertRaises(SheetError) as ctx:
                GoogleSheet(retry_delays=()).require_tab("LePone(Z)")
        self.assertNotIsInstance(ctx.exception, sheets.SheetTabMissing)


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
