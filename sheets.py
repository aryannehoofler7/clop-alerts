#!/usr/bin/env python3
"""Keyless read/write access to the shared CLOP planning Google Sheet.

The sheet is shared ("anyone with the link can edit"), and so is this tool: every member who
clones the repo can read and update it with no Google account, no credential file, and no setup.

Writes to Google Sheets always require an identity *somewhere* -- API keys are read-only. We keep
that identity out of the repo by routing through an Apps Script web app bound to the sheet and
deployed as "execute as owner / accessible to anyone". The Google login lives inside that
deployment; this module holds only its public ``/exec`` URL. See
``docs/superpowers/specs/2026-08-23-google-sheets-module-design.md`` for the full rationale and the
Apps Script source to redeploy from if the deployment is ever lost.

Everything here is Python standard library only, matching the rest of this project.

    >>> sheet = GoogleSheet()
    >>> sheet.read_cell("LePone(Z)", "R11")
    0
    >>> sheet.write_cell("LePone(Z)", "R11", 42)
    42
"""

from __future__ import annotations

import html
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Reuse the monitor's KEY=VALUE .env reader and default path so the nation name is resolved with the
# exact same rules as the credentials (process environment wins, then the .env file). Importing it
# has no side effects -- clop_monitor only runs anything under its own __main__ guard.
from clop_monitor import DEFAULT_ENV_PATH, load_env_file


#: Public Apps Script web-app endpoint bound to the shared sheet. Committed on purpose: the sheet
#: is shared, so the tool is too. Redeploy the script in the spec doc and replace this if it dies.
EXEC_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbxq-yk5Yd5kbBOAm7HKGXWDqsQg85XMTiO80HUCLR91PhoOAyFjLoFwBTh9_uSCYdc3uQ/exec"
)

#: The spreadsheet the endpoint is bound to. Kept for reference and for anonymous CSV reads
#: (``https://docs.google.com/spreadsheets/d/<id>/export?format=csv``), which need no endpoint.
SHEET_ID = "13LWTcalSlpwVAXAnwYo_9hqju5IAosfme5guDToJ3ug"

#: Budget for the first hop -- POSTing to /exec, which runs the script.
DEFAULT_TIMEOUT = 20.0

#: Budget for the second hop -- fetching the one-shot result link.
#:
#: Set from measurement, and the single most important number here. Timed separately over many
#: calls, hop 2 is bimodal with nothing in between:
#:
#:     succeeds -> 0.5, 0.6, 0.5, 0.6 seconds
#:     fails    -> 10.2, 10.3, 14.3 seconds, then a dead-link 404
#:
#: There is no such thing as a slow success. A hop 2 still running at 3 seconds has already failed
#: and is merely taking its time to say so, and every one of those seconds is stolen from the
#: retries that would actually have fixed the call. At 12 seconds two doomed attempts ate a whole
#: 45-second budget, which is exactly what "after 2 attempts in 45s" meant. At 3 seconds a doomed
#: attempt costs hop 1 plus 3, so the same budget buys five or six rolls of the dice instead.
CONTENT_TIMEOUT = 3.0

#: Backstop against a hung endpoint -- NOT a retry budget. It is set high enough that ordinary
#: retrying never reaches it, and only a genuinely wedged call does.
#:
#: The earlier 45 seconds was an invented constraint. It was justified by "the monitor polls every
#: 60 seconds, so a sheet call must not stall it", and that premise is simply false: the loop is
#: work-then-``sleep(interval)``, so the interval is a fixed pause *between* cycles, not a
#: schedule a cycle has to fit inside. Nothing queues up behind a slow cycle and nothing overlaps.
#: The only real cost was that sheet sync ran before the alerting, so its seconds were the alerts'
#: seconds -- and that is fixed properly by running it last, not by cutting the retries short.
#: Strangling the retries to satisfy a constraint that did not exist is what produced "after 2
#: attempts in 45s" on a call that had eight attempts available to it.
DEFAULT_DEADLINE = 180.0

#: Floor on a clamped hop timeout. A budget that has almost run out must still leave enough for a
#: connection to be attempted rather than collapsing to zero, which means "non-blocking" and fails
#: instantly with a misleading error.
MIN_TIMEOUT = 1.0

#: One initial request plus these three retries. Writes are explicit assignments, so repeating the
#: identical payload is idempotent if Google applied it but dropped the response.
#:
#: Everything that can go wrong *between* asking and being answered is retried, because none of it
#: distinguishes "Google hiccuped" from "Google is broken" on a single sample: transport failures,
#: the HTTP statuses below, a reply that dies mid-transfer, and a perfectly ordinary HTTP 200 whose
#: body is not JSON. Only the endpoint's own protocol replies (``{ok: false}``, e.g. ``no such
#: tab``) are definitive and fail on the first attempt.
#:
#: Many cheap attempts, barely any sleeping.
#:
#: Failures here are independent, not sustained: timed back to back, successes and failures
#: interleave (ok, ok, fail, ok, fail, fail) rather than arriving in blocks. So a fresh POST really
#: is a fresh roll of the dice, and the way to make a call reliable is to roll more often -- not to
#: wait politely between rolls, which only spends the budget on sleeping. The old (1, 3, 8) burnt
#: 12 of 45 seconds doing nothing at all.
#:
#: Nine attempts at a measured ~50% per-attempt success during a bad patch puts a single call's
#: failure odds near 0.2%; the deadline stops it early when attempts are running slow.
DEFAULT_RETRY_DELAYS = (0.0, 0.25, 0.5, 0.5, 1.0, 1.0, 2.0, 2.0)
RETRYABLE_HTTP_STATUSES = frozenset({404, 408, 425, 429, 500, 502, 503, 504})

#: How Google Apps Script answers a POST, and the failure mode that falls out of it.
#:
#: ``/exec`` does not return the result. It runs the script and then 302s to a one-shot
#: ``script.googleusercontent.com/macros/echo?user_content_key=...`` link holding the output. That
#: link is consumed by the first read **and** expires on a timer -- measured on this deployment as
#: alive at 15 seconds and dead at 30. Read it twice, or late, and Google does not 404: it falls
#: back to invoking the deployment over GET, which calls ``doGet``. A deployment that defines only
#: ``doPost`` therefore answers ``Script function not found: doGet`` -- as an HTML page, with
#: HTTP 200, which is why this looks like a clean success right up until the JSON parse.
#:
#: ``docs/apps-script/Code.gs`` now defines ``doGet`` so that fall-through returns JSON instead;
#: until that is redeployed, the live endpoint still produces the HTML page and this client
#: recognises it by name. See ``docs/apps-script/README.md``.
EXPIRING_LINK = "Script function not found: doGet"

#: Two markers that together identify a Google Apps Script HTML error page. Matched as a pair
#: because either alone appears on unrelated Google pages.
_APPS_SCRIPT_PAGE_MARKERS = ('alt="Google Apps Script"', "<title>Error</title>")

#: Environment variable naming the sheet tab for your nation (its value is the exact tab name, e.g.
#: ``LePone(Z)``). Set it in ``.env`` alongside CLOP_USERNAME / CLOP_PASSWORD.
NATION_ENV = "CLOP_NATION"

Grid = List[List[Any]]


def cell_int(value: Any) -> int:
    """Normalise a value the sheet handed back into an integer; an empty cell is zero.

    The sheet returns real numbers as ``int``/``float`` and everything else as text, so a cell
    somebody typed ``1,204`` into arrives as a string. Text that is not a whole number -- a label,
    a formula error, the string ``"1.5"`` -- reads as zero, because these cells are all counts.

    A cell holding a real *number* is truncated rather than zeroed, so a numeric ``1.5`` gives
    ``1``. Nothing here writes a fraction, so this only arises if a person types one in; truncating
    it means the next reconcile quietly corrects the cell instead of flagging it, which is the
    milder of the two wrong answers available.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    return int(text) if re.fullmatch(r"-?\d+", text) else 0


def column_letter(index: int) -> str:
    """Convert a 0-based column index to its A1 letter: ``0 -> "A"``, ``26 -> "AA"``.

    Past ``Z`` matters here: the Dashboard is one column per nation, so the alliance growing walks
    it toward ``AA``. A single-letter shortcut would fail silently on the tenth nation.
    """
    if index < 0:
        raise ValueError(f"column index must be >= 0, got {index}")
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def find_in_row(row: Sequence[Any], text: str) -> Optional[int]:
    """Return the 0-based index of the cell in ``row`` equal to ``text``, or ``None``.

    Compared exactly after stripping both sides. A substring match would let ``LePone`` resolve to
    the ``LePone(Z)`` column, and the tab name is the whole cell or nothing.
    """
    wanted = text.strip()
    for index, cell in enumerate(row):
        if cell is not None and str(cell).strip() == wanted:
            return index
    return None


def index_column(grid: Grid) -> Dict[str, List[int]]:
    """Map each non-empty column-A label to the **1-based** rows it occupies.

    Every occurrence is returned, not just the first: reporting duplicates is what lets a caller
    refuse to write a sheet where a label has been pasted twice and the right row is ambiguous.
    """
    found: Dict[str, List[int]] = {}
    for number, row in enumerate(grid, 1):
        cell = row[0] if len(row) > 0 else None
        label = "" if cell is None else str(cell).strip()
        if label:
            found.setdefault(label, []).append(number)
    return found


class SheetError(RuntimeError):
    """Any failure reading or writing the sheet: transport, bad response, or a server-side error."""


class SheetTabMissing(SheetError):
    """A named tab is genuinely absent from the sheet.

    Kept apart from every other ``SheetError`` because the difference decides whether a caller
    should give up or try again. A missing tab is a configuration fault: it will still be a fault
    in a minute, and somebody has to go and fix it. A timeout or an unreachable endpoint is
    weather. Treating the second as the first is how one passing Google outage at startup used to
    switch sheet sync off for an entire session.
    """


#: The endpoint's wording for a missing tab, matched to raise ``SheetTabMissing``. Load-bearing on
#: both sides -- ``docs/apps-script/Code.gs`` says so too. Do not reword either without the other.
TAB_MISSING = "no such tab"


def visible_text(markup: str) -> str:
    """Reduce an HTML page to the words a person would actually see, on one line.

    Google's error pages -- Apps Script's and Drive's alike -- open with kilobytes of telemetry
    (``window['ppConfig'] = ...``) and inline CSS, so quoting their first 200 characters shows
    nothing at all. The sentence that matters is in the body: ``Script function not found: doGet``,
    ``Sorry, unable to open the file at present``.
    """
    start = markup.find("<body")
    body = markup[start:] if start != -1 else markup
    body = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())


def apps_script_error(raw: bytes) -> Optional[str]:
    """Return the message from a Google Apps Script HTML error page, or ``None`` if not one.

    Matched on two markers together, because either alone appears on unrelated Google pages -- in
    particular Drive's own "Page not found", which carries the same telemetry preamble but is a
    different fault and must not be mistaken for this one.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not all(marker in text for marker in _APPS_SCRIPT_PAGE_MARKERS):
        return None
    return visible_text(text) or None


def _snippet(raw: bytes, limit: int = 200) -> str:
    """Render a response body as one short readable line, for quoting in an error message.

    These messages are read in a popup, and the bodies being quoted are HTML pages full of
    newlines and indentation, so the whitespace is collapsed. An empty body is named rather than
    quoted as nothing -- "the endpoint sent no body at all" is a distinct and useful symptom.
    """
    decoded = raw.decode("utf-8", "replace")
    lowered = decoded[:400].lower()
    if "<html" in lowered or "<!doctype html" in lowered:
        text = visible_text(decoded)
    else:
        text = " ".join(decoded.split())
    if not text:
        return "(empty body)"
    return text[:limit] + "..." if len(text) > limit else text


def nation_from_env(env_path: Path = DEFAULT_ENV_PATH) -> str:
    """Return the configured nation tab name, or raise ``SheetError`` if it is not set.

    Resolution matches the credentials: a value already in the process environment wins, otherwise
    the ``.env`` file is consulted.
    """
    value = os.environ.get(NATION_ENV) or load_env_file(env_path).get(NATION_ENV)
    if not value:
        raise SheetError(
            f"{NATION_ENV} is not set. Add it to .env as {NATION_ENV}=<your nation> "
            "(e.g. LePone(Z)); its value is the exact name of your nation's tab in the sheet."
        )
    return value


def _as_grid(values: Any) -> Grid:
    """Coerce a scalar / 1-D row / 2-D block into the 2-D shape the Sheets API expects.

    ``5`` -> ``[[5]]``; ``[1, 2]`` -> ``[[1, 2]]`` (one row); ``[[1], [2]]`` stays as-is (a column).
    Strings are treated as a single scalar cell, not as a sequence of characters.
    """
    if isinstance(values, str) or not isinstance(values, Sequence):
        return [[values]]
    rows = list(values)
    if rows and all(
        isinstance(r, Sequence) and not isinstance(r, str) for r in rows
    ):
        return [list(r) for r in rows]
    return [list(rows)]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop urllib swallowing the /exec redirect, so the two hops can be capped separately.

    Returning ``None`` leaves the 302 to surface as an ``HTTPError`` carrying the ``Location``.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    """Opener for the first hop, which must not follow the redirect.

    A module-level function rather than an inline call so the offline tests have one seam to
    substitute, the same way they already substitute ``urlopen`` for the second hop.
    """
    return urllib.request.build_opener(_NoRedirect)


class GoogleSheet:
    """Read and write ranges of the shared sheet by A1 notation, via the Apps Script endpoint."""

    def __init__(
        self,
        exec_url: str = EXEC_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        content_timeout: float = CONTENT_TIMEOUT,
        deadline: float = DEFAULT_DEADLINE,
        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        self.exec_url = exec_url
        self.timeout = timeout
        self.content_timeout = content_timeout
        self.deadline = deadline
        self.retry_delays = tuple(retry_delays)

    def read(self, tab: str, a1: str) -> Grid:
        """Return the values in ``tab!a1`` as a 2-D list of rows (as the sheet stores them)."""
        return self._call("read", tab, a1)

    def write(self, tab: str, a1: str, values: Any) -> Grid:
        """Write ``values`` to ``tab!a1`` and return what the sheet holds afterward.

        ``values`` may be a scalar, a flat row, or a 2-D block; it is coerced to 2-D. Its shape
        must match ``a1`` (a 1x1 range takes one cell, a 2x3 range takes two rows of three).
        """
        return self._call("write", tab, a1, _as_grid(values))

    def read_cell(self, tab: str, a1: str) -> Any:
        """Read a single-cell range and return its lone value (``""`` if the cell is empty)."""
        return self._first(self.read(tab, a1))

    def write_cell(self, tab: str, a1: str, value: Any) -> Any:
        """Write one value to a single cell and return the stored result."""
        return self._first(self.write(tab, a1, value))

    def tab_exists(self, tab: str) -> bool:
        """Return whether ``tab`` is a tab in the sheet.

        Probes with a trivial read of ``A1``: a success means the tab is there. The endpoint's
        documented ``no such tab: <name>`` arrives as ``SheetTabMissing`` and maps to ``False``;
        any other failure -- a network drop, a dead endpoint -- propagates, so a mere outage is
        never mistaken for a missing tab.
        """
        try:
            self.read(tab, "A1")
        except SheetTabMissing:
            return False
        return True

    def require_tab(self, tab: str) -> None:
        """Raise ``SheetTabMissing`` unless ``tab`` exists; other failures propagate as-is."""
        if not self.tab_exists(tab):
            raise SheetTabMissing(
                f"nation tab {tab!r} does not exist in the shared sheet. Check {NATION_ENV} names a "
                "tab exactly -- it is case-, spacing-, and punctuation-sensitive."
            )

    # -- internals -------------------------------------------------------------------------

    @staticmethod
    def _first(grid: Grid) -> Any:
        for row in grid:
            for cell in row:
                return cell
        return ""

    def _retry_or_raise(
        self,
        message: str,
        attempt: int,
        cause: Optional[BaseException] = None,
        *,
        retryable: bool = True,
        hint: str = "",
        started: Optional[float] = None,
    ) -> None:
        """Return (so the caller retries) if another attempt is due, else raise ``SheetError``.

        Every transient class funnels through here so they all spend the same budget and report it
        the same way. ``hint`` is appended only to the final message, where it can say what a run
        of failures -- as opposed to one -- tends to mean.

        Two things end the retries: running out of attempts, and running out of ``deadline``. The
        clock matters as much as the count, because a Google slow patch makes every attempt take
        tens of seconds, and a monitor stuck in here is not watching the game.
        """
        delay = self.retry_delays[attempt] if attempt < len(self.retry_delays) else None
        elapsed = 0.0 if started is None else time.monotonic() - started
        out_of_time = delay is not None and elapsed + delay > self.deadline
        if retryable and delay is not None and not out_of_time:
            time.sleep(delay)
            return

        if attempt:
            spent = f" in {elapsed:.0f}s" if started is not None and elapsed else ""
            suffix = f" after {attempt + 1} attempts{spent}"
            if out_of_time:
                suffix += f" (stopped at the {self.deadline:.0f}s budget)"
        else:
            suffix = ""
        text = message + suffix + hint
        if cause is None:
            raise SheetError(text)
        raise SheetError(text) from cause

    def _fetch(self, encoded: bytes, budget: Optional[float] = None) -> "tuple[int, str, bytes]":
        """One full round trip, as ``(status, content_type, body)``.

        Both hops are made here rather than left to urllib so each gets its own timeout: the first
        runs the script and may legitimately take half a minute, while the second only fetches an
        already-computed result down a link that will be dead within seconds. Letting one number
        cover both meant waiting 30 seconds for a link that stopped being worth anything at 15.

        ``budget`` is whatever is left of the caller's deadline. Each hop's timeout is clamped to
        it, so the deadline is a real ceiling rather than merely the point at which retrying stops
        -- otherwise a final attempt could run for another 42 seconds past it.
        """
        start = time.monotonic()

        def cap(limit: float) -> float:
            # Floor the *budget*, then apply the configured limit -- never the other way round, or
            # the floor would quietly raise a deliberately small timeout above what was asked for.
            if budget is None:
                return limit
            left = budget - (time.monotonic() - start)
            return min(limit, max(MIN_TIMEOUT, left))

        request = urllib.request.Request(
            self.exec_url,
            data=encoded,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        location = None
        try:
            with _build_opener().open(request, timeout=cap(self.timeout)) as response:
                # No redirect at all: the endpoint answered the POST directly.
                return (
                    response.status,
                    response.headers.get("Content-Type", "unset"),
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
            if not location:
                raise
            exc.close()

        with urllib.request.urlopen(location, timeout=cap(self.content_timeout)) as response:
            return (
                response.status,
                response.headers.get("Content-Type", "unset"),
                response.read(),
            )

    def _call(self, action: str, tab: str, a1: str, values: Any = None) -> Grid:
        payload = {"action": action, "tab": tab, "range": a1}
        if values is not None:
            payload["values"] = values
        encoded = json.dumps(payload).encode("utf-8")
        started = time.monotonic()
        for attempt in range(len(self.retry_delays) + 1):
            try:
                status, content_type, raw = self._fetch(
                    encoded, self.deadline - (time.monotonic() - started)
                )
            except urllib.error.HTTPError as exc:
                detail = _snippet(exc.read())
                self._retry_or_raise(
                    f"HTTP {exc.code} from sheet endpoint: {detail}",
                    attempt,
                    exc,
                    retryable=exc.code in RETRYABLE_HTTP_STATUSES,
                    started=started,
                )
                continue
            except urllib.error.URLError as exc:
                self._retry_or_raise(
                    f"could not reach sheet endpoint: {exc.reason}",
                    attempt,
                    exc,
                    started=started,
                )
                continue
            except TimeoutError as exc:
                # A socket timeout raised while read() is consuming the body is a bare
                # TimeoutError, not a URLError. Uncaught it is not even a SheetError, and nothing
                # above sync_sheet_step catches it -- it would end the monitor with a traceback
                # and no dialog. Same trap clop_monitor's own client fell into.
                self._retry_or_raise(
                    "timed out reading the sheet endpoint's reply",
                    attempt,
                    exc,
                    hint=(
                        ". Google was answering more slowly than its own result links stay "
                        "alive, so waiting longer would only have collected a dead one. Each "
                        "attempt asked again from scratch; this clears once Google speeds up"
                    ),
                    started=started,
                )
                continue
            except http.client.HTTPException as exc:
                # IncompleteRead and friends: neither an HTTPError nor a URLError, so likewise
                # invisible to both handlers above.
                self._retry_or_raise(
                    "the sheet endpoint's reply broke off part-way "
                    f"({exc.__class__.__name__})",
                    attempt,
                    exc,
                    started=started,
                )
                continue

            if status != 200:
                self._retry_or_raise(
                    f"HTTP {status} from sheet endpoint: {_snippet(raw)}",
                    attempt,
                    retryable=status in RETRYABLE_HTTP_STATUSES,
                    started=started,
                )
                continue

            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                # A 200 whose body is not JSON. Which one it is matters a great deal to whoever
                # reads the dialog, so name it rather than dumping bytes: Apps Script's own error
                # page is a Google-side fault that clears itself, while a sign-in page means the
                # deployment needs redeploying. Telling the user to go and redeploy a perfectly
                # healthy deployment is the wrong answer, and was the first version of this.
                detail = apps_script_error(raw)
                if detail and EXPIRING_LINK in detail:
                    message = (
                        "Google returned the sheet result too late to be read: "
                        f"'{detail}'"
                    )
                    hint = (
                        ". Nothing is wrong with your sheet, your data, or the deployment's "
                        "access setting. Apps Script answers a POST by redirecting to a one-shot "
                        "result link that also expires after about 20 seconds; read twice or read "
                        "late, it falls back to running the script over GET, which this "
                        "deployment does not implement. Every retry starts a fresh request, so "
                        "this clears on its own once Google speeds back up"
                    )
                elif detail:
                    message = f"the sheet endpoint returned an Apps Script error page: '{detail}'"
                    hint = ""
                else:
                    message = (
                        f"unexpected non-JSON reply from sheet endpoint "
                        f"(Content-Type: {content_type}): {_snippet(raw)}"
                    )
                    hint = (
                        ". One garbled reply is a Google hiccup; several in a row usually mean "
                        "the Apps Script deployment has stopped being 'Anyone' access and Google "
                        "is serving a sign-in page instead"
                    )
                self._retry_or_raise(message, attempt, exc, hint=hint, started=started)
                continue

            if not isinstance(body, dict) or not body.get("ok"):
                message = ""
                if isinstance(body, dict):
                    message = str(body.get("error", ""))
                # ``retry: true`` is the endpoint saying "this one is worth asking again" -- the
                # ``doGet`` reply uses it, because an expired result link is a Google-side hiccup
                # and not the definitive verdict that every other {ok:false} represents. Without
                # this flag, redeploying with doGet would trade an HTML page that gets retried for
                # a JSON error that does not, which is a worse outcome, not a better one.
                if isinstance(body, dict) and body.get("retry"):
                    self._retry_or_raise(
                        message or "sheet endpoint asked for the request to be repeated",
                        attempt,
                        started=started,
                    )
                    continue
                if TAB_MISSING in message.lower():
                    raise SheetTabMissing(message)
                raise SheetError(message or "sheet endpoint reported failure")
            return body.get("values", [])

        raise AssertionError("unreachable: the loop either returns or raises")  # pragma: no cover


def startup_check(
    exec_url: str = EXEC_URL, env_path: Path = DEFAULT_ENV_PATH
) -> "tuple[GoogleSheet, str]":
    """Resolve the configured nation and confirm its tab exists before any work begins.

    Raises ``SheetError`` if ``CLOP_NATION`` is unset, its tab is missing, or the sheet is
    unreachable. Returns the connected ``GoogleSheet`` and the nation name on success.
    """
    nation = nation_from_env(env_path)
    sheet = GoogleSheet(exec_url)
    sheet.require_tab(nation)
    return sheet, nation


if __name__ == "__main__":
    import sys

    # Startup check: resolve CLOP_NATION and verify its tab exists, then read one cell to prove the
    # round trip. Read-only -- it never edits the sheet.
    from clop_monitor import popup_failure

    # The read is inside the guard too, not just the startup check. It used to sit outside, so a
    # tab that resolved followed by a slow endpoint killed this script with a raw traceback -- the
    # exact thing the guard exists to prevent, in the one script whose whole job is diagnosing the
    # endpoint. Every other standalone here wraps its whole body; this one now does as well.
    try:
        sheet, nation = startup_check()
        print(f"Nation tab {nation!r} found.")
        print(f"{nation}!R11 = {sheet.read_cell(nation, 'R11')!r}")
    except SheetError as error:
        # A dialog, not a terminal line -- the same rule the monitor and the other two scripts
        # follow. A failure nobody sees is the one this project refuses to ship.
        popup_failure(f"The sheet check failed: {error}")
        sys.exit(1)
