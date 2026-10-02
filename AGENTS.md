# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

# apple-reminders-sync-to-home-assistant — working notes

Two-way sync between macOS Reminders and Home Assistant to-do lists. One
stdlib-only Python file, `reminders_ha_sync.py`, running under the
`/usr/bin/python3` that ships with macOS, driven by launchd every few minutes.

Two premises, because everything else follows from them:

1. **Stdlib only, no virtualenv.** This is what makes the LaunchAgent a plist
   with `/usr/bin/python3` in it and nothing else. A dependency would mean a
   venv, and the Reminders permission would then belong to the Python
   interpreter rather than to one small binary. Do not add imports outside the
   standard library.
2. **The state file is the whole design.** `state.json` records the values both
   sides agreed on at the end of the previous run. Without that snapshot a
   missing item is indistinguishable from a new one and every sync resurrects
   whatever the user deleted. Every rule below is downstream of keeping the
   snapshot honest.

Reminders is reached through [keith/reminders-cli](https://github.com/keith/reminders-cli)
(`brew install keith/formulae/reminders-cli`); Home Assistant through its REST
API with a long-lived admin token. Config is a URL and a token — lists are
paired by name and the missing ones created on both sides.

A second, independent feature publishes **battery levels** — the Mac and every
Bluetooth device that reports one — as Home Assistant sensors, via the
`mobile_app` REST API the official companion app uses. `features` in the config
switches each half on and off; they get a LaunchAgent each, on purpose (rule
40). The battery side shares only the config, the logger and the HTTP client
with the sync: no state, no ordering, no failure mode.

## Commands

```bash
make test                            # 156 unit tests: merge engine, pairing, batteries. Nothing live.
make check                           # test + py_compile everything
make e2e                             # 22 real round trips. Needs Reminders access.
make e2e-create                      # same, creating the RHS test lists first
make e2e E2E_ARGS="--only auto_mode" # a subset of the scenarios
make ha-up                           # dev HA on :8124, onboarded, dev/config.json written
make ha-down / ha-logs / ha-reset
make ha-token                        # print a fresh access token
make dev-doctor / dev-sync / dev-dry-run
```

A single unit test, any of these:

```bash
/usr/bin/python3 tests/test_merge.py DueFromHaTest.test_recreate_rebuilds_the_reminder
/usr/bin/python3 -m unittest tests.test_merge.MergeTest
```

The battery side, which needs no Reminders access and writes nothing until the
last step:

```bash
RHS_CONFIG=/tmp/rhs.json ./reminders_ha_sync.py battery --dry-run
RHS_CONFIG=/tmp/rhs.json ./reminders_ha_sync.py --verbose battery
```

`battery` deliberately runs whether or not `features.battery` is set: naming it
is asking for it. But it *registers devices in whatever Home Assistant the
config names*, so point `RHS_CONFIG` at a scratch config with a dev URL — and
give it its own `battery.state_file`, or a real run afterwards finds webhook ids
belonging to the container and re-registers everything.

**Never run `tests/e2e.py` directly.** Dev access tokens live 30 minutes and
`make` re-mints one on every target; run the script on its own and it fails with
a confusing 401. Go through `make e2e`, or `make ha-token` first.

The guided setup, non-interactively. **Always pass `--no-sync`**, and point
`RHS_CONFIG` away from the real config:

```bash
RHS_CONFIG=/tmp/rhs.json ./reminders_ha_sync.py setup --yes --no-sync \
  --url http://127.0.0.1:8124 --token "$(make ha-token)"
```

`setup` without `--no-sync` ends in a real sync, and against a fresh config that
means auto mode: every list on the machine paired up and pushed into whatever
Home Assistant the config names, plus a Reminders list created for every list
that Home Assistant has and Reminders does not. It also writes to the *real*
`state_file` unless the config overrides it — and a state file describing pairs
in the dev container is actively dangerous against a real instance, because
links pointing at uids that no longer exist read as "deleted in Home Assistant"
and propagate as deletions in Reminders. If a stray sync happens, delete
`~/.local/state/reminders-ha-sync/state.json` before syncing for real.

Releasing: `make formula TAG=v0.2.0` after the tag is pushed. See the README's
"Publishing and releasing".

## Layout

```
reminders_ha_sync.py     everything, in banner-commented sections — grep for these:
                           item model                normalizers; the two sides made comparable
                           config                    Config validates, nothing else does
                           Reminders side            the reminders-cli wrapper
                           Home Assistant side       the REST client, plus the mobile_app calls
                           battery: reading macOS    collectors and their pure parsers
                           battery: publishing       BatteryStore, publish_batteries
                           state                     Store, over state.json
                           planning                  plan_pair, merge_link — pure, no I/O
                           execution                 execute_plan — all the I/O
                           pairing lists             resolve_pairs
                           commands                  sync/run/doctor/lists/install/uninstall
                           guided setup              cmd_setup and its prompts
                           entry point               argparse + logging setup
Formula/…rb              the Homebrew formula; distribution is a tap, see the README
dev/formula.py           prints the formula for a pushed tag, with its sha256
config.example.json      the whole config: url + token
config.full-example.json every option with its default
tests/test_merge.py      the pure layers, with FakeReminders/FakeHa for pairing
tests/test_battery.py    the battery parsers over recorded ioreg/system_profiler/pmset
                         output, plus publishing against a FakeHa
tests/e2e.py             scenario list at the bottom; each one resets both sides first
dev/bootstrap.py         walks HA's onboarding API, creates the test lists, writes dev/config.json
dev/ha-config/           only configuration.yaml is committed; the container generates the rest
```

Planning is deliberately separated from execution: `plan_pair` is a pure
function over two item lists and a snapshot, which is why the merge rules are
unit-testable without a Mac, a container, or a permission. Keep new merge logic
on that side of the line.

## Rules that break things quietly

Each has a comment at the site explaining it. This is the index, not the
argument.

### The snapshot must never lie

1. **A failed write must not advance `last`.** `LinkWrite` carries `prev_last`
   and `execute_plan` rolls the link back if either side's write failed. Record
   a value as agreed when it was never written and the change is gone forever —
   both sides look settled on the next run.
2. **A failed Home Assistant removal must keep the link.** Drop it and the
   surviving item becomes a stray that the next sync recreates in Reminders.
   This is why `plan.ha_removes` holds whole links rather than uids, with
   `ha_remove_uids` for the call itself.
3. **`last_sync` only moves when a pair went through cleanly.** It also decides
   which completions count as recent, so advancing it past a failure would let
   the retry write the item off as stale.
4. **State keys are `json.dumps([list_name, entity_id])`.** Not `"a|b"` — list
   names can contain anything. Changing the key format orphans every user's
   state; bump `STATE_VERSION` if it ever has to change.

### reminders-cli's blind spots

`edit` writes only title and notes, and `edit` and `delete` only see **open**
reminders. Everything here follows:

5. **Due dates cannot be written to an existing reminder**, only set at
   creation. `due_from_ha` picks the behaviour and defaults to `revert` — push
   the reminder's own date back, so the sides never drift. `recreate` works but
   discards subtasks, recurrence, attachments, priority and the completion date.
6. **Editing a completed reminder means uncomplete → edit → complete**, which
   resets its completion date. `apply_reminder_update` does this dance; there is
   no other route.
7. **A completed reminder cannot be deleted**, so clearing completed items in
   Home Assistant cannot propagate. Those reminders get tombstoned instead of
   failing on every run. `tests/e2e.py`'s `reset()` reopens before deleting —
   the same trick.
8. **Tombstones live only while the item exists *and* is still completed.**
   Reopening a long-finished item deliberately brings it back into the sync.
9. **The title is a `.remaining` argument.** Options must come before it and it
   must sit behind `--`, or a title starting with a dash swallows the rest of
   the command line. Verified against the real parser; do not reorder.
10. **`--notes ""` clears notes; omitting `--notes` leaves them alone.** The CLI
    reads a missing value as "unchanged". This is why `r_update` signals intent
    by *key presence*: `"notes" in update` means change it, and the value `None`
    means clear it. Passing `None` through to `edit` would silently do nothing.
11. **A refused read looks exactly like an empty one.** Denied Reminders access
    does not always come back as an error: `show-lists` prints `[]` and exits
    0, and writes then fail with `No existing list sources were found`. So
    `Reminders.lists()` treats no lists as `RemindersAccessError` rather than
    as a Mac with nothing on it. Take the empty list at face value and auto
    mode reads every Home Assistant list as unpaired and sets about recreating
    all of them in Reminders, failing on each, every run, forever — which is
    precisely what shipped.

### The two sides' data models

12. **Home Assistant strips summaries on write**, so `normalize_title` strips
    too. Skip it and a padded title looks like a change on every single sync.
13. **Empty is None on both sides.** `""` and absent must not look different, or
    notes churn forever.
14. **A due datetime at exactly local midnight is all-day.** Reminders has a
    real all-day flag but the CLI does not expose it, and midnight is what an
    all-day reminder serializes to. A reminder deliberately set to 00:00
    round-trips as all-day; this is the only signal available.
15. **`due_date` and `due_datetime` are mutually exclusive** — sending both is a
    400 — and either set to `null` clears the date. `due_payload` is the only
    place that decides which one to send.
16. **Timed due dates go out with an explicit UTC offset**, so a mismatch
    between the Mac's timezone and Home Assistant's cannot shift them. The dev
    container's TZ is pinned to Europe/Warsaw in both `docker-compose.yml` and
    `dev/ha-config/configuration.yaml` for the same reason.
17. **`todo.add_item` returns no uid.** The only way to learn one is to re-read
    the list and look for uids that were not there before, matching on title —
    see `create_ha_items`. It also cannot set a status, so an item arriving
    already completed needs a follow-up `update_item`.
18. **Reminders keeps completed items forever.** Seeding a list links only open
    items; already-completed ones are tombstoned. Afterwards an item that turns
    up completed is imported only if `completed_at` is newer than `last_sync`.
    Remove this and the first sync dumps years of history into Home Assistant.
19. **Python 3.9 is the target.** `from __future__ import annotations` covers
    the syntax, but `datetime.fromisoformat` cannot read a trailing `Z` — which
    is exactly what reminders-cli emits — so `parse_iso` exists. No `match`, no
    3.10+ stdlib.

### Installed-in-the-wild concerns

20. **The shebang is `#!/usr/bin/python3`, not `env python3`.** With pyenv on
    PATH, `env python3` picks a shim, and `launchd_python()` exists so the plist
    names the absolute system interpreter regardless. A shim in the plist needs a
    PATH launchd does not provide, and the agent would fail silently every run.
    The code stays 3.9-compatible so this is always a safe pin.
21. **`setup` runs before a config exists**, so `main` dispatches it (and
    `uninstall`) before `load_config`. It must never require one.
22. **Prompts read `/dev/tty`, not stdin.** The installer can arrive down a pipe.
    `ask` opens the tty directly and `getpass` already does.
23. **`SCRIPT_PATH` is `abspath`, never `realpath`.** Installed by Homebrew it
    must stay `/opt/homebrew/bin/reminders-ha-sync` — the symlink `brew upgrade`
    repoints. Resolve it and the LaunchAgent gets a Cellar path that the next
    upgrade takes away.
24. **GitHub answers 200 with an HTML page for a tag that does not exist.** The
    status code proves nothing, so `dev/formula.py` checks the gzip magic, opens
    the tarball, and compares the `VERSION` inside it against the tag. Without
    that it would happily publish a checksum of an error page, and the formula
    would fail for every user.
25. **Homebrew ships `reminders` linker-signed, and TCC cannot hold a grant
    against it.** `codesign --verify --strict` says "code object is not signed
    at all", so `SecStaticCodeCheckValidity` fails — and TCC stores every grant
    together with a code requirement, which unvalidatable code can never
    satisfy. The prompt reappears on every run, the answer never sticks, and in
    between Reminders reads back empty. A terminal is immune because there the
    responsible process is the signed terminal app, holding the grant instead;
    that asymmetry is the whole reason this took an afternoon to find. The cure
    is `codesign --force --sign - <binary>` and one more Allow, and it must be
    redone after every `brew upgrade reminders-cli`. `signature_check` is in
    `doctor` so the next occurrence costs one command.
26. **The LaunchAgent's `StandardErrorPath` is the log file**, so a
    `StreamHandler` on stderr writes every line into it a second time.
    `setup_logging` compares the inode of fd 2 against the log path and skips
    the stream handler when they match. Two writers also break rotation: the
    handler renames the file and launchd keeps writing to the old descriptor.
27. **Renaming `LAUNCH_LABEL` orphans the agent already installed.** The old
    plist stays loaded and two syncs race over one state file, so
    `LEGACY_LAUNCH_LABELS` lists every label ever used, `install` and
    `uninstall` boot them out, and `doctor` reports a leftover. Add to that
    tuple, never edit it.

### Pairing

28. **Names are matched with `casefold`, not `lower`.** The lists are named in
    Russian; `lower` is not the right fold for non-ASCII.
29. **Home Assistant transliterates a list name into its internal key**, so
    `Тест` and `Test` both want `test` and the second cannot be created. This is
    reported as a problem, never guessed around. Two Home Assistant lists
    sharing a name are likewise reported, not resolved.
30. **An excluded list must not come back through the reverse direction.**
    `resolve_pairs` skips any Home Assistant name already present in Reminders,
    excluded or not, before considering whether to create a Reminders list.
31. **Creating a Home Assistant list needs an admin token**, because to-do
    entities come from config entries. `create_local_todo` walks the same config
    flow the UI walks, then finds the entity by friendly name — the entity id is
    a transliterated slug and must not be guessed.
32. **Title adoption is what makes losing `state.json` survivable.** Unlinked
    items with the same title are paired rather than duplicated, then merged by
    the ordinary rules with an empty `last`.
33. **`max_deletes_per_run` is a bad-read guard.** A transient failure should
    not be able to empty both sides; exceeding it fails the pair and asks for
    `--force`.

### Batteries

macOS splits the battery picture across three commands and none of them is
complete. Everything here is downstream of that.

34. **A reading that stops arriving must be blanked, not left behind.** An
    AirPods case is only in the Bluetooth data while the lid is open, so a
    sensor registered yesterday can have no reading today — and would sit at
    62% forever. `stale_payloads` sends `unavailable` for every registered
    sensor missing from this run's readings, which is why the store keeps
    `sensors` as `unique_id -> kind`: by then there is no `Reading` left to ask
    what kind it was.
35. **A disconnected accessory reports `unavailable`, never its last value.**
    macOS keeps stale levels indefinitely. `BatteryDevice.to_publish` blanks
    them, and `available` is what says so — it is not derived from the numbers,
    because the numbers look perfectly current.
36. **A device never seen connected is not registered at all.** Otherwise every
    accessory ever paired with this Mac becomes a card of dead sensors. It gets
    picked up the first run it is actually there.
37. **Accessory charging is joined on the percentage, and may be unknowable.**
    `system_profiler` names devices and gives levels but not charging; `pmset -g
    accps` gives charging but no names; `ioreg` has no `BatteryPercent` for
    these at all. Nothing has both. So `parse_accessory_charging` maps
    percentage to charging, a repeated percentage maps to None, and a cell
    pmset omits entirely — the right earbud, routinely — stays `unavailable`.
    Do not replace this with a guess; the levels are exact and the flag is not.
38. **`InternalBattery` must stay out of that map.** It is on the same list and
    it is the Mac, whose charging state would then be attributed to any
    accessory sitting on the same percentage.
39. **`CurrentCapacity` is a percentage on Apple silicon and mAh on Intel.**
    `MaxCapacity` is pinned at 100 on the former, so dividing whenever it is not
    100 covers both. Taking it at face value on an Intel Mac publishes 2500%.
    Health is `NominalChargeCapacity / DesignCapacity` — the figure System
    Settings shows; `AppleRawMaxCapacity` is a percentage point or two lower and
    is only the fallback.
40. **Batteries get their own LaunchAgent.** The Reminders grant breaks on its
    own schedule (rule 25) and there is no reason for that to take the sensors
    down too. `BATTERY_LAUNCH_LABEL` is subject to rule 27 like any other label.
41. **A missing `features` means reminders on, batteries off.** Every config
    written before this existed says nothing, and an upgrade that started
    registering devices in someone's Home Assistant unasked would be a nasty
    surprise. `setup` offers both only for a config that does not exist yet.
42. **Webhook ids are credentials.** Anyone holding one can write states into
    that Home Assistant, so `batteries.json` is written `0600`. It is a separate
    file from `state.json` deliberately: losing a registration costs a
    re-register, and it must never be able to cost anyone their to-do links.
43. **`not_registered` is answered per sensor, not by failing the call.** A
    deleted entity or a restored backup comes back that way inside a 200, and
    `publish_device` re-registers and retries in the same run. A whole
    registration disappearing is `MobileAppGone` (410, or 404 on some versions)
    and gets the same treatment one level up.
44. **iPhone and Apple Watch cannot be read from a Mac.** They report a battery
    over Bluetooth only while connected to it, which they are not — the Batteries
    widget does not show them either. The companion app on the phone is the
    answer, and the collector is generic, so a phone that *is* connected appears
    with no code change. Do not add a special case for it.

## Testing this thing safely

`tests/e2e.py` **empties the list it points at**, on both sides, before each
scenario.

**`dev/config.json` must stay explicit.** `dev/bootstrap.py` writes an explicit
`lists` array on purpose. Switch it to `"auto"` and `make e2e` pairs up every
real list on the machine and pushes them into the dev container. The three
auto-mode scenarios build their own config with an `exclude` computed as
"everything that is not an RHS list" — computed, never hardcoded.

The auto-mode creation scenario exercises the real config-flow API by deleting
the `RHS Покупки` config entry and letting the sync put it back. Fully
reversible, and it leaves nothing in Reminders — which matters because
**reminders-cli cannot delete a list**, so anything created there has to be
removed by hand in Reminders.app.

The dev container is on **:8124**, not 8123, so it can run beside a real
instance.

Publishing batteries against it is safe and leaves two things behind: a
`mobile_app` config entry per device, and a `device_tracker` entity per
registration that the integration creates on its own and that stays `unknown`
forever. Neither affects `make e2e`, which only touches to-do entities. Delete
the config entries in the UI if they are in the way — the next publish will
notice and register again, which is rule 43 doing its job.

## Permissions, and paths not taken

The Reminders TCC grant attaches to the *responsible* process, which for a
non-bundled CLI binary is whatever launched it. A grant made in Terminal.app
does not cover a run under an IDE, and vice versa — expect this to bite while
developing, and expect `doctor` to be the fastest way to see it. The plist sets
`LimitLoadToSessionType: Aqua` so the prompt can appear at all.

Under launchd there is no responsible app, so attribution falls to the
`reminders` binary itself — and as rule 25 says, a linker-signed binary cannot
hold a grant at all. **This is the difference between a terminal and the
LaunchAgent, and it is invisible from inside the sync**: both sides report
success and Reminders simply reads back empty. The two commands worth reaching
for first, in that order:

```bash
codesign --verify --strict /opt/homebrew/bin/reminders   # silence means fine
/usr/bin/log show --last 10m --predicate 'process == "tccd"' --info \
  --style compact | grep -iE "reminders-cli|Failed to match"
```

A working grant logs `matchesCodeRequirement … status: 0` and
`Auth Right: Allowed (User Consent)`. A broken one logs `status: -67050` and
`Failed to match existing code requirement` — every run, right after the user
clicks Allow. Note `log` is a zsh builtin; without the absolute path the query
silently does nothing.

A durable fix would be to wrap the agent in a signed `.app` with a stable
bundle id and point the plist at that, which survives `brew upgrade`. Not done:
it trades the one-line re-sign for a build step, an Info.plist and a
`NSRemindersUsageDescription`, and `doctor` now catches the breakage anyway.

**EventKit via PyObjC and AppleScript were both evaluated and rejected.** PyObjC
ships with neither the system Python nor a pyenv build, so it means a venv and a
permission granted to the interpreter. `osascript` can write due dates with no
dependency, but needs a second permission (Automation → Reminders) that is
unreliable under launchd. Both trade a rare limitation for a worse install. If
this is ever revisited, `Reminders` is the only class that changes — seven
methods, and the merge engine knows nothing about it.


## Calendar publishing

An optional third feature publishes all EventKit calendars one way to HA.
`native/CalendarExport.swift` is compiled into a signed app bundle by
`make calendar-helper`; Python remains stdlib-only. The receiver lives in
`custom_components/apple_calendar_sync` and must be installed and added in HA.
Each calendar has a real read-only calendar entity; identifiers include the Mac
hardware UUID and native calendar id, never just titles. EventKit expands
recurrences within a bounded window (default 365 past / 730 future days).

Calendar publishing has a separate LaunchAgent and no Reminders state. Old
configs leave it off. Empty calendar reads must fail without publishing. HA
validates the entire snapshot and persists it before replacing live data. Removed
calendars and sources silent for 30 minutes become unavailable; cached events stay.
`calendar --dry-run` reads personal calendars and can request TCC access, but sends
nothing. Use synthetic snapshots against dev HA on :8124 for receiver testing;
never publish personal calendar data into the dev instance without explicit intent.
