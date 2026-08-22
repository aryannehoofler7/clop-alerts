# Hot-reloading settings.json — design

**Date:** 2026-08-23
**Status:** implemented (`settings_changes` and `reload_settings` in `clop_monitor.py`)

Re-read `settings.json` on every poll so that changing it takes effect without restarting the
monitor.

## The problem

`load_settings` ran once in `main` before the polling loop, so every setting was fixed for the
life of the process. Switching a good on, silencing a report, muting a category, or changing the
alert sound all required stopping and restarting the monitor — and restarting also threw away the
in-memory news, report and thread baselines when `cache.persist_to_file` was off.

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

### A successful reload is terminal output, not a dialog

A reload that changes something names the sections it changed. Where a section's setup actually ran,
what it resolved follows, in the words startup already uses: `market_preflight`'s line naming the
goods and the alliance it found, then the newly adopted 4chan baseline post. Those two are decisions
the reader can see nowhere else — the resolved alliance is the entire point of re-running the
preflight after joining or leaving one, and adopting post #N silently decides that every post up to
#N will never alert — and each appears at most once per deliberate edit. Sharing the exact startup
strings is deliberate: reload output should be recognisably the same thing as startup output rather
than a second set of sentences that drifts away from it.

A reload that changes nothing prints nothing. None of this raises a dialog: it is a confirmation
rather than a warning, and popups stay reserved for things that are wrong.

### Change detection gates the setup

Two gates in sequence. The file's raw bytes decide whether to parse at all; the parsed dataclasses
then decide which sections to set up. An unchanged file therefore costs one read and nothing else
— no parse, no extra requests, no rebuilt objects.

**Why the bytes, and not just the dataclasses.** `load_settings` does not only parse: it validates
against the world outside the file, and `wav_path` must name a file that still exists. Parsing an
untouched file every 60 seconds turns that startup-time check into a per-poll one, so a WAV on a
USB stick, a network share, or a cloud folder that evicts local copies raises the blocking dialog
on every poll — with nobody having edited anything, blaming `settings.json`, and, because the
reload runs before the check, leaving the game unread until somebody clicks OK. Unattended, that
is a monitoring outage caused by a sound file, and the README actively invites users to point
`wav_path` at their own.

Only the file's own bytes can say "nobody touched this" without consulting anything outside the
file, which is exactly the property needed. The bytes are compared, never inspected, and they are
adopted whenever the **reload** is accepted — including a cosmetic edit that reindents the file
without changing a setting, which would otherwise be re-parsed on every remaining poll.

Accepted, not merely parsed: a refused reload leaves the remembered bytes as they were. That is
the mechanism behind "warns for as long as the cause lasts". A file that parses cleanly but cannot
be brought into service — a `market.goods` typo naming a good the game does not have — is refused
with its old bytes still held, so the next poll sees the same difference, parses again, fails
again, and warns again, until either the file is corrected or whatever was unreachable comes back.
Adopting the bytes of a refused reload would warn once and then go quiet, which is the outcome
this design least wants: a monitor running on settings you think you replaced, saying nothing.

The bytes are read before the file is parsed, which matters for the same reason in reverse. The
remembered bytes can then only be older than or the same age as the settings they are paired with,
so an edit landing in that window reads as a change on the next poll and costs one redundant parse.
Parsing first would pair new bytes with old settings and the edit would be remembered as already
loaded — silently skipped, permanently. Erring toward the wasted parse is the whole point of the
order.

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

### A file that disappears is a refused reload

An absent settings file loads cleanly as the built-in defaults, which is what lets a clone that has
never been configured run at all. Mid-run the same read is a silent revert: every muted category back
on, the watched goods gone, the 4chan thread off, and `cache.persist_to_file` flipping back to true
so the monitor starts writing a state file the user had deliberately disabled — all of it under a
confirmation line reading as though the edit took.

So `file_found` going true → false is refused like any other bad reload: warn through the dialog
naming the missing path, keep the previous settings whole, keep polling. A file vanishing under a
running monitor is far more likely a rename to `settings.json.bak` while experimenting, or a reload
landing inside a non-atomic editor save, than a deliberate request for the defaults. Refusing it
cannot break the deliberate case either, because writing `{}` still asks for the defaults and is
applied like any other edit.

`file_found` stays out of the general comparison, where it does not belong: it describes the file
rather than what the monitor is doing, and comparing it would make a user who writes an explicit
value where one had been defaulting look like a change. Only the transition is special.

### What is not reloaded

`--interval`, `--state`, `--settings`, `--base-url` and the other command-line arguments are process
arguments rather than settings, and the credentials in `.env` are read once. Those still need a
restart, and the README says so.

### A note on the alliance

`market_preflight` resolves the account's `alliance_id`, so editing the market section re-resolves
it. That softens the existing "restart after joining or leaving an alliance" caveat: touching
`market.goods` is now enough. Leaving the file alone still leaves the id as it was resolved at
startup.

## A known, bounded limitation: the exact-revert race

The gate reads the file twice — once for the bytes, once for the parse — so there is a window
between them. Read-before-parse closes every version of that window except one: a file that
changes *and* changes back to byte-identical content inside it. The bytes then match what is
remembered while the parse has already picked up the intervening content, so the settings in force
are neither the old file nor the current one, and no later poll notices, because the bytes on disk
match the remembered bytes exactly.

It is recorded rather than fixed, deliberately:

- Reaching it needs two writes inside the microseconds between the two reads, the second restoring
  the first byte for byte. Review reproduced it only by forcing a write from inside a patched
  parser; no editor save sequence produces it.
- It self-clears. Any later edit changes the bytes, and the next poll reloads the file whole.
- The clean fix is to parse the bytes already read instead of re-opening the file, which also
  removes the second read. That means `load_settings` taking content rather than a path — and
  `load_settings` is heavily tested and shared with the startup path, so the change is much larger
  than the defect. Not worth taking on a working feature for a race this shape.

If it ever does matter, that one-line reshaping is the fix, and it is the only one worth making:
locking the file or comparing a hash instead changes nothing about the window.

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
- a thread that archives while being watched is still fatal;
- a re-run preflight names the goods and the alliance it resolved, and a swapped thread names the
  post it baselined, in the same words startup uses;
- a deleted file warns, keeps the previous settings whole, and keeps polling, while a monitor that
  never had a settings file is not warned about one and a file that appears later reloads normally;
- an untouched file is never parsed at all, so a `wav_path` that has since become unreachable
  raises nothing, and a cosmetic edit is parsed once and then left alone;
- a reload changing both `market.goods` and `fourchan.thread_url` where the thread check fails
  leaves the resolved goods and the alliance untouched — the ordering invariant, pinned rather
  than merely arranged;
- the notifier a sound edit rebuilds is the one that handles the next alert, and it keeps the
  webhook and desktop settings that are not part of `sound`;
- every field of `MonitorSettings` is either compared by `settings_changes` or listed as
  deliberately not compared, so a field added later cannot silently stop being reloadable.

## Rejected

- **Watching the file's mtime instead of reading it.** mtime is a proxy for the file's content
  rather than the content itself, and it can lie in both directions: an editor or a restored
  backup can put back an identical mtime over changed bytes, and a touch can change the mtime
  over identical bytes. Reading the bytes cannot be wrong about either.

  This is not an argument against a cheap gate in front of the parse; the implementation has one,
  described under "Change detection gates the setup" above. It is an argument about *what* the
  gate compares.
- **Applying the valid parts of a partially bad reload.** It would leave the live configuration as
  a mixture of two files with nothing naming which parts came from where.
- **Making a failed reload fatal.** Consistent with how a bad settings file behaves at startup, but
  it means a stray keystroke while editing ends a running monitor. The file being wrong does not
  stop the monitor reading the game.
- **A popup for a successful reload.** Maximum certainty, but it interrupts on every deliberate
  edit and dilutes the meaning of a dialog.
- **Leaving the 4chan thread restart-only.** Simpler, but it makes one section behave unlike every
  other for no reason the user of the file can see.
