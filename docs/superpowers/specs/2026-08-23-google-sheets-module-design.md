# Google Sheets read/write module — design

Date: 2026-08-23

## Goal

Give this project a small, self-contained module that reads and updates a **shared** Google
Sheet, so any member who clones the repo can use it with no per-user setup, no Google account,
and no credential file. Target sheet:

    https://docs.google.com/spreadsheets/d/13LWTcalSlpwVAXAnwYo_9hqju5IAosfme5guDToJ3ug/edit

Acceptance task: read `LePone(Z)!R11` (currently `0`), change it, read it back, restore it.

## Why not the Sheets API or the editor endpoint

- **Sheets API (`sheets.googleapis.com`)**: API keys are read-only; every write needs an OAuth or
  service-account identity. That ties a Google account to the project and, for service accounts,
  forces a third-party JWT-signing dependency. Rejected — breaks "self-contained, no identity".
- **Anonymous internal editor RPC**: the editor UI accepts anonymous edits on an "anyone can edit"
  sheet, but only over an undocumented, token-guarded private endpoint. Fragile, breaks without
  notice, and amounts to forging Google's internal API. Rejected.
- **Apps Script web app (chosen)**: a script bound to the sheet, deployed as a web app with
  *Execute as: me* / *Who has access: Anyone*, exposes a stable `/exec` URL. The Google identity
  lives inside the deployment, never in the repo. The repo holds only the URL. Stdlib-only,
  documented, durable. This is the sanctioned way to expose a keyless read/write surface.

Confirmed on the wire before choosing: the tab is anonymously readable via
`/gviz/tq?tqx=out:csv` and `LePone(Z)!R11` reads back as `0`.

## Architecture — two halves, one interface

### 1. Apps Script (pasted onto the sheet once, deployed from the owner's browser)

`doPost(e)` parses a JSON body `{action, tab, range, values}` and dispatches:

- `read`  → `{ok:true, values}` for the tab/range (A1 notation).
- `write` → sets the range from `values`, returns `{ok:true, values}` (the written values).

`values` is always a 2-D array (list of rows) on the wire, even for a single cell, matching the
Sheets `getValues()/setValues()` shape. Failures return `{ok:false, error:"..."}` with HTTP 200
(Apps Script can't set arbitrary status codes reliably), so the client distinguishes success by
the `ok` flag, not the HTTP status.

The script text is committed at **`docs/apps-script/Code.gs`**, with a redeploy walkthrough beside
it in `docs/apps-script/README.md`, so any member can rebuild the deployment if it is ever lost. The
**live `/exec` URL** is what the module uses.

> This spec claimed that source was committed from the day it was written. It was not — the folder
> was created later, while diagnosing the expiring-link fault below. Until then, losing the
> deployment would have meant losing the server side entirely.

`doGet` is defined as well as `doPost`, and only to make one specific failure honest — see
[Google's expiring result link](#googles-expiring-result-link).

### 2. `sheets.py` (this repo, standard library only)

```python
EXEC_URL = "https://script.google.com/macros/s/.../exec"   # committed shared endpoint
SHEET_ID = "13LWTcalSlpwVAXAnwYo_9hqju5IAosfme5guDToJ3ug"   # for reference / anonymous CSV read

class SheetError(RuntimeError): ...

class GoogleSheet:
    def __init__(self, exec_url: str = EXEC_URL, *, timeout: float = 30.0): ...
    def read(self, tab: str, a1: str) -> list[list[str]]: ...          # raw 2-D values
    def write(self, tab: str, a1: str, values) -> list[list[str]]: ... # values: scalar | 1-D | 2-D
    def read_cell(self, tab: str, a1: str) -> str: ...   # convenience: first cell as str
    def write_cell(self, tab: str, a1: str, value) -> str: ...
```

- Transport: `urllib.request` POST of `application/json` to `EXEC_URL`, JSON response parsed with
  `json`. No third-party packages. The `/exec` redirect is followed by hand rather than by urllib,
  so each hop gets its own timeout — see [Timing the hops apart](#timing-the-hops-apart).
- `write` coerces a scalar to `[[v]]` and a flat list to `[[...]]` so callers don't hand-build the
  2-D shape; `read_cell`/`write_cell` unwrap to a single string.
- Any non-200, transport error, non-JSON body, or `{ok:false}` raises `SheetError` with the
  server-supplied message where available. Transient classes are retried first — see
  [Error handling](#error-handling).

Config home: `EXEC_URL` and `SHEET_ID` are **committed constants** in `sheets.py`. The sheet is
shared and the tool is shared, so the endpoint is public by design — no token, nothing git-ignored.

## Nation tab and startup check

Each nation has its own tab, named after the nation (ours is `LePone(Z)`). The tab name is
configuration, not a constant: it comes from **`CLOP_NATION`** in `.env`, resolved by the monitor's
existing `load_env_file` so the rules match the credentials (process environment wins, then `.env`).

`nation_from_env()` returns that name or raises `SheetError` if it is unset. `GoogleSheet.tab_exists`
probes with a trivial `A1` read: success means the tab is present; the endpoint's documented
`no such tab: <name>` protocol error maps to `False`; any other failure (network, dead endpoint)
propagates so an outage is never misread as a missing tab. `require_tab` raises for a missing tab.
`startup_check()` composes these — resolve nation, confirm its tab exists — and is what callers run
first; it raises before any real work if the configuration or the sheet is wrong. Running
`python sheets.py` performs this check read-only and reports pass/fail via exit code.

## Data flow

`GoogleSheet.read/write` → `urllib` POST JSON → `/exec` → Apps Script `doPost` →
`SpreadsheetApp` getValues/setValues → JSON `{ok, values|error}` back → parsed to Python.

## Error handling

Every failure leaves `_call` as a `SheetError` — never as a bare `TimeoutError` or
`http.client.HTTPException`, both of which can escape a `urlopen`/`read()` pair and neither of which
is caught by `sync_sheet_step` or anything above it. One of those escaping would end the monitor
with a traceback and no dialog, which is the single outcome this project refuses to ship.

| Condition                             | Result                                        | Retried? |
|---------------------------------------|-----------------------------------------------|----------|
| Network / DNS (`URLError`)            | `SheetError` wrapping the `urllib` error      | yes      |
| Read timeout (bare `TimeoutError`)    | `SheetError` "timed out reading …"            | yes      |
| Body dies mid-transfer (`IncompleteRead`) | `SheetError` "… broke off part-way"       | yes      |
| HTTP status in `RETRYABLE_HTTP_STATUSES` | `SheetError` with status + body snippet    | yes      |
| Any other HTTP status != 200          | `SheetError` with status + body snippet       | no       |
| Apps Script error page (HTML, HTTP 200) | `SheetError` naming the page's own message  | yes      |
| Body not JSON, not that page          | `SheetError` quoting `Content-Type` + snippet | yes      |
| `{ok:false, error, retry:true}`       | `SheetError(error)`                           | yes      |
| `{ok:false, error}`                   | `SheetError(error)`                           | no       |
| Unknown tab / bad range               | Surfaces as `{ok:false}` from the script      | no       |

### Why the retry line falls where it does

The rule is **everything between asking and being answered is retried; the endpoint's own verdict is
not**. A `{ok:false}` reply means the Apps Script ran and decided — `no such tab` will still be true
in three seconds, so retrying it only delays a dialog the user has to act on either way.

The non-JSON row is the subtle one, and it was originally on the wrong side of that line. The parse
sat *after* the retry loop, so a 200 carrying HTML — which is exactly how Apps Script reports a
stumble on the `script.googleusercontent.com` hop it redirects to — took zero retries and popped a
dialog on first sight. It also asserted a cause it could not know ("is the deployment still 'Anyone'
access?"): Google serves HTML both for a lost deployment (a sign-in page) and for a passing hiccup,
and one sample cannot separate them. The message now quotes the `Content-Type` and the first 200
characters of what arrived, and only names the deployment as a likely cause after three failures.

Retrying a **write** after a garbled reply is safe because the payload is an assignment, not an
adjustment: if Google applied the first one and lost the response, the second writes the same value.
A test pins that the retried payload is byte-identical, so this stops being true loudly.

### Google's expiring result link

The dominant failure in production, and the reason `doGet` exists. Established by direct
measurement against the live endpoint, not inference:

`/exec` **does not return the result of a POST.** It runs the script, then 302s to a one-shot
`script.googleusercontent.com/macros/echo?user_content_key=…` link holding the output. That link:

- is **consumed by the first read** — refetching the same link immediately returns the error page;
- **expires on a timer** — measured alive at 15s, dead at 30s, on a link never read at all.

Read twice or read late, Google does not 404. It **falls back to invoking the deployment over GET**.
With only `doPost` defined that produced `Script function not found: doGet`, served as ~5KB of HTML
with **HTTP 200** — a clean success by every signal except the JSON parse.

Three consequences shaped the design:

1. **It must be retried, and retrying is a real fix**, because every retry is a fresh POST and so a
   fresh link. The window widened from `(1, 3)` to `(1, 3, 8)` after a live failure consumed all
   three of the old attempts inside about four seconds.
2. **The message must not blame the deployment.** The first version of this error asked "is the
   deployment still 'Anyone' access?" — and 12 live calls immediately afterwards all returned clean
   JSON, so the deployment was healthy and the advice would have sent the reader to redeploy for
   nothing. The client now reads the page's own one line of body text and reports it.
3. **`doGet` returns `{ok:false, retry:true}`, not a bare `{ok:false}`.** Every other `{ok:false}`
   is a definitive verdict the client does not retry. Without the flag, redeploying would have
   traded an HTML page that *is* retried for a JSON error that is *not* — a regression wearing the
   costume of a fix.

`doGet` cannot do the work itself: the fall-through GET carries only Google's `user_content_key` and
`lib`, never the caller's action/tab/range. Reporting honestly is the most it can do.

#### What actually trips it

Caught live by a probe that timed the two hops separately. Across 56 clean polls the endpoint
answered in 3–6 s; then, inside a single poll:

```
poll 14: hop1= 2.7s  hop2 HTTP 404
poll 14: hop1=11.6s  hop2 HTTP 404
poll 14: hop1=21.8s  hop2=16.3s  *** doGet error page, 5396 bytes ***
```

The trigger is **latency on the second hop**, not request rate (45 back-to-back reads reproduced
nothing) and not cookie or opener state. Google slows down; the script execution stretches from
2.7 s to 21.8 s; and the fetch of the result link itself takes 16.3 s — long enough that the link's
15–30 s lifetime **runs out while that fetch is still in flight**. Google then falls back to the GET
invocation and answers with the error page.

The two `HTTP 404`s just before it are the same fault a moment earlier: the link already gone rather
than expiring mid-fetch. 404 is in `RETRYABLE_HTTP_STATUSES`, so those were already being retried
correctly — which is why only the third one reached the user.

This also settles what *not* to do. Capping the first hop looks appealing but is useless: the link's
clock starts when the 302 is issued, i.e. after hop 1 finishes, so a slow execution hands over a
perfectly fresh link. Only hop 2's own latency can consume it.

#### Timing the hops apart

Following on from the above, `_fetch` makes both hops itself instead of letting urllib follow the
redirect, so each gets its own timeout. A later sample during a bad patch made the case plainly —
2 of 6 calls succeeded in 3.5–6.5 s while all 4 failures took 18–33 s:

Timed apart over many calls, hop 2 turns out to be **bimodal with nothing in between**:

```
hop 2 succeeds -> 0.5, 0.6, 0.5, 0.6 s
hop 2 fails    -> 10.2, 10.3, 14.3 s, then a dead-link 404
```

There is no such thing as a slow success. That single fact sets the budgets:

**The same is true of hop 1**, which took two goes to accept. Timed apart, the two hops move
together and the populations never overlap:

```
hop 1  2.3 - 3.2s  ->  hop 2 answers in 0.5s with JSON   -- every time
hop 1  over 6s     ->  hop 2 fails                       -- every time
                       (6.2, 7.1, 11.0, 12.5, 21.8, 32.6 observed)
```

Whatever congestion stretches the script run also kills the result link it hands back. So a slow
hop 1 is not a slow success either: it is an early announcement that the attempt has already
failed, and every second spent after that is waste.

> An earlier revision of this document claimed "21.8 s measured on a call that still succeeded".
> That was wrong — checking the log, that call returned the `doGet` error page. No slow hop 1 has
> ever been observed to succeed, and the mistaken figure was the reason `DEFAULT_TIMEOUT` was left
> far too generous.

| Hop | Budget | Why |
|---|---|---|
| 1 — POST `/exec`, runs the script | `DEFAULT_TIMEOUT` **8 s** | Slowest success ever measured is 3.2 s; the fastest failure is 6.2 s. 8 admits every success with room and abandons the doomed early |
| 2 — GET the result link | `CONTENT_TIMEOUT` **3 s** | Success is 0.5 s. Anything still running at 3 has already failed and is taking its time to say so |

At 20 s and 12 s a doomed attempt cost the better part of half a minute, so nine of them spent
**158 seconds discovering nothing** — the `after 9 attempts in 158s` dialog. At 8 and 3 the same
nine attempts take about a minute and each is a real chance rather than a wait.

#### Naming the hop

Both hops used to fail with the identical message, `timed out reading the sheet endpoint's reply`,
which made that production dialog impossible to diagnose from its own text — the only way to learn
which hop had timed out was to go and re-time them by hand. `_fetch` now tags the exception with
`HOP_SCRIPT` or `HOP_RESULT` and the message names it. Tagged rather than wrapped, so every handler
in `_call` still matches on the real exception type and only the wording gains the detail.

`CONTENT_TIMEOUT` is the most important number in this module. At 12 s, two doomed attempts consumed
an entire budget — which is exactly what the production message `after 2 attempts in 45s` meant. At
3 s a doomed attempt costs hop 1 plus 3, so the same wall-clock funds five or six attempts.

#### Retry shape

`DEFAULT_RETRY_DELAYS` is `(0, 0.25, 0.5, 0.5, 1, 1, 2, 2)` — nine attempts, almost no sleeping.
Failures are **independent, not sustained**: timed back to back, successes and failures interleave
(ok, ok, fail, ok, fail, fail) rather than arriving in blocks, so a fresh POST is a genuinely fresh
roll of the dice. Backing off politely just spends the budget on sleeping; the previous `(1, 3, 8)`
burnt 12 of 45 seconds doing nothing.

#### The deadline, and the constraint that was invented

`DEFAULT_DEADLINE` is 180 s: a backstop against a wedged call, **not** a retry budget. Ordinary
retrying never reaches it. Each hop's timeout is still clamped to what is left (floored at
`MIN_TIMEOUT`, since zero means "non-blocking" and fails instantly with a misleading error) —
without that clamp the deadline only stops the *next* retry while the attempt in flight runs on,
measured at 50.7 s against a 45 s budget.

The earlier value was 45 s, justified like this: *the monitor polls every 60 s, so a sheet call must
not stall it.* **That premise was false.** The polling loop is work-then-`sleep(interval)` — the
interval is a fixed pause *between* cycles, not a schedule a cycle must fit inside. Nothing queues
behind a slow cycle and nothing overlaps.

The one real cost was ordering: `sync_sheet_step` ran *before* `check_and_notify`, so every second
the sheet spent was a second the alerts were late. The fix is to run the sheet **last**, not to cut
the retries short — and a test pins that order, because it is what licenses the generous retrying.

Measured after the change, against the same endpoint that had been failing 3 of 6: **12 of 12
succeeded, median 3.2 s**. One took 61 s of retrying and still got through, where the old budget
would have given up and raised a dialog.

#### Why this surfaced when it did — and the real fix

The client-side work above makes the fault survivable. It does not explain why the monitor met it so
often, and that explanation turned out to matter more than any of the handling.

A poll ran **15 sheet round trips** (11 with no building corrections outstanding), measured directly
off `sync_sheet_step` against the offline fake:

```
read  <nation> A1:B130          write_cell B11, B15, B64, B28
read  <nation> Q1:W60           write R11:R16 ; write_cell W10
read  Dashboard A1:Z80          write C2:C5, C7:C12, C14:C15, C17:C27, C29:C34, C36:C49
```

At ~3.5 s each that is 40–50 seconds of Google traffic inside every 60-second poll — near-continuous
load. The Dashboard sync (`ff39678`, `db66103`, 2026-08-24) contributed 7 of those calls and took a
poll from ~4 round trips to ~11. If one call fails with probability *p*, a poll survives with
(1−p)^N, so N: 4 → 11 more than doubles the per-poll failure rate **with Google behaving
identically**. "It used to work, now it is intermittent" and that commit are the same event.

And every one of those calls was redundant. The game tick is ~2 hours; stockpiles, buildings and
status only move on a tick or on a player trade. The sync rewrote identical numbers ~120 times per
tick.

`sync_sheet_step` now keeps a digest of the last fully clean sync (`sheet_fingerprint`) and returns
it to the caller to pass back next poll. Matching digest ⇒ the step is skipped entirely, zero round
trips. Ten polls cost ~22 round trips instead of 110; across a full tick the saving is >99%.

Three things make it correct rather than merely cheaper:

1. **It compares against what it last wrote, not against the sheet.** `stockpiles.py`'s docstring
   rejects diff-against-the-sheet for a good reason — an unreadable cell normalises to `0` and
   compares equal for a good the nation holds none of, so the garbage survives while the timestamp
   claims the row was verified. Trusting its own record of what it sent has no such hole, and when
   it does write, the write is as unconditional as it ever was.
2. **`server_time` is excluded from the digest.** It advances every poll, so including it would make
   every poll look like a change and skip nothing. This is the trap that would have silently
   disabled the whole thing, so there is a test named for it.
3. **Only a fully clean run is cached.** A failure, or a region skipped for a layout problem, leaves
   the sheet disagreeing with the game; caching that as "in sync" would bury the disagreement until
   the numbers happened to move.

The cost is a real one and is documented in the README: `W10` changes meaning from *last checked* to
*last changed*, so it is no longer a strict dead-man's switch. A tick always moves something, so a
`W10` older than a couple of hours still indicts the monitor — but between ticks a static stamp is
now normal. Stamping `W10` alone every poll would restore the switch for 1 round trip instead of 11
if that trade is ever wanted.

#### The other face of the same fault

An expired link does not always reach `doGet`. Caught a beat earlier, Google's Drive front-end
answers it with **HTTP 404** and its own "Page not found — Sorry, unable to open the file at
present" page. 404 is already in `RETRYABLE_HTTP_STATUSES`, so this was being handled correctly;
what was wrong was the reporting. That page is 7,805 bytes whose first 200 characters are entirely
`window['ppConfig']` telemetry, so quoting bytes showed the reader nothing at all.

`_snippet` now reduces any HTML body to its visible words, which is what makes both pages legible.
`apps_script_error` stays a separate, stricter check — it needs *two* markers, because the Drive
page carries the same telemetry preamble and must not be mistaken for the `doGet` fault.

## Testing

- **Unit (offline, in `test_clop_monitor.py` or a sibling test):** monkeypatch the module's
  `urllib.request.urlopen` to assert request URL/method/JSON body and to feed canned responses.
  Cover: read parses values; write coerces scalar/1-D/2-D and posts the right body; each error row
  above raises `SheetError`.
- **Live acceptance (run once by hand after deploy):** `read_cell("LePone(Z)", "R11")` == `"0"` →
  `write_cell("LePone(Z)", "R11", "42")` → `read_cell` == `"42"` → `write_cell(..., "0")` to
  restore. This is the exact task the module was asked to perform.

## Out of scope (YAGNI)

No batch/append/formatting/formula API, no auth token, no caching. Just `read`/`write` by A1 range,
which already covers "read/update sheets etc." Extend only when a caller needs more.
