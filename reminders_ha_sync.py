#!/usr/bin/python3
"""Two-way sync between macOS Reminders and Home Assistant to-do lists.

Single file, standard library only, so it runs under /usr/bin/python3 with no
virtualenv -- which is what makes it painless to hand to launchd.

The Reminders side is driven by keith/reminders-cli; Home Assistant is reached
over its REST API with a long-lived token. State lives in a JSON file so the
sync can tell "you deleted this" apart from "this was never here".

Configuration is a URL and a token: lists are paired by name and the missing
ones are created on both sides, so there is nothing to map by hand.

    ./reminders_ha_sync.py doctor         check the setup, show the pairing plan
    ./reminders_ha_sync.py lists          both sides' lists and how they pair up
    ./reminders_ha_sync.py sync           sync once (what launchd runs)
    ./reminders_ha_sync.py sync --dry-run print what a sync would change
    ./reminders_ha_sync.py install        install + load the LaunchAgent
    ./reminders_ha_sync.py uninstall      unload + remove it

See README.md for the config format and the sync rules.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import logging
import logging.handlers
import os
import plistlib
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

LOG = logging.getLogger("reminders-ha-sync")

VERSION = "0.1.0"

# abspath, deliberately not realpath. Installed by Homebrew this resolves to
# /opt/homebrew/bin/reminders-ha-sync -- a symlink that `brew upgrade` repoints
# at the new version. Resolving it would bake a Cellar path into the LaunchAgent
# and the agent would break on the next upgrade.
SCRIPT_PATH = os.path.abspath(__file__)
LAUNCH_LABEL = "com.github.keith-reminders-ha-sync"

DEFAULT_CONFIG_PATH = "~/.config/reminders-ha-sync/config.json"
DEFAULT_STATE_PATH = "~/.local/state/reminders-ha-sync/state.json"
DEFAULT_LOG_PATH = "~/Library/Logs/reminders-ha-sync.log"

REMINDERS_BINARY_CANDIDATES = (
    "/opt/homebrew/bin/reminders",
    "/usr/local/bin/reminders",
)

# The fields that actually get merged. Everything else on a reminder (priority,
# subtasks, recurrence, attachments, location) has no counterpart in Home
# Assistant's to-do model and is left strictly alone.
FIELDS = ("title", "notes", "due", "completed")

STATE_VERSION = 1


class UserError(Exception):
    """Something the user can fix: bad config, missing list, denied access."""


# --------------------------------------------------------------------------- #
# item model
# --------------------------------------------------------------------------- #


class Item:
    """One to-do, normalized so both sides are directly comparable.

    `due` is either "YYYY-MM-DD" (all-day) or a naive local "YYYY-MM-DDTHH:MM:SS".
    `notes` maps to Home Assistant's `description`; empty means None on both
    sides so "" and absent never look like a change.
    """

    __slots__ = ("id", "title", "notes", "due", "completed", "completed_at")

    def __init__(
        self,
        id: str = "",
        title: str = "",
        notes: Optional[str] = None,
        due: Optional[str] = None,
        completed: bool = False,
        completed_at: Optional[str] = None,
    ):
        self.id = id
        self.title = title
        self.notes = notes
        self.due = due
        self.completed = completed
        self.completed_at = completed_at

    def snapshot(self) -> Dict[str, object]:
        return {f: getattr(self, f) for f in FIELDS}

    def __repr__(self) -> str:
        bits = ["%s=%r" % (f, getattr(self, f)) for f in FIELDS]
        return "Item(%s)" % ", ".join(bits)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Item):
            return NotImplemented
        return self.snapshot() == other.snapshot() and self.id == other.id


def parse_iso(value: str) -> dt.datetime:
    """Parse an ISO 8601 timestamp, including the trailing-Z form.

    reminders-cli emits UTC with a "Z" suffix, which datetime.fromisoformat
    only learned to read in Python 3.11 -- and we target the 3.9 that ships
    with macOS.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return dt.datetime.fromisoformat(text)


def normalize_title(value: Optional[str]) -> str:
    # Home Assistant strips the summary on write, so strip here too or a title
    # with stray whitespace would look like an endless change.
    return (value or "").strip()


def normalize_name(value: str) -> str:
    """Fold a list name for comparison across the two sides.

    casefold rather than lower: it is the one that handles non-ASCII properly,
    which matters when the lists are named in Russian.
    """
    return " ".join(value.split()).casefold()


def normalize_notes(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    return text or None


def normalize_due(value: Optional[str]) -> Optional[str]:
    """Collapse either side's due representation into our two-form string.

    A datetime at exactly local midnight is treated as all-day. Reminders has a
    real all-day flag but reminders-cli does not expose it, and midnight is what
    an all-day reminder serializes to, so this is the only signal available.
    A reminder deliberately set to 00:00 therefore round-trips as all-day.
    """
    if not value:
        return None
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    try:
        stamp = parse_iso(text)
    except ValueError:
        LOG.warning("could not parse due date %r, ignoring it", value)
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone().replace(tzinfo=None)
    if (stamp.hour, stamp.minute, stamp.second, stamp.microsecond) == (0, 0, 0, 0):
        return stamp.date().isoformat()
    return stamp.replace(microsecond=0).isoformat()


def due_is_all_day(due: str) -> bool:
    return len(due) == 10


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


class Config:
    def __init__(self, raw: Dict[str, object], path: str):
        self.path = path
        ha = raw.get("home_assistant")
        if not isinstance(ha, dict):
            raise UserError("%s: missing the \"home_assistant\" object" % path)

        url = str(ha.get("url") or "").rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise UserError(
                "%s: home_assistant.url must start with http:// or https:// "
                "(got %r)" % (path, ha.get("url"))
            )
        self.ha_url = url

        token = os.environ.get("RHS_HA_TOKEN") or ha.get("token") or ""
        if not token:
            raise UserError(
                "%s: home_assistant.token is empty. Create a long-lived access "
                "token in Home Assistant under your profile -> Security." % path
            )
        self.ha_token = str(token)
        self.ha_timeout = float(ha.get("timeout", 20))
        self.ha_verify_tls = bool(ha.get("verify_tls", True))

        # "auto" pairs lists by name and creates whatever is missing, so a
        # working config needs nothing but a URL and a token. An explicit array
        # is the escape hatch for lists named differently on the two sides.
        lists = raw.get("lists", "auto")
        self.list_mode = "auto"
        self.explicit_pairs: List[Tuple[str, str]] = []
        if isinstance(lists, str):
            if lists != "auto":
                raise UserError(
                    "%s: \"lists\" must be \"auto\" or an array of "
                    "{\"reminders\": ..., \"ha\": ...} pairs (got %r)" % (path, lists)
                )
        elif isinstance(lists, list) and lists:
            self.list_mode = "explicit"
            for entry in lists:
                if not isinstance(entry, dict) or "reminders" not in entry or "ha" not in entry:
                    raise UserError(
                        "%s: every entry in \"lists\" needs both \"reminders\" "
                        "and \"ha\" keys (got %r)" % (path, entry)
                    )
                entity = str(entry["ha"])
                if not entity.startswith("todo."):
                    raise UserError(
                        "%s: %r is not a to-do entity; it must look like "
                        "todo.something" % (path, entity)
                    )
                self.explicit_pairs.append((str(entry["reminders"]), entity))
        else:
            raise UserError(
                "%s: \"lists\" must be \"auto\" or a non-empty array of "
                "{\"reminders\": ..., \"ha\": ...} pairs" % path
            )

        excluded = raw.get("exclude", [])
        if not isinstance(excluded, list):
            raise UserError("%s: \"exclude\" must be an array of names" % path)
        # Matched against a Reminders list name, a Home Assistant friendly name
        # or an entity id, so any of the three works as a way to opt out.
        self.exclude = {normalize_name(str(name)) for name in excluded}

        self.create_missing = str(raw.get("create_missing", "both"))
        if self.create_missing not in ("both", "ha", "reminders", "none"):
            raise UserError(
                "%s: create_missing must be \"both\", \"ha\", \"reminders\" or "
                "\"none\"" % path
            )
        self.reminders_source = raw.get("reminders_source") or None

        self.conflict_winner = str(raw.get("conflict_winner", "reminders"))
        if self.conflict_winner not in ("reminders", "ha", "manual"):
            raise UserError(
                "%s: conflict_winner must be \"reminders\", \"ha\" or "
                "\"manual\"" % path
            )

        self.due_from_ha = str(raw.get("due_from_ha", "revert"))
        if self.due_from_ha not in ("revert", "ignore", "recreate"):
            raise UserError(
                "%s: due_from_ha must be \"revert\", \"ignore\" or "
                "\"recreate\"" % path
            )

        self.match_by_title = bool(raw.get("match_by_title", True))
        self.max_deletes_per_run = int(raw.get("max_deletes_per_run", 25))
        self.log_level = str(raw.get("log_level", "info"))
        self.log_file = expand(str(raw.get("log_file", DEFAULT_LOG_PATH)))
        self.state_file = expand(str(raw.get("state_file", DEFAULT_STATE_PATH)))
        self.reminders_binary = raw.get("reminders_binary") or None


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def load_config(path: str) -> Config:
    full = expand(path)
    if not os.path.exists(full):
        raise UserError(
            "no config at %s\n"
            "Copy config.example.json there and fill in your Home Assistant "
            "URL, token and list pairs." % full
        )
    try:
        with open(full, encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise UserError("%s is not valid JSON: %s" % (full, exc)) from None
    if not isinstance(raw, dict):
        raise UserError("%s must contain a JSON object" % full)
    return Config(raw, full)


# --------------------------------------------------------------------------- #
# Reminders side (keith/reminders-cli)
# --------------------------------------------------------------------------- #


class Reminders:
    """Thin wrapper over the `reminders` binary.

    Items are addressed by `externalId` (EventKit's calendarItemExternalIdentifier)
    rather than by list position, because positions shift as soon as anything
    changes. Every subcommand that takes an index also accepts an id.
    """

    def __init__(self, binary: Optional[str] = None):
        self.binary = binary or self._find_binary()

    @staticmethod
    def _find_binary() -> str:
        found = shutil.which("reminders")
        if found:
            return found
        for candidate in REMINDERS_BINARY_CANDIDATES:
            if os.access(candidate, os.X_OK):
                return candidate
        raise UserError(
            "the `reminders` binary was not found.\n"
            "Install it with:  brew install keith/formulae/reminders-cli"
        )

    def _run(self, args: Sequence[str], check: bool = True) -> Tuple[int, str, str]:
        cmd = [self.binary] + list(args)
        LOG.debug("$ %s", " ".join(cmd))
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        if check and proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "").strip()
            if "grant reminders access" in message:
                raise UserError(
                    "Reminders access has not been granted to %s.\n"
                    "Run it once from Terminal.app and click Allow:\n"
                    "    %s show-lists\n"
                    "If no dialog appears, enable it under System Settings -> "
                    "Privacy & Security -> Reminders." % (self.binary, self.binary)
                )
            raise UserError("`%s` failed: %s" % (" ".join(cmd), message))
        return proc.returncode, proc.stdout, proc.stderr

    def _run_json(self, args: Sequence[str]) -> object:
        _, out, _ = self._run(args)
        # reminders-cli prints incidental progress lines on some subcommands, so
        # take the JSON document rather than assuming the whole of stdout is it.
        text = out.strip()
        start = min(
            (i for i in (text.find("["), text.find("{")) if i != -1),
            default=-1,
        )
        if start == -1:
            raise UserError("`reminders %s` printed no JSON: %r" % (args[0], text))
        return json.loads(text[start:])

    def lists(self) -> List[str]:
        return list(self._run_json(["show-lists", "--format", "json"]))

    def new_list(self, name: str, source: Optional[str] = None) -> None:
        args = ["new-list", name]
        if source:
            args += ["--source", source]
        try:
            self._run(args)
        except UserError as exc:
            if "Multiple sources" in str(exc):
                raise UserError(
                    "cannot create the Reminders list %r: you have more than "
                    "one account, so the script cannot guess where to put it.\n"
                    "Add \"reminders_source\": \"<name>\" to the config -- the "
                    "names are listed here:\n%s" % (name, exc)
                ) from None
            raise

    def items(self, list_name: str) -> List[Item]:
        raw = self._run_json(
            ["show", list_name, "--include-completed", "--format", "json"]
        )
        items = []
        for entry in raw:
            items.append(
                Item(
                    id=entry["externalId"],
                    title=normalize_title(entry.get("title")),
                    notes=normalize_notes(entry.get("notes")),
                    due=normalize_due(entry.get("dueDate")),
                    completed=bool(entry.get("isCompleted")),
                    completed_at=entry.get("completionDate"),
                )
            )
        return items

    def add(self, list_name: str, item: Item) -> str:
        """Create a reminder and return its externalId."""
        args = [list_name, "--format", "json"]
        if item.notes:
            args += ["--notes", item.notes]
        if item.due:
            args += ["--due-date", format_due_for_cli(item.due)]
        # The title is a `.remaining` argument, so it has to come last and
        # behind `--`; otherwise a title starting with a dash is parsed as an
        # option and everything after it is swallowed.
        args += ["--", item.title]
        created = self._run_json(["add"] + args)
        return created["externalId"]

    def edit(
        self,
        list_name: str,
        item_id: str,
        title: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        args = ["edit", list_name, item_id]
        if notes is not None:
            args += ["--notes", notes]
        if title is not None:
            args += ["--", title]
        self._run(args)

    def set_completed(self, list_name: str, item_id: str, completed: bool) -> None:
        verb = "complete" if completed else "uncomplete"
        self._run([verb, list_name, item_id])

    def delete(self, list_name: str, item_id: str) -> bool:
        """Delete a reminder. False means the CLI refused, which in practice
        means the reminder is completed -- `delete` only looks at open ones."""
        code, _, _ = self._run(["delete", list_name, item_id], check=False)
        return code == 0


def format_due_for_cli(due: str) -> str:
    """Render a due value for `reminders --due-date`.

    The CLI parses this with NSDataDetector and decides all-day vs timed from
    whether a time is present, so the date-only form must stay date-only.
    """
    if due_is_all_day(due):
        return due
    return due.replace("T", " ")[:16]


# --------------------------------------------------------------------------- #
# Home Assistant side
# --------------------------------------------------------------------------- #


class HomeAssistant:
    def __init__(self, url: str, token: str, timeout: float = 20.0, verify_tls: bool = True):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._ssl_context = None
        if not verify_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self._ssl_context = context

    def _request(self, method: str, path: str, body: object = None) -> object:
        url = self.url + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        LOG.debug("%s %s %s", method, path, json.dumps(body, ensure_ascii=False) if body else "")
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._ssl_context
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            if exc.code == 401:
                raise UserError(
                    "Home Assistant rejected the token (401) -- it may have been "
                    "deleted, or belong to a different instance. Create a new "
                    "long-lived access token in your profile, under Security."
                ) from None
            raise UserError(
                "Home Assistant returned %d for %s %s: %s"
                % (exc.code, method, path, detail[:300])
            ) from None
        except urllib.error.URLError as exc:
            raise UserError(
                "cannot reach Home Assistant at %s: %s" % (self.url, exc.reason)
            ) from None
        if not raw:
            return None
        return json.loads(raw)

    def ping(self) -> str:
        result = self._request("GET", "/api/")
        return (result or {}).get("message", "")

    def is_admin(self) -> bool:
        """Whether this token may create config entries, i.e. to-do lists.

        There is no "am I an admin" endpoint, so this asks for something only an
        administrator can read.
        """
        try:
            self._request("GET", "/api/config/config_entries/entry?domain=local_todo")
            return True
        except UserError:
            return False

    def state(self, entity_id: str) -> Optional[dict]:
        try:
            return self._request("GET", "/api/states/" + urllib.parse.quote(entity_id))
        except UserError as exc:
            if "returned 404" in str(exc):
                return None
            raise

    def todo_entities(self) -> Dict[str, str]:
        """entity_id -> friendly name, for every to-do entity."""
        states = self._request("GET", "/api/states") or []
        return {
            s["entity_id"]: s.get("attributes", {}).get("friendly_name", s["entity_id"])
            for s in states
            if s["entity_id"].startswith("todo.")
        }

    def create_local_todo(self, name: str) -> str:
        """Add a Local To-do list and return its entity id.

        To-do entities come from config entries rather than from the service
        API, so this walks the config flow the UI would walk.
        """
        flow = self._request(
            "POST",
            "/api/config/config_entries/flow",
            {"handler": "local_todo", "show_advanced_options": False},
        )
        if not isinstance(flow, dict) or "flow_id" not in flow:
            raise UserError(
                "Home Assistant would not start a Local To-do config flow. The "
                "token must belong to an administrator account."
            )
        result = self._request(
            "POST",
            "/api/config/config_entries/flow/" + flow["flow_id"],
            {"todo_list_name": name},
        )
        if (result or {}).get("type") != "create_entry":
            reason = (result or {}).get("reason", "")
            if reason == "already_configured":
                raise UserError(
                    "Home Assistant already has a Local To-do list whose "
                    "internal key collides with %r. Home Assistant "
                    "transliterates names into that key, so \"Тест\" and "
                    "\"Test\" collide. Rename one of the lists, or map this one "
                    "explicitly in \"lists\"." % name
                )
            raise UserError(
                "could not create the Home Assistant list %r: %s"
                % (name, json.dumps(result, ensure_ascii=False))
            )

        # The entity registry needs a moment, and the entity id is derived from
        # a transliterated slug, so look it up by name rather than guess it.
        deadline = time.time() + 30
        while time.time() < deadline:
            for entity_id, friendly in self.todo_entities().items():
                if normalize_name(friendly) == normalize_name(name):
                    return entity_id
            time.sleep(1)
        raise UserError(
            "created the Home Assistant list %r but its entity never appeared"
            % name
        )

    def _service(self, service: str, data: dict, return_response: bool = False) -> object:
        path = "/api/services/todo/" + service
        if return_response:
            path += "?return_response"
        return self._request("POST", path, data)

    def items(self, entity_id: str) -> List[Item]:
        response = self._service(
            "get_items",
            {"entity_id": entity_id, "status": ["needs_action", "completed"]},
            return_response=True,
        )
        payload = (response or {}).get("service_response", {}) or {}
        entries = (payload.get(entity_id) or {}).get("items", [])
        items = []
        for entry in entries:
            items.append(
                Item(
                    id=entry["uid"],
                    title=normalize_title(entry.get("summary")),
                    notes=normalize_notes(entry.get("description")),
                    due=normalize_due(entry.get("due")),
                    completed=entry.get("status") == "completed",
                    completed_at=entry.get("completed"),
                )
            )
        return items

    def add_item(self, entity_id: str, item: Item) -> None:
        """Create an item. Home Assistant does not return the new uid, so the
        caller has to re-read the list to find it (see link_created_ha_items)."""
        data = {"entity_id": entity_id, "item": item.title}
        if item.notes:
            data["description"] = item.notes
        if item.due:
            data.update(due_payload(item.due))
        self._service("add_item", data)

    def update_item(self, entity_id: str, uid: str, fields: dict) -> None:
        data = {"entity_id": entity_id, "item": uid}
        data.update(fields)
        self._service("update_item", data)

    def remove_items(self, entity_id: str, uids: Sequence[str]) -> None:
        if not uids:
            return
        self._service("remove_item", {"entity_id": entity_id, "item": list(uids)})


def due_payload(due: Optional[str]) -> dict:
    """Build the due part of a todo service call.

    `due_date` and `due_datetime` are mutually exclusive -- sending both is a
    400 -- and either one set to null clears the date.
    """
    if not due:
        return {"due_date": None}
    if due_is_all_day(due):
        return {"due_date": due}
    # Send an explicit offset so a mismatch between the Mac's timezone and Home
    # Assistant's cannot silently shift the time.
    local = dt.datetime.fromisoformat(due).astimezone()
    return {"due_datetime": local.isoformat()}


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


def pair_key(list_name: str, entity_id: str) -> str:
    return json.dumps([list_name, entity_id], ensure_ascii=False)


class Store:
    """The JSON file that remembers what the two sides agreed on last time.

    Without it a missing item is indistinguishable from a new one, and every
    sync would resurrect everything the user deleted.
    """

    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, object] = {"version": STATE_VERSION, "pairs": {}}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict) and loaded.get("version") == STATE_VERSION:
                    self.data = loaded
                else:
                    LOG.warning(
                        "ignoring state file %s: unexpected version %r",
                        path,
                        (loaded or {}).get("version"),
                    )
            except (json.JSONDecodeError, OSError) as exc:
                LOG.warning("ignoring unreadable state file %s: %s", path, exc)
        self.data.setdefault("pairs", {})

    def pair(self, list_name: str, entity_id: str) -> dict:
        pairs = self.data["pairs"]
        key = pair_key(list_name, entity_id)
        state = pairs.get(key)
        if state is None:
            state = {"last_sync": None, "links": [], "r_tombstones": [], "h_tombstones": []}
            pairs[key] = state
        for field, default in (
            ("links", []),
            ("r_tombstones", []),
            ("h_tombstones", []),
        ):
            state.setdefault(field, default)
        state.setdefault("last_sync", None)
        return state

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
        os.replace(temporary, self.path)


# --------------------------------------------------------------------------- #
# planning: pure functions, no I/O
# --------------------------------------------------------------------------- #


class LinkWrite:
    """A linked pair plus whatever has to be written to either side for it.

    `link` is the record that will be persisted and `prev_last` is what it held
    coming in. If any write for this pair fails the executor rolls `last` back,
    so a failed write is retried next run instead of being remembered as agreed.
    """

    __slots__ = ("link", "prev_last", "ha_fields", "r_update", "label", "superseded")

    def __init__(self, link: dict, prev_last: dict, label: str):
        self.link = link
        self.prev_last = prev_last
        self.ha_fields: dict = {}
        self.r_update: dict = {}
        self.label = label
        # Set when the reminder is being recreated: the recreation carries a new
        # id, so this link is replaced rather than kept.
        self.superseded = False

    @property
    def has_writes(self) -> bool:
        return bool(self.ha_fields) or any(f in self.r_update for f in FIELDS)


class Plan:
    def __init__(self) -> None:
        self.ha_creates: List[Tuple[str, Item]] = []     # (reminder id, item)
        # Links whose Home Assistant item must go. The whole link is kept so a
        # failed removal can be retried instead of leaving an orphan that the
        # next sync would happily recreate in Reminders.
        self.ha_removes: List[dict] = []
        self.r_creates: List[Tuple[str, Item]] = []      # (ha uid, item)
        self.r_removes: List[str] = []
        self.r_recreates: List[Tuple[dict, Item]] = []   # (link, desired item)
        self.link_writes: List[LinkWrite] = []
        self.r_tombstones: Set[str] = set()
        self.h_tombstones: Set[str] = set()
        self.conflicts: List[str] = []
        self.warnings: List[str] = []

    @property
    def ha_remove_uids(self) -> List[str]:
        return [link["h"] for link in self.ha_removes]

    @property
    def delete_count(self) -> int:
        return len(self.ha_removes) + len(self.r_removes) + len(self.r_recreates)

    def is_empty(self) -> bool:
        return not (
            self.ha_creates
            or self.ha_removes
            or self.r_creates
            or self.r_removes
            or self.r_recreates
            or any(w.has_writes for w in self.link_writes)
        )

    def describe(self) -> List[str]:
        lines = []
        for _, item in self.ha_creates:
            lines.append("HA  + %s" % item.title)
        for write in self.link_writes:
            if write.ha_fields:
                lines.append("HA  ~ %s: %s" % (write.label, format_fields(write.ha_fields)))
        for link in self.ha_removes:
            lines.append(
                "HA  - %s" % (link.get("last", {}).get("title") or link["h"])
            )
        for _, item in self.r_creates:
            lines.append("REM + %s" % item.title)
        for write in self.link_writes:
            changes = {k: v for k, v in write.r_update.items() if k in FIELDS}
            if changes:
                lines.append("REM ~ %s: %s" % (write.label, format_fields(changes)))
        for rid in self.r_removes:
            lines.append("REM - %s" % rid)
        for _, item in self.r_recreates:
            lines.append("REM ! recreate %s (due date changed in HA)" % item.title)
        return lines


def format_fields(fields: dict) -> str:
    return ", ".join("%s=%r" % (k, v) for k, v in sorted(fields.items()))


def is_fresh_completion(item: Item, last_sync: Optional[str]) -> bool:
    """Was this completed since the previous sync?

    Reminders keeps completed items forever. Without this check, seeding a list
    would push years of finished reminders into Home Assistant.
    """
    if last_sync is None:
        return False
    if not item.completed_at:
        # No timestamp to go on -- Home Assistant omits it for items completed
        # by some integrations. Assume it is recent rather than lose it.
        return True
    try:
        return parse_iso(item.completed_at) >= parse_iso(last_sync)
    except ValueError:
        return True


def plan_pair(
    r_items: Sequence[Item],
    h_items: Sequence[Item],
    state: dict,
    config: Config,
) -> Plan:
    """Work out every write needed to make the two sides agree.

    Three-way merge: each field is compared against the value recorded at the
    end of the previous sync, so "changed here" and "changed there" are
    distinguishable and only real conflicts need a policy.
    """
    plan = Plan()
    last_sync = state.get("last_sync")

    r_by_id = {item.id: item for item in r_items}
    h_by_id = {item.id: item for item in h_items}

    # A tombstone only holds while the item it names still exists *and* is still
    # completed. Reopening a long-finished item is a deliberate act, so it gets
    # picked back up on the next sync.
    def live_tombstones(ids: Iterable[str], index: Dict[str, Item]) -> Set[str]:
        return {i for i in ids if i in index and index[i].completed}

    plan.r_tombstones = live_tombstones(state.get("r_tombstones", []), r_by_id)
    plan.h_tombstones = live_tombstones(state.get("h_tombstones", []), h_by_id)

    linked_r: Set[str] = set()
    linked_h: Set[str] = set()

    for link in state.get("links", []):
        r_id, h_uid = link.get("r"), link.get("h")
        linked_r.add(r_id)
        linked_h.add(h_uid)
        reminder = r_by_id.get(r_id)
        todo = h_by_id.get(h_uid)

        if reminder is None and todo is None:
            continue  # gone from both sides, forget the link
        if reminder is None:
            plan.ha_removes.append(dict(link))
            continue
        if todo is None:
            plan.r_removes.append(r_id)
            continue
        merge_link(dict(link), reminder, todo, plan, config)

    unlinked_r = [i for i in r_items if i.id not in linked_r and i.id not in plan.r_tombstones]
    unlinked_h = [i for i in h_items if i.id not in linked_h and i.id not in plan.h_tombstones]

    # Pair up same-titled strays before deciding anything else. This is what
    # keeps the first sync -- or a sync after the state file is lost -- from
    # duplicating every item instead of adopting it.
    if config.match_by_title:
        by_title: Dict[str, List[Item]] = {}
        for todo in unlinked_h:
            by_title.setdefault(todo.title, []).append(todo)

        remaining_r: List[Item] = []
        adopted_h: Set[str] = set()
        for reminder in unlinked_r:
            candidates = by_title.get(reminder.title)
            if not candidates:
                remaining_r.append(reminder)
                continue
            todo = candidates.pop(0)
            adopted_h.add(todo.id)
            # An empty `last` makes both sides look changed, so the ordinary
            # merge settles the pair with the configured conflict policy.
            merge_link({"r": reminder.id, "h": todo.id, "last": {}}, reminder, todo, plan, config)

        unlinked_r = remaining_r
        unlinked_h = [i for i in unlinked_h if i.id not in adopted_h]

    for reminder in unlinked_r:
        if reminder.completed and not is_fresh_completion(reminder, last_sync):
            plan.r_tombstones.add(reminder.id)
            continue
        plan.ha_creates.append((reminder.id, reminder))

    for todo in unlinked_h:
        if todo.completed and not is_fresh_completion(todo, last_sync):
            plan.h_tombstones.add(todo.id)
            continue
        plan.r_creates.append((todo.id, todo))

    return plan


def merge_link(link: dict, reminder: Item, todo: Item, plan: Plan, config: Config) -> None:
    """Merge one linked pair, appending the resulting writes to `plan`."""
    last = dict(link.get("last") or {})
    agreed = dict(last)
    write = LinkWrite(
        link={"r": reminder.id, "h": todo.id, "last": agreed},
        prev_last=last,
        label=reminder.title or todo.title or reminder.id,
    )
    write.r_update = {"rid": reminder.id, "was_completed": reminder.completed}

    for field in FIELDS:
        r_value = getattr(reminder, field)
        h_value = getattr(todo, field)
        base = last.get(field)
        r_changed = r_value != base
        h_changed = h_value != base

        if r_value == h_value:
            agreed[field] = r_value
            continue
        if not r_changed and not h_changed:
            # Neither side moved yet the values differ: the last sync could not
            # write one of them (a due date, typically). Leave it be.
            continue

        if r_changed and h_changed:
            plan.conflicts.append(
                "%r: %s changed on both sides (reminders=%r, ha=%r)"
                % (write.label, field, r_value, h_value)
            )
            winner = config.conflict_winner
            if winner == "manual":
                continue
        else:
            winner = "reminders" if r_changed else "ha"

        if winner == "reminders":
            apply_to_ha(field, r_value, write.ha_fields)
            agreed[field] = r_value
        elif field == "due":
            resolve_due_from_ha(reminder, todo, plan, config, write, agreed)
        else:
            write.r_update[field] = h_value
            agreed[field] = h_value

    if write.superseded:
        # Recreating the reminder is the only way to change its due date, so the
        # replacement is built from the merged values and the per-field writes
        # to Reminders are dropped as redundant.
        desired = Item(
            id=reminder.id,
            title=str(agreed.get("title", reminder.title)),
            notes=agreed.get("notes", reminder.notes),
            due=agreed.get("due", reminder.due),
            completed=bool(agreed.get("completed", reminder.completed)),
        )
        write.r_update = {}
        # Carry the *previous* snapshot: if the delete fails the old reminder is
        # still there and must not be recorded as agreed.
        plan.r_recreates.append(
            ({"r": reminder.id, "h": todo.id, "last": write.prev_last}, desired)
        )

    plan.link_writes.append(write)


def apply_to_ha(field: str, value: object, fields: dict) -> None:
    if field == "title":
        fields["rename"] = value
    elif field == "notes":
        # "" clears it; Home Assistant has no way to unset a description.
        fields["description"] = value or ""
    elif field == "due":
        fields.update(due_payload(value))  # type: ignore[arg-type]
    elif field == "completed":
        fields["status"] = "completed" if value else "needs_action"


def resolve_due_from_ha(
    reminder: Item,
    todo: Item,
    plan: Plan,
    config: Config,
    write: "LinkWrite",
    agreed: dict,
) -> None:
    """Handle a due date edited in Home Assistant.

    reminders-cli's `edit` only touches title and notes, so a due date cannot
    be written to an existing reminder. The options are to push the reminder's
    own date back (default), let the two diverge, or delete and recreate the
    reminder.
    """
    if config.due_from_ha == "revert":
        apply_to_ha("due", reminder.due, write.ha_fields)
        agreed["due"] = reminder.due
        plan.warnings.append(
            "%r: due date reverted in Home Assistant -- due dates can only be "
            "changed in Reminders (see due_from_ha)" % write.label
        )
    elif config.due_from_ha == "ignore":
        agreed["due"] = todo.due
        plan.warnings.append(
            "%r: due date differs (reminders=%r, ha=%r) and due_from_ha=ignore"
            % (write.label, reminder.due, todo.due)
        )
    elif reminder.completed:
        # `delete` cannot see completed reminders, so there is nothing to
        # recreate; keep the reminder's own date and push it back.
        apply_to_ha("due", reminder.due, write.ha_fields)
        agreed["due"] = reminder.due
        plan.warnings.append(
            "%r: cannot recreate a completed reminder to change its due date"
            % write.label
        )
    else:
        agreed["due"] = todo.due
        write.superseded = True


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #


class Counter:
    def __init__(self) -> None:
        self.created_ha = 0
        self.created_r = 0
        self.updated_ha = 0
        self.updated_r = 0
        self.removed_ha = 0
        self.removed_r = 0
        self.failed = 0

    def summary(self) -> str:
        return (
            "HA +%d ~%d -%d | Reminders +%d ~%d -%d | failures %d"
            % (
                self.created_ha,
                self.updated_ha,
                self.removed_ha,
                self.created_r,
                self.updated_r,
                self.removed_r,
                self.failed,
            )
        )


def execute_plan(
    plan: Plan,
    reminders: Reminders,
    ha: HomeAssistant,
    list_name: str,
    entity_id: str,
    h_items_before: Sequence[Item],
    counter: Counter,
) -> List[dict]:
    """Apply the plan and return the links to persist."""
    for write in plan.link_writes:
        ok = True
        if write.ha_fields:
            try:
                ha.update_item(entity_id, write.link["h"], write.ha_fields)
                counter.updated_ha += 1
            except UserError as exc:
                ok = False
                counter.failed += 1
                LOG.error("could not update %r in %s: %s", write.label, entity_id, exc)
        if any(field in write.r_update for field in FIELDS):
            ok = apply_reminder_update(reminders, list_name, write.r_update, counter) and ok
        if not ok:
            # Do not record a value as agreed when writing it failed, or the
            # change would be treated as already-synced and silently dropped.
            write.link["last"] = write.prev_last

    links = [write.link for write in plan.link_writes if not write.superseded]

    if plan.ha_removes:
        try:
            ha.remove_items(entity_id, plan.ha_remove_uids)
            counter.removed_ha += len(plan.ha_removes)
        except UserError as exc:
            counter.failed += 1
            LOG.error("could not remove items from %s: %s", entity_id, exc)
            # Keep the links so the removal is retried. Dropping them would turn
            # the surviving Home Assistant items into strays that the next sync
            # would recreate in Reminders.
            links.extend(plan.ha_removes)

    for rid in plan.r_removes:
        if reminders.delete(list_name, rid):
            counter.removed_r += 1
        else:
            # `delete` only sees open reminders, so this is almost always a
            # completed one -- e.g. the user cleared completed items in Home
            # Assistant. Stop tracking it instead of retrying forever.
            plan.r_tombstones.add(rid)
            LOG.info(
                "reminder %s could not be deleted (completed?); no longer syncing it",
                rid,
            )

    for link, desired in plan.r_recreates:
        if not reminders.delete(list_name, link["r"]):
            counter.failed += 1
            LOG.error("could not recreate reminder %r: delete failed", desired.title)
            links.append(link)
            continue
        counter.removed_r += 1
        new_id = create_reminder(reminders, list_name, desired, counter)
        if new_id:
            links.append({"r": new_id, "h": link["h"], "last": desired.snapshot()})

    for ha_uid, item in plan.r_creates:
        new_id = create_reminder(reminders, list_name, item, counter)
        if new_id:
            links.append({"r": new_id, "h": ha_uid, "last": item.snapshot()})

    links.extend(
        create_ha_items(plan, ha, entity_id, h_items_before, counter)
    )
    return links


def create_reminder(
    reminders: Reminders, list_name: str, item: Item, counter: Counter
) -> Optional[str]:
    """Create a reminder, completing it afterwards if it arrives done.

    `reminders add` has no way to create something already completed, so an item
    that was completed in Home Assistant needs the extra step.
    """
    try:
        new_id = reminders.add(list_name, item)
    except UserError as exc:
        counter.failed += 1
        LOG.error("could not create reminder %r: %s", item.title, exc)
        return None
    counter.created_r += 1
    if item.completed:
        try:
            reminders.set_completed(list_name, new_id, True)
        except UserError as exc:
            counter.failed += 1
            LOG.error("created %r but could not complete it: %s", item.title, exc)
    return new_id


def apply_reminder_update(
    reminders: Reminders, list_name: str, update: dict, counter: Counter
) -> bool:
    """Write one reminder, working around the CLI's completed-item blind spot.

    `edit` only looks at open reminders, so changing the text of a completed one
    means uncompleting it, editing, and completing it again -- which does reset
    the completion date, but is the only route the CLI offers.
    """
    rid = update["rid"]
    was_completed = update["was_completed"]
    target_completed = update.get("completed", was_completed)
    text_change = "title" in update or "notes" in update

    # `edit` reads None as "leave this alone", so clearing notes has to travel
    # as an empty string. Only pass notes at all when they actually changed.
    notes_arg = None
    if "notes" in update:
        notes_arg = update["notes"] or ""

    try:
        if text_change:
            if was_completed:
                reminders.set_completed(list_name, rid, False)
            reminders.edit(
                list_name,
                rid,
                title=update.get("title"),
                notes=notes_arg,
            )
            if target_completed:
                reminders.set_completed(list_name, rid, True)
        elif target_completed != was_completed:
            reminders.set_completed(list_name, rid, target_completed)
        counter.updated_r += 1
        return True
    except UserError as exc:
        counter.failed += 1
        LOG.error("could not update reminder %r: %s", rid, exc)
        return False


def create_ha_items(
    plan: Plan,
    ha: HomeAssistant,
    entity_id: str,
    h_items_before: Sequence[Item],
    counter: Counter,
) -> List[dict]:
    """Create the queued items in Home Assistant, then find their uids.

    `todo.add_item` returns nothing, so the only way to learn a new uid is to
    re-read the list and look for uids that were not there before.
    """
    if not plan.ha_creates:
        return []

    known = {item.id for item in h_items_before}
    wanted: List[Tuple[str, Item]] = []
    for r_id, item in plan.ha_creates:
        try:
            ha.add_item(entity_id, item)
            counter.created_ha += 1
            wanted.append((r_id, item))
        except UserError as exc:
            counter.failed += 1
            LOG.error("could not create %r in %s: %s", item.title, entity_id, exc)

    if not wanted:
        return []

    fresh = [item for item in ha.items(entity_id) if item.id not in known]
    by_title: Dict[str, List[Item]] = {}
    for item in fresh:
        by_title.setdefault(item.title, []).append(item)

    links = []
    for r_id, item in wanted:
        candidates = by_title.get(item.title)
        if not candidates:
            counter.failed += 1
            LOG.error(
                "created %r in %s but could not find it again; it will be "
                "linked on the next sync",
                item.title,
                entity_id,
            )
            continue
        created = candidates.pop(0)
        snapshot = item.snapshot()
        if item.completed:
            # add_item cannot set a status, so completed reminders need a
            # follow-up update.
            try:
                ha.update_item(entity_id, created.id, {"status": "completed"})
            except UserError as exc:
                counter.failed += 1
                LOG.error("could not mark %r completed: %s", item.title, exc)
                # Record what Home Assistant actually holds, so the next sync
                # sees the completion as still pending rather than done.
                snapshot["completed"] = False
        links.append({"r": r_id, "h": created.id, "last": snapshot})
    return links


# --------------------------------------------------------------------------- #
# pairing lists
# --------------------------------------------------------------------------- #


class Pairing:
    """The outcome of matching the two sides' lists up.

    `pairs` is what gets synced. `plan` is the same information in a form worth
    showing a human, including the lists that were skipped and why.
    """

    def __init__(self) -> None:
        self.pairs: List[Tuple[str, str]] = []
        self.plan: List[Tuple[str, str, str]] = []  # (reminders, ha, note)
        self.problems: List[str] = []

    def add(self, list_name: str, entity_id: str, note: str) -> None:
        self.pairs.append((list_name, entity_id))
        self.plan.append((list_name, entity_id, note))

    def skip(self, list_name: str, entity_id: str, note: str) -> None:
        self.plan.append((list_name, entity_id, note))


def resolve_pairs(
    config: Config,
    reminders: Reminders,
    ha: HomeAssistant,
    create: bool = True,
) -> Pairing:
    """Decide which Reminders list syncs with which to-do entity.

    In auto mode the two sides are matched on name -- case- and
    whitespace-insensitive -- and anything without a counterpart is created,
    which is what keeps the config down to a URL and a token.
    """
    pairing = Pairing()
    r_names = reminders.lists()
    entities = ha.todo_entities()

    if config.list_mode == "explicit":
        have = {normalize_name(name): name for name in r_names}
        for list_name, entity_id in config.explicit_pairs:
            if normalize_name(list_name) not in have:
                pairing.problems.append(
                    "no Reminders list named %r (have: %s)"
                    % (list_name, ", ".join(sorted(r_names)))
                )
                pairing.skip(list_name, entity_id, "missing in Reminders")
                continue
            if entity_id not in entities:
                pairing.problems.append(
                    "no Home Assistant entity %s" % entity_id
                )
                pairing.skip(list_name, entity_id, "missing in Home Assistant")
                continue
            pairing.add(list_name, entity_id, "paired")
        return pairing

    by_name: Dict[str, List[str]] = {}
    for entity_id, friendly in entities.items():
        by_name.setdefault(normalize_name(friendly), []).append(entity_id)

    def is_excluded(*names: str) -> bool:
        return any(normalize_name(name) in config.exclude for name in names)

    matched: Set[str] = set()
    for list_name in r_names:
        key = normalize_name(list_name)
        candidates = by_name.get(key, [])
        if is_excluded(list_name, *candidates):
            pairing.skip(list_name, candidates[0] if candidates else "-", "excluded")
            continue
        if len(candidates) > 1:
            pairing.problems.append(
                "%d Home Assistant lists are called %r (%s); map this one "
                "explicitly in \"lists\"" % (len(candidates), list_name, ", ".join(sorted(candidates)))
            )
            pairing.skip(list_name, ", ".join(sorted(candidates)), "ambiguous")
            continue
        if candidates:
            matched.add(candidates[0])
            pairing.add(list_name, candidates[0], "paired")
            continue
        if config.create_missing not in ("both", "ha"):
            pairing.skip(list_name, "-", "no Home Assistant list, not creating")
            continue
        if not create:
            pairing.skip(list_name, "-", "would create in Home Assistant")
            continue
        try:
            entity_id = ha.create_local_todo(list_name)
        except UserError as exc:
            pairing.problems.append(str(exc))
            pairing.skip(list_name, "-", "could not create in Home Assistant")
            continue
        LOG.info("created Home Assistant list %r as %s", list_name, entity_id)
        matched.add(entity_id)
        pairing.add(list_name, entity_id, "created in Home Assistant")

    # Now the other direction, for lists that only exist in Home Assistant.
    # Names already present in Reminders are skipped whatever happened above,
    # so an excluded list never comes back through this door.
    known = {normalize_name(name) for name in r_names}
    for entity_id, friendly in sorted(entities.items()):
        if entity_id in matched or normalize_name(friendly) in known:
            continue
        if is_excluded(entity_id, friendly):
            pairing.skip("-", entity_id, "excluded")
            continue
        if config.create_missing not in ("both", "reminders"):
            pairing.skip("-", entity_id, "no Reminders list, not creating")
            continue
        if not create:
            pairing.skip(friendly, entity_id, "would create in Reminders")
            continue
        try:
            reminders.new_list(friendly, config.reminders_source)
        except UserError as exc:
            pairing.problems.append(str(exc))
            pairing.skip(friendly, entity_id, "could not create in Reminders")
            continue
        LOG.info("created Reminders list %r for %s", friendly, entity_id)
        pairing.add(friendly, entity_id, "created in Reminders")

    return pairing


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def sync_once(config: Config, dry_run: bool = False, force: bool = False) -> int:
    reminders = Reminders(config.reminders_binary)
    ha = HomeAssistant(
        config.ha_url, config.ha_token, config.ha_timeout, config.ha_verify_tls
    )
    store = Store(config.state_file)
    counter = Counter()

    # A dry run must not create anything, so it reports the list-level changes
    # it would have made instead.
    pairing = resolve_pairs(config, reminders, ha, create=not dry_run)
    failures = len(pairing.problems)
    for problem in pairing.problems:
        LOG.error("%s", problem)

    if dry_run:
        for list_name, entity_id, note in pairing.plan:
            if note != "paired":
                print("%s <-> %s: %s" % (list_name, entity_id, note))

    for list_name, entity_id in pairing.pairs:
        try:
            r_items = reminders.items(list_name)
            h_items = ha.items(entity_id)
        except UserError as exc:
            LOG.error("skipping %s <-> %s: %s", list_name, entity_id, exc)
            failures += 1
            continue

        state = store.pair(list_name, entity_id)
        plan = plan_pair(r_items, h_items, state, config)

        for line in plan.conflicts:
            LOG.warning("conflict %s", line)
        for line in plan.warnings:
            LOG.info("%s", line)

        # A guard against a bad read wiping out both sides: a sync should never
        # need to delete a whole list's worth of items at once.
        if not force and plan.delete_count > config.max_deletes_per_run:
            LOG.error(
                "%s <-> %s: refusing to delete %d items in one run "
                "(max_deletes_per_run is %d). Re-run with --force if that is "
                "really what you want.",
                list_name,
                entity_id,
                plan.delete_count,
                config.max_deletes_per_run,
            )
            failures += 1
            continue

        if dry_run:
            lines = plan.describe()
            if lines:
                print("%s <-> %s" % (list_name, entity_id))
                for line in lines:
                    print("  " + line)
            else:
                print("%s <-> %s: nothing to do" % (list_name, entity_id))
            continue

        if plan.is_empty():
            LOG.debug("%s <-> %s: nothing to do", list_name, entity_id)
        failed_before = counter.failed
        links = execute_plan(
            plan, reminders, ha, list_name, entity_id, h_items, counter
        )
        state["links"] = links
        state["r_tombstones"] = sorted(plan.r_tombstones)
        state["h_tombstones"] = sorted(plan.h_tombstones)
        if counter.failed == failed_before:
            # last_sync also decides which completions count as recent, so it
            # only moves when the whole pair went through. Leaving it behind
            # makes the next run retry rather than write anything off as stale.
            state["last_sync"] = (
                dt.datetime.now().astimezone().isoformat(timespec="seconds")
            )

    if not dry_run:
        store.save()
        if counter.summary() and (
            counter.created_ha
            or counter.created_r
            or counter.updated_ha
            or counter.updated_r
            or counter.removed_ha
            or counter.removed_r
            or counter.failed
        ):
            LOG.info("%s", counter.summary())
        else:
            LOG.debug("nothing changed")

    return 1 if (failures or counter.failed) else 0


def cmd_lists(config: Config) -> int:
    reminders = Reminders(config.reminders_binary)
    ha = HomeAssistant(
        config.ha_url, config.ha_token, config.ha_timeout, config.ha_verify_tls
    )
    print("Reminders lists:")
    for name in reminders.lists():
        print("  %s" % name)
    print("\nHome Assistant to-do entities:")
    for entity_id, name in sorted(ha.todo_entities().items()):
        print("  %-32s %s" % (entity_id, name))

    print("\nWhat a sync would pair up:")
    pairing = resolve_pairs(config, reminders, ha, create=False)
    for list_name, entity_id, note in pairing.plan:
        print("  %-28s %-28s %s" % (list_name, entity_id, note))
    for problem in pairing.problems:
        print("  ! %s" % problem)
    return 0


def cmd_doctor(config: Config) -> int:
    problems = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal problems
        print("%s %s%s" % ("ok  " if ok else "FAIL", label, (" -- " + detail) if detail else ""))
        if not ok:
            problems += 1

    print("version     %s" % VERSION)
    print("script      %s" % SCRIPT_PATH)
    print("config      %s" % config.path)
    print("state       %s" % config.state_file)
    print("log         %s" % config.log_file)
    print("python      %s" % sys.version.split()[0])
    print()

    try:
        reminders = Reminders(config.reminders_binary)
        print("reminders   %s" % reminders.binary)
        names = reminders.lists()
        check("Reminders access", True, "%d lists" % len(names))
    except UserError as exc:
        check("Reminders access", False, str(exc).splitlines()[0])
        names = []
        reminders = None

    ha = HomeAssistant(
        config.ha_url, config.ha_token, config.ha_timeout, config.ha_verify_tls
    )
    entities: Dict[str, str] = {}
    try:
        ha.ping()
        entities = ha.todo_entities()
        check("Home Assistant %s" % config.ha_url, True, "%d to-do lists" % len(entities))
    except UserError as exc:
        check("Home Assistant %s" % config.ha_url, False, str(exc).splitlines()[0])

    if reminders and entities:
        print()
        print("lists       %s" % ("matched by name" if config.list_mode == "auto" else "mapped explicitly"))
        # create=False: doctor reports, it never changes anything.
        pairing = resolve_pairs(config, reminders, ha, create=False)
        for list_name, entity_id, note in pairing.plan:
            print("  %-28s %-28s %s" % (list_name, entity_id, note))
        for problem in pairing.problems:
            check(problem, False)

        for _, entity_id in pairing.pairs:
            state = ha.state(entity_id) or {}
            features = state.get("attributes", {}).get("supported_features", 0)
            missing = [
                label
                for bit, label in ((1, "create"), (2, "delete"), (4, "update"))
                if not features & bit
            ]
            if missing:
                check(
                    "HA entity %s" % entity_id,
                    False,
                    "cannot " + ", ".join(missing),
                )

    print()
    store = Store(config.state_file)
    pairs = store.data.get("pairs", {})
    if not pairs:
        print("state       empty -- the next sync will seed from both sides")
    for key, state in sorted(pairs.items()):
        print(
            "state       %s: %d links, last sync %s"
            % (key, len(state.get("links", [])), state.get("last_sync") or "never")
        )

    print()
    plist = launch_agent_path()
    if os.path.exists(plist):
        loaded = subprocess.run(
            ["launchctl", "print", "gui/%d/%s" % (os.getuid(), LAUNCH_LABEL)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        check("LaunchAgent loaded", loaded.returncode == 0, plist)
    else:
        print("--   LaunchAgent not installed (run `%s install`)" % os.path.basename(SCRIPT_PATH))

    return 1 if problems else 0


def launch_agent_path() -> str:
    return expand("~/Library/LaunchAgents/%s.plist" % LAUNCH_LABEL)


# --------------------------------------------------------------------------- #
# guided setup
# --------------------------------------------------------------------------- #


def ask(question: str, default: str = "") -> str:
    """Read one answer from the terminal.

    Reads /dev/tty rather than stdin so this still works when the installer
    arrives down a pipe, which is how `curl | bash` ends up running it.
    """
    suffix = " [%s]: " % default if default else ": "
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(question + suffix)
            tty.flush()
            answer = tty.readline().strip()
    except OSError:
        answer = input(question + suffix).strip()
    return answer or default


def ask_secret(question: str) -> str:
    # getpass already reads the terminal directly rather than stdin, which is
    # what makes it work under `curl | bash`.
    return getpass.getpass(question + ": ").strip()


def confirm(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask("%s (%s)" % (question, hint)).lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def wait_for_reminders_access(reminders: Reminders) -> List[str]:
    """Return the list names, walking the user through the grant if needed."""
    while True:
        try:
            return reminders.lists()
        except UserError as exc:
            if "access has not been granted" not in str(exc):
                raise
            print()
            print("macOS has not granted Reminders access to:")
            print("    %s" % reminders.binary)
            print()
            print("Run this in a terminal window you opened yourself, and click")
            print("Allow when the dialog appears:")
            print()
            print("    %s show-lists" % reminders.binary)
            print()
            print("If no dialog appears, switch it on under System Settings ->")
            print("Privacy & Security -> Reminders.")
            print()
            if not confirm("Granted it? Check again"):
                raise UserError(
                    "Reminders access is required. Re-run `%s setup` once it is "
                    "granted." % os.path.basename(SCRIPT_PATH)
                ) from None


def cmd_setup(config_path: str, args: argparse.Namespace) -> int:
    """Ask the handful of questions a working config needs, then install."""
    full_path = expand(config_path)
    existing: Dict[str, object] = {}
    if os.path.exists(full_path):
        try:
            with open(full_path, encoding="utf-8") as fh:
                existing = json.load(fh)
        except (json.JSONDecodeError, OSError):
            existing = {}
    previous = existing.get("home_assistant") or {}

    print("Setting up the Reminders <-> Home Assistant sync.")
    print("Config will be written to %s" % full_path)
    print()

    reminders = Reminders(args.reminders_binary)
    if args.yes:
        r_lists = reminders.lists()
    else:
        r_lists = wait_for_reminders_access(reminders)
    print("Reminders: %d lists (%s)" % (len(r_lists), ", ".join(r_lists)))
    print()

    # -- Home Assistant ---------------------------------------------------- #

    url = args.url or ""
    token = args.token or os.environ.get("RHS_HA_TOKEN") or ""
    ha: Optional[HomeAssistant] = None

    while ha is None:
        if not url:
            url = ask(
                "Home Assistant address",
                str(previous.get("url") or "http://homeassistant.local:8123"),
            )
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        if not token:
            print("Profile -> Security -> Long-lived access tokens -> Create Token.")
            print("Use an administrator account, so lists can be created for you.")
            print("(the paste stays hidden)")
            token = ask_secret("Token")

        candidate = HomeAssistant(url.rstrip("/"), token)
        try:
            candidate.ping()
        except UserError as exc:
            print()
            print("! %s" % exc)
            print()
            if args.yes:
                return 2
            if "rejected the token" in str(exc):
                token = ""
            else:
                url = ""
            continue
        ha = candidate

    print("Home Assistant: reachable at %s" % url)
    if not ha.is_admin():
        print()
        print("! This token cannot create to-do lists -- it does not belong to an")
        print("  administrator. Lists that already exist will still sync; missing")
        print("  ones will be reported instead of created.")
        print()
        if not args.yes and not confirm("Carry on with this token", default=False):
            return 2
    print()

    # -- write the config -------------------------------------------------- #

    raw = dict(existing)
    raw["home_assistant"] = dict(previous, url=url.rstrip("/"), token=token)
    write_config(full_path, raw)

    # -- show the plan, offer to trim it ----------------------------------- #

    config = Config(raw, full_path)
    pairing = resolve_pairs(config, reminders, ha, create=False)
    # Only rows that would actually be synced can be opted out of, so those are
    # the ones that get numbers.
    choices = [row for row in pairing.plan if row[2] != "excluded"]
    skipped = [row for row in pairing.plan if row[2] == "excluded"]

    if choices:
        print("Here is what a sync would do:")
        print()
        for index, (list_name, entity_id, note) in enumerate(choices, 1):
            print("  %2d. %-26s %-26s %s" % (index, list_name, entity_id, note))
        if skipped:
            print()
            print("  already excluded: %s" % ", ".join(row[0] if row[0] != "-" else row[1] for row in skipped))
        print()

        if not args.yes:
            answer = ask("Numbers to leave out (comma-separated, Enter to sync all)")
            excluded = []
            for chunk in answer.replace(" ", "").split(","):
                if not chunk:
                    continue
                try:
                    index = int(chunk)
                except ValueError:
                    print("! ignoring %r, that is not a number" % chunk)
                    continue
                if not 1 <= index <= len(choices):
                    print("! ignoring %d, out of range" % index)
                    continue
                list_name, entity_id, _ = choices[index - 1]
                excluded.append(list_name if list_name != "-" else entity_id)
            if excluded:
                raw["exclude"] = sorted(set(list(raw.get("exclude") or []) + excluded))
                write_config(full_path, raw)
                config = Config(raw, full_path)
                print("Excluded: %s" % ", ".join(excluded))
            print()
    else:
        print("Nothing to sync: every list is excluded.")
        print()

    for problem in pairing.problems:
        print("! %s" % problem)

    # -- background it ----------------------------------------------------- #

    interval = args.interval
    if not args.yes:
        interval = int(ask("Sync every how many seconds", str(interval)) or interval)

    if args.no_sync:
        print("Config written. Nothing has been synced and nothing installed.")
        print()
        print("Look before you leap:")
        print("    %s sync --dry-run" % SCRIPT_PATH)
        return 0

    if args.no_install:
        print("Syncing once, without installing the LaunchAgent...")
        result = sync_once(config)
        print()
        print("Sync from now on with:")
        print("    %s sync" % SCRIPT_PATH)
        return result

    if not args.yes and not confirm("Sync now and start syncing every %ds" % interval):
        print()
        print("Nothing installed. When you are ready:")
        print("    %s install --interval %d" % (SCRIPT_PATH, interval))
        return 0

    print()
    result = cmd_install(config, interval)
    if result == 0:
        print()
        print("Done. Check on it any time with:")
        print("    %s doctor" % SCRIPT_PATH)
    return result


def write_config(path: str, raw: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.chmod(path, 0o600)


def launchd_python() -> str:
    """The interpreter to name in the plist.

    Prefer the one macOS ships: the script is stdlib-only and 3.9-compatible, so
    it always works, and it is an absolute path that exists no matter what PATH
    launchd hands the job. A pyenv shim -- which is what `sys.executable` is
    under a pyenv shell -- needs a PATH launchd does not provide, and the agent
    would fail silently every run.
    """
    system = "/usr/bin/python3"
    if os.access(system, os.X_OK):
        return system
    LOG.warning(
        "%s is missing; the LaunchAgent will use %s, which must stay available",
        system,
        sys.executable,
    )
    return sys.executable


def cmd_install(config: Config, interval: int, initial_sync: bool = True) -> int:
    # Seed in the foreground before handing over to launchd. In auto mode the
    # first run can create a lot of lists, and watching it happen beats
    # discovering it in a log afterwards -- and if it fails, nothing is loaded.
    if initial_sync:
        print("running the first sync...")
        if sync_once(config) != 0:
            raise UserError(
                "the first sync reported problems, so the LaunchAgent was not "
                "installed. Fix them, or re-run with --skip-initial-sync."
            )
        print()

    plist_path = launch_agent_path()
    os.makedirs(os.path.dirname(plist_path), exist_ok=True)
    os.makedirs(os.path.dirname(config.log_file), exist_ok=True)

    plist = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [
            launchd_python(),
            SCRIPT_PATH,
            "--config",
            config.path,
            "sync",
        ],
        "StartInterval": interval,
        "RunAtLoad": True,
        "ProcessType": "Background",
        # Aqua only: the Reminders permission prompt can only appear in a GUI
        # session, and a background-session job would just be denied.
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": config.log_file,
        "StandardErrorPath": config.log_file,
        "EnvironmentVariables": {"LANG": "en_US.UTF-8"},
    }
    with open(plist_path, "wb") as fh:
        plistlib.dump(plist, fh)

    target = "gui/%d" % os.getuid()
    subprocess.run(
        ["launchctl", "bootout", target + "/" + LAUNCH_LABEL],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", target, plist_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    if result.returncode != 0:
        raise UserError(
            "launchctl bootstrap failed: %s" % (result.stdout or "").strip()
        )

    print("installed %s" % plist_path)
    print("syncing every %d seconds; log: %s" % (interval, config.log_file))
    return 0


def cmd_uninstall() -> int:
    plist_path = launch_agent_path()
    subprocess.run(
        ["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LAUNCH_LABEL)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if os.path.exists(plist_path):
        os.remove(plist_path)
        print("removed %s" % plist_path)
    else:
        print("nothing to remove at %s" % plist_path)
    return 0


def cmd_run(config: Config, interval: int) -> int:
    """Foreground loop, for people who would rather not use launchd."""
    LOG.info("syncing every %d seconds; Ctrl-C to stop", interval)
    while True:
        started = time.time()
        try:
            sync_once(config)
        except UserError as exc:
            LOG.error("%s", exc)
        except Exception:  # keep the loop alive across surprises
            LOG.exception("unexpected error during sync")
        time.sleep(max(1.0, interval - (time.time() - started)))


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def setup_logging(level_name: str, log_file: Optional[str]) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    LOG.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    LOG.addHandler(stream)

    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            rotating = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
            )
            rotating.setFormatter(formatter)
            LOG.addHandler(rotating)
        except OSError as exc:
            LOG.warning("cannot write to log file %s: %s", log_file, exc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=os.path.basename(SCRIPT_PATH),
        description="Two-way sync between macOS Reminders and Home Assistant.",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("RHS_CONFIG", DEFAULT_CONFIG_PATH),
        help="config file (default: %s)" % DEFAULT_CONFIG_PATH,
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument(
        "--version", action="version", version="reminders-ha-sync " + VERSION
    )

    sub = parser.add_subparsers(dest="command")

    setup = sub.add_parser(
        "setup", help="guided first-time setup: asks, verifies, installs"
    )
    setup.add_argument("--url", help="skip the question and use this address")
    setup.add_argument("--token", help="skip the question and use this token")
    setup.add_argument("--interval", type=int, default=600, help="seconds between syncs")
    setup.add_argument(
        "--reminders-binary", help="path to the `reminders` binary, if it is not on PATH"
    )
    setup.add_argument(
        "--no-install", action="store_true", help="configure and sync, but no LaunchAgent"
    )
    setup.add_argument(
        "--no-sync",
        action="store_true",
        help="only write the config: sync nothing, install nothing",
    )
    setup.add_argument(
        "--yes", "-y", action="store_true", help="ask nothing; fail instead of prompting"
    )

    sync = sub.add_parser("sync", help="sync once and exit")
    sync.add_argument("--dry-run", action="store_true", help="only print what would change")
    sync.add_argument(
        "--force",
        action="store_true",
        help="allow a run that deletes more than max_deletes_per_run items",
    )

    run = sub.add_parser("run", help="sync repeatedly in the foreground")
    run.add_argument("--interval", type=int, default=600, help="seconds between syncs")

    sub.add_parser("doctor", help="check the setup and show the pairing plan")
    sub.add_parser("lists", help="print both sides' lists and how they pair up")

    install = sub.add_parser("install", help="install and load the LaunchAgent")
    install.add_argument("--interval", type=int, default=600, help="seconds between syncs")
    install.add_argument(
        "--skip-initial-sync",
        action="store_true",
        help="install without syncing once first",
    )

    sub.add_parser("uninstall", help="unload and remove the LaunchAgent")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "sync"

    # These two run before a config exists, so they cannot load one.
    if command == "uninstall":
        setup_logging("info", None)
        return cmd_uninstall()

    if command == "setup":
        setup_logging("debug" if args.verbose else "info", None)
        try:
            return cmd_setup(args.config, args)
        except UserError as exc:
            LOG.error("%s", exc)
            return 2
        except KeyboardInterrupt:
            print()
            return 130

    try:
        config = load_config(args.config)
    except UserError as exc:
        setup_logging("info", None)
        LOG.error("%s", exc)
        return 2

    setup_logging("debug" if args.verbose else config.log_level, config.log_file)

    try:
        if command == "sync":
            return sync_once(config, dry_run=args.dry_run, force=args.force)
        if command == "run":
            return cmd_run(config, args.interval)
        if command == "doctor":
            return cmd_doctor(config)
        if command == "lists":
            return cmd_lists(config)
        if command == "install":
            return cmd_install(
                config, args.interval, initial_sync=not args.skip_initial_sync
            )
    except UserError as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
