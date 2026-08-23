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
      "# You sold % and made % bits.",
      "# You bought % from % for % bits.",
      "# Change in Satisfaction:",
      "# Burn Oil",
      "# Distribute Pies",
      "# Build % completed successfully."
    ]
  },
  "market": {
    "goods": {
      "# Machinery Parts": {"friends": true, "alliance": true, "always": [], "never": []},
      "# Oil":             {"friends": true, "alliance": true, "always": [], "never": []},
      "# Pies":            {"friends": true, "alliance": true, "always": [], "never": []}
    }
  },
  "fourchan": {
    "thread_url": "https://boards.4chan.org/mlp/thread/43454282/clop-financial-crisis-edition",
    "_thread_url_help": "Set thread_url to null or remove the fourchan section to disable thread monitoring."
  }
}
```

The `market` section is abbreviated above: the real `settings.example.json` lists all 28 tradeable
goods, every one of them commented out. See
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
queued — and alerting on them buries the ones that matter. `reports.ignore` is a list of patterns,
and **each line of a report is judged against them separately**:

- a line that matches is left out of the alert;
- a report whose **every** line matches raises no alert at all;
- a report with any line left alerts, showing **only the lines that survived**.

That matters because the two-hourly tick is a single report containing everything that happened —
forty lines of production with, once in a while, `You couldn't pay the upkeep for your First Cavalry
and it's gone!` in the middle of them. Judging the report as a whole meant silencing the tick and
that line together, or neither. Judging it a line at a time lets you silence the routine forty and
still be told about the one.

**A pattern starting with `#` is switched off.** JSON has no comment syntax, so this is how a pattern
is commented out: it stays in the list where you can see it, and you delete the two characters to
switch it on. Every shipped pattern is commented out, so the monitor silences nothing until you say
so:

```json
"reports": {
  "ignore": [
    "% completed successfully.",
    "You spent % %.",
    "You paid % bits.",
    "# You bought % from % for % bits.",
    "# Show Details",
    "# Change in Satisfaction:"
  ]
}
```

That file goes quiet for a finished action, and leaves the rest switched off. It takes all three
switched-on patterns to do it, because a finished build is three lines and each needs its own: drop
`You spent % %.` and the report still alerts, showing `You spent 20 Machinery Parts.` on its own.
Leading spaces before the `#` are fine. The `#` only means "off" at the very start of a pattern,
so `Report #% filed` is a normal pattern; the cost of the convention is that you cannot match a
report whose text genuinely begins with `#`.

- A pattern matches **anywhere** in a line.
- `%` stands for **any run of characters**, including none, so `You paid % bits.` covers any price.
- Matching **ignores case**.
- Everything else is literal: `.` and `(` mean themselves, not what they mean in a regular
  expression.
- **One pattern silences one line.** A finished build arrives as three lines — `You spent 20
  Machinery Parts.`, `You paid 50,000 bits.`, `Build Advanced Factory completed successfully.` — and
  going quiet takes a pattern for each. That is the price of being able to keep the fourth line when
  a fourth line turns up.

Patterns are substrings, not whole-line rules, so keep them specific enough to mean what you want.

**A pattern cannot span two lines.** The lines are the game's own: wherever the page breaks a report
— a `<br/>`, or the end of the block the tick wraps its details in — the monitor breaks it too, and
`%` does not reach across the break. So write each pattern to match one line as the page shows it.
The exception proves the rule: three of the game's reports are written with a newline in the middle
of a sentence rather than between two of them, and those still count as one line, because it is the
page's own line breaks that are honoured and not every newline in the text.

#### Silencing finished actions

Every action you complete reports **`<action name> completed successfully.`**, and the name is the
action's own, taken straight from the game's recipe list. That matters more than it sounds: of the
game's 62 actions only **38** are named `Build ...`. The other 24 begin with `Upgrade`, `Dig`,
`Plow`, `Ship`, `Smuggle`, `Manufacture`, `Distribute`, `Burn`, `Drug` or `Receive` — so
`Build Basic Oil Well completed successfully.` and `Dig Basic Copper Mine completed successfully.`
are the same kind of event with different first words.

A pattern of `Build % completed successfully.` therefore silences your oil wells and not your copper
mines. Use **`% completed successfully.`** instead and it covers every completion — builds,
upgrades, digs, shipments, weapons and armour alike — in one line.

The verb-specific patterns ship alongside it for when you want to be selective. Uncommenting
`Build %`, `Upgrade %`, `Dig %` and `Plow %` silences your construction work while still telling you
when a shipment to one of the Empires lands, which is a relations change you may want to see.

Failed and refused builds produce no report at all, so nothing here can hide one.

#### Silencing the two-hourly tick

The tick is the noisiest thing the game writes and the one most worth silencing. It takes a pattern
per routine line, and the settings example ships the whole set, commented out, in one block from
`Show Details` downwards: the Show/Hide wrapper and the `Change in ...` totals, production and
consumption, the relation and satisfaction effects, the satisfaction and relationship caps, the
government and military upkeep bills, and the siphons that take the top off a stockpile. Switch that
block on and a quiet tick says nothing at all.

`Change in Satisfaction:` used to be the pattern to avoid, because it appears in every tick and
whole-report matching meant it silenced the tick's warnings along with its bookkeeping. Per-line
matching is what makes it safe: it now silences one line, the `Change in Satisfaction: -2` total, and
nothing else. A tick containing any of

- `You couldn't pay the upkeep for your First Cavalry and it's gone!`
- `Too many Basic Oil Wells cause environmental damage! (-5 sat)`
- `You don't have enough Oil to run your 3 Basic Factory!`
- `Your Democracy lacks the gasoline and vehicle parts to function properly! (-20 sat)`

still alerts with that line, and with only that line.

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
alerted, so a silenced report is examined once and never again. Report notifications include a
link to `reports.php`. Delete the `.state` folder to
establish fresh news and report baselines.

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
    "Machinery Parts":   {"friends": true, "alliance": true, "always": [], "never": []},
    "# Oil":             {"friends": true, "alliance": true, "always": [], "never": []}
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
https://4clop.org/buyermarketplace.php
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

#### When it alerts, and what it costs

Unlike news and reports, **a market alert repeats on every poll for as long as the order is
pending**. A standing buy order is a current fact rather than an event, and one you were told about
once yesterday is one you have forgotten; this is the same reasoning as the unread-message counts,
which also alert every poll while they are nonzero. Nothing about market orders is written to
`.state/`, so deleting that folder changes nothing here, and a restart re-alerts on whatever is
still pending.

Watching costs requests. Each poll makes one GET of `buyermarketplace.php` to pick up its rotating
form token, then one POST per watched good — the order table exists only in response to a POST —
plus one GET for the alliance roster, which is re-read every poll because members join and leave.
Switching all 28 goods on is therefore about 30 requests every poll, which is worth knowing before
you do it at the default 60-second interval. Nothing watched means no market requests at all. The
roster GET is skipped — leaving the cost at 1 + one POST per good — in two cases: when your nation is
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

Writes to Google Sheets always need a Google identity *somewhere* (API keys are read-only). This
project keeps that identity out of the repo by routing through a small Apps Script web app bound to
the sheet, deployed as *execute as owner / accessible to anyone*. The Google login lives inside that
deployment; the repo holds only its public `/exec` URL. If the deployment is ever lost, the design
doc `docs/superpowers/specs/2026-08-23-google-sheets-module-design.md` contains the Apps Script
source and the redeploy steps — paste it back onto the sheet and replace `EXEC_URL` in `sheets.py`.

Run the module directly to perform a live round-trip against `LePone(Z)!R11` (read, write `42`, read
back, restore) as a smoke test:

```powershell
python .\sheets.py
```

## Tests

The parser tests use synthetic HTML and never contact the hosted game. The Sheets tests
(`test_sheets.py`) stub the network, so they never contact Google. Both run under one command:

```powershell
python -m unittest -v
```
