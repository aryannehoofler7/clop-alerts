# Squashing the tick report in the alert — design

**Date:** 2026-08-25
**Status:** designed, not yet implemented
**Supersedes nothing.** It builds on `2026-08-23-per-line-report-judging-design.md` and the
catalogue in `2026-08-23-clop-report-formats.md`, and it reclassifies five lines in that catalogue.

## The problem

The two-hourly tick is the noisiest thing the game writes, and it is one report row
(`cron/frequent.php:799`) that can carry ninety-odd different sentences. Today the monitor offers
two settings for it and neither is what a player wants at 2am:

- `Tick` **off** (the shipped default, and what `settings.json` currently has): the alert carries
  the entire report — forty-odd lines of production, consumption, upkeep, caps and siphons. The
  popup shows a wall of text, and `build_alerts` truncates it at 800 characters.
- `Tick` **on**: a quiet tick is completely silent. Nothing tells the player the tick ran at all.

What is actually wanted is a third thing: *tell me the tick happened, don't make me read it, and
never do that to a tick where something went wrong.*

## The rule

Applied to a tick report only. Everything else keeps today's behaviour exactly.

```
survivors = surviving_report_lines(message, patterns + ("Tick",))

survivors empty, "Tick" in reports.ignore  ->  no alert                       (unchanged)
survivors empty, "Tick" not in ignore      ->  [TICK HAPPENED - check details in game]
survivors non-empty                        ->  [TICK HAPPENED - check details in game]
                                               + the surviving lines
```

Three decisions are folded into that:

**Forcing `"Tick"` on is what makes the squash the default.** The routine catalogue is applied to
every tick report whether or not the user configured anything, so the routine lines collapse into
the marker out of the box. A player who wants total silence still gets it by switching `Tick` on in
`reports.ignore` — the ignore entry wins over the squash, which is why it keeps its meaning rather
than being deleted.

**The marker is prepended, not substituted, when something survives.** The alternative — printing
the whole report verbatim the moment anything is wrong — was rejected because a full tick is around
2,000 characters and the alert preview cuts at 800. A warning sitting late in the report would be
truncated away by the very mechanism meant to surface it. Marker-plus-survivors is short enough
that the warning can never be cut.

**All channels, not just the popup.** Terminal, webhook and dialog share one alert string. Keeping
a longer variant for the terminal would mean two formats to reason about for no benefit anyone
asked for.

## Why this is safe

The safety property is structural rather than a matter of the catalogue being complete: **only a
line that matches a routine pattern collapses.** An uncatalogued sentence has no pattern, so it
survives and prints. Missing something from the catalogue therefore makes the monitor noisier, not
quieter — which is the correct direction for the failure to lean.

The risk that remains is the reverse: a routine pattern broad enough to swallow a warning. All
sixteen existing patterns were re-checked against the Notable corpus for this design, and each is
anchored on wording unique to its family (`siphoned off.`, `forgets eventually;`,
`Your relationship with the`). The broadest, `Your % used %.`, cannot reach
`Your Democracy lacks the gasoline…` or the combat lines, none of which contain "used".

Merged cells need no special handling. `backend_reports.php:8-12` joins report rows sharing a
timestamp, so a tick cell can also contain the combat row (`frequent.php:1346`), a revolt
(`:955`), an airstrike (`:991`, `:1020`) or an action the player took in the same second. Because
judging is per line, only the tick's own routine lines collapse; `Your Barracks scattered to the
four winds!` prints under the marker.

## Five lines move from Notable to Routine

The catalogue split these on "is this something happening *to* the player". The better line, and
the one this design adopts, is **"does the player already know they did this"** — a standing,
deterministic penalty for a choice they made is bookkeeping, however unwelcome the number.

| Line | Source | Pattern |
|---|---|---|
| `You lose 4 satisfaction for having 4 disabled buildings.` | `frequent.php:269` | `satisfaction for having` |
| `You lost 30 satisfaction for having a military of total size 300.` | `frequent.php:661` | `satisfaction for having` |
| `You lose 10 sat for having an empire of 3 nations.` | `frequent.php:161` | `sat for having an empire of` |
| `You lost 5 satisfaction for not having any buildings!` | `frequent.php:647` | `for not having any buildings!` |
| `Too many Basic Oil Wells cause environmental damage! (-5 sat)` | `frequent.php:281` | `cause environmental damage!` |

Four patterns, added to `TICK_ROUTINE_PATTERNS`. `satisfaction for having` cannot reach
`for not having`, and no line in the Notable corpus contains any of the four phrases.

Disabled buildings is confirmed self-inflicted: `$disabled` sums `resources.disabled`
(`frequent.php:262-263`), a column only the player sets.

The empire penalty is the one that makes the feature work at all. It was previously left alerting
on purpose, and it fires on **every** tick for anyone holding an empire — so without this move the
squash would essentially never happen.

Consequence worth stating: switching `Tick` on in `reports.ignore` now silences these five as well.
That is consistent with calling them routine, and it is a behaviour change for anyone already using
that setting.

## What still breaks the squash

Inside the tick body:

- `You don't have enough Oil to run your 3 Basic Factory!` (`:290`, `:295`) — production silently
  failing
- `You couldn't pay the upkeep for your First Cavalry and it's gone!` (`:391`–`:466`, six sites) —
  permanent loss of a force
- `Your Democracy lacks the gasoline and vehicle parts to function properly!` (`:499`–`:591`) and
  `Your economy lacks the cider to function properly!` (`:615`, `:631`) — the same "you ran out of
  a good" family as the two above, and classified with them
- `You have completed the forbidden research, and the facility has been automatically dismantled.`
  (`:484`)

Arriving as separate rows, merged into the same cell by timestamp:

- the revolt (`:955`), both airstrikes (`:991`, `:1020`)
- the four combat lines and the two force-loss lines (`:1296`–`:1337`, written at `:1346`)

## Scope of the audit behind this

All 38 `INSERT INTO reports` sites across ten files were re-read for this design. Nation death
writes to `graveyard`, not `reports` (`frequent.php:859`, `:1471`), and the forbidden-research
message additionally goes to `messages` (`:486`) — neither adds a reports line beyond the
catalogue. The catalogue in `2026-08-23-clop-report-formats.md` is complete as far as a hand-read
can establish, with the caveat that document already records.

## Testing

`test_clop_monitor.py`, synthetic only, as everywhere else in this repo.

- `test_every_notable_line_survives_the_tick_it_arrives_in` (currently `:1403`) wraps each Notable
  line in a real-shaped tick and demands it reach the alert. With the five lines moved into the
  routine corpus, this becomes the proof that the squash never eats a warning — it is the whole
  safety property and needs no new test, only the corpus edit.
- A fully routine tick, `Tick` not configured, alerts with the marker and nothing else.
- A fully routine tick, `Tick` configured, raises no alert.
- A tick with a lost force alerts with the marker followed by that one line, and none of the
  routine detail lines. (Replaces the existing assertion that the alert is the warning alone.)
- A cell merging a tick with the combat row alerts with the marker and the combat lines.
- A non-tick report — a completed action, a marketplace sale — is unchanged, marker absent.
- Each of the four new patterns silences the line it is for, and none matches a Notable line.

## Rejected

- **Popup-only squashing**, keeping the full text in the terminal. Two alert formats to maintain,
  and the terminal is not where anyone reads a tick.
- **Whole report verbatim when something is wrong.** The 800-character preview cap turns this into
  a way to lose the warning; see the rule above.
- **A separate `reports.collapse_tick` setting.** A third knob on top of `Tick` for a behaviour
  nobody would switch off.
- **Deleting the `Tick` ignore entry** now that squashing is the default. It still does something
  the squash does not — total silence — and removing it would be a regression for anyone using it.
