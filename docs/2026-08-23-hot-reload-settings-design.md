# Hot-reloading settings.json — design

**Date:** 2026-08-23
**Status:** implemented (`settings_changes` and `reload_settings` in `clop_monitor.py`)

Re-read `settings.json` on every poll so that changing it takes effect without restarting the
monitor.

## The problem

`load_settings` runs once in `main` before the polling loop, so every setting is fixed for the
life of the process. Switching a good on, silencing a report, muting a category, or changing the
alert sound all require stopping and restarting the monitor — and restarting also throws away the
in-memory news, report and thread baselines when `cache.persist_to_file` is off.

## Design

### When

At the top of each poll iteration, before the check. Re-reading a small JSON file every 60 seconds
costs nothing, and doing it before the check means an edit takes effect on the very next poll.

### A reload is all-or-nothing

If the file cannot be read, cannot be parsed, or fails validation, the monitor **keeps the settings
it already had and keeps polling**. It never applies half a reload: a file whose `market.goods`
names a good the game does not have does not get its alert changes applied while its market changes
are dropped. That keeps "which settings are live" answerable at all times — they are the last set
that loaded cleanly, in full.

The same rule covers a change that is valid JSON but fails its setup, such as a good name that does
not resolve or a newly-configured 4chan thread that turns out to be archived. Those are rejected the
same way, because a setting that cannot be brought into service is not a setting the monitor can
honour.

### A failed reload is always a popup

Warnings are never terminal-only in this project. A failed reload goes through `notify_failure`,
the same blocking dialog as a failed poll, naming what is wrong and stating that the previous
settings are still in force. It re-warns on each poll while the file stays broken, which matches the
existing deliberate repetition for a poll that keeps failing: a monitor running on settings you
think you replaced is worth interrupting more than once.

### A successful reload is a terminal line

A reload that changes something prints one line naming what changed. It is a confirmation, not a
warning, so it does not raise a dialog — popups stay reserved for things that are wrong. A reload
that changes nothing prints nothing.

### Change detection gates the setup

Only a section that actually changed redoes its setup. An unchanged file therefore costs a file
read and nothing else — no extra requests, no rebuilt objects.

| Section | Detected by | Work when it changes |
|---|---|---|
| `alerts`, `reports.ignore`, `cache` | dataclass equality | none; used on the next poll |
| `sound` | dataclass equality | rebuild the `Notifier` |
| `market.goods` | `goods_to_watch` equality | re-run `market_preflight` |
| `fourchan.thread_url` | dataclass equality | re-run the archived-thread check, start a fresh baseline |

`MonitorSettings` and its members are frozen dataclasses, so equality is structural and free.
Comparing `goods_to_watch(...)` rather than `alerts.market_goods` means muting
`alerts.market_orders` correctly reads as "nothing watched" and releases the preflight.

### Swapping the 4chan thread

Setting `thread_url` to a different thread re-runs the same preflight startup does: fetch the
thread, refuse it if 4chan marks it archived, and adopt its current last post as the baseline so
the swap does not alert for a post that was already there. Setting it to `null` stops watching and
issues no requests.

A thread that becomes archived **while being watched** remains fatal, as it is today — the monitor
stops. A thread that is *already* archived when you configure it mid-run is a rejected reload
instead: it warns and keeps the previous thread. The difference is that the first is the game
telling the monitor its job is over, and the second is a typo in a text file, which should not end
an overnight run.

`build_alerts` already guards the thread swap: it only compares posts when
`current.fourchan_post.thread_url == previous.fourchan_post.thread_url`, so a changed thread cannot
produce a bogus "new post" alert from the old thread's marker.

### What is not reloaded

`--interval`, `--state`, `--settings`, `--base-url` and the other command-line arguments are process
arguments rather than settings, and the credentials in `.env` are read once. Those still need a
restart, and the README says so.

### A note on the alliance

`market_preflight` resolves the account's `alliance_id`, so editing the market section re-resolves
it. That softens the existing "restart after joining or leaving an alliance" caveat: touching
`market.goods` is now enough. Leaving the file alone still leaves the id as it was resolved at
startup.

## Testing

`test_clop_monitor.py`, synthetic only, never contacting the hosted game:

- an unchanged file reloads with no output, no rebuilt notifier and no market requests;
- a changed alert category takes effect on the next poll;
- a newly watched good re-runs the preflight; an unchanged watch list does not;
- muting `alerts.market_orders` releases the preflight the same way removing the goods would;
- changed sound settings rebuild the notifier, unchanged ones do not;
- unreadable and malformed files raise the blocking dialog, keep the previous settings, and keep
  polling;
- a good name that does not resolve rejects the whole reload, leaving the previous alert settings
  in force rather than half-applying;
- a swapped 4chan thread re-runs the preflight and establishes a baseline without alerting;
- a newly configured but already-archived thread is a rejected reload, not a fatal stop;
- a thread that archives while being watched is still fatal.

## Rejected

- **Watching the file's mtime instead of parsing it.** Cheaper, but it misses an edit that
  restores an identical mtime and it adds a second notion of "changed" alongside the structural
  comparison the dataclasses already give for free.
- **Applying the valid parts of a partially bad reload.** It would leave the live configuration as
  a mixture of two files with nothing naming which parts came from where.
- **Making a failed reload fatal.** Consistent with how a bad settings file behaves at startup, but
  it means a stray keystroke while editing ends a running monitor. The file being wrong does not
  stop the monitor reading the game.
- **A popup for a successful reload.** Maximum certainty, but it interrupts on every deliberate
  edit and dilutes the meaning of a dialog.
- **Leaving the 4chan thread restart-only.** Simpler, but it makes one section behave unlike every
  other for no reason the user of the file can see.
