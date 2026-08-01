# apple-reminders-sync-to-home-assistant

Two-way sync between the Reminders app on your Mac and Home Assistant's to-do
lists. Tick something off on your iPhone and it is ticked off in Home Assistant;
add something from an automation and it turns up in Reminders.

One small Python script that runs on the Mac every few minutes in the
background. Your lists are paired up by name and the missing ones created on
both sides, so there is nothing to configure beyond a token.

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

1. Asks macOS for permission to read your Reminders — **click Allow**.
2. Asks for your Home Assistant address and that token, and checks both work
   before writing anything.
3. Shows you which lists it is about to sync and lets you drop any of them.
4. Syncs once while you watch, then keeps syncing every 10 minutes.

Nothing needs `sudo`, and nothing is written outside your home directory.

<details>
<summary>What it looks like</summary>

```
$ reminders-ha-sync setup
Setting up the Reminders <-> Home Assistant sync.
Config will be written to /Users/you/.config/reminders-ha-sync/config.json

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

Sync every how many seconds [600]:
Sync now and start syncing every 600s (Y/n):

running the first sync...
HA +12 ~0 -0 | Reminders +0 ~0 -0 | failures 0

installed /Users/you/Library/LaunchAgents/com.github.keith-reminders-ha-sync.plist
syncing every 600 seconds; log: /Users/you/Library/Logs/reminders-ha-sync.log

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
reminders-ha-sync doctor          # is everything healthy, and what pairs with what
reminders-ha-sync sync --dry-run  # what would change, without changing it
reminders-ha-sync sync            # sync now
reminders-ha-sync setup           # change the address, token or list selection
reminders-ha-sync uninstall       # stop syncing
```

Syncs happen every 10 minutes, plus one at login. The log is
`~/Library/Logs/reminders-ha-sync.log`, rotated at 2 MB. To sync more or less
often, `reminders-ha-sync install --interval 300`.

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

## Development

```
reminders_ha_sync.py           the whole thing
Formula/reminders-ha-sync.rb   the Homebrew formula, canonical copy
config.example.json            the minimum: a URL and a token
config.full-example.json       every option, with its default
docker-compose.yml + dev/      throwaway HA on :8124 for testing
tests/test_merge.py            merge-engine and pairing unit tests (nothing live)
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
