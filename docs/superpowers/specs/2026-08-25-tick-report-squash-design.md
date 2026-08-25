# Squashing the tick report in the alert — design

**Date:** 2026-08-25
**Status:** designed, not yet implemented
**Revised the same day** after an adversarial review against the game source and against a working
prototype of the rule. Every claim below that the first draft asserted has now been measured; where
the measurement contradicted the draft, the design changed rather than the wording. The changes are
listed under [What the review changed](#what-the-review-changed).

Builds on `2026-08-23-per-line-report-judging-design.md` and the catalogue in
`2026-08-23-clop-report-formats.md`. It **amends both**: it reclassifies five lines in the
catalogue, and it changes the user-facing contract of the `Tick` selector that the per-line design
introduced. Neither of those documents is left standing as written.

## The problem

The two-hourly tick is the noisiest thing the game writes, and it is one report row
(`cron/frequent.php:799`) that can carry ninety-odd different sentences. Today the monitor offers
two settings for it and neither is what a player wants at 2am:

- `Tick` **off** (the shipped default, and what `settings.json` currently has): the alert carries
  the entire report — forty-odd lines of production, consumption, upkeep, caps and siphons. The
  popup shows a wall of text, and it is truncated part-way through.
- `Tick` **on**: a quiet tick is completely silent. Nothing tells the player the tick ran at all.

What is wanted is a third thing: *tell me the tick happened, don't make me read it, and never do
that to a tick where something went wrong.*

## The rule

Applied to a tick report only. Everything else keeps today's behaviour exactly.

```
own      = surviving_report_lines(message, patterns)                  # the user's own config
if own is empty                            ->  no alert               (unchanged)

squashed = surviving_report_lines(message, patterns + (Tick,))        # force the catalogue on
if squashed == own                         ->  alert with own         (unchanged)

otherwise                                  ->  marker + squashed
```

with the marker being

```
[TICK HAPPENED - check details in game] (Satisfaction -412)
```

The brackets and the spaced hyphen are literal output, not placeholders. The parenthesised figure
is the tick's own `Change in Satisfaction:` value (`frequent.php:794`), reproduced verbatim
including its sign; see [The satisfaction number](#the-satisfaction-number).

### One idea, stated once

**The marker stands in for the lines the squash removed.** Everything below follows from that:

- It removed the whole tick and nothing survived → the marker is the entire alert.
- It removed the routine lines and warnings survived → the marker heads them.
- **It removed nothing** → there is nothing to stand in for, so no marker.

That last line is what makes the rule respect a player who has already configured tick silencing.
If `Tick` is in `reports.ignore`, or the player's own hand-written patterns already cover the tick,
forcing the catalogue on removes nothing further, `squashed == own`, and the alert is exactly what
it is today — warnings alone, no marker, and total silence for a quiet tick.

The first draft got this wrong in both directions. It claimed "the ignore entry wins over the
squash" while specifying suppression only for the empty-survivors case, so a `Tick`-on player got
`[TICK HAPPENED]` stapled to every combat alert — and combat is written by the same tick run at UTC
hours 0 and 12, so it merges routinely rather than exotically. It also gave no thought to a player
whose own patterns cover the tick, who would have started getting a new alert every two hours
forever with no migration note. The `squashed == own` comparison fixes both with one line.

### The `Tick` membership test must be case-insensitive

`surviving_report_lines` compares the selector with `casefold()` (`clop_monitor.py:1073`). A literal
`"Tick" in settings.report_ignore` is exact, and the divergence is real: measured, a player who
wrote `"tick"` gets the routine lines collapsed by the selector *and* a marker every tick from the
membership check — the worst of both. The rule above sidesteps it by comparing outputs rather than
testing membership, which is the other reason it is written that way. `TICK_REPORT_SELECTOR`
(`clop_monitor.py:1001`) is the constant; the literal string must not be hardcoded a second time.

The `#`-prefixed disable convention needs no special handling: `switchable_patterns`
(`clop_monitor.py:212-226`) drops those entries and strips whitespace before the settings reach
here.

### Envelope and truncation

For a squashed tick the alert is the marker, then the surviving warning lines, then the game link:

```
[TICK HAPPENED - check details in game] (Satisfaction -412)
You don't have enough Oil to run your 3 Basic Factory!
You couldn't pay the upkeep for your First Cavalry and it's gone!
https://4clop.org/reports.php
```

The `New CLOP report (<posted>):` heading that wraps every other report alert
(`clop_monitor.py:1716-1719`) is **dropped** for a squashed tick — the marker is the heading. The
`reports.php` link is **kept**, because "check details in game" is only actionable with it. Reports
that are not ticks keep the heading exactly as today.

**The marker and the link sit outside the length cap.** `build_alerts` currently truncates the
joined body at 800 characters (`clop_monitor.py:1715`). Prepending the marker into that body would
spend 38 of the 800 on the marker and make truncation strictly more likely than it is today, which
is the opposite of the intent. So the cap applies to the warning lines only.

**The cap is also raised to 1,500 characters for report bodies.** The first draft rejected printing
the whole report on the grounds that the cap could truncate a warning, then claimed
marker-plus-survivors "can never be cut". That was measured and is false: a tick carrying twelve
warnings drawn entirely from the list in [What still breaks the
squash](#what-still-breaks-the-squash) produced an 869-character alert with two warnings cut off,
including the forbidden-research line. Survivors are unbounded — a merged cell can carry four
combat lines, two force losses, a revolt and two airstrikes. 800 was the monitor's own constant,
not a platform limit; the Windows dialog takes 2,000 (`clop_monitor.py:1861`), so 1,500 for the
body plus the marker and link fits with room to spare and puts the realistic worst case inside the
cap. It does not make truncation impossible, and this document no longer claims it does.

### Multiple ticks in one batch

After a gap in polling — a laptop asleep for six hours — `new_reports_since` returns several ticks
and each produces its own marker. They are **not** merged. Three markers with three different
satisfaction figures is accurate information about three separate ticks; collapsing them would
invent a total the game never wrote and would hide how the drop was distributed. Noted here because
it was raised as an intent mismatch against "only that one report", and rejected deliberately
rather than overlooked.

## The satisfaction number

Satisfaction is the number that ends the game. Revolt fires below -100, -300 or -500 depending on
government (`frequent.php:916-926`), and a nation below -5000 is deleted outright (`:811`).

The tick's own total, `Change in Satisfaction: {$satdifference}` (`frequent.php:794`), is already
silenced by the shipped `Change in %:` pattern (`clop_monitor.py:1010`). That was harmless while
the *reasons* satisfaction moved still alerted. This design makes five of those reasons routine —
disabled buildings, military size, empire size, pollution, owning nothing — so without a change, a
tick that quietly sheds several hundred satisfaction would show the marker and no number, with
every cause silenced and the total silenced too.

So the marker carries it, always, in the game's own wording and sign. There is no threshold to tune
and no case where it is omitted: a flat tick reads `(Satisfaction 0)`, which is itself worth
seeing, because a stalled economy looks exactly like a healthy one otherwise.

The line is read before the catalogue removes it. When the squash removes nothing the marker does
not appear at all, so neither does the number — that player asked for silence and gets today's
behaviour.

## Why the collapse is safe

**Only a line that matches a routine pattern collapses.** An uncatalogued sentence has no pattern,
so it survives and prints. Missing something from the catalogue therefore makes the monitor
noisier, not quieter — the correct direction for the failure to lean, up to the length cap, which
is why the cap was raised above.

Two claims in the first draft were stronger than the evidence, and are corrected here:

**Merged cells are safe empirically, not structurally.** The draft said "because judging is per
line, only the tick's own routine lines collapse". That is not what the code does:
`clop_monitor.py:1086-1090` applies `TICK_ROUTINE_PATTERNS` to **every** line of the cell once the
tick wrapper is detected, including lines glued in from other report rows by
`backend_reports.php:8-12`. The outcome is still correct — every backend that writes to `reports`
was checked and no non-tick sentence matches a tick-routine pattern — but it holds because nothing
happens to collide, not because the design prevents collision.

**"Each pattern is anchored on wording unique to its family" is false as an absolute.** Nation and
force names are player-supplied free text, restricted to `[0-9a-zA-Z_ ]`
(`backend_majoractions.php:47`, `backend_newuser.php:147`, `backend_createforces.php:61`) — no
periods or colons, which kills most vectors but not all. Two live examples, both verified against
the real matcher:

- `Your % used %.` reaches the *being attacked* line when the attacker's nation is named something
  like `Used Ponies`, because combat damage is `round($damage, 6)` (`frequent.php:1294`) and so
  routinely carries a decimal point to end the pattern.
- `Your population` reaches the same line for a force named `Population Guard`.

Both are pre-existing, neither is introduced here, and the two lines that matter most —
`Your % scattered to the four winds!` (`:1337`) and `Your % lost % size!` (`:1331`) — contain no
period and survive both patterns. Recorded because the safety argument must not overstate itself.

## Five lines move from Notable to Routine

The catalogue split these on "is this something happening *to* the player". For these five the
better test is **"does the player already know they did this"** — a standing, deterministic penalty
for a choice they made is bookkeeping, however unwelcome the number.

This is the reasoning for these five lines. It is deliberately **not** adopted as the catalogue's
general rule: the "happening to the player" split still governs everything else, and any future
reclassification is its own decision rather than something this principle licenses in advance.

| Line | Source | Pattern |
|---|---|---|
| `You lose 4 satisfaction for having 4 disabled buildings.` | `frequent.php:269` | `satisfaction for having` |
| `You lost 140 satisfaction for having a military of total size 300.` | `frequent.php:661` | `satisfaction for having` |
| `You lose 80 sat for having an empire of 3 nations.` | `frequent.php:161` | `sat for having an empire of` |
| `You lost 5 satisfaction for not having any buildings!` | `frequent.php:647` | `for not having any buildings!` |
| `Too many Basic Oil Wells cause environmental damage! (-5 sat)` | `frequent.php:281` | `cause environmental damage!` |

Four patterns, added to `TICK_ROUTINE_PATTERNS`. Verified mechanically: none matches any line in the
Notable corpus (`satisfaction for having` does not reach `satisfaction for **not** having`), none is
redundant against the existing sixteen, none makes an existing pattern redundant, and
`cause environmental damage!` is disjoint from the routine `environmental damage has been repaired.`
(`:355`). Grepped across the whole codebase, the only other occurrences of these phrases are static
page prose in `majoractions.php:322` and `warguide.php:29`, never written to `reports`.

Disabled buildings is confirmed self-inflicted: `$disabled` sums `resources.disabled`
(`frequent.php:262-263`); `backend_overview.php:98` is the only site that increases that column and
it does so from player input, while every other writer only decreases it.

The empire penalty is the one that makes the feature work at all. It was previously left alerting on
purpose, and it fires on **every** tick for anyone holding more than one nation — so without this
move the squash would essentially never happen.

Consequence worth stating: switching `Tick` on in `reports.ignore` now silences these five as well.
That is consistent with calling them routine, and it is a behaviour change for anyone already using
that setting.

One residual risk, accepted: `satisfaction for having` is generic enough to swallow a *future*
`You lost N satisfaction for having X` penalty whether or not that one is self-inflicted. The game
is dormant, so this is theoretical, but it is the one pattern here that is wider than its family.

## What still breaks the squash

**Running out of something, so your buildings and forces stop working, always alerts.** That is the
whole of this family and none of it is routine:

- `You don't have enough Oil to run your 3 Basic Factory!` (`:290`, `:295`) — a building starved of
  its input, producing nothing
- `Your government lacks the gasoline and vehicle parts to function properly!` (`:539`) and its five
  siblings — `Your Independence lacks…` (`:499`), `Your decentralized government lacks…` (`:519`),
  and the three remaining government wordings (`:559`, `:574`, `:591`)
- `Your economy lacks the cider to function properly! (-25 sat, unable to make deals)` (`:615`) and
  the coffee twin (`:631`)
- `You couldn't pay the upkeep for your First Cavalry and it's gone!` (`:391`, `:406`, `:421`,
  `:436`, `:451`, `:466`) — ran out of food, and the force is permanently gone

Also inside the tick body:

- `You have completed the forbidden research, and the facility has been automatically dismantled.`
  (`:484`)

Arriving as separate rows and merged into the same cell by timestamp:

- the revolt (`:955`), both airstrikes (`:991`, `:1020`)
- the four combat lines and the two force-loss lines (`:1296`–`:1337`, written at `:1346`)

This list is not a hand-reading. The real matcher was run over all sixteen shipped patterns plus the
four new ones, against every one of the 95 `$messages[]` sites in `frequent.php`. The surviving tick
lines are exactly the set above, plus the unreachable `Serious empire problem - Report this bug!`
(`:42`, whose call sites all pass literal empire names). No worrying tick sentence is silenced.

## What the reports channel cannot tell you

Out of scope, but it is the honest answer to "what scary thing won't this warn me about". **Nation
death writes no report row at all.** Uprising death writes `graveyard` (`frequent.php:861`),
`messages` (`:904`) and `news` (`:912`); conquest writes `graveyard` (`:1474`), `messages`
(`:1516`, `:1536`) and `news` (`:1518`, `:1543`). Afterwards the nation row is deleted (`:869`,
`:1482`) and `needsnation()` redirects the scrape entirely. The reports channel is structurally
blind to it; the messages and news channels are not, and both are already watched.

## Scope of the audit behind this

All 38 `INSERT INTO reports` sites across ten files were re-read and the count verified. The
catalogue in `2026-08-23-clop-report-formats.md` is complete as far as a hand-read can establish,
with the caveat that document already records — with two corrections found by this review:

- **`Your Democracy lacks the gasoline and vehicle parts to function properly!` is not a game
  string.** The Democracy branch writes `Your government lacks…` (`:539`). The fabricated wording
  originates in the test corpus at `test_clop_monitor.py:888` and was copied into the first draft of
  this document twice. It is harmless to safety — nothing matches any wording of that family — but
  the corpus is wrong and is also missing the `Your Independence lacks…` (`:499`) and
  `Your decentralized government lacks…` (`:519`) shapes.
- Graveyard inserts are at `frequent.php:861` and `:1474`; the first draft cited the preceding
  `real_escape_string` lines.

## Testing

`test_clop_monitor.py`, synthetic only, as everywhere else in this repo. The first draft named one
test to change and was wrong by a factor of four; the following was measured by prototyping the rule
and running the suite (baseline `328 passed`, prototype `4 failed, 324 passed`).

### Existing tests that must be rewritten

| Test | Line | Why |
|---|---|---|
| `test_a_tick_that_lost_a_force_alerts_with_that_line_alone` | `:1395` | asserts `f": {lost}\n"`; the marker now precedes the line |
| `test_every_notable_line_survives_the_tick_it_arrives_in` | `:1403` | all 25 subtests fail on assertion shape; only 5 relate to the reclassification |
| `test_a_merged_block_alerts_with_only_the_notable_report_s_lines` | `:1420` | a merged action-plus-tick cell is still a tick report, so the marker prepends |
| `test_switched_off_as_shipped_the_whole_tick_alerts` | `:1440` | this is the behaviour being deleted; its name encodes the old contract and it must be replaced, not patched |

`:1403` is the safety property and rewriting it is where a mistake is most expensive — it must keep
demanding that every Notable line reaches the alert, with the marker allowed to precede it.

### The silent one

`test_tick_is_one_choice_covering_every_routine_tick_family_from_the_source` (`:1355`) builds its
fixture from a **hard-coded slice**, `ROUTINE_REPORTS[8:44]`, with a comment claiming it is "the
complete frequent.php tick corpus". Inserting the five reclassified lines where they belong leaves
the test **green while covering five fewer tick families** — measured, the siphons, the environment
repair and both empire-jealousy lines drop out of the fixture.

Silent coverage loss is the worst failure mode available here. The slice must become `[8:49]` and
the comment updated; better, the slice should be replaced by an explicit marker so the next person
to add a line cannot reintroduce this.

### New tests

- The exact marker string, asserted as a literal including brackets and the satisfaction figure.
- A fully routine tick with no tick configuration alerts with the marker and the link, no heading.
- A fully routine tick with `Tick` configured raises no alert.
- **A tick with warnings and `Tick` configured shows the warnings with no marker** — the
  `squashed == own` case, the defect the first draft shipped.
- **A tick fully covered by the player's own per-line patterns stays silent** — the other half of
  the same defect.
- `"tick"` in lower case behaves identically to `"Tick"`.
- The satisfaction figure is reproduced with its sign, including `0` and a positive value.
- A cell merging a tick with the combat row alerts with the marker and the combat lines.
- A tick carrying more warning lines than the cap still shows the marker, the link and the
  satisfaction figure — i.e. the cap reaches the warning body only.
- A non-tick report is unchanged: heading present, marker absent.
- Each of the four new patterns silences the line it is for and none matches a Notable line.

### A pre-existing gap this design inherits

`docs/2026-08-23-clop-report-formats.md:232-238` states that two properties are "both tested" and
names `test_the_shipped_set_silences_every_routine_line_the_game_writes` and
`test_no_shipped_pattern_is_redundant`. **Neither test exists anywhere in the repo** — the names
appear only in that document. `TICK_ROUTINE_PATTERNS` is not imported by the test file at all, and
`ROUTINE_REPORTS` is referenced from exactly one place, the slice above.

So the coverage and no-redundancy properties this design leans on are hand-checks, not invariants.
The four new patterns were verified by hand and by running the matcher directly, which is the same
standard as the existing sixteen — but the documentation claiming otherwise is false today and is
corrected as part of this work. Writing the two missing tests is the obvious follow-up and is listed
in the implementation plan rather than assumed here.

## Documentation that must change

Twelve passages, all currently describing behaviour this design replaces. Two of them state the
exact opposite of the new behaviour and would actively mislead the successor.

| File | Passage | Why |
|---|---|---|
| `settings.json`, `settings.example.json` | `_omissions_help` | "Three families" → two; the empire-penalty rationale ("a standing penalty worth watching, not bookkeeping") is **reversed** |
| `settings.json`, `settings.example.json` | `_tick_help` | says a tick that caused environmental damage still alerts — **now false**; "with only those warning lines" gains a marker |
| `settings.json`, `settings.example.json` | `_ignore_help` | `Tick`'s job changes from "collapse the routine bookkeeping" to "suppress the marker as well" |
| `README.md` | `:236-250`, "Ignoring routine reports" | describes `Tick` as what collapses routine lines; that is now unconditional |
| `README.md` | `:255-268` | the example JSON and its narrative need the new default |
| `README.md` | `:296-310`, "Silencing the two-hourly tick" | lists environmental damage as still alerting, and claims "with that line, and with only that line" |
| `docs/2026-08-23-clop-report-formats.md` | `:143` | the "happening *to* the player" heading and rationale need the five-line exception |
| `docs/2026-08-23-clop-report-formats.md` | `:154-158` | the five reclassified lines are listed under Notable |
| `docs/2026-08-23-clop-report-formats.md` | `:228-229` | "a standing penalty worth watching, and filed under Notable above" — reversed |
| `docs/2026-08-23-clop-report-formats.md` | `:232-238` | cites two tests that do not exist |
| `docs/2026-08-23-clop-report-formats.md` | `:13`, `:284-287` | `Tick` described as the user-facing switch that applies the catalogue |
| `docs/2026-08-23-per-line-report-judging-design.md` | `:8`, `:93`, `:97`, `:139` | describes `Tick` as opt-in; needs an "amended by 2026-08-25" note |

Also to correct while there: the fabricated `Your Democracy lacks…` corpus entry
(`test_clop_monitor.py:888`) and the two missing government-lacks shapes.

## What the review changed

Recorded so the next reader can see which parts of this document are load-bearing and which were
wrong once.

- **The suppression rule was rebuilt** around `squashed == own`. The draft's stated rationale
  ("the ignore entry wins over the squash") did not match its own rule table, and silently
  regressed players with hand-written tick patterns.
- **The satisfaction figure was added to the marker.** The draft did not notice that its own
  reclassification silenced every cause *and* the total.
- **"The warning can never be cut" was removed** and the cap raised to 1,500 with the marker outside
  it. The claim was measured false at 869 characters.
- **The merged-cell and pattern-uniqueness safety arguments were downgraded** from structural to
  empirical, with the two live free-text collisions recorded.
- **The classification principle was demoted** from "the rule this design adopts" to the reasoning
  behind five specific lines.
- **Test breakage went from one test to four**, plus one that fails silently.
- **The documentation table went from unmentioned to twelve passages.**
- Two graveyard line numbers corrected, one fabricated game string identified, and the
  arithmetically impossible illustrative figures in the reclassification table replaced with ones
  the game's own formulae produce.

## Rejected

- **Popup-only squashing**, keeping the full text in the terminal. Two alert formats to maintain,
  and the terminal is not where anyone reads a tick.
- **Whole report verbatim when something is wrong.** With the cap raised this is less dangerous than
  the first draft argued, but it still buries a one-line warning under forty lines of production.
- **A satisfaction threshold that breaks the squash on a big drop.** Loud in an emergency, silent on
  the slow slide that produces the emergency, and it needs a number picked. Always showing the figure
  costs less and misses nothing.
- **Merging several ticks' markers into one.** See [Multiple ticks in one
  batch](#multiple-ticks-in-one-batch).
- **A separate `reports.collapse_tick` setting.** The player asked for the squash to be the default
  behaviour; a third knob is not needed to deliver that.
- **Deleting the `Tick` ignore entry** now that squashing is the default. It still does something the
  squash does not — total silence — and removing it would be a regression for anyone using it.
