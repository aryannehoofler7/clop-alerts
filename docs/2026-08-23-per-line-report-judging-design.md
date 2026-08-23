# Judging reports line by line — design

**Date:** 2026-08-23
**Status:** implemented

**Amended 2026-08-24:** per-line judging is still the safety mechanism, but users no longer have to
configure one pattern per internal tick sentence family. `reports.ignore` now accepts the logical
selector `Tick`, which expands to those routine families in code and leaves unmatched warning lines
visible. `Action: <recipe-name pattern>` does the equivalent for a completed action and its
bookkeeping lines.

Apply `reports.ignore` to each line of a report rather than to the whole report, so that a routine
two-hourly tick can be silenced without also silencing the one line in it that matters.

## The problem

The game's tick writes **one** report row per nation containing everything that happened
(`cron/frequent.php:786-799`): a `Show Details` wrapper, every detail line joined with `<br/>`, then
`Change in Satisfaction:` and the relation changes. Routine production and a lost military force
arrive in the same row.

The monitor flattens that row to a single string and matches ignore-patterns against the whole
thing, so a pattern is all-or-nothing for the report. `Change in Satisfaction:` appears in every
tick, which makes it the obvious pattern to silence the tick with — but switching it on also
silences, inside the same row:

- `You couldn't pay the upkeep for your First Cavalry and it's gone!`
- `Too many Basic Oil Wells cause environmental damage! (-5 sat)`
- `You don't have enough Oil to run your 3 Basic Factory!`
- `Your Democracy lacks the gasoline and vehicle parts to function properly! (-20 sat)`

**No pattern can fix this**, because the routine text and the notable text are in one report. Any
pattern that matches the quiet tick also matches the tick where something went wrong. The choice
today is a false positive (tick noise every two hours) or a false negative (silently losing a lost
force), and no third pattern exists.

`backend_reports.php:5-12` compounds it by merging reports that share a timestamp into one block,
so unrelated reports can end up judged together.

## Design

### Judge each line, alert with the survivors

A report is split into its lines, each line is judged against `reports.ignore` independently, and:

- if **every** line matches a pattern, the report raises no alert — the same outcome as today when
  a pattern matched;
- if **any** line survives, the report alerts, showing **only the surviving lines**;
- with no patterns configured, every line survives and the whole report alerts, exactly as today.

So a quiet tick goes silent, and a tick containing a dead force alerts with that one line instead of
two thousand characters of production summary. Both failure directions go away rather than being
traded against each other.

### The line boundaries are the game's own

The game already separates these lines with `<br/>` — in the tick
(`cron/frequent.php:772`, `:786-799`), in action reports (`backend_actions.php:246`), and in the
same-timestamp merge (`backend_reports.php:5-12`). The monitor currently turns those into spaces
when it normalises the cell. It will keep them as line breaks instead, normalise each line's
internal whitespace, and drop blank lines.

Nothing new has to be guessed: the structure being used is the structure the game wrote.

**`<br/>` alone turned out not to be enough**, found while implementing. The tick wraps its details
in `<div>`s and puts **no** `<br/>` between the last detail line and `Change in Satisfaction:` —
only the `</div>` that closes the block (`cron/frequent.php:786-799`). Splitting on `<br/>` alone
would therefore judge the last detail line and the satisfaction total as one line, and the shipped
`Change in Satisfaction:` pattern would silence whatever that last line was: exactly the loss this
design exists to prevent. `Show Details` and `Hide Details` are likewise two `<div>`s with no
`<br/>` between them. So the split is at the page's block boundaries as well — `br`, `div`, `p` —
which is the same claim as above, just stated in the markup the page actually uses.

A newline in the report's *text* is deliberately not a break. Seven of the tick's report strings
span more than one source line: `frequent.php:952` (the revolt), `:985` and `:1014` (the two
airstrikes) are two-line double-quoted strings, and `:1296`, `:1302`, `:1309` and `:1315` are
three-line heredocs (the four combat lines). Each has to stay one line for a pattern to match it.

### What this changes for existing patterns

A pattern can no longer match across a `<br/>`. Today everything is one string, so a pattern could
span what the page showed as two lines; after this it cannot. The README says patterns cannot span
lines, which becomes true in a stricter sense than it was.

In exchange, patterns become more precise. `You paid % bits.` now silences exactly that line of an
action report rather than being unable to distinguish it from the rest, which is what makes the
shipped set able to silence a whole tick without reaching a single notable line.

### The internal catalogue grows

Silencing a tick internally needs a matcher per routine line rather than one literal pattern for
the row. The catalogue in `2026-08-23-clop-report-formats.md` lists them, and they collapse into
roughly fifteen compact patterns behind the one user-facing `Tick` selector — the wrapper lines,
production and consumption, relation and satisfaction effects, caps, upkeep, and siphon lines.

Every one is a "Routine" entry from that catalogue. Nothing from the "Notable" list is part of the
selector, so enabling `Tick` cannot hide a warning.

That rule also cost one pattern that shipped before this change: `You sold % and made % bits.` is a
"Notable" entry, because it fires when somebody else buys from your standing sell order. It was
dropped rather than carried over.

### The report marker survives the change

`Snapshot.latest_report` persists `(message, posted)` and `new_reports_since` matches it by
identity. Changing the message format means a marker saved by an older version will not be found on
the page after upgrading — at which point the existing timestamp fallback takes over and only
reports newer than that timestamp alert. So the upgrade does not replay old reports, and no
migration is needed.

### Alert text

A report alert shows its surviving lines joined by newlines, under the existing
`New CLOP report (<posted>):` heading, with the existing length cap. A report whose lines were all
silenced contributes nothing.

## Testing

`test_clop_monitor.py`, synthetic only:

- a report whose every line matches is silent;
- a report with one surviving line alerts with **only** that line, not the whole report;
- a real-shaped tick report — wrapper, production lines, `Change in Satisfaction:` — is fully
  silenced by the shipped patterns;
- the same tick with `You couldn't pay the upkeep for your % and it's gone!` added alerts, showing
  that line alone;
- the same tick with each of the other Notable lines added likewise;
- an action report (`You spent … You paid … Build X completed successfully.`) is fully silenced by
  the shipped patterns;
- no patterns configured means the whole report alerts unchanged;
- a merged same-timestamp block is judged per line, so one routine and one notable report together
  alert with only the notable lines;
- every shipped pattern still matches the line it is meant to, and none matches any Notable line
  from the catalogue.

## Rejected at the time

- **A better literal tick pattern.** There is not one. The routine and notable text share a report
  row, so any literal pattern matching the first matches the second. The later `Tick` selector is
  deliberately not a literal pattern: it names the report and expands to the safe routine families
  internally.
- **An `alert_anyway` override list**, mirroring the market's `always`/`never`: keep whole-report
  matching, but let a pattern force an alert through. Simpler to build, but the alert still shows
  the entire tick blob, and it requires the user to enumerate every notable line — with the same
  silent-loss failure if they miss one. It moves the burden rather than removing it.
- **Splitting the tick report in the game.** Correct at the source and out of scope; the monitor is
  read-only and the game is dormant.
- **Keeping the flattened string for matching and using lines only for display.** It would preserve
  every existing pattern exactly, but it cannot silence a tick without silencing its warnings, which
  is the entire problem.
