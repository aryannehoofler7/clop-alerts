# CLOP notification monitor (proof of concept)

This is a small, read-only monitor for the hosted game at `https://4clop.org`. It:

- logs in through the normal `login.php` form;
- retains the resulting `PHPSESSID` cookie in memory for the process lifetime;
- polls the authenticated navigation for unread user and alliance-message counts;
- reads and caches only the newest (top) news entry's content and timestamp, ignoring pagination,
  older rows, the random banner, server clock, and countdown;
- reads every report on the page and judges each one that is newer than the last it saw, caching
  only the newest as its marker;
- watches the buyer's marketplace for each good you switch on, and alerts while any friend or
  alliance member has a pending buy order for it;
- keeps the shared planning sheet up to date from one `overview.php` read: your building counts and
  your six `STOCK` goods on your own nation tab, and your whole column on the alliance-wide
  `Dashboard-Stockpile` tab — all 31 goods, how many ticks your six tracked goods last, plus
  satisfaction, both faction relationships, GDP, funds and a last-updated stamp;
- saves the last observed counts, newest news entry, newest report, and configured 4chan post in
  `.state/clop-monitor.json`;
- alerts in the terminal, opens a persistent Windows dialog, and can optionally call a webhook.

It does not store the password or session cookie, mark messages as read, post anything, or perform
game actions. This is deliberately form-aware scraping rather than a claim that the site has an API.

## Install and run

Install Python 3.9 or newer on Windows, selecting **Add Python to PATH** in the installer. There are
no third-party Python packages to install: the monitor uses only the standard library. Open
PowerShell in this folder (whether it came from a clone or from an extracted copy) and verify Python
is available:

```powershell
cd clop-alerts
python --version
```

If `python` is not recognized but the Python launcher is installed, use `py` instead in every command
below. Copy [`.env.example`](./.env.example) to `.env`, open it, and populate both values:

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
```

```dotenv
CLOP_USERNAME=your_username
CLOP_PASSWORD=your_password
```

Then do the same for the settings, which are optional but usually worth having:

```powershell
Copy-Item .\settings.example.json .\settings.json
```

Both `.env` and `settings.json` are git-ignored, so they are yours: `git pull` updates the monitor
without touching either one. Once the credentials are populated, test the included popup and WAV,
then start the monitor:

```powershell
python .\clop_monitor.py --test-notification
python .\clop_monitor.py
```

An empty or missing value falls back to the normal prompt (the password prompt is not echoed).
Values already present in the process environment take precedence over `.env`, and `--username`
takes precedence over both sources. Use `--env-file C:\path\to\credentials.env` to select a different
optional file.

Alternatively, provide the credentials as environment variables in the process that launches it:

```powershell
$credential = Get-Credential -UserName "your_username" -Message "CLOP login"
$env:CLOP_USERNAME = $credential.UserName
$env:CLOP_PASSWORD = $credential.GetNetworkCredential().Password
try {
  python .\clop_monitor.py --interval 60
} finally {
  Remove-Item Env:CLOP_USERNAME, Env:CLOP_PASSWORD
}
```

For a scheduled process, restrict access to the local credentials file, or inject `CLOP_PASSWORD`
through the task scheduler or secret-management mechanism used on the machine. Never force-add or
commit the populated `.env` file.

Useful options:

```powershell
# Poll once, print the observed state, then exit
python .\clop_monitor.py --once --no-desktop-notifications

# Exercise the configured notification channels without logging in
python .\clop_monitor.py --test-notification

# Optional Discord/Slack-compatible incoming webhook
$env:CLOP_WEBHOOK_URL = "https://..."
python .\clop_monitor.py

# Slower polling, a state file outside this folder, and a different host
python .\clop_monitor.py --interval 300 --state C:\path\to\clop-monitor.json
python .\clop_monitor.py --base-url https://test.4clop.org
```

`--interval` defaults to 60 seconds, `--state` defaults to `.state/clop-monitor.json` inside this
folder, and `--base-url` defaults to the live game. Run `python .\clop_monitor.py --help` for the
complete list.

## Settings

Copy [`settings.example.json`](./settings.example.json) to `settings.json` and edit that copy to
choose alert categories and sound behaviour:

```json
{
  "cache": {
    "persist_to_file": true,
    "_persist_to_file_help": "Set false to keep news, report, and 4chan baselines only until this process exits."
  },
  "alerts": {
    "user_messages": true,
    "alliance_messages": true,
    "news": true,
    "reports": true,
    "market_orders": true
  },
  "sound": {
    "wav_path": "sounds/twilight-clock-is-ticking.wav",
    "_wav_path_help": "Set wav_path to null to use the normal Windows alert sound.",
    "loop_while_popup_open": false,
    "repeat_interval_seconds": 10
  },
  "reports": {
    "_ignore_help": "A report matching any pattern below raises no alert. Matching ignores case and looks anywhere in the message; % stands for any run of characters. A pattern starting with # is switched off: delete the # to switch it on.",
    "ignore": [
      "# completed successfully.",
      "# You bought % from % for % bits.",
      "# Change in %:",
      "# Your % used %.",
      "# Your relationship with the",
      "# siphoned off."
    ]
  },
  "market": {
    "goods": {
      "# Machinery Parts": {"friends": true, "alliance": true, "reserve": "none", "reserve_amount": 0, "always": [], "never": []},
      "# Oil":             {"friends": true, "alliance": true, "reserve": "none", "reserve_amount": 0, "always": [], "never": []},
      "# Pies":            {"friends": true, "alliance": true, "reserve": "none", "reserve_amount": 0, "always": [], "never": []}
    }
  },
  "fourchan": {
    "thread_url": "https://boards.4chan.org/mlp/thread/43454282/clop-financial-crisis-edition",
    "_thread_url_help": "Set thread_url to null or remove the fourchan section to disable thread monitoring."
  }
}
```

Two sections are abbreviated above: the real `settings.example.json` lists all 28 tradeable goods,
and all 25 report patterns, every one of them commented out. See
[Ignoring routine reports](#ignoring-routine-reports) and
[Watching the buyer's marketplace](#watching-the-buyers-marketplace).

**The file is optional and every key in it is optional.** `settings.json` is git-ignored: only the
example above is tracked, so an update can never overwrite your copy and you never have to merge one.
The defaults live in the monitor itself, so a key you leave out — or a missing file — still works, and
the monitor names what it filled in when it starts:

```text
Settings: using defaults for sound.wav_path, cache.persist_to_file.
Settings: no settings file at C:\path\to\settings.json; using built-in defaults (4chan thread monitoring off).
```

A file that sets everything prints neither line. When a later version of the monitor adds a setting,
that one new name appears in the first line until you set it yourself. The defaults match the example
except for `fourchan.thread_url`, which defaults to off because threads archive.

**The file is re-read on every poll.** Save an edit and it takes effect on the next poll — within
`--interval` seconds — without restarting anything. Switching a good on, silencing a report, muting a
category and changing the alert sound are all live edits. Restarting would also throw away the
in-memory news, report and thread baselines whenever `cache.persist_to_file` is off, which is reason
enough not to have to. A reload that changed something names the sections it changed, then repeats
whatever a re-run preflight resolved, in the same words startup uses:

```text
Settings reloaded: alerts, market.goods.
Market preflight passed; watching Machinery Parts; alliance is Communist Eradication Front (#12).
```

Those extra lines appear only when that section changed, so a reload says as much as the edit
deserves and no more. A reload that changes nothing prints nothing, and costs nothing beyond reading
the file: no rebuilt sound, no preflight, no extra requests.

An edit to a section that is currently muted is not a change, because the live settings do not depend
on it: with `"market_orders": false`, editing `market.goods` prints nothing and the monitor keeps the
goods it had. Nothing is lost — switching the mute back off loads the whole current file, new goods
included — but expect the silence rather than a confirmation.

**A reload is applied in full or not at all.** If the file cannot be read, cannot be parsed, fails
validation, or names something that cannot be brought into service — a good the game does not have, a
4chan thread that is already archived — the monitor raises a **CLOP monitor problem** dialog naming
what is wrong, keeps every setting it already had, and goes on polling. It never applies the alert
half of a file whose market half was refused, so "which settings are live" always has one answer: the
last file that loaded cleanly, whole. A broken file at *startup* is still fatal, because there are no
previous settings to fall back on.

A refusal repeats for as long as its cause lasts, which is not always something you have to fix. A
typo in the file keeps being refused on every poll until you correct it, and that repetition is
deliberate: a monitor running on settings you think you replaced is worth interrupting more than
once. But bringing a changed section into service means talking to the game or to 4chan, so a
refusal can equally be the network being briefly unreachable — nothing is wrong with your file and
the next poll applies it by itself. The dialog names the cause, so read it before you go looking for
a mistake you did not make.

One case looks alarming and is not: if a dialog appears the instant you save, and the next poll is
happy, you caught the monitor reading a file your editor was still writing. Wait one poll before
worrying about it.

**Deleting the file while the monitor runs is refused the same way.** An absent `settings.json` means
"use the built-in defaults" at startup, but mid-run the same reading would silently switch every muted
category back on, drop your watched goods, turn the 4chan thread off, and start writing the state file
you had disabled. A file that disappears under a running monitor is much more likely a rename to
`settings.json.bak` while you experiment than a request to revert everything, so the monitor warns and
carries on with the settings it had. To go back to the defaults on purpose, put an empty object `{}`
in the file rather than deleting it; that is an ordinary edit and is applied like any other.

**What still needs a restart:** the command-line arguments — `--interval`, `--state`, `--settings`,
`--base-url` and the rest — are process arguments rather than settings, and the credentials in `.env`
are read once. Changing any of those means stopping the monitor with `Ctrl+C` and starting it again.

All five alert categories default to enabled. A disabled category is still read and, when file
persistence is enabled, included in the saved snapshot; it does not produce a terminal message,
popup, sound, or webhook call. `market_orders` is the one exception: disabling it stops the market
work rather than discarding its result, because that work costs a request per watched good on every
poll.

### Ignoring routine reports

Most reports are the game's own bookkeeping — trades you made, the two-hourly tick, a building you
queued — and alerting on them buries the ones that matter.

**The two-hourly tick is always collapsed. That is not a setting.** See
[Silencing the two-hourly tick](#silencing-the-two-hourly-tick).

`reports.ignore` is a list of *further* things you do not want to hear about. Two entries are
logical selectors rather than text patterns:

- `Tick` is an escape hatch: switch it on and a tick that carried **no** warnings raises no alert at
  all. A tick that did carry a warning always alerts.
- `Action: <recipe-name pattern>` means one completed game action, including its spent, paid,
  gained, relation, and satisfaction lines.

The monitor judges a report's contents line by line, so a selector can silence forty routine lines
of a report while still alerting on the one line in it that matters.

**A pattern starting with `#` is switched off.** JSON has no comment syntax, so this is how a pattern
is commented out: it stays in the list where you can see it, and you delete the two characters to
switch it on. Every shipped pattern is commented out, so beyond the tick collapse the monitor
silences nothing until you say so:

```json
"reports": {
  "ignore": [
    "Tick",
    "Action: Build %",
    "# Action: Burn Oil",
    "# Action: Distribute Pies",
    "# You bought % from % for % bits.",
    "# You transferred % to % for % bits."
  ]
}
```

That file silences warning-free ticks entirely, and completed actions whose recipe name begins
`Build`; Burn Oil, Distribute Pies, purchases, and transfers remain switched off. Leading spaces
before `#` are fine.
The `#` only means "off" at the start of an entry, so `Report #% filed` remains a normal custom
pattern.

- A pattern matches **anywhere** in a line.
- `%` stands for **any run of characters**, including none, so `You paid % bits.` covers any price.
- Matching **ignores case**.
- Everything else is literal: `.` and `(` mean themselves, not what they mean in a regular
  expression.
- Plain entries without `Tick` or `Action:` remain per-line patterns for compatibility and for
  standalone report families. They cannot span the page's `<br/>` line breaks.

#### Silencing finished actions

Every completed action ends with **`<recipe name> completed successfully.`**. `Action:` matches that
recipe name and treats its preceding bookkeeping as part of the same report. The shipped examples
are deliberately choices a player recognises:

- `Action: Build %` covers all 38 building, weapon, and armour recipes beginning `Build`;
- `Action: Burn Oil` covers only Burn Oil;
- `Action: Distribute Pies` covers only Distribute Pies.

Use any name shown on the Actions page. `%` makes families such as `Action: Upgrade %`,
`Action: Ship %`, `Action: Dig %`, `Action: Plow %`, `Action: Manufacture %`,
`Action: Smuggle %`, or `Action: Distribute %`.

Failed and refused builds produce no report at all, so nothing here can hide one.

#### Silencing the two-hourly tick

The tick is the noisiest thing the game writes — one report row carrying forty-odd lines, almost
none of them worth reading. **It is always collapsed to a single line**, with no setting to turn
that off:

```
[TICK HAPPENED - check details in game] (Satisfaction -412)
```

**The satisfaction figure is always on the marker.** It is the game's own
`Change in Satisfaction:` number, sign included, and it is there because the collapse hides every
line that explains it. Satisfaction is what ends a nation: your ponies revolt below -100, -300 or
-500 depending on government, and below -5000 the nation is deleted. `(Satisfaction 0)` is worth
seeing too — a stalled economy otherwise looks exactly like a healthy one.

What the collapse absorbs is every routine family in `cron/frequent.php`: the Show/Hide wrapper,
production and consumption, relation and satisfaction effects, caps, government/economy/military
upkeep, stockpile siphons, Empire jealousy, environmental repair, and the five standing satisfaction
penalties you brought on yourself — disabled buildings, military size, empire size, owning no
buildings, and pollution. You already know you did those, and the empire one fires on *every* tick,
which would otherwise stop the tick ever collapsing.

**Warnings are never collapsed.** They print in full underneath the marker. A tick containing any of

- `You don't have enough Oil to run your 3 Basic Factory!` — a building starved of its input
- `Your government lacks the gasoline and vehicle parts to function properly! (-20 sat)`
- `Your economy lacks the cider to function properly! (-25 sat, unable to make deals)`
- `You couldn't pay the upkeep for your First Cavalry and it's gone!` — starved, and gone for good
- combat, a revolt, an airstrike, or the forbidden research

alerts like this:

```
[TICK HAPPENED - check details in game] (Satisfaction -31)
You don't have enough Oil to run your 3 Basic Factory!
```

The `Tick` entry in `reports.ignore` is the one escape hatch, and it only reaches a tick that
carried **no** warnings at all. Switch it on and a quiet tick goes completely silent; a tick that
went wrong still alerts, marker included.

> **If you hand-wrote your own per-line patterns to silence the tick before this change**, you will
> now get the one-line marker every two hours where you used to get silence. That is one line
> instead of forty. Add `Tick` to `reports.ignore` to go back to full silence on a quiet tick.

**Nothing that happens *to* you ships as a pattern.** Every shipped pattern is the game's own
bookkeeping or something you just did, so an unedited settings file cannot hide a warning. That is
also why there is no shipped pattern for `You sold 10 Copper to ... and made 9,000 bits.`, which
fires when somebody else buys from your standing sell order, nor for the deal lines that report what
another nation did. Keep that split if you add your own.

`cache.persist_to_file` defaults to `true`. The monitor then loads
`.state/clop-monitor.json` at startup, compares the first poll with the cached newest news entry,
newest report, and latest configured 4chan post, and updates the file after every successful poll.
The `.state` directory is git-ignored. Set `persist_to_file` to `false` to skip both reading and
writing that file; baselines will still work between polls in the current process, but a restart
will establish fresh news, report, and 4chan baselines. A cache file left by an earlier enabled run
is not deleted or updated while persistence is disabled.

The included Twilight clip is the default. Its source and technical details are recorded in
[`sounds/README.md`](./sounds/README.md). `wav_path` also accepts another absolute path or a path
relative to `settings.json`. Use forward slashes in JSON Windows paths, for example
`"C:/Sounds/clop-alert.wav"`, or put a file beside the settings file and use `"clop-alert.wav"`. It
must refer to an existing `.wav` file. Set it to `null` to use the normal Windows system notification
sound instead. `_wav_path_help` is explanatory and is ignored by the monitor.

A configured WAV plays once when the popup opens by default. Set `loop_while_popup_open` to `true`
to replay it for as long as the popup remains open; `repeat_interval_seconds` controls the delay
between play requests. Closing the popup stops the sound. Test the current sound settings with:

```powershell
python .\clop_monitor.py --test-notification
```

Use `--settings C:\path\to\other-settings.json` to select another settings file.

When `fourchan.thread_url` contains a supported thread URL, the monitor reads that thread through
4chan's public JSON endpoint. Its first successful check caches the current last post without
alerting. Later checks alert when the last post number or content differs, including a plain-text
preview and direct post link. Set `thread_url` to `null`, or remove the `fourchan` section, to disable
these requests and alerts. The normal 60-second polling interval is comfortably above 4chan's API
rate limit.

Before prompting for CLOP credentials or starting the polling loop, the monitor checks the configured
thread. If 4chan marks it as archived, startup fails with instructions to replace the URL with the
new thread or set it to `null`; an archived configured thread is never silently treated as healthy.
The successful startup response is reused as the initial thread snapshot, avoiding an immediate
duplicate API request.

Changing `thread_url` while the monitor is running runs that same check again and adopts the new
thread's current last post as the baseline, so a swap does not alert for a post that was already
there. It prints that post's number in the same words startup does, because adopting post #N is also
a decision that posts up to #N will never alert. Setting `thread_url` to `null` stops watching and
issues no further requests. The two archived cases are
deliberately different. A thread that archives **while you are watching it** stops the monitor, as it
always has: that is the game telling the watch its job is over. A thread that is *already* archived
when you name it mid-run is a refused reload instead — the monitor warns, keeps the thread it had,
and carries on — because a typo in a text file should not end an overnight run.

When there is no saved baseline, the first successful poll caches the current top news entry and top
report rather than alerting for existing entries. On every poll, it alerts whenever the unread
user-message count or alliance-message count is greater than zero, even when that count has not
changed since the previous poll. News alerts remain change-based: a notification is sent only when
the top entry differs from the cached one.

Reports are handled row by row. Each poll takes every report above the last one seen — matched by
its exact text and timestamp, so two reports written in the same second are both delivered, with the
timestamp used only as a fallback when the remembered report has scrolled off the page — and alerts
on each one that has a line left after the [ignore patterns](#ignoring-routine-reports) have taken
theirs, showing those lines. There is no cap: coming back to forty reports produces forty entries in
one dialog. The marker always advances to the newest report on the page whether or not anything was
alerted, so a silenced report is examined once and never again. **No alert carries a page link.**
A bare `reports.php` or `buyermarketplace.php` URL under every entry was noise in a dialog that
often carries several of them, and the alert text already names the page it is about. Delete the
`.state` folder to establish fresh news and report baselines.

The hosted game keeps the alliance "last checked" timestamp inside each login session. When the
monitor sees a nonzero alliance badge, it validates it with a fresh login before alerting. This
prevents a long-running monitor session from repeatedly reporting messages already read in a
browser. Validation does not open the alliance page or mark messages as read.

On Windows, all alerts found by a poll are combined into one dialog that remains open until **OK** is
clicked. Polling pauses while this dialog is open. Immediately after it is dismissed, the monitor
re-reads the message counts, because dismissing the dialog is when messages actually get read, and
saves those. It deliberately does **not** re-read the news, report, and thread markers: those stay at
what the alert was about, so anything that arrived while the dialog was open is still unseen and
alerts on the next poll rather than being stepped over. `--no-desktop-notifications` disables both
the dialog and this pause.

The monitor automatically logs in again if the hosted session expires. Stop it with `Ctrl+C`.

### Watching the buyer's marketplace

A friend or alliance member who posts a buy order for a good you are sitting on is an opportunity
that ends the moment someone else fills it, and nothing in the game tells you about it. The
`market` section watches the buyer's marketplace for the goods you choose and alerts while such an
order is pending.

All 28 tradeable goods — the 16 goods and the 12 DNA strains — ship in the settings example, and
**every one of them ships commented out**, so the monitor watches nothing until you say so. This is
the same convention as [`reports.ignore`](#ignoring-routine-reports): a key starting with `#` is
switched off, and you switch a good on by deleting those two characters from its key.

```json
"market": {
  "goods": {
    "Machinery Parts":   {"friends": true, "alliance": true, "reserve": "qty", "reserve_amount": 100, "always": [], "never": []},
    "# Oil":             {"friends": true, "alliance": true, "reserve": "none", "reserve_amount": 0, "always": [], "never": []}
  }
}
```

That file watches Machinery Parts and leaves Oil switched off. At startup the monitor names what it
resolved:

```text
Market preflight passed; watching Machinery Parts; alliance is Communist Eradication Front (#12).
```

and every poll that finds matching orders produces one block of text per good, along these lines:

```text
Buy orders for Machinery Parts:
  Green Mountain Republic (alliance) wants 5 at 1,000 bits each
  Fish Bucket (alliance) wants 35 at 1,000 bits each
```

Each buyer is labelled with every relation that is true of them, so a buyer who is both reads
`(friend, alliance)`, an enemy reads `(enemy)`, and a buyer with no relation to you at all — which
only ever appears through an `always` pattern, below — reads `(no relation)`.

#### Who counts as worth alerting on

`friends` alerts on a buyer on your friends list, whom the game paints blue. `alliance` alerts on a
buyer in your alliance, whom the game paints green. Both default to `true`.

**They are two separate, independent checks, and a buyer who is both satisfies either one.**
Switching `friends` off does not hide an ally you have also friended. That independence is the
reason the monitor looks up your alliance's member list instead of simply reading the game's
colours: the game paints only one colour per buyer and tests friendship before alliance, so a buyer
who is both a friend and an ally renders blue and nothing else. Reading the colour alone would have
meant that `"friends": false, "alliance": true` silently skipped that buyer — precisely the buyer
that setting exists to catch. So the monitor reads the roster separately and decides alliance
membership by nation, which is exact whatever colour the row happens to be.

`always` and `never` are lists of nation-name patterns that override both checks. They are matched
by exactly the same rule as report ignore patterns: matching **ignores case**, a pattern matches
**anywhere** in the name, `%` stands for **any run of characters**, everything else is literal, and
an entry starting with `#` is switched off.

**Write the nation name, not the player's username.** These two are different things in CLOP and
are usually nothing like each other: the player `Lacera Viscera` plays a nation called
`Fish Bucket`, and `Winter Moon` plays `Green Mountain Republic`. What you write here is matched
against the **Buyer column of the buyer's marketplace**, which shows the nation. Open the market,
select the good, and copy the name you see there — a username will simply never match, and it will
fail silently, because a name that matches nothing is indistinguishable from a nation that has not
posted an order.

There is a second consequence of that. A player may own more than one nation, and the two kinds of
setting treat that differently:

- `friends` and `alliance` work at the **player** level, because the game decides both from the
  account behind the nation. Every nation that player owns is covered.
- `always` and `never` work at the **nation** level. `"never": ["Fish Bucket"]` silences that one
  nation and none of the same player's others.

So to silence a player who fields several nations, list each nation, or use `%` to cover a shared
naming pattern if they have one.

`never` beats `always`, and `always` beats both relation checks. So a nation named in `always`
alerts even when it is your enemy, and both checks switched off with a populated `always` reads as
"only these nations, whoever they are":

```json
"Oil": {"friends": false, "alliance": false, "always": ["Luna Sueno", "Fish %"], "never": []}
```

Two consequences of that matching rule are worth saying out loud, because they are harmless in
report text and surprising in a nation name:

- `"always": ["%"]` matches every name, so it alerts on **every** buyer of that good, strangers and
  enemies included. `never` still wins over it.
- Patterns match a substring, not a whole name, so `"never": ["Luna"]` also silences a nation called
  `Lunar Empire`.

A knob the monitor does not recognise — `friend` for `friends`, say — stops it at startup rather
than being quietly ignored, since a misspelled knob would otherwise fall back to its default and do
the opposite of what you wrote. Two goods whose keys differ only in capitalisation are refused the
same way.

#### Keeping a reserve

Every buyer match is filtered against the stockpile read from `overview.php` before the buyer's
market is checked.

**Your own `Used` amount comes off first, in every mode.** The Resources panel's `Used` column is
what one tick of your own consumption takes off the stock, and that has to stay on hand whatever
you are willing to sell — so what a reserve measures is never the raw `Qty` but the **spare**:

```
spare = Qty - Used
```

No spare, no alert, whatever else is configured. Hold 6 Gems and spend 6 Gems a tick and every
mode is silent on Gems, because there is not one unit in that stockpile you are not already
committed to.

`reserve` then sets what else must be true of that spare. It is a string with exactly three valid
values, and an invalid value refuses the settings file instead of silently choosing a fallback:

- `"none"` alerts while the spare is above zero. `reserve_amount` is ignored.
- `"qty"` alerts when `spare - reserve_amount > 0`. Holding 20 and spending 6 leaves 14 spare, so
  a reserve of 14 stays silent and 13 alerts.
- `"ticks"` alerts when the Resources panel's `Ticks-Worth` is greater than `reserve_amount`.
  That number is **not** charged for `Used` a second time: the game computes it as
  `floor((Qty - Used) / |Net|)`, so it is already counting spare, and it is compared as it stands.
  What the spare floor adds here is the game's `N/A`, which the page prints for any non-negative
  `Net` without regard to how much is spare — `N/A` still means the stock never runs out, but it
  can no longer alert on a stockpile that is entirely spoken for. `NONE` is `Qty` below `Used`,
  which the floor has already refused.

`reserve_amount` must be a non-negative whole number. Reserves are absolute: `always` can override
the friend/alliance checks, but it cannot alert when there is no excess stock to contribute.

If `overview.php` ever renders without its `Used` column, the poll raises rather than assuming you
spend none — the same rule the `Ticks-Worth` column has always followed. "We could not read it"
must not quietly become "you have it all to spare".

```json
"Machinery Parts": {
  "friends": true,
  "alliance": true,
  "reserve": "ticks",
  "reserve_amount": 4,
  "always": [],
  "never": []
}
```

#### When it alerts, and what it costs

Unlike news and reports, **a market alert repeats on every poll for as long as the order is
pending**. A standing buy order is a current fact rather than an event, and one you were told about
once yesterday is one you have forgotten; this is the same reasoning as the unread-message counts,
which also alert every poll while they are nonzero. Nothing about market orders is written to
`.state/`, so deleting that folder changes nothing here, and a restart re-alerts on whatever is
still pending.

Watching costs requests. Each poll first makes one GET of `overview.php` for the reserve check, then
one GET of `buyermarketplace.php` to pick up its rotating form token, then one POST per watched good
— the order table exists only in response to a POST — plus one GET for the alliance roster, which
is re-read every poll because members join and leave. Switching all 28 goods on is therefore about
31 requests every poll, which is worth knowing before you do it at the default 60-second interval.
When sheet sync is enabled, it reuses that same parsed overview instead of fetching the page again.
Nothing watched means no market requests at all. The roster GET is skipped — leaving the cost at
2 + one POST per good — in two cases: when your nation is
in no alliance, and when *every* watched good switches the alliance check off with
`"alliance": false`. Since `alliance` defaults to `true`, one good left at the default is enough to
bring the roster back.

Setting `"market_orders": false` under `alerts` mutes the whole feature in one edit, without
re-commenting the goods you had switched on. Muting stops the work rather than the alerts: no
preflight, no roster, no market requests. Muting while the monitor is running releases the goods the
preflight had already resolved, exactly as deleting them would, so the requests stop on the next
poll.

#### What it reads, and what it checks at startup

The roster comes from `viewalliance.php`, never from `myalliance.php`. Loading `myalliance.php`
marks your alliance messages as read as a side effect, which would break the alliance-message alerts
the monitor already does. Your own `alliance_id` is resolved by the market preflight and kept until
something re-runs it. The preflight runs at startup and again whenever a reload changes the watched
goods, so if you join or leave an alliance while the monitor is running, **switch a good on** in
`market.goods` and the next poll re-resolves it, printing the same `Market preflight passed` line
startup does and naming what it found. Switching a good *off* is not the same move: if it was your
last watched good there is nothing left to resolve, so the preflight is skipped, the alliance is
simply forgotten, and nothing prints. Switch one on — the same one, if you like, after switching it
off. Leaving the file alone leaves the id as startup resolved it.

Also at startup, every watched good name is resolved against the game's own list of tradeable goods.
**A name that is not a tradeable good stops the monitor and names the offending entry**, rather than
leaving you watching nothing while everything looks fine. The same check runs on every reload that
changes the goods, where a name that does not resolve is a refused reload rather than a stop: the
monitor warns, keeps the previous settings whole, and carries on polling. Names are matched case-insensitively, so
`machinery parts` resolves to Machinery Parts; the preflight line then reports the game's spelling.
Expect to see one good under two spellings: with `"machinery PARTS"` in `settings.json`, startup
prints `watching Machinery Parts` (the game's) while the alert reads `Buy orders for machinery PARTS`
(yours). Both name the same good — the startup line confirms what the game matched, and the alert
header echoes your settings file so you can find the entry that produced it.
If the nation has no alliance, the preflight says so and carries on — the alliance check simply never
matches.

## Failures

A failure is never terminal-only. Anything that goes wrong raises the same blocking dialog as an
alert, titled **CLOP monitor problem**, so polling pauses until you acknowledge it instead of
continuing to fail where you cannot see it. Failures are never retried silently: a monitor that
cannot read the game is not monitoring it.

This holds everywhere, not just in the polling loop, because a printed failure is a failure nobody
sees — and a monitor that is quietly doing nothing is indistinguishable from one with nothing to
report. So it also covers:

- **Sheet sync being off.** If `CLOP_NATION` is not set, or your tab genuinely does not exist, the
  monitor still polls and still alerts while never writing a single cell to your tab. You get a
  dialog at startup saying so. A sheet that is merely *unreachable* does **not** switch sync off —
  see [Sheet sync](#sheet-sync).
- **The two hand-run scripts.** `python .\stockpiles.py` and `python .\buildings.py` raise the
  dialog too, not just terminal output — they are what the monitor's own dialogs tell you to run.
- **A broken notification channel.** If the webhook stops accepting alerts you get a dialog; if the
  dialogs themselves stop working you get a webhook message. Each is reported on the other, because
  a channel cannot announce its own silence. If both are down, the terminal is genuinely all that
  is left.

`NoTerminalOnlyFailuresTests` in `test_clop_monitor.py` enforces this by scanning the source: a new
failure that writes to the terminal without a dialog behind it fails the test suite.

Two kinds of failure, distinguished by what the dialog says:

- **The monitor has stopped and is no longer polling.** It cannot continue. The exit code says how
  far it got: **2** means it stopped before it ever logged in, **1** means it stopped from the login
  onwards.
  - **Exit 2:** `settings.json` exists but is unreadable or malformed, the environment file is
    unreadable, no credentials were supplied, or the configured 4chan thread is already archived at
    startup. (An *absent* `settings.json` is not a failure; the monitor uses its built-in defaults.)
    All of these are startup-only: once the monitor is running, the same broken `settings.json` is a
    refused reload rather than a stop, because there are previous settings to fall back on. The
    absent file flips the other way for the same reason — harmless at startup, a refused reload
    once there are settings it would silently revert.
  - **Exit 1:** the state file `.state/clop-monitor.json` is unreadable or corrupt (delete it — the
    next run rebuilds it, at the cost of re-alerting on the newest news and report), **the login was
    rejected**, the startup market preflight failed — a watched good that is not a tradeable good, or
    an account whose active nation could not be identified — or the 4chan thread archived while the
    monitor was running. The market preflight needs a logged-in session, so it runs after login and
    exits 1 rather than 2. A preflight run by a *reload* is refused rather than fatal.
- **The monitor is still running and retries in N seconds.** One check failed — the site was
  unreachable, a page could not be parsed, or the buyer's marketplace did not return the order table
  for a watched good — and polling resumes on the normal interval after you
  dismiss the dialog. A failure that persists alerts again on the next check; that repetition is
  deliberate, because a check that keeps failing is a monitor that is not working.
- **The previous settings are still in force and the monitor will go on polling.** A reload of
  `settings.json` was refused — see [Settings](#settings). Nothing about the running monitor
  changed. It warns again on every poll for as long as the cause lasts, which may be a mistake in
  the file waiting for you to fix it, or may be a network hiccup that clears by itself on the next
  poll; the dialog names which.

With `--once`, a failed check reports the same way and exits 1 rather than 0.
`--no-desktop-notifications` suppresses these dialogs exactly as it suppresses alert dialogs, leaving
the terminal message on stderr; an optional webhook receives failures too, prefixed
`Monitor problem:`.

## Sharing this folder

Everything committed here is safe to hand to someone else. The private pieces are git-ignored and are
never part of a clone: `.env` (your real username and password), `settings.json` (your own
configuration, including whichever thread you watch), `.state/` (your cached snapshot),
`__pycache__/`, and `*.log`. Build the archive from tracked files only, so those are excluded
automatically:

```powershell
git archive --format=zip -o clop-monitor.zip HEAD
```

If you instead copy the folder by hand, delete `.env`, `settings.json`, `.state`, and `__pycache__`
from the copy before sending it. The recipient copies the two `.example` files and fills in their own
values; the tests reference a public 4chan thread URL, which is fine to share as-is.

## Updating

**Stop the monitor first, with `Ctrl+C`, and start it again afterwards.** `settings.json` is
re-read while the monitor runs, but the program itself is not: a running monitor goes on using the
code it started with, so a `git pull` underneath one changes nothing until you restart it. This is
one of the two things a running monitor cannot pick up; the other is
[what still needs a restart](#settings) — the command-line arguments and the `.env` credentials.

Then run `git pull` in this folder. Nothing you own is tracked, so a pull never asks you to merge a
configuration file and never overwrites one. Read the startup lines when you start it again: a new
`Settings: using defaults for ...` name means the update added a setting you may want to set. That
line is printed only at startup — a reload never prints it — which is another reason to do the pull
and the restart together rather than leaving it for later.

## Google Sheets module

`sheets.py` is a separate, self-contained tool for reading and updating the **shared** CLOP
planning spreadsheet. It is standard-library only, like the monitor, and needs no Google account,
credential file, or setup — the endpoint it uses is committed in the module because the sheet is
shared and so is the tool:

```python
from sheets import GoogleSheet

sheet = GoogleSheet()
sheet.read_cell("LePone(Z)", "R11")        # -> 0
sheet.write_cell("LePone(Z)", "R11", 42)   # -> 42  (writes the live sheet)
sheet.read("LePone(Z)", "A11:U11")         # -> a 2-D list of the row
sheet.write("LePone(Z)", "A1:B2", [[1, 2], [3, 4]])
```

Ranges use A1 notation. `write` accepts a scalar, a flat row, or a 2-D block and coerces it to the
shape the range expects. Any failure — network, a non-JSON reply, or a server-side error such as an
unknown tab — raises `sheets.SheetError`.

Transient endpoint failures are retried before that error escapes: the original request, then three
retries after 1, 3 and 8 seconds. The rule is that **everything which can go wrong between asking
and being answered is retried**, because none of it can be told apart from a passing Google hiccup
on a single sample:

- network failures, and HTTP 404, 408, 425, 429 and 5xx responses;
- a reply that times out or breaks off part-way through its body;
- an HTTP **200 whose body is not JSON** — Google serves an HTML page for this, and it looks like a
  clean success right up until the parse;
- an `{ok: false}` reply carrying `retry: true`, which is the endpoint itself saying "ask again".

Apps Script protocol replies such as `no such tab` are definitive and fail immediately, on the first
attempt. Writes assign explicit values rather than adjusting them, so repeating an identical write
is safe whether or not the first one landed.

#### Google's expiring result link

This is the fault behind most `Sheet sync failed …` dialogs, and it is worth understanding because
the obvious reading of the message — "something is wrong with my sheet or my deployment" — is wrong.

`/exec` does not return the result of a POST. It runs the script and then **redirects to a one-shot
link** (`script.googleusercontent.com/macros/echo?user_content_key=…`) holding the output. Measured
on this deployment, that link is **consumed by the first read** and **expires after somewhere
between 15 and 30 seconds**. Read twice, or read late, and Google does not return a 404 — it falls
back to invoking the deployment over **GET**, which the original script did not implement. The
answer is `Script function not found: doGet`, as a 5 KB HTML page carrying **HTTP 200**.

The failure is Google being slow, nothing else. Caught live with both hops timed: after 56 clean
polls answering in 3–6 seconds, one poll saw the script execution stretch to 21.8 seconds and the
fetch of the result link take a further 16.3 — long enough for the link to expire *while that fetch
was still in flight*. The client now names it exactly:

> Google returned the sheet result too late to be read: 'Script function not found: doGet'. Nothing
> is wrong with your sheet, your data, or the deployment's access setting. […] Every retry starts a
> fresh request, so this clears on its own once Google speeds back up.

Each retry is a fresh POST and therefore a fresh link, which is precisely the remedy — the retry
window widened from 4 seconds to 12 after a live failure used up all three of the old attempts.

**The two hops are timed separately**, because measured against the live endpoint they behave quite
differently. From a 20-sample run:

| | on success | on failure | Cap |
|---|---|---|---|
| **hop 1** — runs the script | 2.41 – 7.51 s | 6.2 – 32.6 s | **12 s** |
| **hop 2** — fetches the result | 0.48 – 0.60 s | 10 – 14 s, then a dead-link 404 | **3 s** |

Hop 2 **separates cleanly**: half a second when it wins, ten-plus when it loses, nothing in between.
So it is cut tight — anything still running at 3 seconds has already failed and is just taking its
time to say so, and abandoning it for a fresh POST (which gets a fresh link) is the only real fix.
Waiting that out at the old 12-second cap is what ate whole retry budgets.

Hop 1 **does not separate** — 7.51 s returned good data while 7.1 s failed. There is no threshold
that keeps every success and drops every failure, so it errs generous. Throwing away a success
costs a whole extra round trip; tolerating a doomed attempt costs a few seconds, and there are
plenty of attempts.

The retry schedule is `0, ¼, ½, 1, 2, 4, 8, 15, 30` seconds — ten attempts, **fast at first and then
spreading out**, because the endpoint fails in two different ways. Brief independent failures
interleave with successes second by second, and immediate retries beat those. But sustained bad
patches happen too, and an all-fast schedule cannot outlast one: nine attempts packed into 70
seconds all failed together. The stretched tail puts the last attempts well outside that window.

A failure also **names which hop** it happened on. Both used to report the identical
`timed out reading the sheet endpoint's reply`, which made a dialog impossible to diagnose without
re-timing the hops by hand.

The delays between attempts are correspondingly small (0, ¼, ½, ½, 1, 1, 2, 2 seconds — nine
attempts). Failures here are *independent*, not sustained: timed back to back, successes and
failures interleave rather than arriving in blocks, so a fresh POST really is a fresh roll. Waiting
politely between rolls only spends the budget on sleeping; the old 1/3/8 burnt 12 seconds doing
nothing.

There is a **180-second ceiling** per read or write, retries included, with each hop's timeout
clamped to what is left. It is a backstop against a wedged call, **not** a retry budget — ordinary
retrying never reaches it.

> An earlier version set that ceiling at 45 seconds, reasoning that the monitor polls every 60
> seconds so a sheet call must not stall it. **That premise was wrong.** The loop is
> work-then-`sleep(interval)`, so the interval is a fixed pause *between* cycles, not a schedule a
> cycle must fit inside — nothing queues up behind a slow cycle and nothing overlaps. The one real
> cost was that sheet sync used to run *before* the alerting, making its seconds the alerts'
> seconds. That is fixed by running it **last**, not by cutting the retries short.

Measured with the final settings, during a Google patch bad enough that individual attempts kept
failing: **12 of 12 calls succeeded**, median 5.0 s. Four of them needed 29–64 s of retrying to get
there. Under the earlier settings that same weather produced `after 9 attempts in 158s` and a
failure dialog.

A `doGet` that answers this honestly (JSON with `retry: true`, instead of an HTML page) is committed
at **`docs/apps-script/Code.gs`**, along with a redeploy walkthrough at `docs/apps-script/README.md`.
Redeploying is optional; the client handles both.

Other non-JSON replies keep the older, blunter treatment: the error **quotes what actually arrived**
(`Content-Type` plus the first 200 characters) rather than guessing, and points at the deployment's
access setting only after repeated failures — that being the one cause a sign-in page really does
indicate.

### Your nation tab

Your nation has its own tab in the sheet, and the module reads its name from **`CLOP_NATION`** in
`.env` (resolved with the same rules as the credentials: a value in the process environment wins,
then the `.env` file). Set it to the tab name exactly — it is case-, spacing-, and
punctuation-sensitive:

```dotenv
CLOP_NATION=LePone(Z)
```

`sheets.startup_check()` resolves that name and confirms the tab exists before any work begins,
raising `SheetError` if `CLOP_NATION` is unset, names no such tab, or the sheet is unreachable. Run
the module directly to perform that check (read-only) and read your `R11`:

```powershell
python .\sheets.py
```

It prints the tab it found and exits 0 on success, or prints the reason and exits 1 if the check
fails. In your own code:

```python
from sheets import startup_check
sheet, nation = startup_check()          # errors here if the nation tab is missing
sheet.read_cell(nation, "R11")
```

Writes to Google Sheets always need a Google identity *somewhere* (API keys are read-only). This
project keeps that identity out of the repo by routing through a small Apps Script web app bound to
the sheet, deployed as *execute as owner / accessible to anyone*. The Google login lives inside that
deployment; the repo holds only its public `/exec` URL. If the deployment is ever lost, the design
doc `docs/superpowers/specs/2026-08-23-google-sheets-module-design.md` contains the Apps Script
source and the redeploy steps — paste it back onto the sheet and replace `EXEC_URL` in `sheets.py`.

## Sheet sync

When `CLOP_NATION` is set, the monitor runs one extra step **each poll, after the regular
alerting**: it fetches `overview.php` once and uses that single page to update three parts of the
sheet — your **building counts** and your **stockpiles** on your own nation tab, and your **column
on the alliance-wide `Dashboard-Stockpile` tab**. Nothing on the game is ever changed; this only reads
overview and writes the sheet.

**After, deliberately.** Alerting is what this program is for; the sheet is bookkeeping. Ordered
this way a slow or heavily-retried sheet call cannot delay a message, news or report alert by so
much as a second — it only delays the pause that follows. It also means the sheet may retry for as
long as it genuinely needs to, which is what makes it reliable. It used to run first, and that is
the entire reason its retries were once capped too tightly to succeed.

**A whole sync is two requests, not eleven.** Every request to Google is an independent chance to
hit the expiring-result-link fault, so eleven of them gave a sync eleven chances to fail — and
during a bad patch it always found one. All three reads now travel together in one request, and
every write in another:

| Per-request success | Sync completes — 11 requests | — 2 requests |
|---|---|---|
| 95% | 57% | **90%** |
| 90% | 31% | **81%** |

This needs the `batch` action in the Apps Script, so it only takes effect once the endpoint is
redeployed — see `docs/apps-script/README.md`. **Nothing breaks until then:** the client probes
once per run, falls back to the old one-request-per-range path if the deployment does not
understand `batch`, and starts using it by itself the moment the new script goes live. Retries,
timeouts and the caching above are unaffected either way.

Holding the writes back to the end has a second benefit: a failure part-way through leaves the
sheet **untouched** rather than half-updated, and the stockpile timestamp is still queued after its
values, so the sheet can never claim a freshness it does not have.

The three are independent. If one is skipped because that part of the sheet has been rearranged, the
other two still run, and the dialog says which.

### It only writes when something has actually changed

**The first poll after startup writes everything.** That is the reconcile that proves your sheet
matches the game. After it, the monitor keeps the parsed numbers in memory and compares each poll
against them; while nothing has moved, **the sheet is not touched at all — no reads, no writes, no
requests to Google**. When something does move it writes the lot again, exactly as before.

This is not an optimisation for its own sake. A game tick is about two hours, so writing everything
every 60 seconds rewrote identical numbers roughly 120 times per tick, at **11–15 Google round trips
each time**. That is some 45 seconds of Google traffic inside every 60-second poll — and it is why
Google's occasional slow minutes surfaced as `Sheet sync failed …` dialogs so often. Ten polls now
cost about 22 round trips instead of 110, and over a full tick the saving is upwards of 99%.

Two consequences worth knowing:

- **Your trades show up immediately.** The comparison is against the game, not a clock, so buying or
  selling something is picked up on the very next poll — a tick-shaped schedule would have missed it.
- **A hand-edit to the sheet is not noticed** until the game's numbers next move, or until you
  restart the monitor. Restarting always re-reconciles, so that is the fix if you have been editing
  cells by hand and want the monitor to reassert them.

`W10` therefore means "these are the numbers as at this time" rather than "the monitor looked at this
time" — it stops advancing while nothing changes. That is the more useful of the two claims, and the
honest one for a value that has not moved in two hours.

Sheet sync is **off** if `CLOP_NATION` is unset, and turns itself off (with one warning) if the tab
**is genuinely missing**. The monitor's message/news/report alerting is never affected either way.

A tab that is merely *unreachable* is treated completely differently, and the distinction matters:

| At startup the tab check… | Result |
|---|---|
| succeeds | Sheet sync on, as normal |
| says the tab does not exist | Sheet sync **off** for the session — fix `CLOP_NATION` or the sheet and restart |
| cannot reach Google at all (timeout, outage) | Sheet sync **stays on**; you get one dialog, and the first poll checks again |

That last row used to behave like the middle one, and it was the worst bug in this area: a single
passing Google timeout at startup switched sheet sync off for the entire run, and the monitor then
looked perfectly healthy — polling, alerting, saying nothing — while the tab silently went stale.
An outage is weather; a missing tab is a configuration fault. Only the second is worth giving up on.

The same distinction applies while polling: if the tab goes missing mid-run, sync switches off with
one dialog rather than raising an identical one every 60 seconds forever.

Before it writes anything, the monitor checks that the page it just fetched really is a complete,
normal overview page. **If it is not, nothing at all is written** and you get a dialog. This matters
more than it sounds: a broken page with empty tables looks exactly like a nation that owns nothing
and holds nothing, so writing from one would wipe your tab and mark it freshly checked.
[When a sheet sync dialog appears](#when-a-sheet-sync-dialog-appears) explains each message.

### Building counts

The monitor reads your building counts from `overview.php` and corrects your nation tab to match. It
updates only the cells that are wrong — the **have** count in column B and, in the `DISABLED:` region
lower down, the **disabled** count — and then pops up a dialog listing the corrections it made (e.g.
`Basic Mine have 8 -> 10`). Buildings you no longer own are set back to 0.

The building names on `overview.php` differ from the sheet's column A (the game calls it
`Basic Copper Mine`, the sheet says `Basic Mine`), and one sheet row can stand for several game
buildings (the single `DNA` row covers every regional DNA facility; `Energy Collector` covers the
Solar Collector and Tidal Generator). That translation lives in **`building_map.py`**, whose names
come straight from the game's own building list (`resourcedefs`).

Because that mapping and the sheet's layout can drift, `buildings.py` sanity-checks them before every
write, and skips the write (with a popup) if anything looks wrong rather than writing to the wrong
row. Run the same check yourself any time — for instance after editing the sheet's layout — with:

```powershell
python .\buildings.py
```

It logs in, verifies the mapping against your sheet read-only, and reports pass/fail with an exit
code. If it ever flags a building it can't place, update `building_map.py` (or the sheet) so the
names line up again.

#### Why the corrections can arrive over several polls

Each run is a **full sweep**. `reconcile` walks all 36 sheet rows in both the have region and the
`DISABLED:` region, and queues a write for *every* cell that disagrees with the game; they all go out
in the same request. It never fixes one building and leaves the rest for the next poll.

What it cannot do is see a change you have not made yet. The monitor polls every 60 seconds and
reports what `overview.php` was showing at that instant, so disabling four kinds of building over a
couple of minutes gives you a correction dialog *per poll* rather than one dialog with the lot in it.
That is the poll interval, not a partial write.

A worked example, from `LePone(Z)` on 2026-08-25. The 14:00:02 UTC tick report says *"You lose 2
satisfaction for having 2 disabled buildings"* — and the tick counts that by summing the `disabled`
column over every building the nation owns (`cron/frequent.php`), so at 14:00 the whole nation had
exactly **two** buildings disabled. The dialog at 14:01:28 UTC read `Basic Mine disabled 0 -> 31`,
and that was the complete truth 86 seconds later: the 31 mines had just been switched off, and the
Gem Mine, Tungsten Mine and Mall figures that showed up on later polls had not been switched off yet.

Two things stretch that window further, and both are worth knowing:

- **A dialog freezes the monitor.** Desktop alerts are modal — nothing is polled while one is on
  screen. Anything you disable while reading an alert waits for the poll *after* you dismiss it.
  Dismissing one also **throws away the page** the sync was going to use, because that page was
  fetched before the dialog went up and is therefore as old as the dialog was long. It reads a
  current one instead. This is not a nicety: while it did not, a report alert left standing for 38
  minutes had a Toy Factory built behind it, and dismissing it wrote `Toy Factory have 2 -> 1` over
  a sheet cell that was already right — a *false* correction, in the same dialog real ones use. See
  [The sheet is never reconciled against a page older than the
  dialog](docs/2026-08-26-stale-page-after-a-dialog-design.md).
- **A cell that is wrong on its own stays wrong.** See [It only writes when something has actually
  changed](#it-only-writes-when-something-has-actually-changed): while the game's numbers have not
  moved the sheet is not even read, so a hand-edit — or a stale value left over from an earlier
  state — is not reasserted until the game next moves, or until you restart the monitor.

If you want the answer right now rather than at the next poll, `python .\buildings.py` reads both
sides and reports, without writing anything.

### Stockpile snapshot

In the same step, the monitor records how much of six goods you are holding. It writes **`R11:R16`**
— the `HAVE` column, beside the `apple` / `oil` / `coffee` / `mpart` / `vpart` / `gems` labels in
column Q — and stamps **`W10`** with the date and time the numbers were read.

A few things worth knowing before you read those cells:

- **`W10` is the game's own clock**, copied across exactly as the game prints it at the top of the
  page, with no timezone conversion. Whatever time the game is showing you is the time you will see
  in the sheet. Nothing has to agree about timezones for it to be meaningful. It is stored as **text,
  not as a date** — if you click the cell you will see a leading apostrophe (`'2026-08-23 07:12:30`)
  in the formula bar, which is Google Sheets' marker for "leave this alone". That is deliberate and
  not a fault: stored as a date, Sheets would quietly reinterpret the game's clock as being in the
  *spreadsheet's* timezone and be wrong by that offset without ever looking wrong. If you want to
  calculate with it, convert it explicitly in your own formula, in a different cell.
- **`W10` means *last changed*, not *last checked*** — this changed, and it matters. It used to be
  rewritten every single poll, which made it a dead-man's switch: an old `W10` could only mean the
  snapshot had stopped running. Now that the monitor only writes when the numbers actually move,
  `W10` stops advancing while nothing is happening, and an old one has two possible meanings.

  It is still a usable staleness signal, just a blunter one: **a game tick is about two hours, and a
  tick always moves something**, so a `W10` more than a couple of hours old does mean something is
  wrong — either the monitor is not running or it cannot reach the sheet. Between ticks, though, a
  static `W10` is normal and expected.

  If you want the strict dead-man's switch back, it can be had for one Google request per poll
  instead of eleven — stamping `W10` every poll while still skipping the thirteen value writes. Say
  the word; it is a small change.
- **A routine update is silent.** No dialog, no sound, nothing in the way. Only problems interrupt
  you. (Building *corrections* still pop up, because those are the monitor disagreeing with the
  sheet rather than simply refreshing it.)
- **A good you hold none of is written back as `0`**, not left alone. The game only lists goods you
  actually have, so an absent good means zero, and the sheet is made to say so.
- **`NEED`, `BUY` and `TICKS` are never touched.** Those columns are yours — your formulas and your
  inputs. The monitor only ever writes the `HAVE` column and the timestamp, and never reads what
  your columns say.
- **All six rows are rewritten every time**, rather than only the ones that changed. Comparing first
  would be cheaper, but a cell showing `#REF!` reads back as `0`, which for a good you hold none of
  looks identical to the correct answer — so the broken cell would survive under a fresh timestamp.
  Overwriting removes that hiding place.

**Nothing is addressed by a fixed row or column.** The monitor finds the `STOCK` header in column Q,
then finds `HAVE` in that same header row, then reads the labels beneath it. So you can insert rows
above the block, or move the `HAVE` column, and the snapshot follows it. What it cannot survive is a
label being **renamed, deleted or duplicated** — then it does not know which row is which, and
nothing is written, not even the timestamp, which is deliberately left to go stale so the sheet
visibly stops claiming to be current.

### The `Dashboard-Stockpile` tab

The shared `Dashboard-Stockpile` is the alliance-wide view: one column per nation, `TOTAL (or min)`
in column B. Do not fill the grid in by hand — the monitor is the intended writer.

Two rows on that tab belong to the **sheet**, not to this tool, and it never writes either:

- **`A1`** — the sheet's own "game now" clock. (It used to hold a `READ ONLY` notice.)
- **Row 2, `Ticks Since Recorded`** — a per-nation formula measuring how stale that column is:
  `A1` against that column's `Active` stamp, counted in tick boundaries crossed rather than hours
  elapsed. `docs/2026-08-25-game-time-is-utc.md` has the formula and why it counts ticks.

Off the same page read, the monitor writes your own column, in four blocks:

- **all 31 goods** in the game, not just the six your nation tab tracks;
- **six `<good> - tick` rows** — how many ticks your stock of apples, oil, coffee, machinery parts,
  vehicle parts and gems lasts at the current net rate. This is overview's own `Ticks-Worth`
  column, copied across as it stands: a number, or `N/A` (net is zero or positive, so it never runs
  out) or `NONE` (already less than one tick's worth left);
- **`Active`**, a last-updated timestamp;
- **`Sat`, `NLR`, `SE`** as `current (per tick)`, exactly as the game shows them, and **`GDP`** and
  **`Bits`** as plain numbers so the `TOTAL (or min)` column can add them up.

Your column is found by matching your `CLOP_NATION` against the names in row 1, and each row by
looking its label up in column A — so, again, no fixed addresses. **If your nation is not named in
row 1, nothing on the Dashboard is written** and the dialog prints what row 1 actually contains, so
you can see whether your tab is spelled differently. Your own nation tab still updates.

Only your column is ever written. Nobody else's numbers, never the `TOTAL (or min)` column, and
never rows 1 or 2 — the first block written starts below them.

`docs/2026-08-23-dashboard-goods-map.md` is the full row-by-row map of that tab.

The tab was called `Dashboard` until 2026-08-24, and its blocks have been rearranged twice since —
most recently on 2026-08-25, when `Ticks Since Recorded` was inserted as row 2 and pushed every
block below it down by one. Only the tab *name* has ever needed a code change: every row and column
is found by looking its label up, so moving them around is something the monitor simply follows.

### Checking it yourself

```powershell
python .\stockpiles.py
```

Logs in, prints the server time, then shows **where each lookup resolved to** — the `STOCK` header
row, the `HAVE` column, the timestamp cell, your Dashboard column — followed by what the game
reports beside what the sheet currently holds, and every cell a real run would write. It reports
pass/fail with an exit code. **It never writes.** This is the thing to run when a dialog tells you
the sheet layout is wrong.

### When a sheet sync dialog appears

Every one of these is the blocking **CLOP monitor problem** dialog described under
[Failures](#failures). The monitor keeps polling in every case below — none of them stops it.

The first column is the phrase to look for in the dialog, not the whole text.

| The dialog says | What it means | What to do |
|---|---|---|
| **`overview.php has no Resources panel`** (or `no Buildings panel`) | The game handed back something that is not an overview page at all — an error page, a maintenance page, or a redirect. **This is almost certainly the site, not this tool.** | Open the site in a browser. If it is unhappy too, work through `docs/OUTAGES.md`. **Nothing was written to the sheet.** |
| **`overview.php stopped part-way`** | The page was cut off before it finished sending. Usually the host struggling, not the tool. | Check the site in a browser. If the page looks perfectly complete there and this keeps firing, the check is strict about the page *ending* with `</html>` — something is printing extra output after the footer, and that needs fixing in the game code. **Nothing was written.** |
| **`overview.php lists no resources and no buildings at all`** | The game's own database query failed. The page renders fine but both tables come back empty, which is why this is caught rather than believed. Also fires legitimately for a **brand-new nation before its first tick**, which really does have nothing. | If the nation is new, wait for the first tick and it clears itself. Otherwise it is a game-side fault: see `docs/OUTAGES.md`. **Nothing was written.** |
| **`Stockpile snapshot: some cells were not written`** | Something could not be located or read. The rest of the dialog names every problem found; anything it does not mention was written as usual. | Run `python .\stockpiles.py` — it shows where each lookup resolved to, which is usually enough to spot what moved. Fix the sheet, then rerun it to confirm. **A region that was skipped is untouched, and its timestamp will go stale on purpose** until it is. |
| **`overview.php has no Ticks-Worth column`** | The game's Resources table no longer has the column the `- tick` rows come from. Those six rows are **left exactly as they are** rather than zeroed, because "we could not read it" and "you have none" are different claims. Everything else on the tab was written. | Look at `overview.php` in a browser. If the game has genuinely dropped or renamed that column, `TICKS_HEADING` in `goods.py` needs updating to match. |
| **`overview.php had no Used column`** | The game's Resources table no longer has the column every market reserve subtracts before deciding you have stock spare. The poll raises instead of assuming you spend nothing, which would alert on goods you are fully committed to. Nothing else about the poll changes — this only reaches you when a good is being watched in `market.goods`. | Look at `overview.php` in a browser. If the game has genuinely dropped or renamed that column, `USED_HEADING` in `goods.py` needs updating to match. To keep the monitor running meanwhile, comment out the watched goods with a leading `#`. |
| **`resource '...' has an unreadable Used value`** | Same as the unreadable-quantity row below, for the `Used` column: the game printed something that is not a plain number, and it is refused rather than guessed at. | Look at that resource on `overview.php`. If the game has started formatting that column differently, `goods.py` needs updating. |
| **`stock label '...' is missing from the run`** | A label under the `STOCK` header was renamed, deleted, or pasted twice, so the monitor cannot tell which row is which. Reordering them is fine; losing one is not. | Put `apple, oil, coffee, mpart, vpart, gems` back under the `STOCK` header — any order — then run `python .\stockpiles.py`. **Nothing on your nation tab was written, including the timestamp.** The Dashboard still updated. |
| **`no column in row 1 ... is named`** | Your nation is not in the `Dashboard-Stockpile`'s header row. The dialog prints what row 1 actually holds. | Check `CLOP_NATION` in `.env` against the names in that list — it is case-, spacing- and punctuation-sensitive. If your column is genuinely missing from the shared sheet, ask whoever owns it to add one. **Nothing on that tab was written.** Your own nation tab still updated. |
| **`W10 should be empty or hold a ... stamp`** | The timestamp cell holds something that is not a timestamp. It is the one cell found partly by convention, so it refuses to overwrite anything it does not recognise. | Look at that cell. If someone has started using it for something else, the block needs moving or `TIMESTAMP_COLUMN` in `stockpiles.py` needs changing. **Nothing on your nation tab was written.** |
| **`Building sync skipped`** | The sheet layout or the building mapping looks wrong, so no building cells were changed. | Run `python .\buildings.py`, then fix the sheet or `building_map.py` as it tells you. The stockpile snapshot still runs — the three guard different parts of the sheet. |
| **`no 'Server time:' stamp on the page`** | The page arrived without the clock the monitor stamps into `W10`. Every normal CLOP page carries one, logged in or not, so this means the page is not a normal one. | Same as the first row: check the site in a browser, then `docs/OUTAGES.md`. If the site is fine and this keeps firing, the game's page layout has changed and `stockpiles.py` needs updating. **Nothing was written.** |
| **`resource '...' has an unreadable quantity`** | The game printed a quantity that is not a plain number. It is refused rather than guessed at, because guessing would write a wrong number — most likely a `0`, meaning "you have none of this". | Look at that resource on `overview.php` in a browser. If the game has started formatting quantities differently, `stockpiles.py` needs updating to match. **Nothing was written.** |
| **`not logged in when reading overview.php`** | The session dropped and logging back in did not take. | Check `CLOP_USERNAME` and `CLOP_PASSWORD` in `.env`, and that you can sign in through a browser. **Nothing was written.** |
| **`Could not reach the shared sheet at startup`** | The startup tab check could not get through to Google. **Sheet sync is still on** — this is an outage, not a configuration fault, and the first poll checks again. | Nothing. If it is still failing minutes later, check the sheet opens in a browser. |
| **`Sheet sync is off:`** | Your tab genuinely is not in the sheet, or `CLOP_NATION` is unset. This one *does* stop sheet sync for the session. | Check `CLOP_NATION` in `.env` against the tab names — it is case-, spacing- and punctuation-sensitive — then restart the monitor. Messages, news and reports keep working meanwhile. |
| **`Sheet sync failed during ...`** | The catch-all. If the sentence after the colon matches a row above, use that row. Otherwise it is a network or Google Sheets problem rather than anything wrong with your data, and the message names which half it happened in. | For a plain network or Sheets problem: usually nothing, it clears by itself. If it persists, check your internet connection and that the sheet still opens in a browser. |
| **`Google returned the sheet result too late to be read`** | **The common one, and it is not your fault.** Google answers a POST by redirecting to a single-use result link that expires in well under a minute; when it is read late, Google runs the script over `GET` instead and returns `Script function not found: doGet`. Already retried four times over twelve seconds, so Google was having a genuinely bad minute. | Nothing. It clears itself, and the next poll starts fresh. **Nothing was written on this attempt.** If it fires on most polls for an hour or more, redeploy the endpoint per `docs/apps-script/README.md` — that makes the failure legible but does not make Google faster. |
| **`unexpected non-JSON reply from sheet endpoint`** | Google answered `200 OK` with an HTML page that is *not* an Apps Script error page. Already tried four times. The message quotes the page's `Content-Type` and its first 200 characters. | Read the quoted snippet. If it looks like a Google sign-in page, the deployment has lost its "Anyone" access — redeploy per `docs/apps-script/README.md`. If it is a Google "unable to open the file" page, that is Google's end and it clears by itself. **Nothing was written on this attempt.** |
| **`the sheet endpoint returned an Apps Script error page`** | The script itself failed — e.g. `Authorization is required to perform that action`. This is the deployment, not the network. | Open the script (Extensions → Apps Script on the sheet) and check it still runs and is still deployed as *Execute as: Me* / *Who has access: Anyone*. `docs/apps-script/README.md` has the walkthrough. |
| **`timed out reading the sheet endpoint's reply`** / **`the reply broke off part-way`** | The connection to Google opened but the answer never fully arrived. Already retried four times, inside a 45-second budget. The timeout is deliberately shorter than it used to be: Google's result links expire in well under a minute, so waiting longer collects a dead one rather than a slow one. | Almost always Google, occasionally your connection. It clears by itself. **Nothing was written on this attempt.** |
| **`… (stopped at the 45s budget)`** | Appended when the retries ran out of *time* rather than out of attempts — Google was slow enough that continuing would have stalled the poll. | Nothing. The next poll starts over with a clean budget. |
| **`Sorry, unable to open the file at present`** (as an `HTTP 404`) | The same expiring-link fault as the row above, caught a moment earlier: Google's Drive front-end answers a dead result link with its own "Page not found". Retried like any 404. | Nothing. **Nothing was written on this attempt.** |

Two details that will otherwise confuse you:

- Most of these arrive wrapped as **`Sheet sync failed during reading overview.php: ...`** — every
  row above except `Building sync skipped` and `Stockpile snapshot skipped`, which are raised
  directly. That prefix is just where the monitor had got to; the sentence after the colon is the
  real message, and it is the one to match against the table.
- **A persistent problem raises its dialog again on every poll**, up to two of them, one per half.
  There is deliberately no "don't show this again": in this monitor every warning is a popup, and a
  warning you can dismiss permanently is one you will miss when it matters. While you sort it out,
  either fix the cause or stop the monitor with `Ctrl+C` — do not just keep clicking OK, because you
  will stop reading it.

A response cut off *mid-transfer* — as opposed to the game sending a short page, which the checks
above catch — used to end the monitor with a Python traceback and no dialog at all. It now raises
**`The response from ... broke off part-way`** through the normal dialog, and polling continues.

## Empire relations: when do the airstrikes start?

`relations.py` answers one question the game will not: **how many ticks until the Solar Empire or
the New Lunar Republic bombs us?**

```powershell
python relations.py --se -120 --nlr 76 --government Democracy
python relations.py --se -120 --nlr 76 --drift -6 3 --trace
```

```
start   SE      0   NLR      0   sum      0
drift   SE     -3   NLR     +2   per tick (2h)

SAFE FOR      37 more ticks (74 hours)
ACT BY        tick 37, with SE -123 and NLR 74
              -> 984 Oil or Tungsten to put SE back to 0, 74 Drugs to put NLR back to 0
OTHERWISE     tick 38: Solar Empire sends 32 pegasi, fighting at the next 00:00 or 12:00 UTC
```

`--trace` prints every tick up to the deadline; `--plan` compares reset points by how long they buy
and what they cost. Resetting to 0 / 0 is not the best target — banking the Solar Empire relation
high suppresses the NLR for free and buys **149 safe ticks (12.4 days)** instead of 37, at the same
Oil per tick.

It is a tested port of the relation pipeline in `clop/cron/frequent.php`, in the tick's own order,
including its one live bug. Three things it exists to stop you getting wrong:

- **The trigger is the sum.** `se_relation + nlr_relation < -50` fires the strike; each superpower
  joins in only if its own relation is under -25. `-900 / +900` is never touched.
- **Do not extrapolate the `(per tick)` figure on `overview.php`.** That is a separate
  re-implementation which over-predicts Solar Empire recovery below -700, ignores the airstrike
  rebound, and is only valid for one tick — decay and jealousy are step functions of the current
  values, so the rate moves as you do.
- **Balanced drift survives; lopsided drift does not, however positive its total.** Jealousy takes
  `floor(other/50)` off one relation without giving it to the other, so it drains the sum every
  tick. `+1 SE / +2 NLR` gains three points a tick overall and is still bombed, at tick 182.

The full mechanism — every formula, the SE forgiveness bug, the stasis trap, and a lookup table of
first-strike ticks — is in `../docs/DEVELOPMENT.md`, "Empire relations: decay, jealousy, and exactly
when the airstrikes start".

## Tests

The parser tests use synthetic HTML and never contact the hosted game. The Sheets, overview, goods,
nation, building and stockpile tests (`test_sheets.py`, `test_overview.py`, `test_goods.py`,
`test_nation.py`, `test_buildings.py`, `test_stockpiles.py`) stub the network, so they never contact
Google or the game. `test_relations.py` is pure arithmetic and pins each constant to a hand-read
line of `frequent.php`. All **710** of them run under one command:

```powershell
python -m unittest -v
```
