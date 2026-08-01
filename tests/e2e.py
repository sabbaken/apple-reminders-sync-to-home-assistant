#!/usr/bin/env python3
"""End-to-end test against the dev Home Assistant and the real Reminders app.

Unlike tests/test_merge.py this touches actual data, so it only ever works
inside the list named in dev/config.json. It empties that list on both sides
before each scenario -- never point it at a list you care about.

    make e2e            # bootstraps the dev HA first
    /usr/bin/python3 tests/e2e.py --create-lists

Requires Reminders access to have been granted; run `reminders show-lists` from
Terminal once if it has not.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reminders_ha_sync as rhs  # noqa: E402

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV_CONFIG = os.path.join(PROJECT_DIR, "dev", "config.json")

FAR_FUTURE_DAY = "2031-03-04"
FAR_FUTURE_TIME = "2031-03-04T14:30:00"


class Harness:
    def __init__(self, raw: dict, state_path: str):
        raw = dict(raw, state_file=state_path, log_level="error")
        self.config = rhs.Config(raw, DEV_CONFIG)
        self.state_path = state_path
        self.reminders = rhs.Reminders(self.config.reminders_binary)
        self.ha = rhs.HomeAssistant(
            self.config.ha_url,
            self.config.ha_token,
            self.config.ha_timeout,
            self.config.ha_verify_tls,
        )
        # dev/config.json maps the RHS lists explicitly on purpose: auto mode
        # would drag every real list on this Mac into the dev instance.
        self.list_name, self.entity_id = self.config.explicit_pairs[0]

    # -- plumbing ---------------------------------------------------------- #

    def sync(self, **kwargs) -> int:
        return rhs.sync_once(self.config, **kwargs)

    def r_items(self):
        return self.reminders.items(self.list_name)

    def h_items(self):
        return self.ha.items(self.entity_id)

    def r_by_title(self, title: str):
        return [i for i in self.r_items() if i.title == title]

    def h_by_title(self, title: str):
        return [i for i in self.h_items() if i.title == title]

    def reset(self) -> None:
        """Empty both sides and forget all sync state."""
        for todo in self.h_items():
            self.ha.remove_items(self.entity_id, [todo.id])
        for reminder in self.r_items():
            # `delete` only sees open reminders, so completed ones have to be
            # reopened first. Same trick the sync uses for editing them.
            if reminder.completed:
                self.reminders.set_completed(self.list_name, reminder.id, False)
            if not self.reminders.delete(self.list_name, reminder.id):
                raise AssertionError("could not clean up reminder %r" % reminder.title)
        if os.path.exists(self.state_path):
            os.remove(self.state_path)

    def state(self) -> dict:
        with open(self.state_path, encoding="utf-8") as fh:
            return json.load(fh)

    def pair_state(self) -> dict:
        key = rhs.pair_key(self.list_name, self.entity_id)
        return self.state()["pairs"][key]

    def links(self) -> list:
        return self.pair_state()["links"]

    def stable_state(self) -> str:
        """Everything in the pair's state except the timestamp, which is
        expected to move on every successful run."""
        state = dict(self.pair_state())
        state.pop("last_sync", None)
        return json.dumps(state, sort_keys=True)


    # -- auto-mode support ------------------------------------------------- #

    def config_entries(self, domain: str) -> list:
        return self.ha._request(
            "GET", "/api/config/config_entries/entry?domain=" + domain
        )

    def delete_config_entry(self, entry_id: str) -> None:
        self.ha._request("DELETE", "/api/config/config_entries/entry/" + entry_id)

    def auto_config(self) -> rhs.Config:
        """An auto-mode config scoped to the RHS lists only.

        Auto mode would otherwise pair up every real list on this Mac and push
        it into the dev instance, so everything that is not a test list is
        excluded by name -- computed, not hardcoded, so it holds whatever else
        happens to be on the machine.
        """
        with open(DEV_CONFIG, encoding="utf-8") as fh:
            raw = json.load(fh)
        keep = {
            rhs.normalize_name(name)
            for name, _ in rhs.Config(raw, DEV_CONFIG).explicit_pairs
        }
        exclude = [n for n in self.reminders.lists() if rhs.normalize_name(n) not in keep]
        exclude += [
            entity_id
            for entity_id, friendly in self.ha.todo_entities().items()
            if rhs.normalize_name(friendly) not in keep
        ]
        return rhs.Config(
            dict(raw, lists="auto", exclude=exclude, state_file=self.state_path),
            DEV_CONFIG,
        )


class Failure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def equal(actual, expected, what: str) -> None:
    if actual != expected:
        raise Failure("%s: expected %r, got %r" % (what, expected, actual))


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #


def scenario_create_reminders_to_ha(h: Harness) -> None:
    h.reminders.add(
        h.list_name,
        rhs.Item(title="to ha", notes="a note", due=FAR_FUTURE_TIME),
    )
    h.sync()
    found = h.h_by_title("to ha")
    equal(len(found), 1, "item count in Home Assistant")
    equal(found[0].notes, "a note", "notes carried across")
    equal(found[0].due, FAR_FUTURE_TIME, "timed due date carried across")
    equal(len(h.links()), 1, "link recorded")


def scenario_create_ha_to_reminders(h: Harness) -> None:
    h.ha.add_item(
        h.entity_id,
        rhs.Item(title="to reminders", notes="ha note", due=FAR_FUTURE_DAY),
    )
    h.sync()
    found = h.r_by_title("to reminders")
    equal(len(found), 1, "reminder count")
    equal(found[0].notes, "ha note", "description became notes")
    equal(found[0].due, FAR_FUTURE_DAY, "all-day due date carried across")


def scenario_all_day_survives_round_trip(h: Harness) -> None:
    # An all-day reminder serializes as local midnight; if that were read back
    # as a timed date the two sides would disagree forever.
    h.reminders.add(h.list_name, rhs.Item(title="all day", due=FAR_FUTURE_DAY))
    h.sync()
    equal(h.h_by_title("all day")[0].due, FAR_FUTURE_DAY, "due in Home Assistant")
    h.sync()
    equal(h.r_by_title("all day")[0].due, FAR_FUTURE_DAY, "due still in Reminders")
    check(not any(w for w in h.links() if w["last"]["due"] != FAR_FUTURE_DAY), "snapshot")


def scenario_complete_in_reminders(h: Harness) -> None:
    rid = h.reminders.add(h.list_name, rhs.Item(title="finish me"))
    h.sync()
    h.reminders.set_completed(h.list_name, rid, True)
    h.sync()
    equal(h.h_by_title("finish me")[0].completed, True, "completed in Home Assistant")


def scenario_uncomplete_in_ha(h: Harness) -> None:
    rid = h.reminders.add(h.list_name, rhs.Item(title="reopen me"))
    h.sync()
    h.reminders.set_completed(h.list_name, rid, True)
    h.sync()
    uid = h.h_by_title("reopen me")[0].id
    h.ha.update_item(h.entity_id, uid, {"status": "needs_action"})
    h.sync()
    equal(h.r_by_title("reopen me")[0].completed, False, "reopened in Reminders")


def scenario_rename_in_reminders(h: Harness) -> None:
    rid = h.reminders.add(h.list_name, rhs.Item(title="before"))
    h.sync()
    h.reminders.edit(h.list_name, rid, title="after")
    h.sync()
    equal(len(h.h_by_title("after")), 1, "renamed in Home Assistant")
    equal(len(h.h_by_title("before")), 0, "old title gone")


def scenario_rename_in_ha(h: Harness) -> None:
    h.reminders.add(h.list_name, rhs.Item(title="ha before"))
    h.sync()
    uid = h.h_by_title("ha before")[0].id
    h.ha.update_item(h.entity_id, uid, {"rename": "ha after"})
    h.sync()
    equal(len(h.r_by_title("ha after")), 1, "renamed in Reminders")


def scenario_rename_a_completed_item(h: Harness) -> None:
    # `reminders edit` refuses completed items, so the sync has to reopen,
    # edit and re-complete.
    h.reminders.add(h.list_name, rhs.Item(title="done then renamed"))
    h.sync()
    uid = h.h_by_title("done then renamed")[0].id
    h.ha.update_item(h.entity_id, uid, {"status": "completed"})
    h.sync()
    h.ha.update_item(h.entity_id, uid, {"rename": "renamed while done"})
    h.sync()
    found = h.r_by_title("renamed while done")
    equal(len(found), 1, "renamed in Reminders")
    equal(found[0].completed, True, "still completed afterwards")


def scenario_notes_cleared_in_ha(h: Harness) -> None:
    h.reminders.add(h.list_name, rhs.Item(title="notes go", notes="temporary"))
    h.sync()
    uid = h.h_by_title("notes go")[0].id
    h.ha.update_item(h.entity_id, uid, {"description": ""})
    h.sync()
    equal(h.r_by_title("notes go")[0].notes, None, "notes cleared in Reminders")


def scenario_delete_in_ha(h: Harness) -> None:
    h.reminders.add(h.list_name, rhs.Item(title="delete via ha"))
    h.sync()
    uid = h.h_by_title("delete via ha")[0].id
    h.ha.remove_items(h.entity_id, [uid])
    h.sync()
    equal(len(h.r_by_title("delete via ha")), 0, "reminder deleted")
    equal(len(h.links()), 0, "link dropped")


def scenario_delete_in_reminders(h: Harness) -> None:
    rid = h.reminders.add(h.list_name, rhs.Item(title="delete via reminders"))
    h.sync()
    check(h.reminders.delete(h.list_name, rid), "delete should succeed")
    h.sync()
    equal(len(h.h_by_title("delete via reminders")), 0, "removed from Home Assistant")


def scenario_deleting_completed_in_ha_stops_tracking(h: Harness) -> None:
    # Clearing completed items in Home Assistant cannot propagate: the CLI
    # cannot delete a completed reminder. It must be dropped from sync instead
    # of failing on every run.
    rid = h.reminders.add(h.list_name, rhs.Item(title="cleared in ha"))
    h.sync()
    h.reminders.set_completed(h.list_name, rid, True)
    h.sync()
    h.ha.remove_items(h.entity_id, [h.h_by_title("cleared in ha")[0].id])
    h.sync()
    equal(len(h.r_by_title("cleared in ha")), 1, "reminder survives")
    equal(h.pair_state()["r_tombstones"], [rid], "tombstoned")
    h.sync()
    equal(len(h.h_by_title("cleared in ha")), 0, "and is not pushed back to HA")


def scenario_conflict_reminders_wins(h: Harness) -> None:
    rid = h.reminders.add(h.list_name, rhs.Item(title="tug of war"))
    h.sync()
    uid = h.h_by_title("tug of war")[0].id
    h.reminders.edit(h.list_name, rid, title="reminders version")
    h.ha.update_item(h.entity_id, uid, {"rename": "ha version"})
    h.sync()
    equal(len(h.h_by_title("reminders version")), 1, "Reminders won in Home Assistant")
    equal(len(h.r_by_title("reminders version")), 1, "Reminders kept its value")


def scenario_due_change_in_ha_is_reverted(h: Harness) -> None:
    h.reminders.add(h.list_name, rhs.Item(title="due tug", due=FAR_FUTURE_DAY))
    h.sync()
    uid = h.h_by_title("due tug")[0].id
    h.ha.update_item(h.entity_id, uid, {"due_date": "2031-12-25"})
    h.sync()
    equal(h.h_by_title("due tug")[0].due, FAR_FUTURE_DAY, "due date reverted in HA")
    equal(h.r_by_title("due tug")[0].due, FAR_FUTURE_DAY, "Reminders unchanged")


def scenario_due_cleared_in_reminders(h: Harness) -> None:
    rid = h.reminders.add(h.list_name, rhs.Item(title="due goes away", due=FAR_FUTURE_DAY))
    h.sync()
    equal(h.h_by_title("due goes away")[0].due, FAR_FUTURE_DAY, "due present first")
    # Clearing a due date is not something the CLI can do either, so recreate
    # the reminder without one -- the same situation as an edit on the phone.
    check(h.reminders.delete(h.list_name, rid), "delete for recreate")
    h.reminders.add(h.list_name, rhs.Item(title="due goes away"))
    h.sync()
    h.sync()
    equal(h.h_by_title("due goes away")[0].due, None, "due cleared in Home Assistant")


def scenario_title_adoption(h: Harness) -> None:
    # Losing the state file must not duplicate everything.
    h.reminders.add(h.list_name, rhs.Item(title="shared title"))
    h.ha.add_item(h.entity_id, rhs.Item(title="shared title"))
    h.sync()
    equal(len(h.r_by_title("shared title")), 1, "one reminder")
    equal(len(h.h_by_title("shared title")), 1, "one Home Assistant item")
    equal(len(h.links()), 1, "adopted into a single link")


def scenario_seeding_ignores_old_completions(h: Harness) -> None:
    rid = h.reminders.add(h.list_name, rhs.Item(title="ancient history"))
    h.reminders.set_completed(h.list_name, rid, True)
    h.sync()
    equal(len(h.h_by_title("ancient history")), 0, "not imported on first sync")
    equal(h.pair_state()["r_tombstones"], [rid], "tombstoned instead")


def scenario_sync_is_idempotent(h: Harness) -> None:
    h.reminders.add(
        h.list_name, rhs.Item(title="stable", notes="n", due=FAR_FUTURE_TIME)
    )
    h.ha.add_item(h.entity_id, rhs.Item(title="stable too", due=FAR_FUTURE_DAY))
    h.sync()
    h.sync()
    before = h.stable_state()
    r_before = sorted((i.title, i.due, i.notes, i.completed) for i in h.r_items())
    h_before = sorted((i.title, i.due, i.notes, i.completed) for i in h.h_items())

    h.sync()
    equal(h.stable_state(), before, "links unchanged by a no-op sync")
    equal(
        sorted((i.title, i.due, i.notes, i.completed) for i in h.r_items()),
        r_before,
        "Reminders unchanged",
    )
    equal(
        sorted((i.title, i.due, i.notes, i.completed) for i in h.h_items()),
        h_before,
        "Home Assistant unchanged",
    )
    equal(len(h.links()), 2, "both items still linked")


def scenario_mass_delete_guard(h: Harness) -> None:
    for index in range(4):
        h.reminders.add(h.list_name, rhs.Item(title="bulk %d" % index))
    h.sync()
    equal(len(h.h_items()), 4, "four items synced")

    for reminder in h.r_items():
        h.reminders.delete(h.list_name, reminder.id)

    with open(DEV_CONFIG, encoding="utf-8") as fh:
        raw = json.load(fh)
    strict = rhs.Config(
        dict(raw, state_file=h.state_path, max_deletes_per_run=2), DEV_CONFIG
    )
    equal(rhs.sync_once(strict), 1, "guard should make the run fail")
    equal(len(h.h_items()), 4, "nothing deleted while the guard holds")

    equal(rhs.sync_once(strict, force=True), 0, "--force should get through")
    equal(len(h.h_items()), 0, "and then delete them")


def scenario_auto_mode_pairs_by_name(h: Harness) -> None:
    config = h.auto_config()
    pairing = rhs.resolve_pairs(config, h.reminders, h.ha, create=False)
    paired = {name: entity for name, entity in pairing.pairs}
    equal(paired.get("RHS Test"), h.entity_id, "matched the test list by name")
    equal(len(paired), 2, "both RHS lists paired, nothing else")
    equal(pairing.problems, [], "no problems")


def scenario_auto_mode_creates_the_ha_list(h: Harness) -> None:
    # Exercise create_local_todo against the real config-flow API by removing
    # the Home Assistant side and letting auto mode put it back.
    entries = [e for e in h.config_entries("local_todo") if e["title"] == "RHS Покупки"]
    equal(len(entries), 1, "found the config entry to remove")
    h.delete_config_entry(entries[0]["entry_id"])

    gone = [f for f in h.ha.todo_entities().values() if f == "RHS Покупки"]
    equal(gone, [], "entity is gone after deleting the entry")

    config = h.auto_config()
    pairing = rhs.resolve_pairs(config, h.reminders, h.ha, create=True)
    equal(pairing.problems, [], "recreation reported no problems")

    recreated = {
        friendly: entity for entity, friendly in h.ha.todo_entities().items()
    }
    check("RHS Покупки" in recreated, "entity exists again")
    paired = dict(pairing.pairs)
    equal(paired.get("RHS Покупки"), recreated["RHS Покупки"], "and is paired")


def scenario_auto_mode_reports_without_creating(h: Harness) -> None:
    entries = [e for e in h.config_entries("local_todo") if e["title"] == "RHS Покупки"]
    equal(len(entries), 1, "found the config entry to remove")
    h.delete_config_entry(entries[0]["entry_id"])
    try:
        config = h.auto_config()
        pairing = rhs.resolve_pairs(config, h.reminders, h.ha, create=False)
        notes = [note for name, _, note in pairing.plan if name == "RHS Покупки"]
        equal(notes, ["would create in Home Assistant"], "reported, not created")
        equal(
            [f for f in h.ha.todo_entities().values() if f == "RHS Покупки"],
            [],
            "nothing was created",
        )
    finally:
        # Put it back for whatever runs next.
        rhs.resolve_pairs(h.auto_config(), h.reminders, h.ha, create=True)


SCENARIOS = [
    scenario_create_reminders_to_ha,
    scenario_create_ha_to_reminders,
    scenario_all_day_survives_round_trip,
    scenario_complete_in_reminders,
    scenario_uncomplete_in_ha,
    scenario_rename_in_reminders,
    scenario_rename_in_ha,
    scenario_rename_a_completed_item,
    scenario_notes_cleared_in_ha,
    scenario_delete_in_ha,
    scenario_delete_in_reminders,
    scenario_deleting_completed_in_ha_stops_tracking,
    scenario_conflict_reminders_wins,
    scenario_due_change_in_ha_is_reverted,
    scenario_due_cleared_in_reminders,
    scenario_title_adoption,
    scenario_seeding_ignores_old_completions,
    scenario_sync_is_idempotent,
    scenario_mass_delete_guard,
    scenario_auto_mode_pairs_by_name,
    scenario_auto_mode_reports_without_creating,
    scenario_auto_mode_creates_the_ha_list,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--create-lists",
        action="store_true",
        help="create the Reminders lists from dev/config.json if they are missing",
    )
    parser.add_argument("--only", help="run just the scenarios whose name contains this")
    parser.add_argument(
        "--list-source",
        help="account to create the lists in; needed only when you have more "
        "than one (iCloud plus a local account, say)",
    )
    args = parser.parse_args()

    rhs.setup_logging("error", None)

    if not os.path.exists(DEV_CONFIG):
        print("no %s -- run `make ha-up` first" % DEV_CONFIG, file=sys.stderr)
        return 2
    with open(DEV_CONFIG, encoding="utf-8") as fh:
        raw = json.load(fh)

    handle, state_path = tempfile.mkstemp(prefix="rhs-e2e-", suffix=".json")
    os.close(handle)
    os.remove(state_path)

    try:
        harness = Harness(raw, state_path)
    except rhs.UserError as exc:
        print("setup failed: %s" % exc, file=sys.stderr)
        return 2

    existing = {name.lower() for name in harness.reminders.lists()}
    for list_name, _ in harness.config.explicit_pairs:
        if list_name.lower() in existing:
            continue
        if not args.create_lists:
            print(
                "no Reminders list named %r.\n"
                "Create it in Reminders.app, or re-run with --create-lists."
                % list_name,
                file=sys.stderr,
            )
            return 2
        command = ["new-list", list_name]
        if args.list_source:
            command += ["--source", args.list_source]
        try:
            harness.reminders._run(command)
        except rhs.UserError as exc:
            # `new-list` refuses to guess when there is more than one account,
            # and helpfully prints the names it found.
            print("could not create %r:\n%s" % (list_name, exc), file=sys.stderr)
            print("Re-run with --list-source '<name>'.", file=sys.stderr)
            return 2
        print("created Reminders list %r" % list_name)

    scenarios = SCENARIOS
    if args.only:
        scenarios = [s for s in scenarios if args.only in s.__name__]

    print("list %r <-> %s\n" % (harness.list_name, harness.entity_id))
    passed, failed = 0, []
    for scenario in scenarios:
        name = scenario.__name__.replace("scenario_", "").replace("_", " ")
        try:
            harness.reset()
            scenario(harness)
        except Exception as exc:  # noqa: BLE001 - a scenario failing is the point
            failed.append((name, exc))
            print("FAIL  %s\n      %s" % (name, exc))
            if not isinstance(exc, Failure):
                traceback.print_exc()
        else:
            passed += 1
            print("ok    %s" % name)

    try:
        harness.reset()
    except Exception as exc:  # noqa: BLE001
        print("\nwarning: could not clean up: %s" % exc)
    if os.path.exists(state_path):
        os.remove(state_path)

    print("\n%d passed, %d failed" % (passed, len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
