# Game time is UTC — and how to get "game now" in a sheet formula

Everything this monitor scrapes out of CLOP is stamped in **UTC**. There is no offset to add, no
local timezone, and no daylight-saving shift to undo. This note records how that was established,
and what it means for a spreadsheet that wants to compare an in-game timestamp against *now*.

The game-side evidence lives with the game, in the CLOP repo's `docs/DEVELOPMENT.md` → *"Clocks and
timezone — every clock in CLOP is UTC, with no offset anywhere"* (this monitor is a separate repo,
so that is a reference, not a link). In short, there are three clocks and all three agree:

| Clock | Why it is UTC |
|---|---|
| **PHP** — the nav-bar `Server time:` stamp, and the tick's hour gates | `date_default_timezone_set("UTC")` in `backend/allfunctions.php`, `backend/minimal.php` and `cron/frequent.php`; every page loads one of those before rendering |
| **MariaDB** — every `reports.time`, `news.posted`, `messages.sent`, `chat.posted` | written by MySQL `NOW()`; nothing in the app issues `SET time_zone`, and the DB server's own clock is UTC |
| **The external cron** — tick boundaries | fires on the even UTC hour |

The middle row is the one that needed checking rather than assuming: **stored timestamps are
written by MySQL `NOW()`, not by PHP**, so the PHP timezone call proves nothing about them. Checked
against live production on 2026-08-25 — the daily news items, which the tick only ever writes
during UTC hour 0, carry `00:00:04` and `00:00:03`; the twice-daily ones carry `12:00:04`. On any
other DB clock those rows would show that clock's hour. At the same moment the header read
`2026-08-25 09:58:31` against an independent UTC clock reading `09:58:32`.

So: **tick boundaries are the even UTC hours** — 00:00, 02:00 … 22:00 — the daily layer runs at
00:00 UTC and the war/military layer at 00:00 and 12:00 UTC.

## What this does *not* change

`stockpiles.as_sheet_text()` writes every game stamp with a leading apostrophe so the sheet stores
it as **text**, never as a date value. Keep it that way. The reasoning in that docstring still
holds and is in fact sharper now: a bare `2026-08-25 09:58:31` typed into a cell is parsed as a
date *in the spreadsheet's own timezone*, so on a sheet set to NZ time the cell would silently
claim the game said 09:58 NZST — twelve or thirteen hours off, and looking perfectly normal.
Storing text keeps the game's wall clock intact; the conversion belongs in the formula that reads
it, where it is visible.

## Getting "game now" into a formula

Sheets has no built-in "UTC now". `NOW()` returns the current time **in the spreadsheet's
timezone** (File → Settings → Time zone). Two ways to bridge that:

### Option A (recommended) — set the spreadsheet's timezone to UTC

File → Settings → Time zone → `(GMT+00:00) UTC`. Then `NOW()` *is* game time and every formula
below is plain arithmetic with nothing to keep in sync. The cost is that any date the sheet
formats for a human displays in UTC, which for a sheet whose whole subject is game state is
arguably the right answer anyway.

### Option B — leave the sheet on local time and subtract the offset

Fixed-offset zones are a one-liner: `=NOW() - 8/24` for UTC+8. New Zealand has DST, so it needs the
rule spelled out — NZDT (UTC+13) from the last Sunday in September to the first Sunday in April,
NZST (UTC+12) otherwise:

```
=NOW() - IF(OR(NOW() >= EOMONTH(DATE(YEAR(NOW()),9,1),0) - WEEKDAY(EOMONTH(DATE(YEAR(NOW()),9,1),0),1) + 1 + 2/24,
              NOW() <  DATE(YEAR(NOW()),4,1) + MOD(8-WEEKDAY(DATE(YEAR(NOW()),4,1),1),7) + 3/24),
           13, 12) / 24
```

`EOMONTH(DATE(y,9,1),0) - WEEKDAY(…,1) + 1` is the last Sunday in September; `DATE(y,4,1) +
MOD(8-WEEKDAY(…,1),7)` is the first Sunday in April. The one hour it gets wrong is the repeated
02:00–03:00 hour on the April changeover morning, where local wall time is genuinely ambiguous.
This is why Option A is the recommendation: a whole DST rule maintained in a cell, to undo an
offset nobody wanted, is a lot of surface for one subtraction.

### The formulas themselves

Assume `A2` holds a game stamp as text (`2026-08-25 09:58:31`) and `$B$1` holds game-now from
whichever option above. Parse the stamp explicitly rather than relying on the parser's leniency:

| Want | Formula |
|---|---|
| the stamp as a real datetime | `=DATEVALUE(LEFT(A2,10)) + TIMEVALUE(MID(A2,12,8))` |
| age of the stamp, in hours | `=($B$1 - (DATEVALUE(LEFT(A2,10))+TIMEVALUE(MID(A2,12,8)))) * 24` |
| the tick boundary at or before a time `t` | `=FLOOR(t*12)/12` |
| the next tick boundary after `t` | `=FLOOR(t*12)/12 + 2/24` |
| ticks that have run since the stamp | `=FLOOR($B$1*12) - FLOOR((DATEVALUE(LEFT(A2,10))+TIMEVALUE(MID(A2,12,8)))*12)` |

The `*12` trick works because a day holds exactly twelve two-hour ticks and the day boundary is
itself a tick boundary — so flooring a serial datetime to twelfths of a day lands exactly on the
even UTC hours.

**Gotcha worth knowing before trusting a `NOW()` cell:** by default a spreadsheet recalculates
volatile functions **on change only**, so a "game now" cell can sit stale for hours. File →
Settings → Calculation → *On change and every minute* is what makes it actually track the clock.
Even then a stale-looking value on a sheet nobody has touched is Sheets, not the game.

An Apps Script custom function is the obvious-looking third option and is a trap: custom functions
are not volatile, so `=GAMENOW()` caches and goes stale in exactly the situations where the number
matters. If the sheet ever does need a script-written clock, it should be a **time-driven trigger
stamping a cell**, not a custom function — the deployment that would host it already exists at
`docs/apps-script/Code.gs`.
