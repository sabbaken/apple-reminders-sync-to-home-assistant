# apple-reminders-sync-to-home-assistant

Two-way sync between macOS Reminders and Home Assistant to-do lists.

One stdlib-only Python file that runs under the `/usr/bin/python3` already on
your Mac, driven by launchd every few minutes. The Reminders side goes through
[keith/reminders-cli](https://github.com/keith/reminders-cli); Home Assistant is
reached over its REST API with a long-lived token.

```
reminders_ha_sync.py       the whole thing
config.example.json        the whole config: a URL and a token
config.full-example.json   every option, with its default
docker-compose.yml + dev/  throwaway HA on :8124 for testing
tests/test_merge.py        merge-engine and pairing unit tests (nothing live)
tests/e2e.py               real round trips against the dev HA
```

## Setup

There are two things to do by hand: grant a permission, and paste a token.
Everything else — pairing the lists up, creating the ones that are missing —
happens on the first run.

**1. Install the CLI and grant it access.**

```sh
brew install keith/formulae/reminders-cli
reminders show-lists          # run this in Terminal.app and click Allow
```

The permission dialog only appears for a process running in your GUI session, so
this first run has to happen in a terminal you opened yourself. Until it
succeeds every command prints `error: you need to grant reminders access`.

**2. Paste a token.**

Home Assistant → your profile → Security → Long-lived access tokens → Create. It
has to belong to an **administrator** account, because creating a to-do list
means creating a config entry.

```sh
mkdir -p ~/.config/reminders-ha-sync
cp config.example.json ~/.config/reminders-ha-sync/config.json
chmod 600 ~/.config/reminders-ha-sync/config.json
$EDITOR ~/.config/reminders-ha-sync/config.json    # url + token, that is all
```

**3. Look at what it is about to do.**

```sh
./reminders_ha_sync.py doctor
```

`doctor` prints the pairing table without changing anything:

```
lists       matched by name
  Список Покупок               -                    would create in Home Assistant
  Личные Покупки               -                    would create in Home Assistant
  Movies                       -                    would create in Home Assistant
  Покупки                      todo.pokupki         paired
  Shopping List                todo.shopping_list   would create in Reminders
```

Anything you would rather it left alone goes in `exclude` — by Reminders list
name, by Home Assistant name, or by entity id:

```json
{ "exclude": ["Movies", "todo.shopping_list"] }
```

**4. Put it in the background.**

```sh
./reminders_ha_sync.py install --interval 600
```

`install` runs the first sync in the foreground and prints what it did, then
writes `~/Library/LaunchAgents/com.github.keith-reminders-ha-sync.plist` and
loads it: a sync every 10 minutes, plus one at login. If that first sync
reports problems, nothing is loaded — the initial run is the one worth watching,
since it creates the lists and seeds them. `--skip-initial-sync` opts out.

Logs go to `~/Library/Logs/reminders-ha-sync.log` (rotated at 2 MB).
`uninstall` reverses it. `run --interval 600` is the foreground alternative if
you would rather not use launchd.

If the LaunchAgent's syncs fail with a permission error while the same command
works from your terminal, macOS is treating the launchd job as a separate
requester. Running `reminders show-lists` in Terminal once more, and keeping the
binary at its Homebrew path, is what clears it — the grant is keyed to that path.

## Config

Only `home_assistant.url` and `home_assistant.token` are required.

| Key | Default | Meaning |
| --- | --- | --- |
| `home_assistant.url` | — | Base URL, e.g. `http://homeassistant.local:8123` |
| `home_assistant.token` | — | Long-lived token. `RHS_HA_TOKEN` overrides it |
| `home_assistant.timeout` | `20` | Per-request timeout in seconds |
| `home_assistant.verify_tls` | `true` | Set false for a self-signed certificate |
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

**All-day vs timed.** A reminder due at exactly 00:00 is indistinguishable from
an all-day one in the CLI's output, so it round-trips as all-day.

**Timezones.** Due datetimes are sent with an explicit UTC offset, so the Mac
and Home Assistant disagreeing about timezone cannot shift them.

## Development

The dev instance runs on **:8124** so it can sit next to a real one on :8123.

```sh
make ha-up        # start it, onboard it, create the test lists, write dev/config.json
make test         # merge-engine unit tests -- no HA, no Reminders access
make e2e          # real round trips: dev HA <-> the "RHS Test" Reminders list
make dev-dry-run  # what a sync against the dev instance would change
make ha-reset     # wipe it and start over
```

`make ha-up` logs in as `dev` / `devdevdev` at http://127.0.0.1:8124. Onboarding
gives a refresh token, kept in `dev/secrets.json`; every make target re-mints a
30-minute access token from it into `dev/config.json`, which is why there is no
long-lived token to manage here.

`tests/e2e.py` **empties the Reminders list it is pointed at**, on both sides,
before each scenario. It only ever touches the lists named in `dev/config.json`
(`RHS Test`, `RHS Покупки`) and refuses to run if they do not exist unless you
pass `--create-lists`.
