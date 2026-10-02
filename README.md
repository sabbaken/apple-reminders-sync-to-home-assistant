# apple-reminders-sync-to-home-assistant

Two-way sync between the Reminders app on your Mac and Home Assistant's to-do
lists. Tick something off on your iPhone and it is ticked off in Home Assistant;
add something from an automation and it turns up in Reminders.

One small Python script that runs on the Mac every few minutes in the
background. Your lists are paired up by name and the missing ones created on
both sides, so there is nothing to configure beyond a token.

It can also publish **battery levels** — this Mac, and every Bluetooth device
around it: AirPods cell by cell, mouse, keyboard, trackpad — as real Home
Assistant sensors, each with a charging flag of its own. It can also publish
all macOS calendars through one Home Assistant integration (see [Calendars](#calendars)).
The features run independently, and `setup` asks which you want.

## Install

You need [Homebrew](https://brew.sh) and a Home Assistant token.

**First, get the token.** This is the only thing you have to prepare, and it
lives in the Home Assistant web interface:

1. Click your name, bottom left of the sidebar.
2. Open the **Security** tab.
3. Scroll to **Long-lived access tokens** → **Create token**, give it any name.
4. Copy it. Home Assistant shows it once.

Use an **administrator** account. Creating a to-do list means creating a config
entry, which only an administrator may do. A non-admin token still syncs lists
that already exist.

**Then, on the Mac whose Reminders you want to sync:**

```sh
brew install sabbaken/tap/reminders-ha-sync
reminders-ha-sync setup
```

The first command brings `reminders-cli` with it — that is the part that talks
to the Reminders app. The second one walks through the rest:

1. Asks which features you want: Reminders, batteries and optionally calendars.
2. Asks macOS for permission to read your Reminders — **click Allow**.
3. Asks for your Home Assistant address and that token, and checks both work
   before writing anything.
4. Shows you which lists it is about to sync and lets you drop any of them.
5. Syncs once while you watch, then keeps going in the background.

Nothing needs `sudo`, and nothing is written outside your home directory.

<details>
<summary>What it looks like</summary>

```
$ reminders-ha-sync setup
Setting up reminders-ha-sync.
Config will be written to /Users/you/.config/reminders-ha-sync/config.json

What should run?
  up/down move, space toggles, enter accepts

> [x] Reminders sync    two-way sync between Reminders and to-do lists
  [x] Battery sensors   this Mac, AirPods, and other Bluetooth devices

Reminders: 3 lists (Покупки, Личное, Movies)

Home Assistant address [http://homeassistant.local:8123]:
Profile -> Security -> Long-lived access tokens -> Create Token.
Use an administrator account, so lists can be created for you.
(the paste stays hidden)
Token:
Home Assistant: reachable at http://homeassistant.local:8123

Here is what a sync would do:

   1. Покупки                    todo.pokupki               paired
   2. Личное                     -                          would create in Home Assistant
   3. Movies                     -                          would create in Home Assistant
   4. Shopping List              todo.shopping_list         would create in Reminders

Numbers to leave out (comma-separated, Enter to sync all): 3,4
Excluded: Movies, Shopping List

Battery sensors will be published for:
  MacBook Pro                    8 sensors
  Кирилл’s AirPods Pro 3         6 sensors
  Magic Mouse                    not connected -- will appear once it is

Sync every how many seconds [600]:
Publish batteries every how many seconds [300]:
Start now, and sync every 600s and publish batteries every 300s (Y/n):

running the first sync...
HA +12 ~0 -0 | Reminders +0 ~0 -0 | failures 0

installed /Users/you/Library/LaunchAgents/com.github.sabbaken.reminders-ha-sync.plist
  syncing every 600 seconds
installed /Users/you/Library/LaunchAgents/com.github.sabbaken.reminders-ha-sync-battery.plist
  publishing batteries every 300 seconds
log: /Users/you/Library/Logs/reminders-ha-sync.log

Done. Check on it any time with:
    reminders-ha-sync doctor
```

</details>

## Updating

```sh
brew update && brew upgrade reminders-ha-sync
```

Your config and sync state are left alone, and the background syncs pick the new
version up on their own: each one is a fresh process launched from
`/opt/homebrew/bin/reminders-ha-sync`, a symlink `brew upgrade` repoints. There
is nothing to restart.

## Everyday use

```sh
reminders-ha-sync doctor             # is everything healthy, and what pairs with what
reminders-ha-sync sync --dry-run     # what would change, without changing it
reminders-ha-sync sync               # sync now
reminders-ha-sync battery --dry-run  # what the batteries read right now
reminders-ha-sync battery            # publish them now
reminders-ha-sync setup              # change the address, token, features or lists
reminders-ha-sync uninstall          # stop everything
```

Syncs happen every 10 minutes and batteries every 5, plus one of each at login.
The log is `~/Library/Logs/reminders-ha-sync.log`, rotated at 2 MB. To sync more
or less often, `reminders-ha-sync install --interval 300`; the battery interval
lives in the config, as `battery.interval`.

The two run as separate LaunchAgents on separate schedules, which is deliberate:
the Reminders permission breaks on its own every so often (see below), and there
is no reason for that to stop the battery sensors as well.

To remove it completely: `reminders-ha-sync uninstall`, then
`brew uninstall reminders-ha-sync` and delete `~/.config/reminders-ha-sync/`.
Nothing is deleted from either Reminders or Home Assistant.

If the background syncs fail with a permission error while the same command
works when you type it, macOS is treating the LaunchAgent as a separate
requester. Run `reminders show-lists` in Terminal once more; the grant is keyed
to the binary's path, so keep it where Homebrew put it.

## Config

`~/.config/reminders-ha-sync/config.json`. `setup` writes it, and it is plain
JSON if you would rather edit it. Only the address and token are required.

| Key | Default | Meaning |
| --- | --- | --- |
| `home_assistant.url` | — | Base URL, e.g. `http://homeassistant.local:8123` |
| `home_assistant.token` | — | Long-lived token. `RHS_HA_TOKEN` overrides it |
| `home_assistant.timeout` | `20` | Per-request timeout in seconds |
| `home_assistant.verify_tls` | `true` | Set false for a self-signed certificate |
| `features.reminders` | `true` | Sync the to-do lists |
| `features.battery` | `false` | Publish battery sensors |
| `lists` | `"auto"` | `"auto"`, or explicit `[{"reminders": …, "ha": …}]` pairs |
| `exclude` | `[]` | Lists to leave alone, by either side's name or entity id |
| `create_missing` | `"both"` | `both`, `ha`, `reminders` or `none` |
| `reminders_source` | auto | Account for new Reminders lists; needed only if you have several |
| `conflict_winner` | `reminders` | `reminders`, `ha` or `manual` |
| `due_from_ha` | `revert` | What to do about due dates edited in HA |
| `match_by_title` | `true` | Adopt same-titled items instead of duplicating |
| `max_deletes_per_run` | `25` | Refuse runs that would delete more (see below) |
| `log_level` | `info` | `debug` for the individual API calls |
| `state_file` | `~/.local/state/reminders-ha-sync/state.json` | |
| `log_file` | `~/Library/Logs/reminders-ha-sync.log` | |
| `reminders_binary` | auto | Override the `reminders` path |
| `battery.interval` | `300` | Seconds between battery publishes |
| `battery.exclude` | `[]` | Devices to leave alone, by the name macOS shows |
| `battery.state_file` | `~/.local/state/reminders-ha-sync/batteries.json` | |

`config.full-example.json` has all of it with the defaults filled in.

### How lists get paired

In `auto` mode, names are matched ignoring case and surrounding whitespace —
using `casefold`, so it works for Cyrillic and not just ASCII. Whatever has no
counterpart is created: a Reminders list becomes a **Local To-do** list in Home
Assistant, and a Home Assistant list becomes a Reminders list.

Reminders lists you cannot write to (shared lists you only subscribe to) never
show up, so they are never synced.

Two cases stop and ask rather than guess:

- **Two Home Assistant lists with the same name.** Map that one explicitly.
- **A name collision after transliteration.** Home Assistant derives a list's
  internal key by transliterating its name, so `Тест` and `Test` both want
  `test` and the second one cannot be created. Rename one, or map it explicitly.

Set `lists` to an explicit array to turn all of this off and pair by hand; then
nothing is ever created and only the listed pairs are touched.

## How the sync decides things

State lives in `state.json`: for every linked pair it records the values both
sides agreed on at the end of the last run. That snapshot is what makes a
three-way merge possible — without it a missing item is indistinguishable from a
new one, and every run would resurrect whatever you deleted.

Per field (title, notes, due date, completion), each run compares both sides
against the snapshot:

- changed on one side only → written to the other
- changed on both sides to the same value → nothing to do
- changed on both sides differently → `conflict_winner` decides, and the loss is
  logged. `manual` writes nothing and reports it again next run.

**Seeding.** The first sync of a pair links open items and ignores items that
were already completed — Reminders keeps completed items forever, and importing
them would dump years of history into Home Assistant. Ignored items are recorded
so they stay ignored. Reopening one brings it back into the sync.

Afterwards, an item that turns up already completed is imported only if it was
completed since the previous run, which is what makes "created and ticked off
between two syncs" work.

**Adoption.** Unlinked items with the same title on both sides are paired rather
than duplicated, then merged by the normal rules. This is what makes losing
`state.json` a non-event instead of a duplicate storm. Turn it off with
`match_by_title: false`.

**Deletions** propagate both ways. A run that would delete more than
`max_deletes_per_run` items stops and asks for `--force` instead — a transient
read failure should not be able to empty both sides.

## What the CLI cannot do, and what happens instead

`reminders-cli`'s `edit` only writes titles and notes, and both `edit` and
`delete` only see *open* reminders. Everything below follows from that.

**Due dates only travel Reminders → Home Assistant.** They are applied when a
reminder is created from a Home Assistant item, but an existing reminder's due
date cannot be rewritten. `due_from_ha` picks the behaviour:

- `revert` (default) — push the reminder's own date back to Home Assistant, so
  the two never drift apart. Change due dates in Reminders.
- `ignore` — let them differ, log it once.
- `recreate` — delete the reminder and make a new one with the new date. It does
  work, and it throws away everything the CLI cannot copy: subtasks, recurrence,
  attachments, priority, flags, the completion date and the item's position.
  Off by default for that reason.

**Clearing completed items in Home Assistant does not delete them in
Reminders,** because `delete` cannot see them. Those reminders are dropped from
the sync instead of failing forever; reopening one picks it up again.

**Renaming a completed item** works, by reopening it, editing, and completing it
again — which resets its completion date. There is no other route.

**Priority, subtasks, recurrence, URLs, locations, images and flags are never
touched.** Home Assistant's to-do model has no counterpart for them, so the sync
reads and writes only title, notes, due date and completion.

**Deleting a list is manual.** The CLI cannot remove one, so a list created by
mistake has to go from Reminders.app. Adding it to `exclude` stops it syncing.

**All-day vs timed.** A reminder due at exactly 00:00 is indistinguishable from
an all-day one in the CLI's output, so it round-trips as all-day.

**Timezones.** Due datetimes are sent with an explicit UTC offset, so the Mac
and Home Assistant disagreeing about timezone cannot shift them.

### Why not EventKit directly

Talking to EventKit through PyObjC would close the due-date gap and make
completed items editable. It is not used because it costs more than it buys:
PyObjC ships with neither the system Python 3.9 nor a pyenv build, so it means a
virtualenv — and the Reminders permission would then be granted to the Python
interpreter itself rather than to one small binary, which is both broader and
less stable. `osascript` can write due dates without any dependency, but it needs
a *second* permission (Automation → Reminders) that is unreliable under launchd.
Both trade a rare limitation for a worse install.

If it ever becomes worth it, the `Reminders` class is the only thing that would
change: seven methods, and the merge engine knows nothing about it.

## Battery sensors

Switched on during `setup`, or by setting `features.battery` to `true`. Each
physical device becomes a device in Home Assistant of its own, so AirPods get
their own card rather than being strays on the Mac's.

For the Mac, from `ioreg`:

| Entity | |
| --- | --- |
| `sensor.<mac>_battery_level` | charge, `%` |
| `binary_sensor.<mac>_battery_charging` | |
| `binary_sensor.<mac>_ac_connected` | on mains, charging or not |
| `binary_sensor.<mac>_battery_full` | diagnostic |
| `sensor.<mac>_battery_health` | the "Maximum Capacity" figure, `%` |
| `sensor.<mac>_battery_cycles` | diagnostic |
| `sensor.<mac>_battery_temperature` | `°C`, diagnostic |
| `sensor.<mac>_battery_time_remaining` | minutes to empty or full, diagnostic |

For every Bluetooth device that reports a battery, a level and a charging flag
per cell — AirPods report `left`, `right` and `case` separately, a mouse just
one. The five diagnostic entities are tucked into the device's diagnostic
section rather than shown alongside the rest.

Sensors are created through the same REST API the official companion app uses,
so they are real entities: unique ids, renameable in the UI, and they survive a
restart of Home Assistant with their values intact. The webhook ids this hands
back live in `~/.local/state/reminders-ha-sync/batteries.json`, mode `600`,
because anyone holding one can write states into your Home Assistant. Deleting a
device in Home Assistant is honoured and then undone: the next run notices and
registers it again. Delete it for good by switching the feature off first.

Registering also creates a `device_tracker` entity per device, always `unknown`.
That comes with the integration and there is no way to decline it; hide it if it
bothers you.

**What it cannot do:**

**iPhone and Apple Watch are not available from a Mac.** They only report a
battery over Bluetooth while actually connected to it, which iPhones normally
are not — macOS's own Batteries widget does not show them either. Install the
[Home Assistant companion app](https://companion.home-assistant.io) on the
phone: it publishes its own battery, and the Watch's, and does it far better
than anything here could. Should a phone ever be connected over Bluetooth, this
picks it up with no changes.

**Charging is not always knowable for accessories.** macOS splits the
information in two: `system_profiler` names the devices and gives their levels,
`pmset` knows which cells are charging but names none of them, and nothing has
both. They are joined on the percentage, so a cell `pmset` does not list — the
right earbud, often — or two cells sitting on the same percentage come out
`unavailable` rather than guessed. The levels themselves are always exact.

**Disconnected devices report nothing rather than something stale.** macOS keeps
the last reading indefinitely, so a case that has been in a drawer for a month
still says 62%. Those go out as `unavailable`, and a device that has never been
seen connected is not registered at all — otherwise every accessory ever paired
with the Mac would become a card of dead sensors.

## Development

```
reminders_ha_sync.py           the whole thing
Formula/reminders-ha-sync.rb   the Homebrew formula, canonical copy
config.example.json            the minimum: a URL and a token
config.full-example.json       every option, with its default
docker-compose.yml + dev/      throwaway HA on :8124 for testing
tests/test_merge.py            merge-engine and pairing unit tests (nothing live)
tests/test_battery.py          battery parsing and publishing, over recorded output
tests/e2e.py                   real round trips against the dev HA
```

The dev instance runs on **:8124** so it can sit next to a real one on :8123.

```sh
make ha-up        # start it, onboard it, create the test lists, write dev/config.json
make test         # unit tests -- no HA, no Reminders access
make e2e          # real round trips: dev HA <-> the "RHS Test" Reminders list
make dev-dry-run  # what a sync against the dev instance would change
make ha-reset     # wipe it and start over
```

`make ha-up` logs in as `dev` / `devdevdev` at http://127.0.0.1:8124. Onboarding
gives a refresh token, kept in `dev/secrets.json`; every make target re-mints a
30-minute access token from it into `dev/config.json`, which is why there is no
long-lived token to manage here — and why `tests/e2e.py` must be run through
`make`, not on its own.

`tests/e2e.py` **empties the Reminders list it is pointed at**, on both sides,
before each scenario. It only ever touches the lists named in `dev/config.json`
(`RHS Test`, `RHS Покупки`) and refuses to run if they do not exist unless you
pass `--create-lists`.

### Publishing and releasing

`brew install sabbaken/tap/reminders-ha-sync` needs a tap, which is just a repo
named `homebrew-tap` with the formula in `Formula/`. One-time setup:

```sh
gh repo create sabbaken/homebrew-tap --public --clone
mkdir -p homebrew-tap/Formula
cp Formula/reminders-ha-sync.rb homebrew-tap/Formula/
# commit and push
```

Prefer not to keep a second repo? Tap this one directly — the formula is already
in `Formula/`, it just makes the install two commands for users:

```sh
brew tap sabbaken/tap https://github.com/sabbaken/apple-reminders-sync-to-home-assistant
brew install sabbaken/tap/reminders-ha-sync
```

The formula copies `LICENSE` into the install prefix, because the script is
installed as a single standalone file and would otherwise arrive without one.

Each release:

1. Bump `VERSION` in `reminders_ha_sync.py`, commit.
2. `git tag v0.2.0 && git push --tags`
3. `make formula TAG=v0.2.0` — prints the formula with the release's real
   `sha256`, and refuses if the tag is not pushed or its `VERSION` does not
   match.
4. Paste that over `Formula/reminders-ha-sync.rb` in the tap, commit, push.

Users then get it with `brew upgrade`.

## Licence

[GNU AGPL v3.0 only](LICENSE). If you run a modified version somewhere others
interact with over a network, the AGPL asks you to offer them its source.

## Calendars

The optional calendar feature publishes **every EventKit calendar on this Mac**
into Home Assistant through one **Apple Calendar Sync** integration. This includes
local, iCloud, Google, Exchange and subscribed calendars that macOS exposes to
EventKit. Add accounts in macOS Calendar first; no provider passwords or OAuth
setup are needed in this app. The Mac must remain awake and connected for updates.

Each calendar gets its own read-only `calendar` entity, usable in the Calendar
panel and calendar automations. Names include the account name; calendars with
identical names remain separate because their native identifiers are used.
Events retain their title, notes, location, timed start/end and all-day dates.
EventKit expands recurring events, including modified occurrences, into the
exported window. Deleted events disappear with the next successful snapshot.
This is **one-way macOS → Home Assistant**; editing events in HA is not supported.
Attendees, alarms, attachments, availability and conference metadata are not
exported as separate fields.

### Set up

1. Copy `custom_components/apple_calendar_sync` from this repository into
   `<HA config>/custom_components/apple_calendar_sync` on your Home Assistant
   machine. A Homebrew installation also includes this folder under
   `$(brew --prefix reminders-ha-sync)/share/reminders-ha-sync/custom_components`.
2. Restart Home Assistant. In **Settings → Devices & services → Add integration**,
   add **Apple Calendar Sync** once. That single entry receives all calendars,
   including new calendars discovered on later runs and calendars from other Macs.
3. When running from this checkout, run `make calendar-helper` (requires Xcode
   Command Line Tools). Homebrew builds and installs the helper automatically.
   It is a signed native app bundle using Apple's EventKit; the Python script
   still uses only the standard library and `/usr/bin/python3`.
4. Enable `"calendar": true` alongside your existing feature selections:

   ```json
   "features": {"reminders": true, "battery": false, "calendar": true},
   "calendar": {"interval": 300, "past_days": 365, "future_days": 730}
   ```

5. Run `reminders-ha-sync calendar --dry-run` (or
   `./reminders_ha_sync.py calendar --dry-run` from the checkout). Allow calendar
   access when macOS asks. This reads data without sending anything to HA.
6. Run `reminders-ha-sync calendar` to publish once, then
   `reminders-ha-sync install --skip-initial-sync` to install the independent
   calendar LaunchAgent alongside any other enabled agents.

Alternatively, select Calendar sync in `setup`, after installing the HA receiver.
Use an administrator token for calendar publishing. The receiver accepts the
same authenticated Home Assistant token already used for Reminders and batteries;
there is no unauthenticated calendar feed or additional listening server on Mac.

### Range and failures

By default each snapshot covers one year in the past and two years in the future.
Change `calendar.past_days` and `calendar.future_days` to choose the range; their
sum cannot exceed 1460 days (EventKit limits queries to four years). Older history
and occurrences beyond the window are not available in HA. Repeating calendars
cannot be exported as an infinite set of events.

The receiver stores the last successful snapshot across HA restarts. Failed reads,
denied access, invalid exports or failed storage writes do not replace it. An empty
calendar list is rejected to protect against permission failures. A calendar
removed from macOS becomes unavailable in HA; its entity stays in the registry
so restoring it retains its identity. After 30 minutes without a successful
snapshot, entities become unavailable, retaining their cached events. Keep the
publish interval below 30 minutes. The published range moves forward every run.

`calendar.binary` can point at `CalendarExport` **inside its signed app bundle**.
`calendar.source_id` overrides the Mac's hardware UUID if needed; keep this value
stable, since changing it creates a new set of entities. `doctor` checks the
helper signature, calendar access, receiver registration and LaunchAgent file.
A helper rebuild or upgrade may require granting calendar access again; use
**System Settings → Privacy & Security → Calendars** if access fails.

Calendar publishing is disabled for existing configs until explicitly enabled.
The `calendar` command runs when requested regardless of that flag, like `battery`.
It shares neither the Reminders state nor the battery registration file.

For receiver development, `make ha-up` copies the integration into the throwaway
Home Assistant on port 8124. `tests/test_calendar.py` uses synthetic events and
never requests access to personal calendars. `make calendar-e2e` verifies the
receiver against that dev instance using synthetic calendars only.
