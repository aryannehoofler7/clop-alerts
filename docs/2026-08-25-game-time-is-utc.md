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

### Option A (recommended, and **already true of the shared planning sheet**) — spreadsheet timezone = UTC

File → Settings → Time zone → `(GMT+00:00) UTC`. Then `NOW()` *is* game time and every formula
below is plain arithmetic with nothing to keep in sync. Checked on the shared sheet on 2026-08-25:
`NOW()` already tracks the game's `Server time:`, so this option needs no change — Option B below
is kept only for anyone working in a copy that is on local time.

**`NOW()` still *looks* different, and that is formatting, not timezone.** The sheet renders a
datetime in its locale format (`8/25/2026 10:09:00`) while the game prints
`2026-08-25 10:05:28`. Same instant, same clock. To make it read like the game:

- **Format the cell** — Format → Number → Custom date and time → `yyyy-mm-dd hh:mm:ss`. The cell
  stays a real datetime, so it can still be subtracted from a parsed stamp. Prefer this.
- **Or format in the formula** — `=TEXT(NOW(),"yyyy-mm-dd hh:mm:ss")`. This produces *text*
  matching the game byte for byte, which is right for a display cell and wrong for arithmetic.

Don't read a mismatch in the **seconds** as drift: `NOW()` only moves when the sheet recalculates,
so its seconds are frozen at the last recalc (`:00` on the minute, if recalc is set to every
minute). Expect up to a minute of skew against the game's clock no matter what, and compare in
whole ticks rather than seconds when it has to be exact.

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
even UTC hours. Verified 2026-08-25 by evaluating the same serial arithmetic outside Sheets: every
block start across a full day lands on `00:00, 02:00 … 22:00`, midnight crossings included.

### How stale is a stamp, in ticks

This is the staleness question the sheet actually wants answered, so it is worth stating on its
own.

**It is now live on the shared sheet.** `Dashboard-Stockpile!A1` holds game-now, and row 2 —
`Ticks Since Recorded`, inserted 2026-08-25 — runs this per nation column against that column's
`Active` stamp. See [`2026-08-23-dashboard-goods-map.md`](2026-08-23-dashboard-goods-map.md). The
monitor does not write either of those rows; what it owes them is that `Active` keeps going in as
**text**, which is what the `DATEVALUE`/`TIMEVALUE` parse below depends on.

With `$A$1` holding game-now and the stamp in `W10`:

```
=FLOOR($A$1*12) - FLOOR((DATEVALUE(LEFT(W10,10))+TIMEVALUE(MID(W10,12,8)))*12)
```

If either cell might be text *or* a real datetime — a hand-typed cell, a reformatted one — this
form takes both and shows `?` rather than `#VALUE!` on junk:

```
=IFERROR(LET(p, LAMBDA(c, IF(ISNUMBER(c), c, DATEVALUE(LEFT(c,10))+TIMEVALUE(MID(c,12,8)))),
             FLOOR(p($A$1)*12) - FLOOR(p(W10)*12)), "?")
```

**It counts tick boundaries crossed, not elapsed time** — which is the useful definition, because
what makes a stamp stale is a tick having *run*, not an interval having passed:

| stamp | now | ticks | why |
|---|---|---|---|
| `09:58:31` | `10:09:00` | 1 | the 10:00 tick ran |
| `10:00:04` | `11:59:59` | 0 | same block — nothing has run, however old the stamp looks |
| `09:59:59` | `10:00:01` | 1 | two seconds apart, but a boundary fell between them |
| `2026-08-24 23:10` | `2026-08-25 01:10` | 1 | across midnight |
| `2026-08-23 12:00:04` | `2026-08-25 10:09` | 23 | |

Three things it deliberately does not do:

- **It is not clamped at zero.** A negative result means the stamp is *ahead* of the reference —
  clock skew or a mis-parsed cell. `MAX(0, …)` would turn a visible fault into a plausible `0`.
- **It cannot see the cron's lag.** The tick fires within a few seconds *after* the boundary, so a
  stamp taken in that window counts as 0 ticks stale while predating that tick's effects. No
  formula can resolve this; only the game knows.
- **It inherits `NOW()`'s recalc lag** (below) — for up to a minute after a boundary it can still
  say 0.

**Gotcha worth knowing before trusting a `NOW()` cell:** by default a spreadsheet recalculates
volatile functions **on change only**, so a "game now" cell can sit stale for hours. File →
Settings → Calculation → *On change and every minute* is what makes it actually track the clock.
Even then a stale-looking value on a sheet nobody has touched is Sheets, not the game.

An Apps Script custom function is the obvious-looking third option and is a trap: custom functions
are not volatile, so `=GAMENOW()` caches and goes stale in exactly the situations where the number
matters. If the sheet ever does need a script-written clock, it should be a **time-driven trigger
stamping a cell**, not a custom function — the deployment that would host it already exists at
`docs/apps-script/Code.gs`.
