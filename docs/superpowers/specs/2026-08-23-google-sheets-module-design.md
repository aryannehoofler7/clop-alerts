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

The script text is committed to the repo (see `docs/` reference below) so any member can
re-deploy it if the deployment is ever lost, but the **live `/exec` URL** is what the module uses.

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
  `json`. No third-party packages.
- `write` coerces a scalar to `[[v]]` and a flat list to `[[...]]` so callers don't hand-build the
  2-D shape; `read_cell`/`write_cell` unwrap to a single string.
- Any non-200, transport error, non-JSON body, or `{ok:false}` raises `SheetError` with the
  server-supplied message where available.

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

| Condition                         | Result                                             |
|-----------------------------------|----------------------------------------------------|
| Network / DNS / timeout           | `SheetError` wrapping the `urllib` error           |
| HTTP status != 200                | `SheetError` with status + first bytes of body     |
| Body not JSON (e.g. Google login) | `SheetError` "unexpected non-JSON response"        |
| `{ok:false, error}`               | `SheetError(error)`                                |
| Unknown tab / bad range           | Surfaces as `{ok:false}` from the script           |

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
